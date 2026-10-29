"""Guidellm execution — persistent pod management and benchmark running."""

import os
import json
import time
import subprocess
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional, Callable

from core.config_generator import TestConfig

logger = logging.getLogger(__name__)


def _build_synthetic_data_dict(config, turns):
    """Synthetic-text data spec from a config's shape fields.

    Shared by the single-workload and parallel-workload paths so the
    optional-attribute mapping and the max_model_len capping can never
    drift between them.
    """
    import json as _dj

    data_dict = {"kind": "synthetic_text", "prompt_tokens": config.isl, "output_tokens": config.osl}
    for attr, key in [
        ("isl_stdev", "prompt_tokens_stdev"),
        ("osl_stdev", "output_tokens_stdev"),
        ("isl_min", "prompt_tokens_min"),
        ("osl_min", "output_tokens_min"),
        ("osl_max", "output_tokens_max"),
        ("first_prompt_tokens", "first_prompt_tokens"),
        ("first_prompt_tokens_stdev", "first_prompt_tokens_stdev"),
        ("first_prompt_tokens_min", "first_prompt_tokens_min"),
        ("first_prompt_tokens_max", "first_prompt_tokens_max"),
        ("first_output_tokens", "first_output_tokens"),
        ("first_output_tokens_stdev", "first_output_tokens_stdev"),
        ("turn_delay", "delay"),
        ("turn_delay_stdev", "delay_stdev"),
        ("turn_delay_min", "delay_min"),
        ("turn_delay_max", "delay_max"),
    ]:
        val = getattr(config, attr, None)
        if val is not None:
            data_dict[key] = val
    if turns > 1:
        data_dict["turns"] = turns
    if getattr(config, "prefix_tokens", None):
        data_dict["prefix_buckets"] = [
            {
                "bucket_weight": 100,
                "prefix_count": getattr(config, "prefix_count", 1) or 1,
                "prefix_tokens": config.prefix_tokens,
            }
        ]
    if getattr(config, "isl_max", None):
        data_dict["prompt_tokens_max"] = config.isl_max
    else:
        max_model_len = getattr(config, "max_model_len", 0)
        if max_model_len and (getattr(config, "isl_stdev", None) or getattr(config, "osl_stdev", None)):
            # Budget for prefix + sampled output tail + template overhead,
            # same terms used by the pipeline's max_model_len bump.
            ctx_budget = (getattr(config, "prefix_tokens", None) or 0) + _ctx_max_output(config) + 512
            prompt_max = max_model_len - ctx_budget
            if prompt_max > 0:
                data_dict["prompt_tokens_max"] = prompt_max
    # Cap first_prompt_tokens_max so turn-0 prompts fit the model context.
    # The sampler uses first_prompt_tokens_max as its upper bound; if it
    # exceeds max_model_len, the synthetic generator will produce prompts
    # that vLLM rejects with a context-length error.
    if getattr(config, "first_prompt_tokens", None):
        max_model_len = getattr(config, "max_model_len", 0)
        if max_model_len:
            ctx_budget = (getattr(config, "prefix_tokens", None) or 0) + _ctx_max_output(config) + 512
            fpt_ceiling = max_model_len - ctx_budget
            if fpt_ceiling > 0:
                fpt_max = getattr(config, "first_prompt_tokens_max", None)
                if fpt_max is not None and int(fpt_max) > fpt_ceiling:
                    data_dict["first_prompt_tokens_max"] = fpt_ceiling
    return _dj.dumps(data_dict)


def _ctx_max_output(config) -> int:
    """Worst-case sampled output tokens: explicit cap if set, else 4-sigma tail."""
    osl = config.osl or 0
    if getattr(config, "osl_max", None) is not None:
        return config.osl_max
    osl_stdev = config.osl_stdev or 0
    return osl + (4 * osl_stdev if osl_stdev else 0)


class GuidellmMixin:
    """Mixin providing guidellm pod management and benchmark execution."""

    _guidellm_pod_name = "serveit-workload"

    def _resolve_guidellm_pod_name(self):
        """Find the actual pod name from the workload deployment."""
        kubectl = self.deployment_manager.kubectl
        r = kubectl.run(
            [
                "get",
                "pods",
                "-l",
                "app=serveit-workload",
                "-n",
                self.namespace,
                "-o",
                "jsonpath={.items[0].metadata.name}",
            ],
            check=False,
        )
        if r.returncode == 0 and r.stdout.strip():
            self._guidellm_pod_name = r.stdout.strip()
        return self._guidellm_pod_name

    def ensure_guidellm_pod(self, config, log_callback=None):
        """Deploy the workload deployment if not already running."""
        kubectl = self.deployment_manager.kubectl

        # Check if a workload pod is already running
        r = kubectl.run(
            [
                "get",
                "pods",
                "-l",
                "app=serveit-workload",
                "-n",
                self.namespace,
                "-o",
                "jsonpath={.items[0].status.phase}",
            ],
            check=False,
        )
        if r.returncode == 0 and r.stdout.strip() == "Running":
            self._resolve_guidellm_pod_name()
            return True

        if log_callback:
            log_callback("📦 Deploying guidellm benchmark pod...")

        # Ensure modelserver ServiceAccount exists before creating the workload pod
        r = kubectl.run(["get", "sa", "llm-d-modelserver", "-n", self.namespace], check=False)
        if r.returncode != 0:
            from core.prereq_manager import PrereqManager

            pm = PrereqManager(namespace=self.namespace, kubectl_runner=kubectl)
            pm._ensure_modelserver_rbac(log_callback=log_callback)

        from core.template_manager import TemplateManager

        tmgr = TemplateManager()
        deploy_yaml = tmgr.render_template(
            "benchmark/guidellm-pod.yaml.j2",
            namespace=self.namespace,
            image=os.environ.get("GUIDELLM_IMAGE", "quay.io/bbenshab/serveit-studio:workload"),
            pvc_name=self._get_storage_pvc_name(config),
            hf_token=os.environ.get("HF_TOKEN", ""),
            node_nfs_pvcs=getattr(config, "node_nfs_pvcs", None) or [],
        )

        result = kubectl.run(["apply", "-f", "-", "-n", self.namespace], input_data=deploy_yaml)
        if result.returncode != 0:
            if log_callback:
                log_callback(f"❌ Failed to create guidellm deployment: {result.stderr}")
            return False

        # Wait for pod to be running
        for _ in range(120):
            r = kubectl.run(
                [
                    "get",
                    "pods",
                    "-l",
                    "app=serveit-workload",
                    "-n",
                    self.namespace,
                    "-o",
                    "jsonpath={.items[0].status.phase}",
                ],
                check=False,
            )
            phase = r.stdout.strip() if r.returncode == 0 else ""
            if phase == "Running":
                self._resolve_guidellm_pod_name()
                if log_callback:
                    log_callback("✅ Guidellm pod ready")
                return True
            if phase in ("Failed", "Error"):
                if log_callback:
                    log_callback(f"❌ Guidellm pod failed: {phase}")
                return False
            time.sleep(5)

        if log_callback:
            log_callback("❌ Guidellm pod did not start within 10 minutes")
        return False

    def _get_storage_pvc_name(self, config):
        """Get the PVC name for guidellm pod — must be RWX for multi-pod access."""
        kubectl = self.deployment_manager.kubectl
        # Get the optimizer pod's PVC
        try:
            r = kubectl.run(
                [
                    "get",
                    "pod",
                    "-l",
                    "app=serveit-optimizer",
                    "-n",
                    self.namespace,
                    "-o",
                    "jsonpath={.items[0].spec.volumes[?(@.persistentVolumeClaim)].persistentVolumeClaim.claimName}",
                ],
                check=False,
            )
            if r.returncode == 0 and r.stdout.strip():
                pvc = r.stdout.strip().split()[0]
                # Verify it's RWX
                mode_r = kubectl.run(
                    ["get", "pvc", pvc, "-n", self.namespace, "-o", "jsonpath={.spec.accessModes[0]}"], check=False
                )
                if mode_r.returncode == 0 and "ReadWriteMany" in mode_r.stdout:
                    return pvc
                # PVC is RWO — look for an RWX alternative
                rwx_r = kubectl.run(
                    [
                        "get",
                        "pvc",
                        "-n",
                        self.namespace,
                        "-o",
                        'jsonpath={range .items[*]}{.metadata.name}{" "}{.spec.accessModes[0]}{"\\n"}{end}',
                    ],
                    check=False,
                )
                if rwx_r.returncode == 0:
                    for line in rwx_r.stdout.strip().split("\n"):
                        parts = line.strip().split()
                        if len(parts) == 2 and parts[1] == "ReadWriteMany":
                            return parts[0]
        except Exception:
            pass
        return getattr(config, "pvc_name", None) or "serveit-cache"

    def _smoke_test_gateway(
        self,
        endpoint: str,
        config: TestConfig,
        log_callback: Optional[Callable[[str], None]] = None,
        timeout: int = 60,
        attempts: int = 3,
    ) -> bool:
        """Send one tiny completion through the gateway before the load test.

        The models endpoint alone is not enough: a wedged EPP still serves
        /v1/models but hangs real completions, which turns the load test
        into a multi-minute dead run. The curl uses a total-time cap so a
        hung gateway fails fast instead of blocking forever.
        """
        turns = getattr(config, "turns", 1) or 1
        if turns > 1:
            url = f"{endpoint.rstrip('/')}/v1/chat/completions"
            payload = {
                "model": config.model_name,
                "messages": [{"role": "user", "content": "Hello"}],
                "max_tokens": 1,
                "temperature": 0.0,
            }
        else:
            url = f"{endpoint.rstrip('/')}/v1/completions"
            payload = {"model": config.model_name, "prompt": "Hello", "max_tokens": 1, "temperature": 0.0}
        payload_json = json.dumps(payload)
        curl_cmd = (
            f"curl -s -o /dev/null -w '%{{http_code}}' --max-time {timeout} "
            f"-X POST -H 'Content-Type: application/json' -d '{payload_json}' '{url}'"
        )
        for attempt in range(1, attempts + 1):
            try:
                r = self.deployment_manager.kubectl.run(
                    ["exec", self._guidellm_pod_name, "-n", self.namespace, "--", "bash", "-c", curl_cmd],
                    check=False,
                    timeout=timeout + 30,
                )
                code = r.stdout.strip() if r.returncode == 0 else "000"
            except Exception as e:
                code = f"000 ({str(e)[:40]})"
            if code == "200":
                if log_callback:
                    log_callback("   ✅ Gateway smoke test passed (completion returned 200)")
                return True
            if log_callback:
                hint = (
                    "request hung — gateway/EPP not completing requests"
                    if code.startswith("000")
                    else "non-200 response"
                )
                log_callback(f"   ⚠️  Gateway smoke test attempt {attempt}/{attempts}: HTTP {code} ({hint})")
            if attempt < attempts:
                time.sleep(15)
        return False

    def _run_guidellm_job(
        self,
        endpoint: str,
        config: TestConfig,
        log_callback: Optional[Callable[[str], None]] = None,
        monitor_pods: bool = False,
        expected_pod_count: int = 0,
        collect_metrics: bool = True,
        stop_check: Optional[Callable[[], bool]] = None,
    ) -> tuple[bool, Optional[str], Optional[str]]:
        """Run guidellm via kubectl exec on the persistent guidellm pod.

        Same interface as _run_guidellm_test. The persistent pod is deployed
        once at optimization start and reused for all tests.
        """
        kubectl = self.deployment_manager.kubectl

        # Ensure guidellm pod is running
        if not self.ensure_guidellm_pod(config, log_callback):
            return False, None, None

        if endpoint is None:
            endpoint = self._discover_istio_gateway(self.namespace, config.test_id, config.architecture, log_callback)

        # Parallel workloads: N shapes run simultaneously against the same
        # serving stack (one EPP/pool/gateway), each with its own
        # concurrency. Same result contract — one merged output file.
        parallel = getattr(config, "parallel_workloads", None)
        if parallel:
            return self._run_parallel_guidellm_jobs(
                endpoint, config, log_callback, monitor_pods, expected_pod_count, collect_metrics, stop_check
            )

        # Build guidellm 0.7.x command
        request_rate = getattr(config, "request_rate", 1)
        request_profile = getattr(config, "request_type", "constant")
        turns = getattr(config, "turns", 1) or 1

        # Data argument
        workload_mode = getattr(config, "workload_mode", "synthetic") or "synthetic"
        data_arg = ""
        column_args = ""
        if workload_mode == "dataset" and getattr(config, "dataset_source", None):
            dataset_path = config.dataset_source
            exists = (
                kubectl.run(
                    ["exec", self._guidellm_pod_name, "-n", self.namespace, "--", "test", "-f", dataset_path],
                    check=False,
                ).returncode
                == 0
            )
            if exists:
                if log_callback:
                    log_callback(f"   Dataset already on workload pod: {os.path.basename(dataset_path)}")
            elif os.path.isfile(dataset_path):
                remote_dir = os.path.dirname(dataset_path)
                kubectl.run(
                    ["exec", self._guidellm_pod_name, "-n", self.namespace, "--", "mkdir", "-p", remote_dir],
                    check=False,
                )
                import subprocess as _sp

                env = os.environ.copy()
                env["KUBECONFIG"] = os.path.expanduser(kubectl.kubeconfig)
                _sp.run(
                    [
                        kubectl.kubectl_cmd,
                        "cp",
                        dataset_path,
                        f"{self._guidellm_pod_name}:{dataset_path}",
                        "-n",
                        self.namespace,
                    ],
                    env=env,
                    check=False,
                    timeout=120,
                )
                if log_callback:
                    log_callback(f"   Copied dataset to workload pod: {os.path.basename(dataset_path)}")
            else:
                if log_callback:
                    log_callback(f"   ⚠️  Dataset not found: {dataset_path}")
            col = getattr(config, "dataset_column", None) or "prompt"
            if dataset_path.endswith(".jsonl") or dataset_path.endswith(".json"):
                data_arg = f'{{"kind":"json_file","path":"{dataset_path}","load_kwargs":{{"split":"train"}}}}'
            else:
                data_arg = f"kind=csv_file,path={dataset_path}"
            column_args = f' --data-column-mapper \'{{"kind":"generative_column_mapper","column_mappings":{{"text_column":"{col}","output_tokens_count_column":"output_tokens_count"}}}}\''
        else:
            data_arg = _build_synthetic_data_dict(config, turns)

        # Profile argument (rate type + rate)
        request_format = "/v1/chat/completions" if turns > 1 else "/v1/completions"
        warmup = min(60, max(0, config.test_duration - 30)) if hasattr(config, "test_duration") else 60
        if request_profile in ("concurrent",):
            profile_arg = f"kind=concurrent,streams={request_rate},warmup={warmup}"
        elif request_profile == "sweep":
            profile_arg = f"kind=sweep,warmup={warmup}"
        elif request_profile == "throughput":
            profile_arg = f"kind=throughput,warmup={warmup}"
        elif request_profile == "synchronous":
            profile_arg = f"kind=synchronous,warmup={warmup}"
        elif request_profile == "poisson":
            profile_arg = f"kind=poisson,rate={request_rate},warmup={warmup}"
        else:
            profile_arg = f"kind=constant,rate={request_rate},warmup={warmup}"

        # Constraint argument (stop condition)
        # Use max_requests when set — multi-turn workloads have unpredictable duration
        max_requests = getattr(config, "max_requests", None)
        if max_requests:
            constraint_arg = f"kind=max_requests,count={max_requests}"
        else:
            constraint_arg = f"kind=max_duration,seconds={config.test_duration}"

        # Backend argument — LoRA model list from config (same logic as
        # _run_guidellm_test, built locally since this is a separate function)

        lora_adapters_list = None
        _ln = int(getattr(config, "lora_num_adapters", 0) or 0)
        _lm = getattr(config, "lora_benchmark_mode", "round_robin") or "round_robin"
        _session_pinned = _lm == "session_pinned"
        if getattr(config, "lora_enabled", False) and _ln > 0 and _lm in ("round_robin", "session_pinned"):
            lora_adapters_list = [f"agent-{i:04d}" for i in range(1, _ln + 1)]
            if log_callback:
                if _session_pinned:
                    log_callback(f"🎯 LoRA session-pinned: each conversation keeps one of {_ln} adapters")
                else:
                    log_callback(f"🔀 LoRA round-robin: {_ln} adapters via native guidellm lora_adapters")

        if lora_adapters_list:
            import json as _json

            _backend = {
                "kind": "openai_http",
                "target": endpoint,
                "model": config.model_name,
                "lora_adapters": lora_adapters_list,
                "request_format": request_format,
                "http2": False,
                "timeout": 300,
            }
            if _session_pinned:
                # One model per conversation, kept for all of its turns —
                # the conversation's history stays cacheable under a single
                # model key and session-affinity routing can pin the whole
                # conversation to one backend.
                _backend["model_assignment"] = "conversation"
            backend_arg = _json.dumps(_backend)
        else:
            backend_arg = f"kind=openai_http,target={endpoint},model={config.model_name},request_format={request_format},http2=false,timeout=300"

        # Output path on the workload pod — use PVC to avoid node disk pressure
        output_path = f"/mnt/storage/tmp/guidellm-{config.test_id}.json"

        fail_path = output_path.replace(".json", "_fail.json")
        exec_cmd = (
            f"mkdir -p /mnt/storage/tmp && "
            f"[ -f {output_path} ] && mv {output_path} {fail_path} 2>/dev/null; "
            f"rm -f {output_path} && "
            f"export TMPDIR=/mnt/storage/tmp && "
            f"guidellm run"
            f' --backend "{backend_arg}"'
            f' --tokenizer "kind=huggingface_auto,model={config.model_name}"'
            f" --data '{data_arg}'"
            f' --profile "{profile_arg}"'
            f' --constraint "{constraint_arg}"'
            f' --output "kind=json,path={output_path}"'
            f' --metrics "kind=generative,sample_size=50"'
            f"{column_args}"
        )

        if log_callback:
            rate_label = (
                f"{request_rate} concurrent users"
                if request_profile == "concurrent"
                else f"{request_rate} req/s ({request_profile})"
            )
            if request_profile == "sweep":
                rate_label = "sweep (auto-discover rates)"
            log_callback(f"🏃 Running guidellm on pod {self._guidellm_pod_name}")
            log_callback(
                f'🔧 guidellm command: guidellm run --backend "{backend_arg}" --tokenizer "kind=huggingface_auto,model={config.model_name}" --data \'{data_arg}\' --profile "{profile_arg}" --constraint "{constraint_arg}" --output "kind=json,path={output_path}"{column_args}'
            )
            log_callback(f"   Target: {endpoint}")
            log_callback(f"   Load: {rate_label}")

        benchmark_start = time.time()
        max_requests = getattr(config, "max_requests", None)
        monitor_timeout = 3600 if max_requests else config.test_duration
        monitor_timeout += warmup + 1800

        # Run guidellm detached on the workload pod and poll for completion
        # with short-lived execs. Holding one kubectl exec stream open for
        # the whole benchmark is fragile: proxies can drop the exec
        # websocket after ~30 minutes, which fakes a guidellm failure while
        # the remote process keeps running — and a retry then stacks a
        # second benchmark on top of the first.
        script_path = f"/mnt/storage/tmp/guidellm-run-{config.test_id}.sh"
        log_path = f"/mnt/storage/tmp/guidellm-{config.test_id}.log"
        exit_path = f"/mnt/storage/tmp/guidellm-{config.test_id}.exit"

        def _remote_exec(cmd, timeout=30):
            return kubectl.run(
                ["exec", self._guidellm_pod_name, "-n", self.namespace, "--", "bash", "-c", cmd],
                check=False,
                timeout=timeout,
            )

        # Bracket form so pkill never matches its own wrapper shell
        kill_cmd = "pkill -f 'guidellm[ ]run' 2>/dev/null; sleep 1; pkill -9 -f 'guidellm[ ]run' 2>/dev/null; true"

        try:
            # Kill stale guidellm from a previous attempt or run so retries
            # never stack two benchmarks against the same endpoint.
            _remote_exec(kill_cmd)

            # Write the run script via stdin (avoids nested quoting) and
            # launch it detached; the script records guidellm's exit code
            # in a file the poll loop below watches for.
            script = exec_cmd + f"\necho $? > {exit_path}"
            write_r = kubectl.run(
                [
                    "exec",
                    "-i",
                    self._guidellm_pod_name,
                    "-n",
                    self.namespace,
                    "--",
                    "bash",
                    "-c",
                    f"cat > {script_path}",
                ],
                input_data=script,
                check=False,
                timeout=60,
            )
            if write_r.returncode != 0:
                if log_callback:
                    log_callback(f"❌ Failed to stage guidellm script on workload pod: {write_r.stderr[:150]}")
                return False, None, None

            launch_r = _remote_exec(
                f"rm -f {exit_path} {log_path}; nohup bash {script_path} > {log_path} 2>&1 & echo launched"
            )
            if launch_r.returncode != 0:
                if log_callback:
                    log_callback(f"❌ Failed to launch guidellm on workload pod: {launch_r.stderr[:150]}")
                return False, None, None
            if log_callback:
                log_callback(f"🚀 Guidellm launched detached on {self._guidellm_pod_name} (log: {log_path})")

            benchmark_success = False
            seen_log_lines = set()
            last_log_check = benchmark_start
            last_pod_check = benchmark_start
            poll_failures = 0

            while time.time() - benchmark_start < monitor_timeout:
                if stop_check and stop_check():
                    if log_callback:
                        log_callback("   Stopping guidellm...")
                    _remote_exec(kill_cmd, timeout=15)
                    return False, None, None

                # Poll exit status with a short-lived exec (no long streams)
                status_r = _remote_exec(
                    f"if [ -f {exit_path} ]; then cat {exit_path}; else echo RUNNING; fi", timeout=20
                )
                if status_r.returncode != 0:
                    poll_failures += 1
                    if poll_failures >= 12:
                        if log_callback:
                            log_callback("❌ Lost contact with workload pod while polling guidellm")
                        _remote_exec(kill_cmd, timeout=15)
                        return False, None, None
                else:
                    poll_failures = 0
                    status = status_r.stdout.strip()
                    if status != "RUNNING":
                        guidellm_exit = int(status) if status.lstrip("-").isdigit() else -1
                        elapsed = int(time.time() - benchmark_start)
                        if guidellm_exit == 0:
                            if log_callback:
                                log_callback(f"ℹ️  Guidellm completed ({elapsed}s)")
                            benchmark_success = True
                        else:
                            if log_callback:
                                log_callback(f"❌ Guidellm exited with code {guidellm_exit} ({elapsed}s)")
                                tail_r = _remote_exec(f"tail -n 10 {log_path} 2>/dev/null", timeout=20)
                                if tail_r.returncode == 0 and tail_r.stdout.strip():
                                    for line in tail_r.stdout.strip().splitlines():
                                        log_callback(f"   {line.strip()[:200]}")
                        break

                # Surface new error/warning lines from the guidellm log
                if time.time() - last_log_check >= 30:
                    last_log_check = time.time()
                    tail_r = _remote_exec(f"tail -n 200 {log_path} 2>/dev/null", timeout=20)
                    if tail_r.returncode == 0:
                        for raw in tail_r.stdout.splitlines():
                            stripped = raw.strip()
                            if not stripped or stripped in seen_log_lines:
                                continue
                            if "error" in stripped.lower() or "warning" in stripped.lower():
                                if len(seen_log_lines) > 2000:
                                    seen_log_lines.clear()
                                seen_log_lines.add(stripped)
                                if log_callback:
                                    log_callback(f"   Guidellm: {stripped[:100]}")

                # Check inference pod health
                if monitor_pods and time.time() - last_pod_check >= 30:
                    pod_error = self._monitor_pods_during_benchmark(
                        self.namespace,
                        config.test_id,
                        expected_pod_count,
                        benchmark_start,
                        config.test_duration,
                        30,
                        log_callback,
                    )
                    if pod_error:
                        benchmark_success = False
                        _remote_exec(kill_cmd, timeout=15)
                        break
                    last_pod_check = time.time()

                time.sleep(5)
            else:
                if log_callback:
                    log_callback(f"   ⚠️  Guidellm timed out after {int(monitor_timeout)}s")
                _remote_exec(kill_cmd, timeout=15)
                benchmark_success = False

            elapsed_total = int(time.time() - benchmark_start)

            if benchmark_success:
                if log_callback:
                    log_callback(f"✅ Benchmark completed ({elapsed_total}s)")

                # Extract metrics from the workload pod using parse_guidellm
                if log_callback:
                    log_callback("   Extracting results from workload pod...")

                extract_cmd = [
                    kubectl.kubectl_cmd,
                    "exec",
                    self._guidellm_pod_name,
                    "-n",
                    self.namespace,
                    "--",
                    "python3",
                    "/usr/local/bin/parse_guidellm",
                    output_path,
                ]
                extract_env = os.environ.copy()
                extract_env["KUBECONFIG"] = os.path.expanduser(kubectl.kubeconfig)

                extract_result = subprocess.run(
                    extract_cmd, capture_output=True, text=True, timeout=120, env=extract_env
                )

                if extract_result.returncode != 0 or not extract_result.stdout.strip():
                    # Fallback: try reading the file directly (local workload pod)
                    local_path = Path(output_path)
                    if local_path.exists() and local_path.stat().st_size > 0:
                        if log_callback:
                            log_callback("   Using local results file")
                        result_file = str(local_path)
                    else:
                        if log_callback:
                            log_callback(f"   ❌ Failed to extract results: {extract_result.stderr[:100]}")
                        return False, None, None
                else:
                    # Write extracted metrics to local temp file for the parser
                    import json as _json

                    local_results_dir = Path("/tmp/guidellm-extracted")
                    local_results_dir.mkdir(parents=True, exist_ok=True)
                    result_file = str(local_results_dir / f"{config.test_id}.json")

                    # parse_guidellm outputs extracted metrics — wrap in benchmarks format
                    # so _parse_guidellm_results can read it
                    try:
                        extracted = _json.loads(extract_result.stdout)
                        if "error" in extracted:
                            if log_callback:
                                log_callback(f"   ❌ Parse error: {extracted['error']}")
                            return False, None, None
                        # token totals for window-exact tok/s (parse_guidellm drops them)
                        _totals = self._extract_guidellm_totals(output_path, kubectl) or {}

                        # Reconstruct the benchmarks format the parser expects
                        # parse_guidellm flattens percentiles — nest them back
                        def _reconstitute_dist(flat):
                            pcts = {}
                            top = {}
                            for k, v in (flat or {}).items():
                                if k.startswith("p") and k[1:].isdigit():
                                    pcts[k] = v
                                else:
                                    top[k] = v
                            top["percentiles"] = pcts
                            return top

                        reconstituted = {
                            "benchmarks": [
                                {
                                    "start_time": extracted.get("start_time"),
                                    "end_time": extracted.get("end_time"),
                                    "duration": extracted.get("benchmark_duration_s"),
                                    "warmup_duration": extracted.get("warmup_duration_s"),
                                    "metrics": {
                                        "time_to_first_token_ms": {
                                            "successful": _reconstitute_dist(extracted.get("ttft_ms")),
                                            **({"total": {
                                                "mean": (_totals.get("ttft_total") or {}).get("mean"),
                                                "count": (_totals.get("ttft_total") or {}).get("count"),
                                                "percentiles": {k: (_totals.get("ttft_total") or {}).get(k)
                                                                for k in ("p50", "p90", "p95", "p999")},
                                            }} if (_totals.get("ttft_total") or {}).get("count") else {}),
                                        },
                                        "inter_token_latency_ms": {
                                            "successful": _reconstitute_dist(extracted.get("itl_ms"))
                                        },
                                        "requests_per_second": {
                                            "successful": _reconstitute_dist(extracted.get("throughput_rps"))
                                        },
                                        "time_per_output_token_ms": {
                                            "successful": _reconstitute_dist(extracted.get("tpot_ms"))
                                        },
                                        "request_latency": {
                                            "successful": _reconstitute_dist(extracted.get("e2e_latency_s"))
                                        },
                                        "output_tokens_per_second": {
                                            "successful": _reconstitute_dist(extracted.get("output_tps"))
                                        },
                                        "prompt_token_count": {
                                            "successful": _reconstitute_dist(extracted.get("prompt_tokens"))
                                        },
                                        "output_token_count": {
                                            "successful": _reconstitute_dist(extracted.get("output_tokens"))
                                        },
                                        "request_concurrency": {
                                            "successful": _reconstitute_dist(extracted.get("concurrency"))
                                        },
                                        "request_totals": extracted.get("request_totals", {}),
                                        "total_token_count": {"total": {"total_sum": _totals.get("tokens")}},
                                    },
                                }
                            ]
                        }
                        with open(result_file, "w") as rf:
                            _json.dump(reconstituted, rf)

                        if log_callback:
                            log_callback(f"   ✅ Results extracted ({config.test_id})")
                    except Exception as parse_err:
                        if log_callback:
                            log_callback(f"   ❌ Failed to process extracted results: {str(parse_err)[:100]}")
                        return False, None, None

                # Collect Prometheus metrics if configured
                metrics_path = None
                if collect_metrics:
                    try:
                        if self.metrics_collector:
                            results_dir = Path(f"/mnt/storage/results/{config.test_id}")
                            results_dir.mkdir(parents=True, exist_ok=True)
                            metrics_file = str(results_dir / "metrics.json")
                            self.metrics_collector.config.pod_name_pattern = config.test_id
                            self.metrics_collector.config.architecture = config.architecture
                            self.metrics_collector.collect_all_metrics(
                                start_time=datetime.fromtimestamp(benchmark_start),
                                end_time=datetime.now(),
                                output_file=metrics_file,
                            )
                            metrics_path = metrics_file
                    except Exception as e:
                        if log_callback:
                            log_callback(f"   ⚠️  Metrics collection failed: {str(e)[:100]}")

                return True, result_file, metrics_path

            return False, None, None

        except Exception as e:
            if log_callback:
                log_callback(f"❌ Guidellm exec failed: {str(e)[:200]}")
            return False, None, None

    def _extract_guidellm_totals(self, output_path, kubectl, timeout=60):
        """Pull total token count from a raw guidellm output on the workload pod.

        parse_guidellm (baked into the workload image) flattens only the
        summary distributions — the window-exact tokens_per_second needs
        total_token_count.total_sum, which the reconstituted benchmarks
        would otherwise drop. Best-effort: None on any failure.
        """
        try:
            cmd = (
                "python3 -c \"import json; b=json.load(open('%s'))['benchmarks'][0]; "
                "m=b.get('metrics',{}) or {}; "
                "tc=((m.get('total_token_count',{}) or {}).get('total',{}) or {}); "
                "tt=((m.get('time_to_first_token_ms',{}) or {}).get('total',{}) or {}); "
                "tp=tt.get('percentiles',{}) or {}; "
                "print(json.dumps({'tokens': tc.get('total_sum') or 0, "
                "'ttft_total': {'mean': tt.get('mean'), 'count': tt.get('count'), "
                "'p50': tp.get('p50'), 'p90': tp.get('p90'), "
                "'p95': tp.get('p95'), 'p999': tp.get('p999')}}))\"" % output_path
            )
            r = kubectl.run(
                ["exec", self._guidellm_pod_name, "-n", self.namespace, "--", "bash", "-c", cmd],
                check=False,
                timeout=timeout,
            )
            if r.returncode == 0 and r.stdout.strip():
                import json as _json

                return _json.loads(r.stdout.strip().splitlines()[-1])
        except Exception:
            pass
        return None

    def _run_parallel_guidellm_jobs(
        self,
        endpoint: str,
        config: TestConfig,
        log_callback=None,
        monitor_pods: bool = False,
        expected_pod_count: int = 0,
        collect_metrics: bool = True,
        stop_check=None,
    ) -> tuple[bool, Optional[str], Optional[str]]:
        """Run N guidellm workloads simultaneously against one endpoint.

        Global vs workload-specific split:
        - global: model, endpoint, duration, workload mode + dataset,
          turns/prefix structure, LoRA settings, max_model_len, seed base
        - per workload: name, isl, osl, stdevs, min/max, concurrency,
          rate_type (defaults to the global request_type)

        All workloads stage on the persistent workload pod with suffixed
        paths, launch detached together, and are polled with one
        short-lived exec. Results merge into a single benchmarks[] file
        (one entry per workload, tagged with its name) — the parser
        produces per-workload summaries plus the request-weighted
        combined view.
        """
        import dataclasses
        import json as _json

        kubectl = self.deployment_manager.kubectl

        if not self.ensure_guidellm_pod(config, log_callback):
            return False, None, None

        workloads = [w for w in (config.parallel_workloads or []) if w]
        if not workloads:
            return False, None, None

        turns = getattr(config, "turns", 1) or 1
        request_format = "/v1/chat/completions" if turns > 1 else "/v1/completions"
        warmup = min(60, max(0, config.test_duration - 30))
        max_requests = getattr(config, "max_requests", None)
        if max_requests:
            constraint_arg = f"kind=max_requests,count={max_requests}"
        else:
            constraint_arg = f"kind=max_duration,seconds={config.test_duration}"

        # Dataset mode: one shared data arg (a dataset has fixed content —
        # per-workload variation is the concurrency, not the corpus).
        workload_mode = getattr(config, "workload_mode", "synthetic") or "synthetic"
        shared_data_arg = None
        if workload_mode == "dataset" and getattr(config, "dataset_source", None):
            dataset_path = config.dataset_source
            if dataset_path.endswith(".jsonl") or dataset_path.endswith(".json"):
                shared_data_arg = f'{{"kind":"json_file","path":"{dataset_path}","load_kwargs":{{"split":"train"}}}}'
            else:
                shared_data_arg = f"kind=csv_file,path={dataset_path}"

        # Backend arg (global: model + LoRA wiring)
        lora_adapters_list = None
        _ln = int(getattr(config, "lora_num_adapters", 0) or 0)
        _lm = getattr(config, "lora_benchmark_mode", "round_robin") or "round_robin"
        if getattr(config, "lora_enabled", False) and _ln > 0 and _lm in ("round_robin", "session_pinned"):
            lora_adapters_list = [f"agent-{i:04d}" for i in range(1, _ln + 1)]
        if lora_adapters_list:
            _backend = {
                "kind": "openai_http",
                "target": endpoint,
                "model": config.model_name,
                "lora_adapters": lora_adapters_list,
                "request_format": request_format,
                "http2": False,
                "timeout": 300,
            }
            if _lm == "session_pinned":
                _backend["model_assignment"] = "conversation"
            backend_arg = _json.dumps(_backend)
        else:
            backend_arg = f"kind=openai_http,target={endpoint},model={config.model_name},request_format={request_format},http2=false,timeout=300"

        # Build one staged command per workload
        jobs = []
        for i, wl in enumerate(workloads):
            name = wl.get("name") or f"workload-{i}"
            overrides = {}
            for src, dst in (
                ("isl", "isl"),
                ("osl", "osl"),
                ("isl_stdev", "isl_stdev"),
                ("osl_stdev", "osl_stdev"),
                ("isl_min", "isl_min"),
                ("isl_max", "isl_max"),
                ("osl_min", "osl_min"),
                ("osl_max", "osl_max"),
                ("prefix_tokens", "prefix_tokens"),
                ("prefix_count", "prefix_count"),
            ):
                if wl.get(src) is not None:
                    overrides[dst] = wl[src]
            # A workload that defines its own shape replaces the global shape
            # ENTIRELY: leftover global min/max (e.g. a prior session's
            # isl_min=15000) override the mean in guidellm's sampler and
            # blow past max_model_len. Clear every shape field the workload
            # did not explicitly set.
            if overrides.get("isl") is not None:
                for f in ("isl_stdev", "isl_min", "isl_max"):
                    if f not in overrides:
                        overrides[f] = None
                if "prefix_tokens" not in overrides:
                    overrides["prefix_tokens"] = None
            if overrides.get("osl") is not None:
                for f in ("osl_stdev", "osl_min", "osl_max"):
                    if f not in overrides:
                        overrides[f] = None
            if wl.get("concurrency"):
                overrides["request_rate"] = int(wl["concurrency"])
            if wl.get("rate_type"):
                overrides["request_type"] = wl["rate_type"]
            shaped = dataclasses.replace(config, **overrides) if overrides else config

            # A workload that defines its own isl/osl wants a SHAPE — generate
            # synthetic data for it even when the global config runs a dataset
            # (e.g. the multi-group prefix-cache corpus overrides the data arg
            # globally; per-workload shapes must not be silently dropped).
            # Shape-less workloads share the global corpus.
            has_own_shape = wl.get("isl") is not None or wl.get("osl") is not None
            if shared_data_arg is not None and not has_own_shape:
                data_arg = shared_data_arg
            else:
                data_arg = _build_synthetic_data_dict(shaped, turns)
                # Fit the workload's own budget: prefix_buckets + isl + osl
                # must fit max_model_len or vLLM rejects every request with
                # 400 (context length). Shrink the prefix first, then drop it;
                # the SHAPE the user specified is the point of the workload.
                _mml = int(getattr(config, "max_model_len", 0) or 0)
                _d = json.loads(data_arg)
                _pt = int(_d.get("prompt_tokens", 0) or 0)
                _ot = int(_d.get("output_tokens", 0) or 0)
                _pb = _d.get("prefix_buckets") or []
                _pfx = int(_pb[0].get("prefix_tokens", 0)) if _pb else 0
                if _mml and _pt + _pfx + _ot > _mml - 64:
                    _budget = _mml - 64 - _pt - _ot
                    if _budget >= 64 and _pfx:
                        _pb[0]["prefix_tokens"] = _budget
                    else:
                        _d.pop("prefix_buckets", None)
                    data_arg = json.dumps(_d)

            rate = shaped.request_rate
            rtype = getattr(shaped, "request_type", "constant")
            if rtype == "concurrent":
                profile_arg = f"kind=concurrent,streams={rate},warmup={warmup}"
            elif rtype == "poisson":
                profile_arg = f"kind=poisson,rate={rate},warmup={warmup}"
            else:
                profile_arg = f"kind=constant,rate={rate},warmup={warmup}"

            output_path = f"/mnt/storage/tmp/guidellm-{config.test_id}-w{i}.json"
            seed = 42 + i  # decorrelate prompt sampling across workloads
            exec_cmd = (
                f"mkdir -p /mnt/storage/tmp && "
                f"rm -f {output_path} && "
                f"export TMPDIR=/mnt/storage/tmp && "
                f"guidellm run"
                f' --backend "{backend_arg}"'
                f' --tokenizer "kind=huggingface_auto,model={config.model_name}"'
                f" --data '{data_arg}'"
                f' --profile "{profile_arg}"'
                f' --constraint "{constraint_arg}"'
                f' --seed "kind=static,value={seed}"'
                f' --output "kind=json,path={output_path}"'
                f' --metrics "kind=generative,sample_size=50"'
            )
            jobs.append(
                {
                    "name": name,
                    "index": i,
                    "cmd": exec_cmd,
                    "output_path": output_path,
                    "script": f"/mnt/storage/tmp/guidellm-run-{config.test_id}-w{i}.sh",
                    "log": f"/mnt/storage/tmp/guidellm-{config.test_id}-w{i}.log",
                    "exit": f"/mnt/storage/tmp/guidellm-{config.test_id}-w{i}.exit",
                    "shape": (shaped.isl, shaped.osl, shaped.request_rate),
                }
            )

        if log_callback:
            log_callback(f"🧵 Parallel workloads: {len(jobs)} shapes on {endpoint}")
            for j in jobs:
                log_callback(
                    f"   {j['name']}: ISL={j['shape'][0]} OSL={j['shape'][1]} "
                    f"concurrency={j['shape'][2]} → {j['output_path']}"
                )

        def _remote_exec(cmd, timeout=30):
            return kubectl.run(
                ["exec", self._guidellm_pod_name, "-n", self.namespace, "--", "bash", "-c", cmd],
                check=False,
                timeout=timeout,
            )

        kill_cmd = "pkill -f 'guidellm[ ]run' 2>/dev/null; sleep 1; pkill -9 -f 'guidellm[ ]run' 2>/dev/null; true"
        benchmark_start = time.time()
        monitor_timeout = (3600 if max_requests else config.test_duration) + warmup + 1800

        try:
            _remote_exec(kill_cmd)
            for j in jobs:
                script = j["cmd"] + f"\necho $? > {j['exit']}"
                write_r = kubectl.run(
                    [
                        "exec",
                        "-i",
                        self._guidellm_pod_name,
                        "-n",
                        self.namespace,
                        "--",
                        "bash",
                        "-c",
                        f"cat > {j['script']}",
                    ],
                    input_data=script,
                    check=False,
                    timeout=60,
                )
                if write_r.returncode != 0:
                    if log_callback:
                        log_callback(f"❌ Failed to stage script for {j['name']}")
                    return False, None, None
                _remote_exec(f"rm -f {j['exit']} {j['log']}")
            # '&' terminates each backgrounded command — a ';' after '&'
            # is a bash syntax error, so the ampersand is the separator
            launch_cmd = " & ".join(f"nohup bash {j['script']} > {j['log']} 2>&1" for j in jobs)
            launch_r = _remote_exec(launch_cmd + " & echo launched")
            if launch_r.returncode != 0:
                if log_callback:
                    log_callback(f"❌ Failed to launch parallel guidellm jobs: {launch_r.stderr[:150]}")
                return False, None, None
            if log_callback:
                log_callback(f"🚀 {len(jobs)} guidellm workloads launched detached")

            # Poll all exit files with one short-lived exec
            check_cmd = " ; ".join(f"if [ -f {j['exit']} ]; then cat {j['exit']}; else echo RUNNING; fi" for j in jobs)
            exits = {j["index"]: None for j in jobs}
            poll_failures = 0
            last_pod_check = benchmark_start
            while time.time() - benchmark_start < monitor_timeout:
                if stop_check and stop_check():
                    _remote_exec(kill_cmd, timeout=15)
                    return False, None, None
                status_r = _remote_exec(check_cmd, timeout=20)
                if status_r.returncode != 0:
                    poll_failures += 1
                    if poll_failures >= 12:
                        if log_callback:
                            log_callback("❌ Lost contact with workload pod while polling")
                        _remote_exec(kill_cmd, timeout=15)
                        return False, None, None
                else:
                    poll_failures = 0
                    lines = [l.strip() for l in status_r.stdout.splitlines() if l.strip()]
                    if len(lines) == len(jobs):
                        for j, line in zip(jobs, lines):
                            if exits[j["index"]] is None and line != "RUNNING":
                                exits[j["index"]] = int(line) if line.lstrip("-").isdigit() else -1
                                if log_callback:
                                    tag = (
                                        "completed" if exits[j["index"]] == 0 else f"FAILED (exit {exits[j['index']]})"
                                    )
                                    elapsed = int(time.time() - benchmark_start)
                                    log_callback(f"   {j['name']}: {tag} ({elapsed}s)")
                        if all(v is not None for v in exits.values()):
                            break
                if monitor_pods and time.time() - last_pod_check >= 30:
                    pod_error = self._monitor_pods_during_benchmark(
                        self.namespace,
                        config.test_id,
                        expected_pod_count,
                        benchmark_start,
                        config.test_duration,
                        30,
                        log_callback,
                    )
                    if pod_error:
                        _remote_exec(kill_cmd, timeout=15)
                        return False, None, None
                    last_pod_check = time.time()
                time.sleep(5)
            else:
                if log_callback:
                    log_callback(f"   ⚠️  Parallel workloads timed out after {int(monitor_timeout)}s")
                _remote_exec(kill_cmd, timeout=15)
                return False, None, None

            if any(v != 0 for v in exits.values()):
                if log_callback:
                    failed = [jobs[i]["name"] for i, v in exits.items() if v != 0]
                    log_callback(f"❌ Workload(s) failed: {', '.join(failed)}")
                    for j in jobs:
                        if exits[j["index"]] != 0:
                            tail_r = _remote_exec(f"tail -n 8 {j['log']} 2>/dev/null", timeout=20)
                            if tail_r.returncode == 0 and tail_r.stdout.strip():
                                for line in tail_r.stdout.strip().splitlines():
                                    log_callback(f"   {line.strip()[:200]}")
                return False, None, None

            # Extract each output and merge into one benchmarks[] file
            import subprocess as _sp

            extract_env = os.environ.copy()
            extract_env["KUBECONFIG"] = os.path.expanduser(kubectl.kubeconfig)
            benchmarks = []
            for j in jobs:
                extract_r = _sp.run(
                    [
                        kubectl.kubectl_cmd,
                        "exec",
                        self._guidellm_pod_name,
                        "-n",
                        self.namespace,
                        "--",
                        "python3",
                        "/usr/local/bin/parse_guidellm",
                        j["output_path"],
                    ],
                    capture_output=True,
                    text=True,
                    timeout=120,
                    env=extract_env,
                )
                if extract_r.returncode != 0 or not extract_r.stdout.strip():
                    if log_callback:
                        log_callback(f"❌ Failed to extract results for {j['name']}: {extract_r.stderr[:100]}")
                    return False, None, None
                extracted = _json.loads(extract_r.stdout)
                if "error" in extracted:
                    if log_callback:
                        log_callback(f"❌ Parse error ({j['name']}): {extracted['error']}")
                    return False, None, None
                # token totals for window-exact tok/s (parse_guidellm drops them)
                _totals = self._extract_guidellm_totals(j["output_path"], kubectl) or {}
                # total-bucket TTFT: incomplete requests still have valid
                # TTFTs and the reconstituted 'successful' dists censor them
                _ttft_total = _totals.get("ttft_total") or {}
                _ttft_total_dist = (
                    {
                        "mean": _ttft_total.get("mean"),
                        "count": _ttft_total.get("count"),
                        "percentiles": {
                            k: _ttft_total.get(k)
                            for k in ("p50", "p90", "p95", "p999")
                        },
                    }
                    if _ttft_total.get("count")
                    else None
                )

                def _reconstitute_dist(flat, _extracted=extracted):
                    pcts = {}
                    top = {}
                    for k, v in (flat or {}).items():
                        if k.startswith("p") and k[1:].isdigit():
                            pcts[k] = v
                        else:
                            top[k] = v
                    top["percentiles"] = pcts
                    return top

                benchmarks.append(
                    {
                        "workload_name": j["name"],
                        "duration": extracted.get("benchmark_duration_s"),
                        "warmup_duration": extracted.get("warmup_duration_s"),
                        "start_time": extracted.get("start_time"),
                        "end_time": extracted.get("end_time"),
                        "metrics": {
                            "time_to_first_token_ms": {
                                "successful": _reconstitute_dist(extracted.get("ttft_ms")),
                                **({"total": _ttft_total_dist} if _ttft_total_dist else {}),
                            },
                            "inter_token_latency_ms": {"successful": _reconstitute_dist(extracted.get("itl_ms"))},
                            "requests_per_second": {"successful": _reconstitute_dist(extracted.get("throughput_rps"))},
                            "time_per_output_token_ms": {"successful": _reconstitute_dist(extracted.get("tpot_ms"))},
                            "request_latency": {"successful": _reconstitute_dist(extracted.get("e2e_latency_s"))},
                            "output_tokens_per_second": {"successful": _reconstitute_dist(extracted.get("output_tps"))},
                            "prompt_token_count": {"successful": _reconstitute_dist(extracted.get("prompt_tokens"))},
                            "output_token_count": {"successful": _reconstitute_dist(extracted.get("output_tokens"))},
                            "request_concurrency": {"successful": _reconstitute_dist(extracted.get("concurrency"))},
                            "request_totals": extracted.get("request_totals", {}),
                            "total_token_count": {"total": {"total_sum": _totals.get("tokens")}},
                        },
                    }
                )

            local_results_dir = Path("/tmp/guidellm-extracted")
            local_results_dir.mkdir(parents=True, exist_ok=True)
            result_file = str(local_results_dir / f"{config.test_id}.json")
            with open(result_file, "w") as rf:
                _json.dump({"benchmarks": benchmarks}, rf)
            if log_callback:
                log_callback(f"✅ {len(benchmarks)} parallel workloads completed and merged")

            metrics_path = None
            if collect_metrics:
                try:
                    if self.metrics_collector:
                        results_dir = Path(f"/mnt/storage/results/{config.test_id}")
                        results_dir.mkdir(parents=True, exist_ok=True)
                        metrics_file = str(results_dir / "metrics.json")
                        self.metrics_collector.config.pod_name_pattern = config.test_id
                        self.metrics_collector.config.architecture = config.architecture
                        self.metrics_collector.collect_all_metrics(
                            start_time=datetime.fromtimestamp(benchmark_start),
                            end_time=datetime.now(),
                            output_file=metrics_file,
                        )
                        metrics_path = metrics_file
                except Exception as e:
                    if log_callback:
                        log_callback(f"   ⚠️  Metrics collection failed: {str(e)[:100]}")

            return True, result_file, metrics_path

        except Exception as e:
            if log_callback:
                log_callback(f"❌ Parallel guidellm exec failed: {str(e)[:200]}")
            return False, None, None

    def _run_guidellm_test(
        self,
        endpoint: str,
        config: TestConfig,
        log_callback: Optional[Callable[[str], None]] = None,
        monitor_pods: bool = False,
        expected_pod_count: int = 0,
        collect_metrics: bool = True,
    ) -> tuple[bool, Optional[str], Optional[str]]:
        """
        Run guidellm load test with optional pod crash monitoring and metrics collection.

        Args:
            endpoint: Service endpoint URL (can be None to auto-discover Istio gateway)
            config: Test configuration
            log_callback: Optional callback for logging
            monitor_pods: Whether to monitor pods for crashes during benchmark
            expected_pod_count: Expected number of pods (for crash detection)
            collect_metrics: Whether to collect Prometheus/Thanos metrics (default: True)

        Environment Variables:
            HOME_STORAGE_DIR: Storage mount point (set by deploy.sh, default: /mnt/storage)
            HF_HOME: HuggingFace cache directory (optional)
                     If not set, uses ${HOME_STORAGE_DIR}/.cache/huggingface
                     Falls back to /tmp/huggingface_cache if mount unavailable

        Returns:
            Tuple of (success, guidellm_output_file_path, metrics_file_path)
        """
        try:
            if log_callback:
                stop_mode = getattr(config, "stop_mode", "duration")
                max_reqs = getattr(config, "max_requests", None)
                if stop_mode == "max_requests" and max_reqs:
                    log_callback(f"🏃 Running guidellm benchmark for {max_reqs} requests...")
                else:
                    log_callback(f"🏃 Running guidellm benchmark for {config.test_duration}s...")

            # LoRA: guidellm natively round-robins across a comma-separated
            # model list (assigned at request-generation time in the scheduler,
            # immune to multiprocessing). Build the list: base + all adapters.
            # lora_benchmark_mode controls whether guidellm targets adapters:
            #   round_robin — cycle across base + all adapters (stress test)
            #   session_pinned — each conversation keeps one adapter
            #   base_only/off — all traffic hits base (LoRA overhead baseline)
            _lora_n = int(getattr(config, "lora_num_adapters", 0) or 0)
            _lora_mode = getattr(config, "lora_benchmark_mode", "round_robin") or "round_robin"
            if getattr(config, "lora_enabled", False) and _lora_n > 0:
                if _lora_mode == "round_robin":
                    if log_callback:
                        log_callback(
                            f"🔀 LoRA round-robin: {_lora_n} adapters + base via native guidellm multi-model", "info"
                        )
                elif _lora_mode == "session_pinned":
                    if log_callback:
                        log_callback(
                            f"🎯 LoRA session-pinned: each conversation keeps "
                            f"one of {_lora_n} adapters (conversation cacheable, "
                            f"session-affinity routable)",
                            "info",
                        )
                else:
                    if log_callback:
                        log_callback(
                            f"🔌 LoRA server-side only ({_lora_n} adapters mounted, "
                            f"benchmark mode: {_lora_mode}) — traffic hits base model",
                            "info",
                        )

            # Auto-discover Istio gateway if endpoint not provided
            if endpoint is None:
                endpoint = self._discover_istio_gateway(
                    self.namespace, config.test_id, config.architecture, log_callback
                )

            request_profile = getattr(config, "request_type", "constant")
            request_rate = getattr(config, "request_rate", 1)

            if log_callback:
                log_callback(f"   Target: {endpoint}")
                rate_label = (
                    f"{request_rate} concurrent users"
                    if request_profile == "concurrent"
                    else f"{request_rate} req/s ({request_profile})"
                )
                if request_profile == "sweep":
                    rate_label = "sweep (auto-discover rates)"
                log_callback(f"   Load: {rate_label}")

            output_dir = Path(f"/tmp/guidellm-{config.test_id}")
            output_dir.mkdir(parents=True, exist_ok=True)
            output_file = output_dir / f"{config.test_id}.json"

            workload_mode = getattr(config, "workload_mode", "synthetic") or "synthetic"
            turns = getattr(config, "turns", 1) or 1
            column_mapper = None

            if workload_mode == "dataset" and getattr(config, "dataset_source", None):
                dataset_path = config.dataset_source
                col = getattr(config, "dataset_column", None) or "prompt"
                column_mapper = f'{{"kind":"generative_column_mapper","column_mappings":{{"text_column":"{col}"}}}}'
                if log_callback:
                    log_callback(f"   Using dataset: {dataset_path}")
                if dataset_path.endswith(".jsonl") or dataset_path.endswith(".json"):
                    data_arg = f'{{"kind":"json_file","path":"{dataset_path}","load_kwargs":{{"split":"train"}}}}'
                else:
                    data_arg = f"kind=csv_file,path={dataset_path}"
            else:
                data_arg = f"kind=synthetic_text,prompt_tokens={config.isl},output_tokens={config.osl}"
                if getattr(config, "isl_stdev", None):
                    data_arg += f",prompt_tokens_stdev={config.isl_stdev}"
                if getattr(config, "osl_stdev", None):
                    data_arg += f",output_tokens_stdev={config.osl_stdev}"
                if getattr(config, "isl_min", None):
                    data_arg += f",prompt_tokens_min={config.isl_min}"
                if getattr(config, "osl_min", None):
                    data_arg += f",output_tokens_min={config.osl_min}"
                if getattr(config, "osl_max", None):
                    data_arg += f",output_tokens_max={config.osl_max}"
                if getattr(config, "first_prompt_tokens", None):
                    data_arg += f",first_prompt_tokens={config.first_prompt_tokens}"
                if getattr(config, "first_prompt_tokens_stdev", None):
                    data_arg += f",first_prompt_tokens_stdev={config.first_prompt_tokens_stdev}"
                if getattr(config, "first_prompt_tokens_min", None):
                    data_arg += f",first_prompt_tokens_min={config.first_prompt_tokens_min}"
                if getattr(config, "first_prompt_tokens_max", None):
                    fpt_max = int(config.first_prompt_tokens_max)
                    max_model_len = getattr(config, "max_model_len", 0)
                    if max_model_len:
                        fpt_ceiling = max_model_len - config.osl - 200
                        if fpt_ceiling > 0 and fpt_max > fpt_ceiling:
                            fpt_max = fpt_ceiling
                    data_arg += f",first_prompt_tokens_max={fpt_max}"
                if getattr(config, "first_output_tokens", None):
                    data_arg += f",first_output_tokens={config.first_output_tokens}"
                if getattr(config, "first_output_tokens_stdev", None):
                    data_arg += f",first_output_tokens_stdev={config.first_output_tokens_stdev}"
                if getattr(config, "prefix_tokens", None):
                    data_arg += f",prefix_tokens={config.prefix_tokens}"
                if getattr(config, "prefix_count", None):
                    data_arg += f",prefix_count={config.prefix_count}"
                if turns > 1:
                    data_arg += f",turns={turns}"
                if getattr(config, "turn_delay", None):
                    data_arg += f",delay={config.turn_delay}"
                if getattr(config, "turn_delay_stdev", None):
                    data_arg += f",delay_stdev={config.turn_delay_stdev}"
                if getattr(config, "turn_delay_min", None):
                    data_arg += f",delay_min={config.turn_delay_min}"
                if getattr(config, "turn_delay_max", None):
                    data_arg += f",delay_max={config.turn_delay_max}"
                # prompt_tokens_max: explicit isl_max takes precedence, else safety cap from max_model_len
                if getattr(config, "isl_max", None):
                    data_arg += f",prompt_tokens_max={config.isl_max}"
                else:
                    max_model_len = getattr(config, "max_model_len", 0)
                    if max_model_len and (getattr(config, "isl_stdev", None) or getattr(config, "osl_stdev", None)):
                        prompt_max = max_model_len - config.osl - 200
                        if prompt_max > 0:
                            data_arg += f",prompt_tokens_max={prompt_max}"

            request_format = "/v1/chat/completions" if turns > 1 else "/v1/completions"
            warmup = min(60, max(0, config.test_duration - 30)) if hasattr(config, "test_duration") else 60

            # Profile
            if request_profile == "concurrent":
                profile_arg = f"kind=concurrent,streams={request_rate},warmup={warmup}"
            elif request_profile == "sweep":
                profile_arg = f"kind=sweep,warmup={warmup}"
            elif request_profile == "throughput":
                profile_arg = f"kind=throughput,warmup={warmup}"
            elif request_profile == "synchronous":
                profile_arg = f"kind=synchronous,warmup={warmup}"
            elif request_profile == "poisson":
                profile_arg = f"kind=poisson,rate={request_rate},warmup={warmup}"
            else:
                profile_arg = f"kind=constant,rate={request_rate},warmup={warmup}"

            # Constraint — use max_requests when set, fall back to duration
            max_requests = getattr(config, "max_requests", None)
            if max_requests:
                constraint_arg = f"kind=max_requests,count={max_requests}"
            else:
                constraint_arg = f"kind=max_duration,seconds={config.test_duration}"

            # Build guidellm 0.7.x command
            # http2=false is critical for PD deployments:
            # Istio ext_proc (EPP) cannot unmarshal HTTP/2 streamed request bodies
            # LoRA: comma-separated model list via JSON (breaks key=value parsing)
            _lora_n = int(getattr(config, "lora_num_adapters", 0) or 0)
            _lora_mode = getattr(config, "lora_benchmark_mode", "round_robin") or "round_robin"
            if (
                getattr(config, "lora_enabled", False)
                and _lora_n > 0
                and _lora_mode in ("round_robin", "session_pinned")
            ):
                _adapters = [f"agent-{i:04d}" for i in range(1, _lora_n + 1)]
                import json as _json

                _backend = {
                    "kind": "openai_http",
                    "target": endpoint,
                    "model": config.model_name,
                    "lora_adapters": _adapters,
                    "request_format": request_format,
                    "http2": False,
                    "timeout": 300,
                }
                if _lora_mode == "session_pinned":
                    _backend["model_assignment"] = "conversation"
                _backend_arg = _json.dumps(_backend)
            else:
                _backend_arg = f"kind=openai_http,target={endpoint},model={config.model_name},request_format={request_format},http2=false,timeout=300"
            cmd = [
                "guidellm",
                "run",
                "--backend",
                _backend_arg,
                "--tokenizer",
                f"kind=huggingface_auto,model={config.model_name}",
                "--data",
                data_arg,
                "--profile",
                profile_arg,
                "--constraint",
                constraint_arg,
                "--output",
                f"kind=json,path={output_file}",
                "--metrics",
                "kind=generative,sample_size=20",
            ]

            if column_mapper:
                cmd.extend(["--data-column-mapper", column_mapper])

            # Log and store the full guidellm command
            cmd_str = " \\\n  ".join(cmd)
            logger.info(f"guidellm command:\n  {cmd_str}")
            if log_callback:
                log_callback(f"🔧 guidellm command:\n  {cmd_str}")
            # Store on config object for DB persistence
            if hasattr(config, "__dict__"):
                config._guidellm_command = cmd_str

            # Start guidellm in background
            logger.debug("Starting guidellm...")

            # Set environment variables for guidellm
            env = os.environ.copy()

            # Determine HuggingFace cache directory dynamically
            # Priority: 1) HF_HOME already set, 2) HOME_STORAGE_DIR/.cache/huggingface, 3) /tmp fallback
            hf_home = env.get("HF_HOME")
            if not hf_home:
                # Use HOME_STORAGE_DIR from deploy.sh configuration
                storage_dir = env.get("HOME_STORAGE_DIR", "/mnt/storage")
                hf_home = f"{storage_dir}/.cache/huggingface"

                # Create cache directory if parent exists
                if os.path.exists(storage_dir):
                    os.makedirs(hf_home, exist_ok=True)
                else:
                    # Fallback to /tmp if storage mount doesn't exist
                    hf_home = "/tmp/huggingface_cache"
                    os.makedirs(hf_home, exist_ok=True)
                    if log_callback:
                        log_callback(f"   ⚠️  HOME_STORAGE_DIR not found, using temporary cache: {hf_home}")

            env["HF_HOME"] = hf_home
            env["HF_DATASETS_CACHE"] = f"{hf_home}/datasets"
            env["TRANSFORMERS_CACHE"] = f"{hf_home}/transformers"

            logger.debug(f"HF cache directory: {hf_home}")

            benchmark_start = time.time()

            process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, env=env
            )

            # Monitor pods and guidellm output during benchmark
            check_interval = 30  # Check every 30 seconds
            last_check = benchmark_start
            benchmark_success = True
            error_message = None

            # For max_requests mode, use a generous timeout (1h) since we don't know how long it'll take
            _stop_mode = getattr(config, "stop_mode", "duration")
            _max_reqs = getattr(config, "max_requests", None)
            monitor_timeout = 3600 if (_stop_mode == "max_requests" and _max_reqs) else config.test_duration

            while time.time() - benchmark_start < monitor_timeout:
                # Check if guidellm process ended
                if process.poll() is not None:
                    returncode = process.returncode
                    if returncode == 0:
                        if log_callback:
                            elapsed = int(time.time() - benchmark_start)
                            log_callback(f"ℹ️  Guidellm completed ({elapsed}s)")
                        break
                    else:
                        remaining_output = process.stdout.read() if process.stdout else ""
                        if log_callback:
                            log_callback(f"❌ Guidellm process exited early (code: {returncode})")
                            if remaining_output:
                                for line in remaining_output.strip().splitlines()[-20:]:
                                    log_callback(f"   Guidellm: {line.strip()[:200]}")
                        benchmark_success = False
                        error_message = f"Guidellm crashed with exit code {returncode}"
                        if remaining_output:
                            error_message += f"\n{remaining_output[-500:]}"
                        break

                # Read guidellm output (non-blocking)
                try:
                    import select

                    if select.select([process.stdout], [], [], 0)[0]:
                        line = process.stdout.readline()
                        if line:
                            # Log interesting guidellm output
                            if "error" in line.lower() or "warning" in line.lower():
                                if log_callback:
                                    log_callback(f"   Guidellm: {line.strip()[:100]}")
                except:
                    pass

                # Sleep until next check
                remaining = monitor_timeout - (time.time() - benchmark_start)
                time.sleep(min(5, check_interval, max(0.1, remaining)))

                # Check pod health
                current_time = time.time()
                if monitor_pods and current_time - last_check >= check_interval:
                    pod_error = self._monitor_pods_during_benchmark(
                        self.namespace,
                        config.test_id,
                        expected_pod_count,
                        benchmark_start,
                        config.test_duration,
                        check_interval,
                        log_callback,
                    )

                    if pod_error:
                        benchmark_success = False
                        error_message = pod_error
                        break

                    last_check = current_time

            # Wait for guidellm to finish — no timeout, let it complete naturally
            if process.poll() is None:
                if log_callback:
                    log_callback("   Waiting for guidellm to finish...")
                process.wait()

            elapsed_total = int(time.time() - benchmark_start)

            # Check final status
            result_file = output_file
            output_exists = result_file.exists() and result_file.stat().st_size > 0

            if benchmark_success and (process.returncode == 0 or output_exists):
                if log_callback:
                    log_callback(f"✅ Benchmark completed successfully ({elapsed_total}s)")

                # Extract actual benchmark time window from guidellm output
                # guidellm records precise start_time/end_time (epoch) of the
                # active benchmark, excluding data generation and result writing
                metrics_file = None
                if collect_metrics and self.metrics_collector and output_exists:
                    try:
                        import json as _json

                        with open(result_file) as f:
                            guidellm_data = _json.load(f)
                        bench = guidellm_data.get("benchmarks", [{}])[0]
                        gl_start = bench.get("start_time")
                        gl_end = bench.get("end_time")
                        if gl_start and gl_end:
                            metrics_start = datetime.fromtimestamp(gl_start)
                            metrics_end = datetime.fromtimestamp(gl_end)
                            if log_callback:
                                log_callback(
                                    f"   Using guidellm benchmark window for metrics: {metrics_start.strftime('%H:%M:%S')} - {metrics_end.strftime('%H:%M:%S')} ({gl_end - gl_start:.0f}s)"
                                )
                            metrics_file = self._collect_metrics(
                                config=config,
                                start_time=metrics_start.isoformat(),
                                end_time=metrics_end.isoformat(),
                                log_callback=log_callback,
                            )
                        else:
                            if log_callback:
                                log_callback(
                                    "⚠️  No benchmark timestamps in guidellm output, skipping metrics collection"
                                )
                    except Exception as e:
                        logger.warning(f"Failed to parse guidellm timestamps: {e}")
                        if log_callback:
                            log_callback(f"⚠️  Failed to parse guidellm timestamps: {e}")
                elif collect_metrics and not self.metrics_collector:
                    if log_callback:
                        log_callback("⚠️  Metrics collection requested but Thanos URL not configured")

                return True, str(result_file), metrics_file
            else:
                if log_callback:
                    log_callback(f"❌ Benchmark failed: {error_message or 'unknown error'}")
                return False, None, None

        except Exception as e:
            error_msg = f"Failed to run guidellm test: {e}"
            logger.error(error_msg)
            if log_callback:
                log_callback(f"❌ {error_msg}")
            return False, None, None
