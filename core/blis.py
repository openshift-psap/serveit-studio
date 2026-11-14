"""BLIS (Blackbox Inference Simulator) integration.

Pre-screens deployment configurations in CPU simulation before GPU time is
spent: generates a blis-schemas Scenario+Deployment pair from a ServeIt test
config, runs the discrete-event simulator, and parses the Result into the
same metric shape the cluster benchmark produces.

BLIS embeds the real llm-d router (EndpointPickerConfig) and vLLM 0.32.0
cost models. Models must exist in the BLIS catalog (or an overlay); the
cluster hardware must be catalogued (e.g. h200). Simulation is skipped
with an explanatory message otherwise — it never blocks a cluster run.

Layout on shared storage (BLIS_HOME):
    blis            static linux/amd64 binary (CGO_ENABLED=0 build)
    scenarios/      example scenarios + router/ EPP configs
    runs/<run_id>/  generated per-run scenarios and Result documents
"""

import json
import math
import os
import subprocess

BLIS_HOME = os.environ.get("BLIS_HOME", "/mnt/storage/blis")

# Coefficient sets every scenario prices with (the standard list the
# blis-substrate examples use; recurrent models add cost-model-recurrent).
_COEFFICIENTS = [
    "cost-model-primitives",
    "cost-model-collectives",
    "cost-model-host-overheads",
    "cost-model-attention",
    "cost-model-recurrent",
    "cost-model-memory",
]

# Metric name -> (ServeIt report label, better-is-lower)
_RESULT_METRICS = {
    "ttft_mean_ms": ("TTFT mean (sim)", True),
    "ttft_p50_ms": ("TTFT p50 (sim)", True),
    "ttft_p90_ms": ("TTFT p90 (sim)", True),
    "ttft_p95_ms": ("TTFT p95 (sim)", True),
    "ttft_p99_ms": ("TTFT p99 (sim)", True),
    "itl_mean_ms": ("ITL mean (sim)", True),
    "itl_p95_ms": ("ITL p95 (sim)", True),
    "itl_p99_ms": ("ITL p99 (sim)", True),
    "e2e_mean_ms": ("E2E mean (sim)", True),
    "e2e_p95_ms": ("E2E p95 (sim)", True),
    "e2e_p99_ms": ("E2E p99 (sim)", True),
    "request_throughput": ("Throughput req/s (sim)", False),
}

_DEFAULT_ROUTER_CONFIG = """\
# llm-d router defaults (queue + kv-utilization + prefix-cache scorers),
# the configuration the router builds when given none — written as a file
# so generated scenarios can name it explicitly.
apiVersion: llm-d.ai/v1
kind: EndpointPickerConfig
plugins:
- type: queue-scorer
- type: kv-cache-utilization-scorer
- type: prefix-cache-scorer
- type: metrics-data-source
- type: core-metrics-extractor
schedulingProfiles:
- name: default
  plugins:
  - pluginRef: queue-scorer
    weight: 2
  - pluginRef: kv-cache-utilization-scorer
    weight: 2
  - pluginRef: prefix-cache-scorer
    weight: 3
dataLayer:
  sources:
  - pluginRef: metrics-data-source
    extractors:
    - pluginRef: core-metrics-extractor
"""


def _quantization_of(model_name: str) -> str:
    """Map the model repo name to a BLIS engine quantization."""
    m = (model_name or "").lower()
    if "nvfp4" in m or "-fp4" in m:
        return "nvfp4"
    if "fp8" in m:
        return "fp8"
    return "bf16"


class BlisRunner:
    """Thin wrapper around the blis binary for scenario generation and runs.

    Execution modes:
      local  — subprocess on this host (binary at BLIS_HOME/blis)
      remote — kubectl exec into the workload pod on the target cluster
               (binary baked into the workload image at /usr/local/bin/blis,
               with the shared-storage copy as fallback)

    Scenarios and Result documents live on shared storage so both the
    server and the workload pod see the same paths.
    """

    REMOTE_BINARY_FALLBACK = "/mnt/storage/blis/blis"

    def __init__(self, log=None, blis_home=None, kubectl=None, namespace=None, pod_name=None):
        self.home = blis_home or BLIS_HOME
        self.binary = os.path.join(self.home, "blis")
        self.scenarios_dir = os.path.join(self.home, "scenarios")
        self._log = log or (lambda msg, level="info": None)
        # remote mode: kubectl runner + workload pod on the target cluster
        self.kubectl = kubectl
        self.namespace = namespace
        self.pod_name = pod_name

    @property
    def remote(self):
        return bool(self.kubectl and self.pod_name)

    # -- environment ----------------------------------------------------

    def available(self) -> bool:
        if self.remote:
            return True  # workload pod guarantees the binary (image or NFS)
        return os.path.isfile(self.binary) and os.access(self.binary, os.X_OK)

    def _exec(self, args, timeout=600):
        if self.remote:
            import shlex
            # prefer the in-image binary; fall back to the shared-storage
            # copy for workload pods built before BLIS was baked in
            cmd_str = (
                'B=$(command -v blis || echo ' + shlex.quote(self.REMOTE_BINARY_FALLBACK) + '); '
                '"$B" ' + " ".join(shlex.quote(a) for a in args)
            )
            return self.kubectl.run(
                ["exec", self.pod_name, "-n", self.namespace, "--", "bash", "-c", cmd_str],
                check=False, timeout=timeout,
            )
        cmd = [self.binary] + args
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

    def ensure_default_router(self):
        """Write the default colocated router config if missing."""
        rdir = os.path.join(self.scenarios_dir, "router")
        os.makedirs(rdir, exist_ok=True)
        rpath = os.path.join(rdir, "default-colocated.yaml")
        if not os.path.isfile(rpath):
            with open(rpath, "w") as f:
                f.write(_DEFAULT_ROUTER_CONFIG)
        return rpath

    # -- resolution -----------------------------------------------------

    def resolve_model(self, model_name: str):
        """Resolve a ServeIt model name to a BLIS catalog identity.

        Tries the full HF name, the repo basename, and progressively
        de-quantized variants. Returns the catalog identity or None.
        """
        if not model_name:
            return None
        candidates = [model_name]
        base = model_name.split("/")[-1]
        if base != model_name:
            candidates.append(base)
        # strip common quant suffixes from the basename
        for suffix in ("-FP8-dynamic", "-FP8-Dynamic", "-fp8", "-FP8",
                       "-nvfp4", "-NVFP4", "-bf16", "-GPTQ-Int4"):
            if base.endswith(suffix):
                candidates.append(base[: -len(suffix)])
                break
        seen = set()
        for cand in candidates:
            # entry names match case-sensitively; source repos do not —
            # try each candidate as-given and lowercased
            for variant in (cand, cand.lower()):
                if variant in seen:
                    continue
                seen.add(variant)
                r = self._exec(["catalog", "resolve", "--model", variant, "--json"])
                if r.returncode != 0:
                    continue
                try:
                    out = json.loads(r.stdout)
                except json.JSONDecodeError:
                    continue
                if out.get("identity"):
                    return out["identity"]
        return None

    def hardware_supported(self, gpu_type: str) -> bool:
        if not gpu_type:
            return False
        r = self._exec(["catalog", "resolve", "--chip", gpu_type.lower(), "--json"])
        if r.returncode != 0:
            return False
        try:
            return bool(json.loads(r.stdout).get("identity"))
        except json.JSONDecodeError:
            return False

    # -- scenario generation ---------------------------------------------

    def generate_scenario(self, cfg, name: str, out_dir: str,
                          gpu_type: str = "h200", gpus_per_node: int = 8,
                          epp_config=None):
        """Write a Scenario+Deployment YAML for an aggregated deployment.

        cfg is any object with the ServeIt test-config fields (model_name,
        tensor_parallelism, replicas/prefill_pods, max_model_len,
        max_num_seqs, max_num_batched_tokens, block_size, isl, osl, ...).

        epp_config is ServeIt's EndpointPickerConfig dict (the same document
        deployed as the gaie configmap, including calibrated affinity
        parameters). When given it is written beside the scenario and the
        embedded llm-d router runs OUR routing policy; otherwise the
        router's built-in defaults apply.

        Returns the scenario file path, or None when the model is not in
        the catalog (caller logs and skips simulation).
        """
        model = self.resolve_model(getattr(cfg, "model_name", ""))
        if not model:
            return None
        if not self.hardware_supported(gpu_type):
            self._log(f"BLIS: hardware '{gpu_type}' not in catalog — skipping simulation", "warn")
            return None

        os.makedirs(out_dir, exist_ok=True)
        self.ensure_default_router()
        # router configs resolve relative to the run's scenarios dir —
        # stage the default beside the generated scenario
        run_router_dir = os.path.join(out_dir, "router")
        os.makedirs(run_router_dir, exist_ok=True)
        run_router = os.path.join(run_router_dir, "default-colocated.yaml")
        if not os.path.isfile(run_router):
            with open(run_router, "w") as f:
                f.write(_DEFAULT_ROUTER_CONFIG)
        router_ref = "router/default-colocated.yaml"
        if epp_config:
            # OUR EPP config: same llm-d.ai/v1 EndpointPickerConfig the
            # pipeline deploys as the gaie configmap (calibrated scorers,
            # affinity filter, scheduling profiles). The embedded router
            # in the simulator runs this policy.
            try:
                import yaml as _yaml
                epp_text = _yaml.safe_dump(epp_config, sort_keys=False)
            except ImportError:
                epp_text = json.dumps(epp_config, indent=2)
            our_router = os.path.join(run_router_dir, "serveit-epp.yaml")
            with open(our_router, "w") as f:
                f.write(epp_text)
            router_ref = "router/serveit-epp.yaml"

        tp = int(getattr(cfg, "tensor_parallelism", 1) or 1)
        replicas = int(getattr(cfg, "prefill_pods", None)
                       or getattr(cfg, "replicas", 1)
                       or getattr(cfg, "single_test_replicas", 1) or 1)
        total_gpus = tp * replicas
        nodes = max(1, math.ceil(total_gpus / max(gpus_per_node, 1)))

        max_model_len = int(getattr(cfg, "max_model_len", 32768) or 32768)
        max_num_seqs = int(getattr(cfg, "max_num_seqs", 0) or 256)
        max_num_batched_tokens = int(getattr(cfg, "max_num_batched_tokens", 0) or 8192)
        block_size = int(getattr(cfg, "block_size", 0) or 64)
        quant = _quantization_of(getattr(cfg, "model_name", ""))

        scenario = f"""\
kind: Scenario
name: {name}
engine:
  kind: vllm
  version: "0.32.0"

model: {model}
coefficients: [{', '.join(_COEFFICIENTS)}]

cluster:
  hardware: {gpu_type.lower()}
  nodes: {nodes}
  gpus_per_node: {gpus_per_node}
---
kind: Deployment
name: {name}

pools:
  - role: colocated
    nodes: {nodes}
    parallel:
      tp: {tp}
      pp: 1
      dp: 1
      enable_expert_parallel: false
    engine:
      quantization: {quant}
      kv_cache_dtype: fp8
      block_size: {block_size}
      max_num_batched_tokens: {max_num_batched_tokens}
      max_num_seqs: {max_num_seqs}
      max_model_len: {max_model_len}
      gpu_memory_utilization: 0.9
      compilation_config: {{cudagraph_mode: PIECEWISE}}
"""
        # Multi-instance deployments route through the embedded llm-d
        # router; single-instance skips it entirely.
        if replicas > 1:
            scenario += f"\nrouter:\n  endpoint_picker_config: {router_ref}\n"

        path = os.path.join(out_dir, f"{name}.yaml")
        with open(path, "w") as f:
            f.write(scenario)
        return path

    # -- execution --------------------------------------------------------

    def run(self, scenario_path: str, scenario_name: str, cfg,
            seed: int = 42, timeout: int = 900):
        """Run the simulator with the workload derived from cfg.

        Returns the parsed Result dict, or None on failure (with the
        error logged).
        """
        if not self.available():
            self._log("BLIS: binary not found — skipping simulation", "warn")
            return None

        isl = int(getattr(cfg, "isl", 2048) or 2048)
        osl = int(getattr(cfg, "osl", 512) or 512)
        isl_stdev = int(getattr(cfg, "isl_stdev", 0) or 0)
        osl_stdev = int(getattr(cfg, "osl_stdev", 0) or 0)
        qps = float(getattr(cfg, "qps", 10) or 10)
        rate_type = getattr(cfg, "rate_type", "concurrent") or "concurrent"
        duration = int(getattr(cfg, "test_duration", 300) or 300)
        max_requests = int(getattr(cfg, "max_requests", 0) or 0)
        num_requests = max_requests or min(int(qps * duration), 5000)
        prefix_tokens = int(getattr(cfg, "prefix_tokens", 0) or 0)

        out_file = os.path.splitext(scenario_path)[0] + ".result.json"
        if not scenario_name.endswith(".yaml"):
            scenario_name += ".yaml"

        # Remote mode: /mnt/storage is NOT shared between the server and
        # the target cluster — stage the scenario (and any router config
        # it references) onto the workload pod, and parse the Result
        # document from stdout so nothing needs copying back.
        pod_scenarios_dir = None
        if self.remote:
            pod_scenarios_dir = "/tmp/blis-runs"
            self.kubectl.run(
                ["exec", self.pod_name, "-n", self.namespace, "--",
                 "mkdir", "-p", pod_scenarios_dir + "/router"], check=False)
            self.kubectl.run(
                ["cp", scenario_path,
                 f"{self.pod_name}:{pod_scenarios_dir}/{scenario_name}",
                 "-n", self.namespace], check=False, timeout=120)
            import re as _re
            with open(scenario_path) as f:
                _scn = f.read()
            _m = _re.search(r"endpoint_picker_config:\s*(\S+)", _scn)
            if _m:
                _ref = _m.group(1).strip("\"'")
                _local_router = os.path.join(os.path.dirname(scenario_path), _ref)
                if os.path.isfile(_local_router):
                    self.kubectl.run(
                        ["exec", self.pod_name, "-n", self.namespace, "--",
                         "mkdir", "-p",
                         f"{pod_scenarios_dir}/{os.path.dirname(_ref) or '.'}"],
                        check=False)
                    self.kubectl.run(
                        ["cp", _local_router,
                         f"{self.pod_name}:{pod_scenarios_dir}/{_ref}",
                         "-n", self.namespace], check=False, timeout=120)

        scenarios_dir = pod_scenarios_dir or os.path.dirname(scenario_path)
        args = [
            "run",
            "--scenarios", scenarios_dir,
            "--scenario", scenario_name,
            "--prompt-tokens", str(isl),
            "--prompt-tokens-min", str(max(2, isl - 2 * isl_stdev)),
            "--prompt-tokens-max", str(isl + 2 * isl_stdev),
            "--prompt-tokens-stdev", str(isl_stdev),
            "--output-tokens", str(osl),
            "--output-tokens-min", str(max(2, osl - 2 * osl_stdev)),
            "--output-tokens-max", str(osl + 2 * osl_stdev),
            "--output-tokens-stdev", str(osl_stdev),
            "--num-requests", str(num_requests),
            "--seed", str(seed),
        ]
        if not self.remote:
            args += ["--output", out_file]
        if rate_type == "concurrent":
            args += ["--concurrency", str(int(qps))]
        else:
            args += ["--rate", str(qps)]
        if prefix_tokens > 0:
            args += ["--prefix-tokens", str(prefix_tokens)]

        try:
            r = self._exec(args, timeout=timeout)
        except subprocess.TimeoutExpired:
            self._log(f"BLIS: simulation timed out after {timeout}s", "warn")
            return None
        if r.returncode != 0:
            self._log(f"BLIS: run failed: {(r.stderr or r.stdout)[:300]}", "warn")
            return None
        try:
            if self.remote:
                return json.loads(r.stdout)
            with open(out_file) as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            self._log(f"BLIS: result parse failed: {e}", "warn")
            return None

    # -- results ----------------------------------------------------------

    @staticmethod
    def summarize(result: dict):
        """Flatten a BLIS Result into ServeIt-style metric names."""
        if not result:
            return {}
        metrics = result.get("metrics", {})
        out = {}
        for blis_key, (label, _) in _RESULT_METRICS.items():
            entry = metrics.get(blis_key)
            if isinstance(entry, dict) and entry.get("value") is not None:
                out[blis_key] = entry["value"]
        return out
