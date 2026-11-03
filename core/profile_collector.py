"""Per-pod pprof profile collection for ServeIt tests.

Collects Go pprof profiles from the EPP (always available, port 9090)
and — when the run opts in — from vLLM pods (requires --enable-pprof,
served on the vLLM API port: 8000 aggregated/prefill, 8200 decode).

CPU profiles are captured DURING the benchmark load (background
threads started right before guidellm launches; the blocking
?seconds=N fetch self-terminates inside the window). Heap and
goroutine snapshots are taken once the benchmark finishes.

All raw files land in /mnt/storage/test-artifacts/{test_id}/profiles/
(MLflow export picks them up automatically). A parsed summary (via
core.pprof_parser) is attached to the TestResult for the report.
"""

import base64
import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Dict, Optional

from core.pprof_parser import summarize_cpu_profile

logger = logging.getLogger(__name__)

# Cap the load-window CPU capture (guidellm runs are typically >=120s;
# a 120s window is plenty of samples at 10ms resolution)
MAX_CPU_WINDOW_S = 120
# Total embedded raw-profile budget per test (base64 in the DB)
MAX_EMBED_BYTES = 2 * 1024 * 1024


class ProfileCollector:
    """Collects pprof profiles from EPP and vLLM pods for one test."""

    def __init__(self, kubectl, namespace: str, workload_pod: Optional[str] = None):
        self.kubectl = kubectl          # KubectlRunner
        self.namespace = namespace
        self.workload_pod = workload_pod

    # ── pod discovery ───────────────────────────────────────────────────────

    def _run_kubectl(self, args, timeout=30, binary=False):
        env = os.environ.copy()
        env["KUBECONFIG"] = os.path.expanduser(self.kubectl.kubeconfig)
        return subprocess.run(
            ["kubectl"] + args,
            capture_output=True,
            text=not binary,
            timeout=timeout,
            check=False,
            env=env,
        )

    def discover_epp_pod(self, architecture: str) -> Optional[Dict[str, str]]:
        """Find the EPP pod (name + IP) for an architecture's EPP deployment."""
        arch = (architecture or "aggregated").lower()
        r = self._run_kubectl([
            "get", "pods", "-n", self.namespace,
            "-l", f"inferencepool=gaie-{arch}-epp",
            "-o", "jsonpath={range .items[*]}{.metadata.name}{\" \"}{.status.podIP}{\"\\n\"}{end}",
        ])
        if r.returncode != 0 or not r.stdout.strip():
            return None
        line = r.stdout.strip().splitlines()[0].split()
        if len(line) < 2 or not line[1]:
            return None
        return {"name": line[0], "ip": line[1], "port": 9090}

    def discover_vllm_pods(self, test_id: str):
        """All vLLM pods for a test (name only; port probed per pod)."""
        r = self._run_kubectl([
            "get", "pods", "-n", self.namespace,
            "-l", f"llm-d.ai/test-id={test_id}",
            "-o", 'jsonpath={range .items[*]}{.metadata.name}{" "}{end}',
        ])
        if r.returncode != 0:
            return []
        return [p for p in r.stdout.strip().split() if p]

    def _probe_vllm_port(self, pod: str) -> Optional[int]:
        """Find which localhost port serves pprof in a pod's vllm container.

        Aggregated/prefill pods: vLLM on 8000. Decode pods: the
        routing-proxy sidecar owns 8000 and vLLM listens on 8200.
        """
        for port in (8000, 8200):
            r = self._run_kubectl([
                "exec", pod, "-n", self.namespace, "-c", "vllm", "--",
                "python3", "-c",
                f"import urllib.request,sys;\n"
                f"req=urllib.request.Request('http://localhost:{port}/debug/pprof/');\n"
                f"sys.exit(0 if urllib.request.urlopen(req,timeout=5).status==200 else 1)",
            ], timeout=20)
            if r.returncode == 0:
                return port
        return None

    # ── profile fetch primitives ────────────────────────────────────────────

    def _fetch_via_workload_pod(self, url: str, timeout_s: int) -> Optional[bytes]:
        """Fetch a URL from inside the cluster via the persistent workload pod."""
        if not self.workload_pod:
            return None
        env = os.environ.copy()
        env["KUBECONFIG"] = os.environ.expanduser(self.kubectl.kubeconfig)
        r = subprocess.run(
            ["kubectl", "exec", self.workload_pod, "-n", self.namespace, "--",
             "curl", "-s", "--max-time", str(timeout_s), url],
            capture_output=True, timeout=timeout_s + 30, check=False, env=env,
        )
        if r.returncode != 0 or not r.stdout:
            return None
        return r.stdout

    def _fetch_via_pod_python(self, pod: str, port: int, path: str, timeout_s: int) -> Optional[bytes]:
        """Fetch http://localhost:{port}{path} inside a pod's vllm container."""
        env = os.environ.copy()
        env["KUBECONFIG"] = os.environ.expanduser(self.kubectl.kubeconfig)
        code = (
            "import urllib.request,sys\n"
            f"sys.stdout.buffer.write(urllib.request.urlopen('http://localhost:{port}{path}', timeout={timeout_s}).read())\n"
        )
        r = subprocess.run(
            ["kubectl", "exec", pod, "-n", self.namespace, "-c", "vllm", "--",
             "python3", "-c", code],
            capture_output=True, timeout=timeout_s + 60, check=False, env=env,
        )
        if r.returncode != 0 or not r.stdout:
            return None
        return r.stdout

    # ── collection lifecycle ────────────────────────────────────────────────

    def start_cpu_captures(self, config, log_callback: Optional[Callable[[str], None]] = None) -> Dict:
        """Kick off background CPU profile captures covering the benchmark load.

        Called right before the guidellm benchmark launches. CPU profile
        fetches block for the requested window (?seconds=N), so they run
        in daemon threads and are joined by finish_captures().
        """
        test_id = config.test_id
        window = min(MAX_CPU_WINDOW_S, max(30, getattr(config, "test_duration", 300) or 300))
        enable_vllm = bool(getattr(config, "enable_pprof", False))
        handle = {
            "test_id": test_id,
            "window_s": window,
            "enable_vllm": enable_vllm,
            "threads": [],
            "captures": [],   # {kind, pod, port, ptype, data} filled by threads
            "started": time.time(),
        }

        # EPP (always)
        epp = self.discover_epp_pod(config.architecture)
        if epp:
            url = f"http://{epp['ip']}:{epp['port']}/debug/pprof/profile?seconds={window}"
            t = threading.Thread(target=self._capture_worker, args=(
                handle, "epp", epp["name"], epp["port"], "cpu",
                lambda: self._fetch_via_workload_pod(url, window + 30),
            ), daemon=True)
            handle["threads"].append(t)
            t.start()
            if log_callback:
                log_callback(f"   🔬 EPP CPU profile capture started ({window}s window, pod {epp['name']})")
        else:
            if log_callback:
                log_callback("   ⚠️  EPP pod not found — skipping EPP profile capture")

        # vLLM pods (opt-in)
        if enable_vllm:
            pods = self.discover_vllm_pods(test_id)
            for pod in pods:
                port = self._probe_vllm_port(pod)
                if not port:
                    if log_callback:
                        log_callback(f"   ⚠️  No pprof endpoint on {pod} (is --enable-pprof set?)")
                    continue
                path = f"/debug/pprof/profile?seconds={window}"
                t = threading.Thread(target=self._capture_worker, args=(
                    handle, "vllm", pod, port, "cpu",
                    lambda p=pod, pt=port: self._fetch_via_pod_python(p, pt, path, window + 30),
                ), daemon=True)
                handle["threads"].append(t)
                t.start()
            if log_callback:
                log_callback(f"   🔬 vLLM CPU profile capture started on {len(pods)} pod(s) ({window}s window)")

        return handle

    def _capture_worker(self, handle, kind, pod, port, ptype, fetch_fn):
        try:
            data = fetch_fn()
            if data:
                handle["captures"].append({"kind": kind, "pod": pod, "port": port, "ptype": ptype, "data": data})
        except Exception as e:
            logger.debug(f"profile capture failed ({kind}/{pod}/{ptype}): {e}")

    def finish_captures(self, handle: Dict, config, log_callback: Optional[Callable[[str], None]] = None) -> Optional[Dict]:
        """Join CPU threads, snapshot heap/goroutine, write files, build summary.

        Returns the pprof summary dict for the report:
        {epp: {...}, vllm: [{pod, ...}], files: [{path, kind, pod, ptype}]}
        """
        if not handle:
            return None
        test_id = handle.get("test_id") or config.test_id
        artifact_dir = Path(f"/mnt/storage/test-artifacts/{test_id}/profiles")
        try:
            artifact_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.warning(f"cannot create profile artifact dir: {e}")
            return None

        # 1. Join CPU capture threads
        for t in handle.get("threads", []):
            t.join(timeout=30)
        cpu_captures = handle.get("captures", [])
        if not cpu_captures and not handle.get("enable_vllm"):
            if log_callback:
                log_callback("   ⚠️  No CPU profiles captured")

        # 2. Heap + goroutine snapshots (post-load, point-in-time)
        snapshots = []
        epp = self.discover_epp_pod(config.architecture)
        if epp:
            for ptype in ("heap", "goroutine"):
                url = f"http://{epp['ip']}:{epp['port']}/debug/pprof/{ptype}"
                data = self._fetch_via_workload_pod(url, 30)
                if data:
                    snapshots.append({"kind": "epp", "pod": epp["name"], "port": epp["port"], "ptype": ptype, "data": data})
        if handle.get("enable_vllm"):
            for pod in self.discover_vllm_pods(test_id):
                port = self._probe_vllm_port(pod)
                if not port:
                    continue
                for ptype in ("heap", "goroutine"):
                    data = self._fetch_via_pod_python(pod, port, f"/debug/pprof/{ptype}", 30)
                    if data:
                        snapshots.append({"kind": "vllm", "pod": pod, "port": port, "ptype": ptype, "data": data})

        # 3. Write raw files to the artifact dir
        all_captures = cpu_captures + snapshots
        written = []
        for cap in all_captures:
            out = artifact_dir / cap["kind"] / cap["pod"] / f"{cap['ptype']}.prof"
            try:
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(cap["data"])
                written.append({
                    "kind": cap["kind"], "pod": cap["pod"], "port": cap["port"],
                    "ptype": cap["ptype"], "path": str(out.relative_to(artifact_dir)),
                    "size": len(cap["data"]),
                })
            except Exception as e:
                logger.warning(f"failed writing profile {out}: {e}")

        # 4. Build the report summary (CPU profiles parsed; others listed)
        summary = {"files": [], "epp": None, "vllm": []}
        embed_budget = MAX_EMBED_BYTES
        for cap, meta in zip(all_captures, written):
            entry = dict(meta)
            if embed_budget > 0 and cap["ptype"] == "cpu":
                entry["blob"] = base64.b64encode(cap["data"]).decode("ascii")
                embed_budget -= len(entry["blob"])
            summary["files"].append({k: v for k, v in entry.items() if k != "blob"})
            if cap["ptype"] == "cpu":
                try:
                    parsed = summarize_cpu_profile(cap["data"], top_n=25)
                    parsed["pod"] = cap["pod"]
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
            log_callback(f"   🔬 Collected {len(written)} profile file(s) ({n_cpu} CPU, {total_kb} KB) → {artifact_dir}")

        return summary if (summary.get("epp") or summary.get("vllm") or summary.get("files")) else None
