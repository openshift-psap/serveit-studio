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
import copy
import gzip
import os
import subprocess
from typing import Optional

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
    "itl_p50_ms": ("ITL p50 (sim)", True),
    "itl_p90_ms": ("ITL p90 (sim)", True),
    "itl_p95_ms": ("ITL p95 (sim)", True),
    "itl_p99_ms": ("ITL p99 (sim)", True),
    "e2e_mean_ms": ("E2E mean (sim)", True),
    "e2e_p95_ms": ("E2E p95 (sim)", True),
    "e2e_p99_ms": ("E2E p99 (sim)", True),
    "request_throughput": ("Throughput req/s (sim)", False),
    "scheduling_delay_mean_ms": ("Scheduling delay mean (sim)", True),
    "scheduling_delay_p95_ms": ("Scheduling delay p95 (sim)", True),
    "preemptions": ("Preemptions (sim)", True),
    "interactivity_mean": ("Interactivity tok/s/user (sim)", False),
    "step_interval_mean_ms": ("Step interval mean (sim)", True),
    "output_tok_s_per_gpu": ("Output tok/s per GPU (sim)", False),
    "total_tokens_per_second_per_gpu": ("Total tok/s per GPU (sim)", False),
    "slo_attainment": ("SLO attainment (sim)", False),
    "goodput_rps": ("Goodput req/s (sim)", False),
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
    # empty = checkpoint's own dtype (the catalog graph governs) —
    # the kernel refuses invented values like bf16/fp16
    return ""


# Engine release the pinned simulator models. inference-sim v0.18.1 ships
# blis-vllm v0.33.0-blis.1 and router v1.0.0-blis.5; a scenario naming any
# other version is refused by the binary ("this build runs vllm X").
SIM_ENGINE_VERSION = "0.33.0"

# vLLM 0.31 OpenAI-server batch defaults by device memory (arg_utils
# get_batch_defaults): the deployment passes neither flag, so these are the
# values it EFFECTIVELY runs with and the scenario must mirror.
_VLLM_DEVICE_GB = {
    "b200": 192, "b300": 192, "gb200": 192, "h200": 141, "h100": 80,
    "h800": 80, "a100": 80, "l40s": 48, "l4": 24, "mi300x": 192,
}


def _vllm_batch_defaults(gpu_type: str, max_model_len: int, chunked: bool):
    """(max_num_batched_tokens, max_num_seqs) the deployed vLLM picks."""
    gb = _VLLM_DEVICE_GB.get((gpu_type or "h200").lower(), 80)
    if gb >= 160:
        batched, seqs = 16384, 1024
    elif gb >= 70 and "a100" not in (gpu_type or "").lower():
        batched, seqs = 8192, 1024
    else:
        batched, seqs = 2048, 256
    if not chunked:
        batched = max(max_model_len, batched)
    return min(seqs * max_model_len, batched), seqs


def _engine_settings(cfg, arch: str, gpu_type: str):
    """Mirror the values the deployment will run with.

    Templates emit a flag only when its TestConfig field is truthy; an unset
    field means vLLM's own default, which for booleans can be ON (chunked
    prefill and prefix caching both default on for decoder models). So the
    effective value is: field if set, else the engine default.
    """
    max_model_len = int(getattr(cfg, "max_model_len", 32768) or 32768)
    prefix = bool(getattr(cfg, "enable_prefix_caching", True))
    # absent flag -> vLLM default (on for decoder-only models)
    # An absent --enable-chunked-prefill flag leaves vLLM's default (ON for
    # decoder-only models); the effective value is True either way.
    chunked = True
    batched_def, seqs_def = _vllm_batch_defaults(gpu_type, max_model_len, chunked)

    shared = {
        "block_size": int(getattr(cfg, "block_size", 0) or 16),
        "kv_cache_dtype": getattr(cfg, "kv_cache_dtype", None) or "auto",
        "enable_prefix_caching": prefix,
        "enable_chunked_prefill": chunked,
        "max_model_len": max_model_len,
    }
    if getattr(cfg, "dtype", None):
        shared["dtype"] = cfg.dtype
    if getattr(cfg, "speculative_method", None):
        spec = {"method": cfg.speculative_method}
        if getattr(cfg, "speculative_num_tokens", None):
            spec["num_spec_tokens"] = int(cfg.speculative_num_tokens)
        if getattr(cfg, "speculative_model", None):
            spec["model"] = cfg.speculative_model
        shared["speculative_config"] = spec

    def _role(prefix_attr, default_gmu=0.9):
        return {
            "max_num_seqs": int(getattr(cfg, prefix_attr + "max_num_seqs", 0)
                                or getattr(cfg, "max_num_seqs", 0) or seqs_def),
            "max_num_batched_tokens": int(getattr(cfg, prefix_attr + "max_num_batched_tokens", 0)
                                          or getattr(cfg, "max_num_batched_tokens", 0) or batched_def),
            "gpu_memory_utilization": float(getattr(cfg, prefix_attr + "gpu_memory_utilization", 0)
                                            or getattr(cfg, "gpu_memory_utilization", 0) or default_gmu),
        }

    settings = {"shared": shared}
    if arch == "pd":
        settings["prefill"] = _role("prefill_")
        settings["decode"] = _role("decode_", 0.85)
    else:
        settings["colocated"] = _role("")
    if arch == "ep":
        settings["ep"] = {"enable_expert_parallel": True,
                          "dp": int(getattr(cfg, "data_parallel_size", 0) or 1),
                          "enable_eplb": bool(getattr(cfg, "enable_eplb", False))}
        for k in ("moe_backend", "all2all_backend"):
            if getattr(cfg, k, None):
                settings["ep"][k] = getattr(cfg, k)
    return settings


def _not_modelled(cfg):
    """Deployment settings BLIS does not model (warned, not silently dropped)."""
    out = []
    if getattr(cfg, "lora_enabled", False):
        out.append("lora (--enable-lora/--lora-modules)")
    if getattr(cfg, "enable_dbo", False):
        out.append("dbo thresholds")
    if getattr(cfg, "enable_scale_out", False):
        out.append("enable_scale_out (kv scale-out)")
    for attr, label in (("cpu_offload_gb", "cpu offload"),
                        ("disk_offload_kv_path", "disk KV offload"),
                        ("decode_attention_config", "attention_config"),
                        ("decode_compilation_config", "compilation_config")):
        if getattr(cfg, attr, None):
            out.append(label)
    return out


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

    # in-image binary (workload-v4+); the pod's /mnt/storage is node-local,
    # so the server-side shared-storage copy is NOT visible there
    # in-image binary (workload-v4+); the pod's /mnt/storage is node-local,
    # so the server-side shared-storage copy is NOT visible there
    REMOTE_BINARY_FALLBACK = "/usr/local/bin/blis"

    # Catalog overlays: the repo ships entries the embedded catalog lacks
    # (deployed with the code — no manual staging); the shared-storage dir
    # extends/overrides for models added ad hoc on an install.
    REPO_OVERLAY_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "blis-overlay")
    STORAGE_OVERLAY_DIR = os.path.join(BLIS_HOME, "catalog-overlay")

    @property
    def OVERLAY_DIR(self):  # noqa: N802 — kept for call-site compatibility
        if os.path.isdir(os.path.join(self.STORAGE_OVERLAY_DIR, "models")):
            return self.STORAGE_OVERLAY_DIR
        if os.path.isdir(os.path.join(self.REPO_OVERLAY_DIR, "models")):
            return self.REPO_OVERLAY_DIR
        return None

    def __init__(self, log=None, blis_home=None, kubectl=None, namespace=None, pod_name=None):
        self.home = blis_home or BLIS_HOME
        self.binary = os.path.join(self.home, "blis")
        self.scenarios_dir = os.path.join(self.home, "scenarios")
        self._log = log or (lambda msg, level="info": None)
        # remote mode: kubectl runner + workload pod on the target cluster
        self.kubectl = kubectl
        self.namespace = namespace
        self.pod_name = pod_name
        # Last command line handed to the simulator (populated by run /
        # run_replay). Callers record it in the report; no monkeypatching.
        self.last_command = None
        # Text of the workload spec used, when one was generated (parallel
        # shapes) and of the EPP router config the scenario references.
        # Both are shown alongside the command in the report.
        self.last_workload_spec = None
        self.last_epp_config = None
        self.last_epp_config_name = None
        # Mirrored engine settings written into the scenario, deployment
        # settings BLIS cannot model, and P/D instance counts
        self.last_engine_settings = None
        self.last_not_modelled = []
        self.last_scenario_arch = None
        self.last_instance_counts = (0, 0)

    @property
    def remote(self):
        return bool(self.kubectl and self.pod_name)

    # -- environment ----------------------------------------------------

    def available(self) -> bool:
        if self.remote:
            return True  # workload pod guarantees the binary (image or NFS)
        return os.path.isfile(self.binary) and os.access(self.binary, os.X_OK)

    def _exec_result_file(self, args, out_file, timeout=600):
        """Execute writing the Result to a file, then copy it back.

        A Result with thousands of request rows is ~10 MB of JSON; streaming
        that through kubectl exec's stdout intermittently dies with
        'error reading from error stream: unexpected EOF' (client-go v2
        stream). Writing to a file inside the pod keeps exec stdout to a
        single EXIT line, and the copy is a plain file transfer.
        """
        if not self.remote:
            return subprocess.run([self.binary] + args, capture_output=True,
                                  text=True, timeout=timeout)
        import shlex
        # out_file is a *server-side* path; /mnt/storage is not mounted in
        # the workload pod, so the pod writes under /tmp and we copy back
        _base = os.path.basename(out_file)
        remote_out = f"/tmp/{_base}.pod.json"
        remote_err = f"/tmp/{_base}.pod.stderr"
        cmd_str = (
            'B=$(command -v blis || echo ' + shlex.quote(self.REMOTE_BINARY_FALLBACK) + '); '
            '"$B" ' + " ".join(shlex.quote(a) for a in args) +
            ' > ' + shlex.quote(remote_out) +
            ' 2> ' + shlex.quote(remote_err) + '; echo "EXIT:$?"'
        )
        r = self.kubectl.run(
            ["exec", self.pod_name, "-n", self.namespace, "--", "bash", "-c", cmd_str],
            check=False, timeout=timeout,
        )
        rc = 1
        for line in (r.stdout or "").splitlines():
            if line.startswith("EXIT:"):
                try:
                    rc = int(line.split(":", 1)[1].strip())
                except ValueError:
                    rc = 1
        err_text = ""
        try:
            er = self.kubectl.run(
                ["exec", self.pod_name, "-n", self.namespace, "--", "cat", remote_err],
                check=False, timeout=120)
            err_text = er.stdout or ""
        except Exception:
            pass
        stdout_text = ""
        if rc == 0:
            # The Result for a high-concurrency run is ~70 MB of JSON. A
            # plain `kubectl cp` of that tar stream intermittently truncates
            # while returning success-like output, which surfaced as
            # "result parse failed: Unterminated string" on 2 of 10 cycles
            # (run 327). Compress in the pod first, verify the copy's exit
            # code, and fall back to the raw transfer before giving up.
            _gz_remote = remote_out + ".gz"
            _gz_local = out_file + ".gz"
            _copied = False
            try:
                # gzip INSIDE the pod, then kubectl cp the archive file —
                # no large JSON crosses an exec stdout stream.
                self.kubectl.run(
                    ["exec", self.pod_name, "-n", self.namespace, "--",
                     "gzip", "-kf", remote_out],
                    check=False, timeout=timeout)
                cr = self.kubectl.run(
                    ["cp", f"{self.pod_name}:{_gz_remote}", _gz_local,
                     "-n", self.namespace], check=False, timeout=timeout)
                if getattr(cr, "returncode", 1) == 0 and os.path.isfile(_gz_local):
                    with gzip.open(_gz_local, "rb") as f:
                        stdout_text = f.read().decode("utf-8", "replace")
                    _copied = True
            except (OSError, ValueError, EOFError):
                _copied = False
            if not _copied:
                try:
                    cr = self.kubectl.run(
                        ["cp", f"{self.pod_name}:{remote_out}", out_file,
                         "-n", self.namespace], check=False, timeout=timeout)
                    if getattr(cr, "returncode", 1) == 0:
                        with open(out_file) as f:
                            stdout_text = f.read()
                except OSError:
                    stdout_text = ""
            else:
                try:
                    with open(out_file, "w") as f:
                        f.write(stdout_text)
                except OSError:
                    pass
            if stdout_text:
                try:
                    json.loads(stdout_text)
                except json.JSONDecodeError as e:
                    # Truncated transfer: try the raw copy once more.
                    try:
                        cr = self.kubectl.run(
                            ["cp", f"{self.pod_name}:{remote_out}", out_file,
                             "-n", self.namespace], check=False,
                            timeout=timeout)
                        if getattr(cr, "returncode", 1) == 0:
                            with open(out_file) as f:
                                stdout_text = f.read()
                    except OSError:
                        stdout_text = ""
                    if stdout_text:
                        try:
                            json.loads(stdout_text)
                        except json.JSONDecodeError:
                            self._log(
                                "BLIS: Result transfer is truncated "
                                f"({e}); retrying the copy did not help", "warn")
        class _R:
            pass
        o = _R()
        o.returncode = rc
        o.stdout = stdout_text
        o.stderr = err_text
        return o

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
        # Local catalog overlay first: models added on this install
        # (resolve has no --catalog-overlay flag, so check the directory)
        overlay_models = os.path.join(self.OVERLAY_DIR, "models") if self.OVERLAY_DIR else None
        if overlay_models and os.path.isdir(overlay_models):
            for cand in candidates:
                for variant in (cand, cand.lower()):
                    d = os.path.join(overlay_models, variant)
                    if os.path.isdir(d) and os.path.isfile(os.path.join(d, "graph.yaml")):
                        return variant
            # source-repo match against overlay model.yaml files
            try:
                import glob as _glob
                import yaml as _yaml
                for my in _glob.glob(os.path.join(overlay_models, "*", "model.yaml")):
                    try:
                        repo = ((_yaml.safe_load(open(my) or {}).get("source") or {}).get("repo") or "").lower()
                    except Exception:
                        continue
                    if repo and repo in (model_name.lower(),):
                        return os.path.basename(os.path.dirname(my))
            except ImportError:
                pass
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
                          epp_config=None, cluster_nodes: int = None,
                          fabric: str = None):
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

        arch = (getattr(cfg, "architecture", "aggregated") or "aggregated").lower()
        self.last_scenario_arch = arch
        es = _engine_settings(cfg, arch, gpu_type)
        self.last_engine_settings = es
        self.last_not_modelled = _not_modelled(cfg)
        sh = es["shared"]
        quant = _quantization_of(getattr(cfg, "model_name", ""))

        def engine_block(d, indent=6):
            pad = " " * indent
            lines = [f"{pad}quantization: {quant}"]
            for k in ("dtype", "kv_cache_dtype", "enable_prefix_caching",
                      "enable_chunked_prefill", "block_size",
                      "max_num_batched_tokens", "max_num_seqs",
                      "max_model_len", "gpu_memory_utilization"):
                if k in d:
                    lines.append(f"{pad}{k}: {json.dumps(d[k])}")
            if "enable_eplb" in d:
                lines.append(f"{pad}enable_eplb: {json.dumps(d['enable_eplb'])}")
            for k in ("moe_backend", "all2all_backend"):
                if k in d:
                    lines.append(f"{pad}{k}: {json.dumps(d[k])}")
            if "speculative_config" in d:
                lines.append(f"{pad}speculative_config:")
                for kk, vv in d["speculative_config"].items():
                    lines.append(f"{pad}  {kk}: {json.dumps(vv)}")
            return "\n".join(lines)

        # ── P/D disaggregated: one pool per role, never sharing nodes ──
        if arch == "pd":
            tp_p = int(getattr(cfg, "prefill_tp", 0) or getattr(cfg, "tensor_parallelism", 1) or 1)
            tp_d = int(getattr(cfg, "decode_tp", 0) or getattr(cfg, "tensor_parallelism", 1) or 1)
            pods_p = int(getattr(cfg, "prefill_replicas", 0) or 0)
            pods_d = int(getattr(cfg, "decode_replicas", 0) or 0)
            if pods_p < 1 or pods_d < 1:
                self._log("BLIS: PD scenario needs prefill/decode replica counts — "
                          "falling back to a colocated scenario", "warn")
                arch = "aggregated"
                self.last_scenario_arch = arch
            else:
                nodes_p = max(1, math.ceil(tp_p * pods_p / max(gpus_per_node, 1)))
                nodes_d = max(1, math.ceil(tp_d * pods_d / max(gpus_per_node, 1)))
                total_nodes = nodes_p + nodes_d
                eng_p = {**sh, **es["prefill"]}
                eng_d = {**sh, **es["decode"]}
                self.last_instance_counts = (pods_p, pods_d)
                scenario = f"""\
kind: Scenario
name: {name}
engine:
  kind: vllm
  version: "{SIM_ENGINE_VERSION}"
model: {model}
coefficients: [{', '.join(_COEFFICIENTS)}]
cluster:
  hardware: {gpu_type.lower()}
  nodes: {total_nodes}
  gpus_per_node: {gpus_per_node}
  fabric: {fabric or 'ib-400g'}
---
kind: Deployment
name: {name}
pools:
  - role: prefill
    nodes: {nodes_p}
    parallel:
      tp: {tp_p}
      pp: 1
      dp: 1
      enable_expert_parallel: false
    engine:
{engine_block(eng_p, 6)}
  - role: decode
    nodes: {nodes_d}
    parallel:
      tp: {tp_d}
      pp: 1
      dp: 1
      enable_expert_parallel: false
    engine:
{engine_block(eng_d, 6)}
pd_transfer:
  connector: NixlConnector
"""
                if pods_p + pods_d > 1:
                    scenario += f"\nrouter:\n  endpoint_picker_config: {router_ref}\n"
                path = os.path.join(out_dir, f"{name}.yaml")
                with open(path, "w") as f:
                    f.write(scenario)
                return path

        # ── colocated (aggregated / EP) ──
        tp = int(getattr(cfg, "tensor_parallelism", 1) or 1)
        replicas = int(getattr(cfg, "prefill_pods", None)
                       or getattr(cfg, "replicas", 1)
                       or getattr(cfg, "single_test_replicas", 1) or 1)
        total_gpus = tp * replicas
        if not cluster_nodes or int(cluster_nodes) < 1:
            cluster_nodes = max(1, math.ceil(total_gpus / max(gpus_per_node, 1)))
        cluster_nodes = int(cluster_nodes)
        nodes = cluster_nodes
        self.last_instance_counts = (0, 0)

        _fabric = f"\n  fabric: {fabric or 'ib-400g'}" if cluster_nodes > 1 else ""
        ep = es.get("ep") or {}
        dp = int(ep.get("dp") or 1)
        if "enable_eplb" in ep:
            ep["enable_eplb"] = bool(ep["enable_eplb"])
        parallel_extra = (f"\n      dp: {dp}"
                          f"\n      enable_expert_parallel: {json.dumps(bool(ep.get('enable_expert_parallel', False)))}")
        engine = {**sh, **es["colocated"], **{k: v for k, v in ep.items()
                                              if k in ("enable_eplb", "moe_backend", "all2all_backend")}}

        scenario = f"""\
kind: Scenario
name: {name}
engine:
  kind: vllm
  version: "{SIM_ENGINE_VERSION}"
model: {model}
coefficients: [{', '.join(_COEFFICIENTS)}]
cluster:
  hardware: {gpu_type.lower()}
  nodes: {cluster_nodes}
  gpus_per_node: {gpus_per_node}{_fabric}
---
kind: Deployment
name: {name}
pools:
  - role: colocated
    nodes: {nodes}
    parallel:
      tp: {tp}
      pp: 1{parallel_extra}
    engine:
{engine_block(engine, 6)}
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

    def _record_scenario_epp(self, scenario_path):
        """Store the EndpointPickerConfig the scenario points at (post-fallback)."""
        try:
            with open(scenario_path) as f:
                _scn = f.read()
            import re as _re
            m = _re.search(r"endpoint_picker_config:\s*(\S+)", _scn)
            if not m:
                return
            ref = m.group(1)
            path = os.path.join(os.path.dirname(scenario_path), ref)
            with open(path) as f:
                self.last_epp_config = f.read()
            self.last_epp_config_name = ref
        except OSError:
            self.last_epp_config = None

    def run(self, scenario_path: str, scenario_name: str, cfg,
            seed: int = 42, timeout: int = 900, instances: int = None):
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
        # parallel-workload shapes run via a BLIS workload spec (one
        # closed-loop client per shape) instead of single-shape flags
        workload_spec_flag = None
        if getattr(cfg, "parallel_workloads", None):
            spec_path = BlisRunner.build_workload_spec(
                cfg.parallel_workloads,
                duration_s=duration,
                out_dir=os.path.dirname(scenario_path),
                max_model_len=int(getattr(cfg, "max_model_len", 0) or 0) or None)
            if spec_path:
                try:
                    with open(spec_path) as _f:
                        self.last_workload_spec = _f.read()
                except OSError:
                    self.last_workload_spec = None
                if self.remote:
                    self.kubectl.run(
                        ["cp", spec_path,
                         f"{self.pod_name}:{pod_scenarios_dir}/serveit-workload-spec.yaml",
                         "-n", self.namespace], check=False, timeout=120)
                    workload_spec_flag = f"{pod_scenarios_dir}/serveit-workload-spec.yaml"
                else:
                    workload_spec_flag = spec_path
        overlay_flag = None
        if self.OVERLAY_DIR and os.path.isdir(self.OVERLAY_DIR):
            if self.remote:
                _pod_overlay = f"{pod_scenarios_dir}/catalog-overlay"
                self.kubectl.run(
                    ["cp", self.OVERLAY_DIR, f"{self.pod_name}:{_pod_overlay}",
                     "-n", self.namespace], check=False, timeout=300)
                overlay_flag = _pod_overlay
            else:
                overlay_flag = self.OVERLAY_DIR
        args = [
            "run",
            "--scenarios", scenarios_dir,
            "--scenario", scenario_name,
            "--seed", str(seed),
        ]
        if workload_spec_flag:
            args += ["--workload-spec", workload_spec_flag,
                     "--horizon", str(duration * 1_000_000)]
        else:
            args += [
                "--prompt-tokens", str(isl),
                "--prompt-tokens-min", str(max(2, isl - 2 * isl_stdev)),
                "--prompt-tokens-max", str(isl + 2 * isl_stdev),
                "--prompt-tokens-stdev", str(isl_stdev),
                "--output-tokens", str(osl),
                "--output-tokens-min", str(max(2, osl - 2 * osl_stdev)),
                "--output-tokens-max", str(osl + 2 * osl_stdev),
                "--output-tokens-stdev", str(osl_stdev),
                "--num-requests", str(num_requests),
            ]
        if overlay_flag:
            args += ["--catalog-overlay", overlay_flag]
        if not self.remote:
            args += ["--output", out_file]
        if not workload_spec_flag:
            if rate_type == "concurrent":
                args += ["--concurrency", str(int(qps))]
            else:
                args += ["--rate", str(qps)]
        if prefix_tokens > 0:
            args += ["--prefix-tokens", str(prefix_tokens)]
        # instance count: multiple replicas pack onto the scenario's nodes.
        # Explicit param wins; the attribute chain is a fallback with None
        # defaults (a truthy default like 1 short-circuits the or-chain
        # when the recipe config lacks the field — run 253 simulated a
        # single instance because getattr(cfg, "replicas", 1) returned the
        # default 1 and single_test_replicas was never reached).
        _arch = (getattr(self, "last_scenario_arch", None)
                 or getattr(cfg, "architecture", "aggregated") or "aggregated").lower()
        _pre, _dec = getattr(self, "last_instance_counts", (0, 0))
        if _arch == "pd" and int(_pre) > 0 and int(_dec) > 0:
            # Dissaggregated: every instance is prefill or decode and the
            # per-role counts must add up to --num-instances
            args += ["--prefill-instances", str(int(_pre)),
                     "--decode-instances", str(int(_dec)),
                     "--num-instances", str(int(_pre) + int(_dec))]
        else:
            _rep = instances or getattr(cfg, "prefill_pods", None) \
                or getattr(cfg, "replicas", None) \
                or getattr(cfg, "single_test_replicas", None) or 1
            if int(_rep) > 1:
                args += ["--num-instances", str(int(_rep))]

        self._record_scenario_epp(scenario_path)
        self.last_command = " ".join(str(a) for a in args)
        try:
            r = self._exec_result_file(args, out_file, timeout=timeout)
        except subprocess.TimeoutExpired:
            self._log(f"BLIS: simulation timed out after {timeout}s", "warn")
            return None

        # Our rendered EndpointPickerConfig can lag the simulator's pinned
        # router (llm-d v1.0.0) — plugin renames, format changes. On a
        # config parse failure, fall back to the router's built-in defaults
        # so the simulation still runs, with a clear note.
        if r.returncode != 0 and "failed to parse" in (r.stderr or ""):
            with open(scenario_path) as f:
                _scn = f.read()
            if "router/serveit-epp.yaml" in _scn:
                self._log(
                    "BLIS: ServeIt's EPP config doesn't parse in the simulator's "
                    "pinned router — retrying with the router's built-in defaults "
                    "(routing policy differs from the deployed cluster)", "warn")
                _scn = _scn.replace("router/serveit-epp.yaml",
                                    "router/default-colocated.yaml")
                with open(scenario_path, "w") as f:
                    f.write(_scn)
                if self.remote:
                    self.kubectl.run(
                        ["cp", scenario_path,
                         f"{self.pod_name}:{pod_scenarios_dir}/{scenario_name}",
                         "-n", self.namespace], check=False, timeout=120)
                try:
                    r = self._exec_result_file(args, out_file, timeout=timeout)
                except subprocess.TimeoutExpired:
                    self._log(f"BLIS: simulation timed out after {timeout}s", "warn")
                    return None

        if r.returncode != 0:
            _err = r.stderr or r.stdout or ""
            # Warnings dominate the head of stderr; the actual error is at
            # the tail. Persist the full text next to the scenario and log
            # the tail so failures are diagnosable.
            _err_path = out_file + ".stderr.log"
            try:
                with open(_err_path, "w") as f:
                    f.write(_err)
            except OSError:
                _err_path = None
            _tail = " | ".join(
                ln.strip() for ln in _err.strip().splitlines()[-3:] if ln.strip()
            )
            self._log(
                f"BLIS: run failed (exit {r.returncode}"
                f"{', full stderr: ' + _err_path if _err_path else ''}): "
                f"{_tail[:400]}", "warn")
            return None
        try:
            if self.remote:
                return json.loads(r.stdout)
            with open(out_file) as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            self._log(f"BLIS: result parse failed: {e}", "warn")
            return None

    def fetch_traces(self, traces_path: str) -> Optional[str]:
        """Return a server-readable path for the traces file.

        The wizard's default WekaTrace source is the path baked into the
        WORKLOAD image (/app/traces/...), which the server cannot read.
        When the file is missing server-side and we run remotely, pull it
        from the workload pod once into a shared-storage cache.
        """
        if os.path.isfile(traces_path):
            return traces_path
        if not self.remote:
            return None
        cached = os.path.join(self.home, "traces", os.path.basename(traces_path))
        if os.path.isfile(cached):
            return cached
        try:
            os.makedirs(os.path.dirname(cached), exist_ok=True)
            self.kubectl.run(
                ["cp", f"{self.pod_name}:{traces_path}", cached,
                 "-n", self.namespace], check=False, timeout=600)
            if os.path.isfile(cached):
                self._log(f"   BLIS: pulled traces from the workload pod "
                          f"({os.path.basename(traces_path)}, {os.path.getsize(cached)//1024//1024}MB)", "info")
                return cached
        except Exception as e:
            self._log(f"   BLIS: traces fetch failed: {e}", "warn")
        return None

    @staticmethod
    def build_workload_spec(shapes, duration_s: int = 300, out_dir: str = "/tmp",
                            max_model_len: int = None):
        """Convert ServeIt parallel-workload shapes to a BLIS workload spec.

        Each shape becomes a closed-loop ClientSpec with its own
        concurrency, ISL/OSL distribution and shared prefix group — the
        analog of guidellm's parallel per-shape processes.
        Returns the spec file path or None.
        """
        if not shapes:
            return None
        import yaml as _yaml
        clients = []
        total_conc = 0
        for sh in shapes:
            isl = int(sh.get("isl") or 4500)
            isl_min = sh.get("isl_min")
            isl_max = sh.get("isl_max")
            osl = int(sh.get("osl") or 200)
            conc = int(sh.get("concurrency") or 1)
            total_conc += conc
            # BLIS PREPENDS prefix_length ON TOP of the input
            # distribution — guidellm CARVES prefix_tokens OUT of the ISL.
            # The non-prefix portion = ISL - prefix_tokens. Without this
            # subtraction, W1 (ISL 6000, prefix 4800) becomes 10800 total
            # tokens and the cache hit collapses (3.5% vs the real 29.5%).
            prefix_len = int(sh.get("prefix_tokens") or 0)
            non_prefix = max(2, isl - prefix_len)
            if isl_min is not None and isl_max is not None:
                lo, hi = int(isl_min) - prefix_len, int(isl_max) - prefix_len
                lo = max(2, lo)
                params = {"mean": (lo + hi) // 2, "std_dev": max(1, (hi - lo) // 4),
                          "min": lo, "max": hi}
            else:
                params = {"mean": non_prefix, "std_dev": 0,
                          "min": non_prefix, "max": non_prefix}
            # BLIS prepends the prefix, so the *total* prompt must fit the
            # model window: clamp the non-prefix part. Real guidellm serves
            # nothing longer than the deployment's limit, and a capped
            # request makes the simulator emit an invalid Result (PD runs
            # even abort on it).
            if max_model_len:
                headroom = max(64, int(osl))
                allowed = max(2, int(max_model_len) - headroom - prefix_len)
                if params["max"] > allowed:
                    params["max"] = allowed
                    params["min"] = min(params["min"], allowed)
                    params["mean"] = min(params["mean"], allowed)
                    if params.get("std_dev"):
                        params["std_dev"] = max(1, min(params["std_dev"],
                                                       (allowed - params["min"]) // 2 or 1))
            # Distinct per-user prefixes: split the shape's concurrency
            # across `prefix_count` prefix groups so the router sees
            # independent caches instead of one shared warm group. A single
            # shared prefix makes every pod equally warm, which masks the
            # affinity hotspots this knob exists to study.
            prefix_count = int(sh.get("prefix_count") or 1)
            name = sh.get("name") or f"shape-{len(clients)}"
            client = {
                "closed_loop": True,
                "prefix_length": prefix_len,
                "input_distribution": {"type": "gaussian", "params": params},
                "output_distribution": {
                    "type": "constant", "params": {"value": osl}},
            }
            if prefix_count > 1 and prefix_len > 0 and conc > 1:
                groups = min(prefix_count, conc)
                per, extra = divmod(conc, groups)
                for gi in range(groups):
                    c = per + (1 if gi < extra else 0)
                    if c <= 0:
                        continue
                    entry = copy.deepcopy(client)
                    entry.update(id=f"{name}-p{gi}", concurrency=c,
                                 prefix_group=f"{name}-p{gi}")
                    clients.append(entry)
            else:
                entry = copy.deepcopy(client)
                entry.update(id=name, concurrency=conc, prefix_group=name)
                clients.append(entry)
        # enough requests to fill the window at these concurrencies;
        # horizon caps the simulated duration
        # generous request budget — the horizon is the binding
        # constraint, matching the real run's duration semantics
        num_requests = max(1000, total_conc * duration_s * 5)
        spec = {"version": "2", "num_requests": num_requests, "clients": clients}
        os.makedirs(out_dir, exist_ok=True)
        spec_path = os.path.join(out_dir, "serveit-workload-spec.yaml")
        with open(spec_path, "w") as f:
            _yaml.safe_dump(spec, f, sort_keys=False)
        return spec_path

    # -- trace replay ------------------------------------------------------

    @staticmethod
    def convert_wekatrace(traces_path: str, out_dir: str, name: str,
                          max_traces: int = 0, duration_s: int = 300,
                          model_name: str = "model", block_tokens: int = 64,
                          max_input_tokens: int = None):
        """Convert WekaTrace agentic-session JSONL to BLIS TraceV2.

        Each JSONL line is one session (requests with hash_ids, in/out
        token counts, arrival t in seconds). Produces the header YAML +
        data CSV blis replay consumes. Arrival times are COMPRESSED to
        fit duration_s (order, session structure, burst ratios and
        prefix sharing preserved; absolute pacing is not).

        max_input_tokens drops requests whose prompt exceeds the model's
        context budget (they would be unservable in the simulator exactly
        as they would 400 on a real deployment).

        Returns (header_path, csv_path) or (None, None) on failure.
        """
        import csv as _csv

        sessions = []
        try:
            with open(traces_path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        sessions.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            return None, None
        sessions = [s for s in sessions if s.get("requests")]
        if max_traces and max_traces > 0:
            sessions = sessions[:max_traces]
        if not sessions:
            return None, None

        rows = []
        skipped = 0
        request_id = 0
        t_max = 0.0
        for s_idx, sess in enumerate(sessions):
            sid = f"weka-{s_idx:03d}"
            reqs = []
            for r in sess.get("requests", []):
                if r.get("type") == "subagent" or not r.get("hash_ids"):
                    continue
                _in = int(r.get("in", 0) or 0)
                _out = max(1, int(r.get("out", 1) or 1))
                if max_input_tokens and (_in + _out) > max_input_tokens:
                    skipped += 1
                    continue
                reqs.append(r)
            # Conversation budget: guidellm's native weka deserializer drops a
            # turn (and all later turns of that session) once cumulative
            # input+output exceeds max_context_len. Mirror it here so BLIS and
            # the real replay serve the SAME request set — a per-request filter
            # kept ~3.7x more long turns and made the two incomparable.
            if max_input_tokens:
                _cum = 0
                _kept = []
                for _r in reqs:
                    _tok = int(_r.get("in", 0) or 0) + max(1, int(_r.get("out", 1) or 1))
                    if _cum + _tok > max_input_tokens:
                        skipped += len(reqs) - len(_kept)
                        break
                    _cum += _tok
                    _kept.append(_r)
                reqs = _kept
            prev_ids = []
            for r_idx, r in enumerate(reqs):
                t = float(r.get("t", 0) or 0)
                t_max = max(t_max, t)
                hash_ids = r.get("hash_ids") or []
                # prefix shared with the previous request of this session
                shared = 0
                for a, b in zip(prev_ids, hash_ids):
                    if a != b:
                        break
                    shared += 1
                rows.append({
                    "_t": t,
                    "request_id": request_id,
                    "session_id": sid,
                    "client_id": sid,
                    "round_index": r_idx,
                    "arrival_time_us": 0,  # filled after scaling
                    "input_tokens": int(r.get("in", 0) or 0),
                    "output_tokens": max(1, int(r.get("out", 1) or 1)),
                    "prefix_length": shared * block_tokens,
                    "prefix_group": sid,
                    "model": model_name,
                    "status": "completed",
                    "slo_class": "standard",
                    "streaming": "true",
                    "tenant_id": "serveit",
                    "deadline_us": 0,
                    "first_chunk_time_us": 0,
                    "last_chunk_time_us": 0,
                    "think_time_us": 0,
                    "reason_ratio": 0.0,
                    "text_tokens": int(r.get("in", 0) or 0),
                    "image_tokens": 0,
                    "audio_tokens": 0,
                    "video_tokens": 0,
                })
                request_id += 1
                prev_ids = hash_ids

        if not rows:
            return None, None

        # compress arrivals into the soak window
        scale = (duration_s / t_max) if t_max > 0 else 1.0
        for row in rows:
            row["arrival_time_us"] = int(row["_t"] * scale * 1_000_000)
            del row["_t"]

        os.makedirs(out_dir, exist_ok=True)
        header_path = os.path.join(out_dir, f"{name}.trace-header.yaml")
        csv_path = os.path.join(out_dir, f"{name}.trace-data.csv")
        fields = [
            "request_id", "session_id", "client_id", "round_index",
            "arrival_time_us", "deadline_us", "first_chunk_time_us",
            "last_chunk_time_us", "think_time_us", "input_tokens",
            "output_tokens", "prefix_length", "prefix_group", "model",
            "status", "slo_class", "streaming", "tenant_id", "reason_ratio",
            "text_tokens", "image_tokens", "audio_tokens", "video_tokens",
        ]
        with open(csv_path, "w", newline="") as f:
            w = _csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(rows)
        with open(header_path, "w") as f:
            f.write("mode: converted\n"
                    "time_unit: us\n"
                    "trace_version: 4\n"
                    "workload_seed: 42\n")
        if skipped:
            import sys as _sys
            print(f"WekaTrace->TraceV2: kept {len(rows)} requests, dropped "
                  f"{skipped} over the {max_input_tokens}-token context budget",
                  file=_sys.stderr)
        # sidecar stats so reports can explain thin routing cells
        try:
            with open(os.path.join(out_dir, f"{name}.stats.json"), "w") as f:
                json.dump({"kept": len(rows), "dropped_over_budget": skipped,
                           "max_input_tokens": max_input_tokens}, f)
        except OSError:
            pass
        return header_path, csv_path

    def run_replay(self, scenario_path: str, scenario_name: str,
                   trace_header: str, trace_data: str, timeout: int = 1800,
                   instances: int = None, slo_ttft_ms: int = None):
        """Replay a TraceV2 file through the scenario (blis replay).

        Stages scenario, router config, catalog overlay and both trace
        files to the workload pod in remote mode; Result parsed from
        stdout like run().
        """
        if not scenario_name.endswith(".yaml"):
            scenario_name += ".yaml"
        if not self.available():
            self._log("BLIS: binary not found — skipping replay", "warn")
            return None

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
            for local in (trace_header, trace_data):
                self.kubectl.run(
                    ["cp", local,
                     f"{self.pod_name}:{pod_scenarios_dir}/{os.path.basename(local)}",
                     "-n", self.namespace], check=False, timeout=600)
            _th = f"{pod_scenarios_dir}/{os.path.basename(trace_header)}"
            _td = f"{pod_scenarios_dir}/{os.path.basename(trace_data)}"
        else:
            _th, _td = trace_header, trace_data

        scenarios_dir = pod_scenarios_dir or os.path.dirname(scenario_path)
        overlay_flag = None
        if self.OVERLAY_DIR and os.path.isdir(self.OVERLAY_DIR):
            if self.remote:
                _pod_overlay = f"{pod_scenarios_dir}/catalog-overlay"
                self.kubectl.run(
                    ["cp", self.OVERLAY_DIR, f"{self.pod_name}:{_pod_overlay}",
                     "-n", self.namespace], check=False, timeout=300)
                overlay_flag = _pod_overlay
            else:
                overlay_flag = self.OVERLAY_DIR

        args = [
            "replay",
            "--scenarios", scenarios_dir,
            "--scenario", scenario_name,
            "--trace-header", _th,
            "--trace-data", _td,
        ]
        if overlay_flag:
            args += ["--catalog-overlay", overlay_flag]
        # instance count: multiple replicas pack onto the scenario's nodes
        if instances and int(instances) > 1:
            args += ["--num-instances", str(int(instances))]
        # SLO target from the wizard's latency constraint: enables
        # slo_attainment + goodput_rps in the Result
        if slo_ttft_ms and int(slo_ttft_ms) > 0:
            args += ["--slo-ttft", f"standard={int(slo_ttft_ms)}ms"]
        # Saturation detection: composite detector + verdict trace
        # inference-sim v0.18.1 replaced the composite/threshold/backlog
        # detectors with "settle"; any other value is a fatal error.
        args += ["--detectors", "settle"]
        if self.remote:
            args += ["--saturation-report", f"{pod_scenarios_dir}/saturation.json"]

        self._record_scenario_epp(scenario_path)
        self.last_command = " ".join(str(a) for a in args)
        try:
            r = self._exec(args, timeout=timeout)
        except subprocess.TimeoutExpired:
            self._log(f"BLIS: replay timed out after {timeout}s", "warn")
            return None
        if r.returncode != 0:
            self._log(f"BLIS: replay failed: {(r.stderr or r.stdout)[:400]}", "warn")
            return None
        try:
            result = json.loads(r.stdout)
        except (OSError, json.JSONDecodeError) as e:
            self._log(f"BLIS: replay result parse failed: {e}", "warn")
            return None
        # merge the saturation verdict (written to a file, not the Result)
        if self.remote:
            _sat = self.kubectl.run(
                ["exec", self.pod_name, "-n", self.namespace, "--",
                 "cat", f"{pod_scenarios_dir}/saturation.json"],
                check=False, timeout=60)
            if _sat.returncode == 0 and _sat.stdout.strip():
                try:
                    result["saturation"] = json.loads(_sat.stdout)
                except json.JSONDecodeError:
                    pass
        return result

    # -- results ----------------------------------------------------------

    @staticmethod
    def _pct(values, p):
        """Nearest-rank percentile of a list."""
        if not values:
            return None
        s = sorted(values)
        k = min(len(s) - 1, max(0, int(round((p / 100.0) * (len(s) - 1)))))
        return s[k]

    @staticmethod
    def summarize(result: dict):
        """Flatten a BLIS Result into ServeIt-style metric names.

        blis run Results carry ttft_*/itl_*/e2e_* in metrics; blis replay
        Results carry per-request rows (ttft_us/tpot_us/e2e_us) with only
        throughput keys in metrics — aggregate the rows in that case.
        """
        if not result:
            return {}
        metrics = result.get("metrics", {})
        out = {}
        for blis_key, (label, _) in _RESULT_METRICS.items():
            entry = metrics.get(blis_key)
            if isinstance(entry, dict) and entry.get("value") is not None:
                out[blis_key] = entry["value"]

        if not out.get("ttft_p95_ms") and result.get("request_rows"):
            rows = [r for r in result["request_rows"]
                    if r.get("fate") == "completed" and r.get("ttft_us") is not None]
            if rows:
                ttft = [r["ttft_us"] / 1000.0 for r in rows]
                tpot = [r["tpot_us"] / 1000.0 for r in rows if r.get("tpot_us")]
                e2e = [r["e2e_us"] / 1000.0 for r in rows if r.get("e2e_us")]
                out["ttft_mean_ms"] = sum(ttft) / len(ttft)
                out["itl_mean_ms"] = (sum(tpot) / len(tpot)) if tpot else None
                out["e2e_mean_ms"] = (sum(e2e) / len(e2e)) if e2e else None
                for p in (50, 90, 95, 99):
                    out[f"ttft_p{p}_ms"] = BlisRunner._pct(ttft, p)
                    out[f"itl_p{p}_ms"] = BlisRunner._pct(tpot, p)
                    out[f"e2e_p{p}_ms"] = BlisRunner._pct(e2e, p)
            if out.get("request_throughput") is None:
                rt = metrics.get("request_throughput")
                if isinstance(rt, dict) and rt.get("value") is not None:
                    out["request_throughput"] = rt["value"]
        return out
