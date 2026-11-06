"""Per-pod pprof + torch profile collection for ServeIt tests.

Collects Go pprof profiles from the EPP (always available, port 9090)
and — when the run opts in — from vLLM pods (requires a vLLM image that
serves /debug/pprof on the API port: 8000 aggregated/prefill, 8200
decode; probed per pod) plus torch.profiler traces (CPU + CUDA).

CPU profiles are captured DURING the benchmark load: the blocking
?seconds=N fetch runs as a DETACHED SUBPROCESS started right before
guidellm launches. Torch profiling uses a similar background exec that
runs torch.profiler inside the vLLM container for a fixed window.

Heap and goroutine snapshots are fetched once the benchmark finishes.

All raw files land in /mnt/storage/test-artifacts/{test_id}/profiles/
(MLflow export picks them up automatically). A parsed summary is
attached to the TestResult for the report.
"""

import base64
import json
import logging
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable, Dict, Optional

from core.pprof_parser import summarize_cpu_profile

logger = logging.getLogger(__name__)

# Cap the load-window CPU capture
MAX_CPU_WINDOW_S = 120
# Total embedded raw-profile budget per test (base64 in the DB)
MAX_EMBED_BYTES = 2 * 1024 * 1024

# Inline torch profiling script — runs inside vLLM pods.
# __DURATION__ is replaced with the actual profiling window in seconds.
# Inline torch profiler control script — runs inside vLLM pods.
# Uses vLLM's /start_profile and /stop_profile API endpoints, which are
# only mounted when --profiler-config is passed to vLLM serve.
# __DURATION__ is replaced with the actual profiling window in seconds.
# __ACTION__ is either 'start' or 'stop'.
# Inline torch profiler control script — runs inside vLLM pods.
# Uses vLLM's /start_profile and /stop_profile API endpoints.
# __ACTION__ is 'start' or 'stop'.
PYSPY_PROFILE_SCRIPT = """\
import os, json, subprocess, sys, time, urllib.request

def find_our_port():
    with open('/proc/self/cgroup') as f:
        our_cgroup = f.read().strip()
    for pid in os.listdir('/proc'):
        if not pid.isdigit():
            continue
        try:
            with open(f'/proc/{pid}/cgroup') as f:
                cg = f.read().strip()
            if cg == our_cgroup:
                with open(f'/proc/{pid}/cmdline') as f:
                    cmd = f.read()
                if 'vllm_patched' in cmd and pid != str(os.getpid()):
                    break
        except (OSError, IOError):
            pass
    for port in [8000, 8200]:
        try:
            req = urllib.request.Request(f'http://localhost:{port}/health')
            resp = urllib.request.urlopen(req, timeout=3)
            if resp.status == 200:
                return port
        except Exception:
            pass
    return 8000

port = find_our_port()
action = '__ACTION__'
output_dir = '/tmp/torch_traces'
os.makedirs(output_dir, exist_ok=True)

if action == 'start':
    try:
        req = urllib.request.Request(
            f'http://localhost:{port}/start_profile',
            method='POST', data=b'{}',
            headers={'Content-Type': 'application/json'})
        resp = urllib.request.urlopen(req, timeout=10)
        print(json.dumps({'status': 'started', 'port': port}))
    except Exception as e:
        print(json.dumps({'status': 'error', 'error': str(e), 'port': port}))

elif action == 'stop':
    try:
        req = urllib.request.Request(
            f'http://localhost:{port}/stop_profile',
            method='POST', data=b'{}',
            headers={'Content-Type': 'application/json'})
        resp = urllib.request.urlopen(req, timeout=60)
        # Brief wait for trace files to be written
        time.sleep(10)

        # List trace files (NO parsing — just sizes)
        trace_files = []
        if os.path.exists(output_dir):
            for f in sorted(os.listdir(output_dir)):
                if f.endswith('.json') or f.endswith('.gz') or f.endswith('.trace'):
                    fp = os.path.join(output_dir, f)
                    try:
                        trace_files.append({
                            'name': f,
                            'size': os.path.getsize(fp),
                            'path': fp
                        })
                    except OSError:
                        pass

        summary = {
            'profiler': 'torch',
            'status': 'stopped',
            'port': port,
            'trace_files': trace_files,
            'operators': [],
            'total_cpu_ms': 0,
            'total_cuda_ms': 0,
            'operator_count': 0,
        }

        # Save summary (without parsing)
        with open(os.path.join(output_dir, 'summary.json'), 'w') as f:
            json.dump(summary, f)
        print('SUMMARY_JSON:' + json.dumps(summary))

    except Exception as e:
        print(json.dumps({'status': 'error', 'error': str(e), 'port': port}))
"""




def _parse_ls_lh(output: str) -> list:
    """Parse `ls -lh` output into [{name, size, path}] entries.

    Only counts regular files with 9+ whitespace-separated columns
    (perms, links, owner, group, size, month, day, time, name).
    """
    files = []
    for line in output.split("\n"):
        parts = line.split()
        if len(parts) < 9 or parts[-1] == "total" or parts[0].startswith("d"):
            continue
        fname = parts[-1]
        # Skip the profiler's own text output — not a Chrome trace
        if fname.startswith("profiler_out"):
            continue
        size_str = parts[4]
        size = 0
        try:
            if size_str.endswith("G"):
                size = int(float(size_str[:-1]) * 1024 * 1024 * 1024)
            elif size_str.endswith("M"):
                size = int(float(size_str[:-1]) * 1024 * 1024)
            elif size_str.endswith("K"):
                size = int(float(size_str[:-1]) * 1024)
            else:
                size = int(size_str)
        except ValueError:
            pass
        files.append({"name": fname, "size": size,
                      "path": f"/tmp/torch_traces/{fname}"})
    return files


def _torch_parser_b64() -> str:
    """Base64 of core/torch_trace_summary.py for pushing into vLLM pods.

    The parser streams the Chrome trace (16MB peak RSS for a 1.3GB
    decompressed trace) and emits a compact SUMMARY_JSON — raw traces
    are never copied off the pod.
    """
    import base64 as _b64
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "torch_trace_summary.py")) as f:
        return _b64.b64encode(f.read().encode()).decode()


class ProfileCollector:
    """Collects pprof profiles from EPP and vLLM pods for one test."""

    # Class-level cache: {pod_name: port_or_None} — avoids re-probing
    _pprof_port_cache = {}
    _pprof_warned = False

    def __init__(self, kubectl, namespace: str, workload_pod: Optional[str] = None):
        self.kubectl = kubectl
        self.namespace = namespace
        self.workload_pod = workload_pod

    # ── subprocess plumbing ─────────────────────────────────────────────────

    def _kubectl_env(self):
        env = os.environ.copy()
        env["KUBECONFIG"] = os.path.expanduser(self.kubectl.kubeconfig)
        return env

    def _run_kubectl(self, args, timeout=30):
        return subprocess.run(
            ["kubectl"] + args,
            capture_output=True, text=True, timeout=timeout, check=False,
            env=self._kubectl_env(),
        )

    def _spawn_capture(self, cmd: list, timeout_s: int) -> Dict:
        fd, outfile = tempfile.mkstemp(prefix="serveit-pprof-", suffix=".bin")
        os.close(fd)
        fd, errfile = tempfile.mkstemp(prefix="serveit-pprof-err-", suffix=".txt")
        os.close(fd)
        out = open(outfile, "wb")
        errf = open(errfile, "wb")
        proc = subprocess.Popen(cmd, stdout=out, stderr=errf, env=self._kubectl_env())
        out.close()
        errf.close()
        return {"proc": proc, "outfile": outfile, "errfile": errfile,
                "timeout_s": timeout_s, "started": time.time()}

    def _reap_capture(self, cap: Dict, label: str = "") -> Optional[bytes]:
        try:
            elapsed = time.time() - cap["started"]
            remaining = cap["timeout_s"] + 60 - elapsed
            proc = cap["proc"]
            try:
                proc.wait(timeout=max(5, remaining))
            except subprocess.TimeoutExpired:
                proc.kill()
                logger.warning(f"profile capture {label}: timed out")
                return None
            size = 0
            try:
                size = Path(cap["outfile"]).stat().st_size
            except OSError:
                pass
            if proc.returncode != 0 or size == 0:
                return None
            data = Path(cap["outfile"]).read_bytes()
            if data and data[:2] != b'\x1f\x8b':
                logger.warning(f"profile capture {label}: non-pprof response: {data[:120]!r}")
                return None
            return data if data else None
        except Exception as e:
            logger.warning(f"profile capture reap failed ({label}): {e}")
            return None
        finally:
            for f in ("outfile", "errfile"):
                try:
                    os.unlink(cap[f])
                except (OSError, KeyError):
                    pass

    # ── pod discovery ───────────────────────────────────────────────────────

    def discover_epp_pod(self, architecture: str) -> Optional[Dict[str, str]]:
        arch = (architecture or "aggregated").lower()
        r = self._run_kubectl([
            "get", "pods", "-n", self.namespace,
            "-l", f"inferencepool=gaie-{arch}-epp",
            "-o",
            "jsonpath={range .items[*]}{.metadata.name}{\" \"}{.status.podIP}{\" \"}{.spec.nodeName}{\"\\n\"}{end}",
        ])
        if r.returncode != 0 or not r.stdout.strip():
            return None
        line = r.stdout.strip().splitlines()[0].split()
        if len(line) < 2 or not line[1]:
            return None
        return {"name": line[0], "ip": line[1], "port": 9090,
                "node": line[2] if len(line) > 2 else None}

    def discover_vllm_pods(self, test_id: str):
        r = self._run_kubectl([
            "get", "pods", "-n", self.namespace,
            "-l", f"llm-d.ai/test-id={test_id}",
            "-o",
            'jsonpath={range .items[*]}{.metadata.name}{" "}{.spec.nodeName}{"\\n"}{end}',
        ])
        if r.returncode != 0:
            return []
        pods = []
        for line in r.stdout.strip().splitlines():
            parts = line.split()
            if not parts:
                continue
            pods.append({"name": parts[0], "node": parts[1] if len(parts) > 1 else None})
        return pods

    def _probe_vllm_port(self, pod: str) -> Optional[int]:
        # Check cache — skip probing pods that already failed on a previous test
        if pod in ProfileCollector._pprof_port_cache:
            return ProfileCollector._pprof_port_cache[pod]
        for port in (8000, 8200):
            r = self._run_kubectl([
                "exec", pod, "-n", self.namespace, "-c", "vllm", "--",
                "python3", "-c",
                f"import urllib.request,sys;\n"
                f"req=urllib.request.Request('http://localhost:{port}/debug/pprof/');\n"
                f"sys.exit(0 if urllib.request.urlopen(req,timeout=5).status==200 else 1)",
            ], timeout=20)
            if r.returncode == 0:
                ProfileCollector._pprof_port_cache[pod] = port
                return port
        ProfileCollector._pprof_port_cache[pod] = None
        return None

    # ── collection lifecycle ────────────────────────────────────────────────

    def start_cpu_captures(self, config, log_callback: Optional[Callable[[str], None]] = None) -> Dict:
        test_id = config.test_id
        selector = config.deploy_selector_id
        window = min(MAX_CPU_WINDOW_S, max(30, getattr(config, "test_duration", 300) or 300))
        enable_vllm = bool(getattr(config, "enable_pprof", False))
        self._resolve_workload_pod()
        handle = {
            "test_id": test_id,
            "window_s": window,
            "enable_vllm": enable_vllm,
            "started": time.time(),
            "epp_spawn": None,
            "vllm_spawns": [],
            "torch_procs": None,
        }

        # EPP (toggle: enable_epp_profile, default on)
        enable_epp = getattr(config, 'enable_epp_profile', True)
        epp = self.discover_epp_pod(config.architecture) if enable_epp else None
        if epp and self.workload_pod:
            url = f"http://{epp['ip']}:{epp['port']}/debug/pprof/profile?seconds={window}"
            cmd = ["kubectl", "exec", self.workload_pod, "-n", self.namespace, "--",
                   "curl", "-s", "--max-time", str(window + 30), url]
            handle["epp_spawn"] = dict(
                self._spawn_capture(cmd, window + 30),
                kind="epp", pod=epp["name"], node=epp.get("node"),
                port=epp["port"], ptype="cpu")
            if log_callback:
                log_callback(f"   \U0001f52c EPP CPU profile capture started ({window}s window, pod {epp['name']})")
        else:
            if log_callback:
                log_callback("   \u26a0\ufe0f  EPP pod/workload pod not found — skipping EPP profile capture")

        # vLLM pods (opt-in)
        if enable_vllm:
            pods = self.discover_vllm_pods(selector)
            probed = 0
            for pod in pods:
                pod_name = pod["name"] if isinstance(pod, dict) else pod
                pod_node = pod.get("node") if isinstance(pod, dict) else None
                port = self._probe_vllm_port(pod_name)
                if not port:
                    if log_callback:
                        log_callback(f"   \u26a0\ufe0f  No pprof endpoint on {pod_name} (vLLM image does not serve /debug/pprof — skipping)")
                    continue
                probed += 1
                code = (
                    "import urllib.request,sys\n"
                    f"sys.stdout.buffer.write(urllib.request.urlopen('http://localhost:{port}/debug/pprof/profile?seconds={window}', timeout={window + 30}).read())\n"
                )
                cmd = ["kubectl", "exec", pod_name, "-n", self.namespace, "-c", "vllm", "--",
                       "python3", "-c", code]
                handle["vllm_spawns"].append(dict(
                    self._spawn_capture(cmd, window + 60),
                    kind="vllm", pod=pod_name, node=pod_node, port=port, ptype="cpu"))
            if log_callback:
                log_callback(f"   \U0001f52c vLLM CPU profile capture started on {probed}/{len(pods)} pod(s) ({window}s window)")

            # Torch profiling (same window, separate background process)
            handle["torch_procs"] = self.start_torch_profiles(selector, window, log_callback)

        return handle

    # ── torch profiler ──────────────────────────────────────────────────────

    def start_torch_profiles(self, selector_id, window_s, log_callback=None):
        """Start torch profiler on all vLLM pods via /start_profile API.

        Uses curl from the workload pod (like Go pprof) — the endpoint
        responds slowly, so fire-and-forget with short timeout.
        """
        self._resolve_workload_pod()
        pods = self.discover_vllm_pods(selector_id)
        results = []
        for pod in pods:
            pod_name = pod["name"] if isinstance(pod, dict) else pod
            if not self.workload_pod:
                continue
            r = self._run_kubectl(["get", "pod", pod_name, "-n", self.namespace,
                                    "-o", "jsonpath={.status.podIP}"])
            if r.returncode != 0 or not r.stdout.strip():
                continue
            pod_ip = r.stdout.strip()
            started = False
            started_port = None
            for port in [8000, 8200]:
                url = f"http://{pod_ip}:{port}/start_profile"
                if self._post_via_workload_pod(url, 15):
                    started = True
                    started_port = port
                    break
            results.append({"pod": pod_name, "started": started, "ip": pod_ip,
                            "port": started_port or 8000})
        if log_callback and results:
            n_started = sum(1 for r in results if r.get("started"))
            log_callback(f"   \U0001f52c Torch profiler started on {n_started}/{len(results)} pod(s)")
        return results

    def collect_torch_profiles(self, results, log_callback=None, written=None, test_id=None):
        """Stop torch profilers via /stop_profile and collect traces."""
        if written is None:
            written = []
        torch_results = []
        for entry in results:
            pod_name = entry.get("pod")
            if not pod_name or not entry.get("started"):
                continue
            if not self.workload_pod:
                continue

            # Call /stop_profile on the port that worked during start.
            # With max_iterations set, the worker auto-stops mid-benchmark
            # (traces already flushed); this call stops the frontend
            # (async_llm) profiler and is a no-op for an inactive worker.
            stop_url = f"http://{entry.get('ip')}:{entry.get('port') or 8000}/stop_profile"
            if not self._post_via_workload_pod(stop_url, 120):
                logger.warning(f"stop_profile non-2xx for {pod_name}")

            # List trace files — retry while empty (flush is asynchronous
            # and can lag the stop_profile 200 response by tens of seconds)
            trace_files = []
            for attempt_i in range(6):
                time.sleep(10 if attempt_i else 15)
                ls_result = self._run_kubectl(
                    ["exec", pod_name, "-n", self.namespace, "-c", "vllm",
                     "--", "ls", "-lh", "/tmp/torch_traces/"], timeout=30)
                if ls_result.returncode == 0:
                    trace_files = _parse_ls_lh(ls_result.stdout)
                    if trace_files:
                        break
            try:
                summary = {
                    "profiler": "torch",
                    "pod": pod_name,
                    "status": "stopped",
                    "trace_files": trace_files,
                    "operators": [],
                    "total_cpu_ms": 0,
                    "total_cuda_ms": 0,
                    "operator_count": 0,
                }

                # Parse the trace IN-POD into a compact summary (~6KB) —
                # raw traces are 30-80MB each and are never copied off the
                # pod (kubectl binary copies proved unreliable anyway).
                summary.update({"parsed": False})
                try:
                    b64 = _torch_parser_b64()
                    parse_result = subprocess.run(
                        ["kubectl", "exec", pod_name, "-n", self.namespace, "-c", "vllm",
                         "--", "bash", "-c",
                         "echo " + b64 + " | base64 -d > /tmp/tts.py && "
                         "python3 /tmp/tts.py /tmp/torch_traces/"],
                        capture_output=True, text=True, timeout=300,
                        env=self._kubectl_env())
                    if parse_result.returncode == 0:
                        for line in parse_result.stdout.split("\n"):
                            if line.startswith("SUMMARY_JSON:"):
                                parsed = json.loads(line[len("SUMMARY_JSON:"):])
                                summary.update(parsed)
                                summary["parsed"] = True
                                break
                    if not summary.get("parsed"):
                        logger.warning(f"trace parse no-summary {pod_name}: "
                                       f"{parse_result.stderr[-200:] if parse_result.stderr else 'rc=' + str(parse_result.returncode)}")
                except Exception as e:
                    logger.warning(f"trace parse failed {pod_name}: {e}")

                # Free the pod's ephemeral trace storage
                try:
                    subprocess.run(
                        ["kubectl", "exec", pod_name, "-n", self.namespace, "-c", "vllm",
                         "--", "bash", "-c", "rm -f /tmp/torch_traces/*.gz"],
                        capture_output=True, timeout=30,
                        env=self._kubectl_env())
                except Exception:
                    pass

                torch_results.append(summary)
                if log_callback:
                    if summary.get("parsed"):
                        log_callback(f"   \U0001f52c {pod_name}: {summary.get('kernel_count', 0)} kernels, "
                                     f"{summary.get('total_cuda_ms', 0)}ms CUDA, {summary.get('total_cpu_ms', 0)}ms CPU")
                    else:
                        log_callback(f"   \U0001f52c {pod_name}: {len(trace_files)} trace file(s) (parse failed)")

            except Exception as e:
                logger.warning(f"torch profile collection failed for {pod_name}: {e}")

        if log_callback and torch_results:
            log_callback(f"   \U0001f52c Torch profiles collected from {len(torch_results)} pod(s)")
        return torch_results

    # ── resolve workload pod ────────────────────────────────────────────────

    def _resolve_workload_pod(self):
        if self.workload_pod and "-" in self.workload_pod.strip("serveit-workload-"):
            r = self._run_kubectl(["get", "pod", self.workload_pod, "-n", self.namespace, "-o", "name"])
            if r.returncode == 0:
                return
        r = self._run_kubectl([
            "get", "pods", "-l", "app=serveit-workload", "-n", self.namespace,
            "-o", "jsonpath={.items[0].metadata.name}",
        ])
        if r.returncode == 0 and r.stdout.strip():
            self.workload_pod = r.stdout.strip()

    # ── finish ──────────────────────────────────────────────────────────────

    def _fetch_via_workload_pod(self, url: str, timeout_s: int) -> Optional[bytes]:
        if not self.workload_pod:
            return None
        r = subprocess.run(
            ["kubectl", "exec", self.workload_pod, "-n", self.namespace, "--",
             "curl", "-s", "--max-time", str(timeout_s), url],
            capture_output=True, timeout=timeout_s + 30, check=False,
            env=self._kubectl_env(),
        )
        if r.returncode != 0 or not r.stdout:
            return None
        return r.stdout

    def _post_via_workload_pod(self, url: str, timeout_s: int) -> bool:
        """POST JSON {} to url from the workload pod; True on HTTP 2xx.

        Unlike _fetch_via_workload_pod (GET, returns body), this checks the
        HTTP status code — vLLM's /start_profile and /stop_profile return
        200 with an EMPTY body, so body-presence checks are meaningless.
        """
        if not self.workload_pod:
            return False
        r = subprocess.run(
            ["kubectl", "exec", self.workload_pod, "-n", self.namespace, "--",
             "curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
             "-X", "POST", "-H", "Content-Type: application/json",
             "-d", "{}", "--max-time", str(timeout_s), url],
            capture_output=True, timeout=timeout_s + 30, check=False,
            env=self._kubectl_env(),
        )
        if r.returncode != 0:
            return False
        try:
            code = int(r.stdout.decode("utf-8", "replace").strip().splitlines()[-1])
        except (ValueError, IndexError):
            return False
        return 200 <= code < 300

    def finish_captures(self, handle: Dict, config, log_callback: Optional[Callable[[str], None]] = None) -> Optional[Dict]:
        if not handle:
            return None
        self._resolve_workload_pod()
        test_id = handle.get("test_id") or config.test_id
        artifact_dir = Path(f"/mnt/storage/test-artifacts/{test_id}/profiles")
        try:
            artifact_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.warning(f"cannot create profile artifact dir: {e}")
            return None

        cpu_captures = []

        # 1. Reap the EPP CPU capture subprocess
        spawn = handle.get("epp_spawn")
        if spawn:
            data = self._reap_capture(spawn, label="epp-cpu")
            if data:
                cpu_captures.append({"kind": "epp", "pod": spawn["pod"],
                                     "node": spawn.get("node"),
                                     "port": spawn["port"], "ptype": "cpu", "data": data})
            elif log_callback:
                log_callback("   \u26a0\ufe0f  EPP CPU profile capture returned no data")

        # 2. Reap vLLM CPU capture subprocesses
        for spawn in handle.get("vllm_spawns", []):
            data = self._reap_capture(spawn, label=f"vllm-cpu-{spawn['pod']}")
            if data:
                cpu_captures.append({"kind": "vllm", "pod": spawn["pod"],
                                     "node": spawn.get("node"),
                                     "port": spawn["port"], "ptype": "cpu", "data": data})

        if not cpu_captures and not handle.get("enable_vllm"):
            if log_callback:
                log_callback("   \u26a0\ufe0f  No CPU profiles captured")

        # 3. Collect torch profiler traces — collect_torch_profiles copies
        #    files to artifact_dir/torch/<pod>/ internally and appends entries
        #    to torch_written (kept separate so the capture zip below stays aligned)
        torch_results = []
        if handle.get("torch_procs"):
            torch_results = self.collect_torch_profiles(handle["torch_procs"], log_callback, None, test_id)

        # 4. Heap + goroutine snapshots (post-load, point-in-time)
        snapshots = []
        epp = self.discover_epp_pod(config.architecture)
        if epp:
            for ptype in ("heap", "goroutine"):
                url = f"http://{epp['ip']}:{epp['port']}/debug/pprof/{ptype}"
                data = self._fetch_via_workload_pod(url, 30)
                if data:
                    snapshots.append({"kind": "epp", "pod": epp["name"],
                                     "node": epp.get("node"), "port": epp["port"],
                                     "ptype": ptype, "data": data})

        # 5. Write CPU capture files to the artifact dir
        all_captures = cpu_captures + snapshots
        cap_written = []
        for cap in all_captures:
            out = artifact_dir / cap["kind"] / cap["pod"] / f"{cap['ptype']}.prof"
            try:
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(cap["data"])
                cap_written.append({
                    "kind": cap["kind"], "pod": cap["pod"], "port": cap["port"],
                    "node": cap.get("node"),
                    "ptype": cap["ptype"], "path": str(out.relative_to(artifact_dir)),
                    "size": len(cap["data"]),
                })
            except Exception as e:
                logger.warning(f"failed writing profile {out}: {e}")

        written = cap_written

        # 6. Build the report summary
        summary = {"files": [], "epp": None, "vllm": [], "torch": torch_results}

        # Parse the EPP heap snapshot — inuse bytes + top allocation sites,
        # trended across long-run cycles for leak detection
        for cap in snapshots:
            if cap.get("ptype") == "heap":
                try:
                    from core.pprof_parser import summarize_heap_profile
                    heap = summarize_heap_profile(cap["data"])
                    if heap:
                        heap["pod"] = cap.get("pod")
                        summary["epp_heap"] = heap
                except Exception as e:
                    logger.warning(f"heap parse failed: {e}")
        embed_budget = MAX_EMBED_BYTES
        for cap, meta in zip(all_captures, cap_written):
            entry = dict(meta)
            if embed_budget > 0:
                entry["blob"] = base64.b64encode(cap["data"]).decode("ascii")
                embed_budget -= len(entry["blob"])
            summary["files"].append({k: v for k, v in entry.items() if k != "blob"})
            if cap["ptype"] == "cpu":
                try:
                    parsed = summarize_cpu_profile(cap["data"], top_n=25)
                    parsed["pod"] = cap["pod"]
                    parsed["node"] = cap.get("node")
                    parsed["port"] = cap["port"]
                    if "blob" in entry:
                        parsed["blob"] = entry["blob"]
                    if cap["kind"] == "epp":
                        summary["epp"] = parsed
                    else:
                        summary["vllm"].append(parsed)
                except Exception as e:
                    logger.warning(f"failed parsing CPU profile {cap['pod']}: {e}")

        # Torch traces are parsed in-pod — no artifact files, nothing to list

        if log_callback and written:
            total_kb = sum(w["size"] for w in written) // 1024
            n_cpu = sum(1 for w in written if w["ptype"] == "cpu")
            log_callback(f"   \U0001f52c Collected {len(written)} profile file(s) ({n_cpu} CPU, {total_kb} KB) + {len(torch_results)} torch profile(s) → {artifact_dir}")

        return summary if (summary.get("epp") or summary.get("vllm") or
                          summary.get("files") or summary.get("torch")) else None
