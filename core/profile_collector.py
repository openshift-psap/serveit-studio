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
PYSPY_PROFILE_SCRIPT = """\
import os, json, subprocess, sys, time

def find_vllm_pid():
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
                    return int(pid)
        except (OSError, IOError):
            pass
    return None

vllm_pid = find_vllm_pid()
if not vllm_pid:
    print(json.dumps({'error': 'vLLM process not found'}))
    sys.exit(0)

try:
    subprocess.run(['py-spy', '--version'], capture_output=True, timeout=5)
except (FileNotFoundError, subprocess.TimeoutExpired):
    subprocess.run(['pip', 'install', '-q', 'py-spy'], capture_output=True, timeout=60)

duration = __DURATION__
output_dir = '/tmp/torch_profiles'
os.makedirs(output_dir, exist_ok=True)

trace_path = os.path.join(output_dir, 'trace.json')
r = subprocess.run([
    'py-spy', 'record',
    '--pid', str(vllm_pid),
    '--duration', str(duration),
    '--rate', '50',
    '--format', 'speedscope',
    '--output', trace_path,
    '--nonblocking'
], capture_output=True, text=True, timeout=duration + 60)

if r.returncode != 0:
    print(json.dumps({'error': f'py-spy failed: {r.stderr[:200]}'}))
    sys.exit(0)

trace_size = os.path.getsize(trace_path) if os.path.exists(trace_path) else 0

summary = {'profiler': 'py-spy', 'duration_s': duration,
           'trace_size_bytes': trace_size, 'operators': []}
try:
    with open(trace_path) as f:
        data = json.load(f)
    frames = data.get('shared', {}).get('frames', [])
    profiles = data.get('profiles', [])
    from collections import Counter
    func_counts = Counter()
    total_samples = 0
    for prof in profiles:
        for sample in prof.get('samples', []):
            if sample:
                leaf = frames[sample[-1]]['name'] if sample[-1] < len(frames) else 'unknown'
                func_counts[leaf] += 1
                total_samples += 1
    for name, count in func_counts.most_common(25):
        pct = round(100 * count / max(total_samples, 1), 1)
        summary['operators'].append({
            'name': name,
            'cpu_total_ms': round(count * 20, 1),
            'cuda_total_ms': 0,
            'self_cpu_ms': round(count * 20, 1),
            'self_cuda_ms': 0,
            'calls': count,
            'sample_pct': pct
        })
    summary['total_cpu_ms'] = round(total_samples * 20, 1)
    summary['total_cuda_ms'] = 0
    summary['operator_count'] = len(summary['operators'])
    summary['total_samples'] = total_samples
except Exception as e:
    summary['parse_error'] = str(e)

with open(os.path.join(output_dir, 'summary.json'), 'w') as f:
    json.dump(summary, f)
print('SUMMARY_JSON:' + json.dumps(summary))
"""


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
            pods = self.discover_vllm_pods(test_id)
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
            handle["torch_procs"] = self.start_torch_profiles(test_id, window, log_callback)

        return handle

    # ── torch profiler ──────────────────────────────────────────────────────

    def start_torch_profiles(self, test_id, window_s, log_callback=None):
        """Start torch profiler on all vLLM pods via background kubectl exec."""
        pods = self.discover_vllm_pods(test_id)
        procs = []
        for pod in pods:
            pod_name = pod["name"] if isinstance(pod, dict) else pod
            script = PYSPY_PROFILE_SCRIPT.replace("__DURATION__", str(window_s))
            cmd = ["kubectl", "exec", pod_name, "-n", self.namespace, "-c", "vllm",
                   "--", "python3", "-c", script]
            fd, outfile = tempfile.mkstemp(prefix="torch-prof-", suffix=".out")
            os.close(fd)
            p = subprocess.Popen(cmd, stdout=open(outfile, "wb"),
                                 stderr=subprocess.DEVNULL,
                                 env=self._kubectl_env())
            procs.append({"pod": pod_name, "proc": p, "outfile": outfile})
        if log_callback and procs:
            log_callback(f"   \U0001f52c Torch profiler started on {len(procs)} pod(s) ({window_s}s window)")
        return procs

    def collect_torch_profiles(self, procs, log_callback=None):
        """Wait for torch profilers and collect their summaries."""
        results = []
        for entry in procs:
            pod_name = entry["pod"]
            try:
                entry["proc"].wait(timeout=300)
                out = open(entry["outfile"], "rb").read().decode("utf-8", "replace")
                for line in out.split("\n"):
                    if line.startswith("SUMMARY_JSON:"):
                        summary = json.loads(line[len("SUMMARY_JSON:"):])
                        summary["pod"] = pod_name
                        results.append(summary)
                        break
                else:
                    r = subprocess.run(
                        ["kubectl", "exec", pod_name, "-n", self.namespace, "-c", "vllm",
                         "--", "cat", "/tmp/torch_profiles/summary.json"],
                        capture_output=True, text=True, timeout=30,
                        env=self._kubectl_env())
                    if r.returncode == 0 and r.stdout.strip():
                        summary = json.loads(r.stdout)
                        summary["pod"] = pod_name
                        results.append(summary)
            except Exception as e:
                logger.warning(f"torch profile collection failed for {pod_name}: {e}")
            finally:
                try:
                    os.unlink(entry["outfile"])
                except OSError:
                    pass
        if log_callback and results:
            log_callback(f"   \U0001f52c Torch profiles collected from {len(results)} pod(s)")
        return results

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

        # 3. Collect torch profiler summaries
        torch_results = []
        if handle.get("torch_procs"):
            torch_results = self.collect_torch_profiles(handle["torch_procs"], log_callback)

        # 3b. Copy torch trace files from pods to artifact dir
        for tr in torch_results:
            pod_name = tr.get('pod')
            if not pod_name:
                continue
            try:
                r = subprocess.run(
                    ['kubectl', 'exec', pod_name, '-n', self.namespace, '-c', 'vllm',
                     '--', 'cat', '/tmp/torch_profiles/trace.json'],
                    capture_output=True, timeout=60,
                    env=self._kubectl_env())
                if r.returncode == 0 and r.stdout:
                    trace_out = artifact_dir / 'torch' / pod_name / 'trace.json'
                    trace_out.parent.mkdir(parents=True, exist_ok=True)
                    trace_out.write_bytes(r.stdout)
                    written.append({
                        'kind': 'torch', 'pod': pod_name, 'port': 0,
                        'ptype': 'trace', 'path': str(trace_out.relative_to(artifact_dir)),
                        'size': len(r.stdout)
                    })
            except Exception as e:
                logger.warning(f'failed copying torch trace from {pod_name}: {e}')

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

        # 5. Write raw files to the artifact dir
        all_captures = cpu_captures + snapshots
        written = []
        for cap in all_captures:
            out = artifact_dir / cap["kind"] / cap["pod"] / f"{cap['ptype']}.prof"
            try:
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(cap["data"])
                written.append({
                    "kind": cap["kind"], "pod": cap["pod"], "port": cap["port"],
                    "node": cap.get("node"),
                    "ptype": cap["ptype"], "path": str(out.relative_to(artifact_dir)),
                    "size": len(cap["data"]),
                })
            except Exception as e:
                logger.warning(f"failed writing profile {out}: {e}")

        # 6. Build the report summary
        summary = {"files": [], "epp": None, "vllm": [], "torch": torch_results}
        embed_budget = MAX_EMBED_BYTES
        for cap, meta in zip(all_captures, written):
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

        if log_callback and written:
            total_kb = sum(w["size"] for w in written) // 1024
            n_cpu = sum(1 for w in written if w["ptype"] == "cpu")
            log_callback(f"   \U0001f52c Collected {len(written)} profile file(s) ({n_cpu} CPU, {total_kb} KB) + {len(torch_results)} torch profile(s) → {artifact_dir}")

        return summary if (summary.get("epp") or summary.get("vllm") or
                          summary.get("files") or summary.get("torch")) else None
