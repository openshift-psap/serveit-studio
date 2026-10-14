"""Shared post-processing for cluster scans.

Both scan entry points — the web UI socket scanner (web/realtime.py) and
the launcher multi-cluster scanner (launcher/cluster_scanner.py) — run
``SystemScanner.scan_cluster()`` and then compute the same derived facts.
This module is the single home for that shared logic, so the two paths
cannot drift apart again:

- count_gpus_in_use: running-pod requests + DRA ResourceClaims
- get_infra_warnings: required CRD presence checks (LWS, Istio)
- scan_versions_safe: infrastructure component versions
- zero_gpu_warning: GPU-operator diagnostic when no GPUs are reported

Result-dict shaping stays with each caller — the web and launcher UIs
consume different shapes by design.
"""

import logging
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


def count_gpus_in_use(kubectl) -> int:
    """Count GPUs currently allocated across the cluster.

    Two allocation mechanisms, both counted:
    - Device plugin: ``nvidia.com/gpu`` requests on Running pods
    - DRA: allocated GPU devices in ResourceClaims (DRA clusters do not
      set nvidia.com/gpu on pods, so pod requests alone undercount)
    """
    gpus_in_use = 0
    try:
        import json

        # Device plugin: count nvidia.com/gpu requests on running pods
        r = kubectl.run(["get", "pods", "--all-namespaces", "-o", "json"], check=False)
        if r.returncode == 0:
            pods = json.loads(r.stdout)
            for pod in pods.get("items", []):
                if pod.get("status", {}).get("phase") != "Running":
                    continue
                for container in pod.get("spec", {}).get("containers", []):
                    reqs = container.get("resources", {}).get("requests", {})
                    gpu_req = reqs.get("nvidia.com/gpu", 0)
                    if gpu_req and str(gpu_req) != "0":
                        gpus_in_use += int(gpu_req)

        # DRA: count allocated GPU devices from ResourceClaims
        r = kubectl.run(["get", "resourceclaim", "--all-namespaces", "-o", "json"], check=False)
        if r.returncode == 0:
            claims = json.loads(r.stdout)
            for item in claims.get("items", []):
                alloc = item.get("status", {}).get("allocation", {})
                for dev in alloc.get("devices", {}).get("results", []):
                    driver = dev.get("driver", "")
                    if "nvidia" in driver.lower() or "gpu" in driver.lower():
                        gpus_in_use += 1
    except Exception as e:
        logger.debug(f"Could not calculate GPU usage: {e}")

    return gpus_in_use


def get_infra_warnings(kubectl) -> List[str]:
    """Check for missing infrastructure components the deployment depends on."""
    warnings = []
    try:
        r = kubectl.run(
            ["get", "crd", "leaderworkersets.leaderworkerset.x-k8s.io", "--ignore-not-found", "--no-headers"],
            check=False,
        )
        if r.returncode != 0 or not r.stdout.strip():
            warnings.append("LeaderWorkerSet (LWS) not installed — required for vLLM pod deployment")
    except Exception:
        pass
    try:
        r = kubectl.run(
            ["get", "crd", "virtualservices.networking.istio.io", "--ignore-not-found", "--no-headers"], check=False
        )
        if r.returncode != 0 or not r.stdout.strip():
            warnings.append("Istio not found — required for inference gateway routing")
    except Exception:
        pass
    return warnings


def scan_versions_safe(kubectl) -> Dict[str, Optional[str]]:
    """Scan infrastructure component versions; empty dict on failure."""
    try:
        from core.version_scanner import scan_versions

        return scan_versions(kubectl) or {}
    except Exception as e:
        logger.debug(f"Version scan failed: {e}")
        return {}


def zero_gpu_warning(total_gpus: int, versions: Dict[str, Optional[str]]) -> Optional[str]:
    """Diagnostic message when a scan reports zero GPUs, else None."""
    if total_gpus:
        return None
    if versions.get("gpu_operator"):
        return (
            f"GPU Operator {versions['gpu_operator']} is installed but no GPUs are detected. "
            f"Check that GPU nodes exist, the ClusterPolicy is applied, and either the device plugin "
            f"or DRA driver pods are running."
        )
    return "No GPU Operator installed. Install the NVIDIA GPU Operator to enable GPU resources on this cluster."
