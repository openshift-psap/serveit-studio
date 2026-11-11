"""Scan cluster infrastructure component versions."""

import logging
from typing import Optional, Dict
from .k8s_utils import KubectlRunner

logger = logging.getLogger(__name__)


def scan_versions(kubectl: KubectlRunner) -> Dict[str, Optional[str]]:
    """Detect infrastructure component versions from the cluster.

    Returns a dict of component names to version strings.
    """
    versions = {}

    # GPU Operator
    r = kubectl.run(
        ['get', 'csv', '-n', 'nvidia-gpu-operator',
         '-o', 'jsonpath={range .items[*]}{.metadata.name}{"\\t"}{.spec.version}{"\\n"}{end}'],
        check=False)
    if r.returncode == 0:
        for line in r.stdout.strip().splitlines():
            parts = line.split('\t')
            if len(parts) == 2 and 'gpu-operator' in parts[0]:
                versions['gpu_operator'] = parts[1]

    # GPU Driver (from node label)
    r = kubectl.run(
        ['get', 'nodes', '-l', 'nvidia.com/gpu.present=true',
         '-o', 'jsonpath={.items[0].metadata.labels.nvidia\\.com/cuda\\.driver-version\\.full}'],
        check=False)
    if r.returncode == 0 and r.stdout.strip():
        versions['gpu_driver'] = r.stdout.strip()

    # CUDA Runtime (from node label)
    r = kubectl.run(
        ['get', 'nodes', '-l', 'nvidia.com/gpu.present=true',
         '-o', 'jsonpath={.items[0].metadata.labels.nvidia\\.com/cuda\\.runtime-version\\.full}'],
        check=False)
    if r.returncode == 0 and r.stdout.strip():
        versions['cuda_runtime'] = r.stdout.strip()

    # Network Operator
    r = kubectl.run(
        ['get', 'csv', '-n', 'nvidia-network-operator',
         '-o', 'jsonpath={range .items[*]}{.metadata.name}{"\\t"}{.spec.version}{"\\n"}{end}'],
        check=False)
    if r.returncode == 0:
        for line in r.stdout.strip().splitlines():
            parts = line.split('\t')
            if len(parts) == 2 and 'network-operator' in parts[0]:
                versions['network_operator'] = parts[1]

    # MOFED / DOCA
    r = kubectl.run(
        ['get', 'nicclusterpolicy', '-o', 'jsonpath={.items[0].spec.ofedDriver.version}'],
        check=False)
    if r.returncode == 0 and r.stdout.strip():
        versions['mofed'] = r.stdout.strip()

    # Service Mesh / Istio
    r = kubectl.run(
        ['get', 'csv', '-A',
         '-o', 'jsonpath={range .items[*]}{.metadata.name}{"\\t"}{.spec.version}{"\\n"}{end}'],
        check=False)
    if r.returncode == 0:
        for line in r.stdout.strip().splitlines():
            parts = line.split('\t')
            if len(parts) != 2:
                continue
            name, ver = parts
            if 'servicemeshoperator' in name and 'service_mesh' not in versions:
                versions['service_mesh'] = ver
            elif name.startswith('nfd.') and 'nfd' not in versions:
                versions['nfd'] = ver

    # Istio version — from Istio CR spec.version
    r = kubectl.run(
        ['get', 'istio', '-A', '-o', 'jsonpath={.items[0].spec.version}'],
        check=False)
    if r.returncode == 0 and r.stdout.strip():
        versions['istio'] = r.stdout.strip()

    # OpenShift version
    r = kubectl.run(['version', '-o', 'json'], check=False)
    if r.returncode == 0 and r.stdout.strip():
        try:
            import json
            v = json.loads(r.stdout)
            sv = v.get('serverVersion', {})
            versions['k8s'] = f"{sv.get('major', '')}.{sv.get('minor', '')}"
            ov = v.get('openshiftVersion')
            if ov:
                versions['openshift'] = ov
        except Exception:
            pass

    # OpenShift version fallback (from clusterversion)
    if 'openshift' not in versions:
        r = kubectl.run(
            ['get', 'clusterversion', 'version', '-o', 'jsonpath={.status.desired.version}'],
            check=False)
        if r.returncode == 0 and r.stdout.strip():
            versions['openshift'] = r.stdout.strip()

    # EPP / Inference Scheduler version. Prefer ServeIt's gaie-* EPP
    # deployments; fall back to the NEWEST llm-d-router-endpoint-picker
    # image cluster-wide. The old fallback matched any image containing
    # 'scheduler'/'epp' — it grabbed other teams' stale EPPs (observed:
    # forge-nemotron-bench's v0.9.0 reported while the actual EPP ran
    # v0.11.0) and even accepted sha256 digests as versions.
    def _image_tag(img):
        img = img.strip()
        if not img or "@sha256" in img or ":" not in img:
            return None
        tag = img.rsplit(":", 1)[-1].strip()
        if not tag or tag == "latest" or tag.startswith("sha256"):
            return None
        return tag

    def _version_key(tag):
        import re
        nums = [int(x) for x in re.findall(r"\d+", tag)]
        return tuple(nums) if nums else (0,)

    epp_candidates = []
    for deploy_name in ("gaie-aggregated-epp", "gaie-pd-epp", "gaie-ep-epp"):
        r = kubectl.run(
            ["get", "deploy", "-A", "-l", f"app.kubernetes.io/name={deploy_name}",
             "-o", "jsonpath={.items[*].spec.template.spec.containers[0].image}"],
            check=False)
        if r.returncode == 0:
            for img in r.stdout.split():
                tag = _image_tag(img)
                if tag:
                    epp_candidates.append(tag)
    if not epp_candidates:
        r = kubectl.run(
            ["get", "deploy", "-A",
             "-o", "jsonpath={range .items[*]}{.spec.template.spec.containers[0].image}{'\\n'}{end}"],
            check=False)
        if r.returncode == 0:
            for img in r.stdout.strip().splitlines():
                if "endpoint-picker" in img:
                    tag = _image_tag(img)
                    if tag:
                        epp_candidates.append(tag)
    if epp_candidates:
        versions["epp"] = max(epp_candidates, key=_version_key)

    # LWS version
    r = kubectl.run(
        ['get', 'deploy', '-A', '-l', 'app.kubernetes.io/name=lws',
         '-o', 'jsonpath={.items[0].spec.template.spec.containers[0].image}'],
        check=False)
    if r.returncode == 0 and r.stdout.strip():
        img = r.stdout.strip()
        tag = img.split(':')[-1] if ':' in img else ''
        if tag:
            versions['lws'] = tag

    return versions
