"""Per-pod pprof profile collection for ServeIt tests.

Collects Go pprof profiles from the EPP (always available, port 9090)
and — when the run opts in — from vLLM pods (requires a vLLM image that
serves /debug/pprof on the API port: 8000 aggregated/prefill, 8200
decode; probed per pod).

CPU profiles are captured DURING the benchmark load: the blocking
?seconds=N fetch runs as a DETACHED SUBPROCESS started right before
guidellm launches (the server runs under gevent — capture "threads"
would be starved greenlets, so Popen + on-disk staging files is the only
reliable mechanism). Heap and goroutine snapshots are fetched once the
benchmark finishes.

All raw files land in /mnt/storage/test-artifacts/{test_id}/profiles/
(MLflow export picks them up automatically). A parsed summary (via
core.pprof_parser) is attached to the TestResult for the report.
"""

import base64
import logging
import os
import subprocess
import tempfile
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

    def _resolve_workload_pod(self) -> Optional[str]:
        """Resolve the actual workload pod name by label.

        The constructor may receive the bare deployment name
        ("serveit-workload"); the real pod has a replicaset hash suffix
        (e.g. serveit-workload-74459d9f7f-pd4g8). Always resolve fresh —
        the pod is recreated on redeploy.
        """
        if self.workload_pod and "-" in self.workload_pod.strip("serveit-workload-"):
            # Looks like a fully-qualified pod name already — but verify it
            # exists; fall through to label resolution if it doesn't.
            r = self._run_kubectl(["get", "pod", self.workload_pod, "-n", self.namespace, "-o", "name"])
            if r.returncode == 0:
                return self.workload_pod
        r = self._run_kubectl([
            "get", "pods", "-l", "app=serveit-workload", "-n", self.namespace,
            "-o", "jsonpath={.items[0].metadata.name}",
        ])
        if r.returncode == 0 and r.stdout.strip():
            self.workload_pod = r.stdout.strip()
            return self.workload_pod
        return self.workload_pod

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
        """Spawn a detached capture subprocess writing stdout to a staging file.

        Runs independently of the gevent event loop; finish_captures()
        reaps it. Returns {proc, outfile, errfile, timeout_s, started}.
        """
        fd, outfile = tempfile.mkstemp(prefix="serveit-pprof-", suffix=".bin")
        os.close(fd)
        fd, errfile = tempfile.mkstemp(prefix="serveit-pprof-err-", suffix=".txt")
        os.close(fd)
        out = open(outfile, "wb")
        errf = open(errfile, "wb")
        proc = subprocess.Popen(
            cmd, stdout=out, stderr=errf, env=self._kubectl_env(),
        )
        out.close()
        errf.close()
        return {"proc": proc, "outfile": outfile, "errfile": errfile,
                "timeout_s": timeout_s, "started": time.time()}

    def _reap_capture(self, cap: Dict, label: str = "") -> Optional[bytes]:
        """Wait for a spawned capture to finish and read its staged output."""
        try:
            elapsed = time.time() - cap["started"]
            remaining = cap["timeout_s"] + 60 - elapsed
            proc = cap["proc"]
            try:
                proc.wait(timeout=max(5, remaining))
            except subprocess.TimeoutExpired:
                proc.kill()
                logger.warning(f"profile capture {label}: timed out after {elapsed:.0f}s")
                return None
            size = 0
            try:
                size = Path(cap["outfile"]).stat().st_size
            except OSError:
                pass
            if proc.returncode != 0 or size == 0:
                stderr_txt = ""
                try:
                    stderr_txt = Path(cap["errfile"]).read_text()[:200]
                except OSError:
                    pass
                logger.warning(
                    f"profile capture {label}: rc={proc.returncode} size={size} "
                    f"stderr={stderr_txt!r}"
                )
                return None
            data = Path(cap["outfile"]).read_bytes()
            if data and data[:2] != b'\x1f\x8b':
                # Not a gzipped pprof — Go's handler returns a plain-text
                # error body ("Could not enable CPU profiling: ... already
                # in progress") when another capture is active.
                logger.warning(
                    f"profile capture {label}: non-pprof response: {data[:120]!r}"
                )
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

    # ── direct fetch (short, blocking — snapshots) ──────────────────────────

    def _fetch_via_workload_pod(self, url: str, timeout_s: int) -> Optional[bytes]:
        """Fetch a URL from inside the cluster via the persistent workload pod."""
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

    # ── collection lifecycle ────────────────────────────────────────────────

    def start_cpu_captures(self, config, log_callback: Optional[Callable[[str], None]] = None) -> Dict:
        """Kick off background CPU profile captures covering the benchmark load.

        Called right before the guidellm benchmark launches. CPU profile
        fetches block for the requested window (?seconds=N), so they run
        as detached subprocesses and are reaped by finish_captures().
        """
        test_id = config.test_id
        window = min(MAX_CPU_WINDOW_S, max(30, getattr(config, "test_duration", 300) or 300))
        enable_vllm = bool(getattr(config, "enable_pprof", False))
        self._resolve_workload_pod()  # bare names fail: pods "serveit-workload" not found
        handle = {
            "test_id": test_id,
            "window_s": window,
            "enable_vllm": enable_vllm,
            "started": time.time(),
            "epp_spawn": None,     # EPP CPU capture subprocess
            "vllm_spawns": [],     # vLLM CPU capture subprocesses
        }

        # EPP (always)
        epp = self.discover_epp_pod(config.architecture)
        if epp and self.workload_pod:
            url = f"http://{epp['ip']}:{epp['port']}/debug/pprof/profile?seconds={window}"
            cmd = ["kubectl", "exec", self.workload_pod, "-n", self.namespace, "--",
                   "curl", "-s", "--max-time", str(window + 30), url]
            handle["epp_spawn"] = dict(
                self._spawn_capture(cmd, window + 30),
                kind="epp", pod=epp["name"], port=epp["port"], ptype="cpu")
            if log_callback:
                log_callback(f"   🔬 EPP CPU profile capture started ({window}s window, pod {epp['name']})")
        else:
            if log_callback:
                log_callback("   ⚠️  EPP pod/workload pod not found — skipping EPP profile capture")

        # vLLM pods (opt-in)
        if enable_vllm:
            pods = self.discover_vllm_pods(test_id)
            probed = 0
            for pod in pods:
                port = self._probe_vllm_port(pod)
                if not port:
                    if log_callback:
                        log_callback(f"   ⚠️  No pprof endpoint on {pod} (vLLM image does not serve /debug/pprof — skipping)")
                    continue
                probed += 1
                code = (
                    "import urllib.request,sys\n"
                    f"sys.stdout.buffer.write(urllib.request.urlopen('http://localhost:{port}/debug/pprof/profile?seconds={window}', timeout={window + 30}).read())\n"
                )
                cmd = ["kubectl", "exec", pod, "-n", self.namespace, "-c", "vllm", "--",
                       "python3", "-c", code]
                handle["vllm_spawns"].append(dict(
                    self._spawn_capture(cmd, window + 60),
                    kind="vllm", pod=pod, port=port, ptype="cpu"))
            if log_callback:
                log_callback(f"   🔬 vLLM CPU profile capture started on {probed}/{len(pods)} pod(s) ({window}s window)")

        return handle

    def finish_captures(self, handle: Dict, config, log_callback: Optional[Callable[[str], None]] = None) -> Optional[Dict]:
        """Reap CPU capture subprocesses, snapshot heap/goroutine, write files.

        Returns the pprof summary dict for the report:
        {epp: {...}, vllm: [{pod, ...}], files: [{path, kind, pod, ptype}]}
        """
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
                                     "port": spawn["port"], "ptype": "cpu", "data": data})
            else:
                # Retry synchronously with a short window — better a short
                # profile than none. Another capture may still be winding
                # down (Go pprof allows one CPU profile at a time), so
                # wait-and-retry on the "already in progress" collision.
                epp_now = self.discover_epp_pod(config.architecture)
                if epp_now and self.workload_pod:
                    for attempt in range(3):
                        url = f"http://{epp_now['ip']}:{epp_now['port']}/debug/pprof/profile?seconds=15"
                        retry = self._fetch_via_workload_pod(url, 45)
                        if retry and retry[:2] == b'\x1f\x8b':
                            if log_callback:
                                log_callback("   ⚠️  EPP CPU profile captured via post-benchmark retry (15s window)")
                            cpu_captures.append({"kind": "epp", "pod": epp_now["name"],
                                                 "port": epp_now["port"], "ptype": "cpu", "data": retry})
                            break
                        if retry and b'already in progress' in retry[:200]:
                            if log_callback:
                                log_callback("   ⏳ Another CPU profile still active on EPP — waiting 15s before retry...")
                            time.sleep(15)
                            continue
                        break
                if not cpu_captures and log_callback:
                    log_callback("   ⚠️  EPP CPU profile capture returned no data")

        # 2. Reap vLLM CPU capture subprocesses
        for spawn in handle.get("vllm_spawns", []):
            data = self._reap_capture(spawn, label=f"vllm-cpu-{spawn['pod']}")
            if data:
                cpu_captures.append({"kind": "vllm", "pod": spawn["pod"],
                                     "port": spawn["port"], "ptype": "cpu", "data": data})

        if not cpu_captures and not handle.get("enable_vllm"):
            if log_callback:
                log_callback("   ⚠️  No CPU profiles captured")

        # 3. Heap + goroutine snapshots (post-load, point-in-time)
        snapshots = []
        epp = self.discover_epp_pod(config.architecture)
        if epp:
            for ptype in ("heap", "goroutine"):
                url = f"http://{epp['ip']}:{epp['port']}/debug/pprof/{ptype}"
                data = self._fetch_via_workload_pod(url, 30)
                if data:
                    snapshots.append({"kind": "epp", "pod": epp["name"], "port": epp["port"], "ptype": ptype, "data": data})

        # 4. Write raw files to the artifact dir
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

        # 5. Build the report summary (CPU profiles parsed; others listed)
        summary = {"files": [], "epp": None, "vllm": []}
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
