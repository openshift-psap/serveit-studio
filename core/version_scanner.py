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

    # EPP / Inference Scheduler version
    for ns_label in [
        ('gaie-aggregated-epp', None),
        ('gaie-pd-epp', None),
    ]:
        deploy_name = ns_label[0]
        r = kubectl.run(
            ['get', 'deploy', '-A', '-l', f'app.kubernetes.io/name={deploy_name}',
             '-o', 'jsonpath={.items[0].spec.template.spec.containers[0].image}'],
            check=False)
        if r.returncode != 0 or not r.stdout.strip():
            r = kubectl.run(
                ['get', 'deploy', '-A',
                 '-o', 'jsonpath={range .items[*]}{.metadata.name}{"\\t"}{.spec.template.spec.containers[0].image}{"\\n"}{end}'],
                check=False)
            if r.returncode == 0:
                for line in r.stdout.strip().splitlines():
                    parts = line.split('\t')
                    if len(parts) == 2 and 'epp' in parts[0].lower() and 'scheduler' in parts[1].lower():
                        tag = parts[1].split(':')[-1] if ':' in parts[1] else ''
                        if tag:
                            versions['epp'] = tag
                        break
        else:
            img = r.stdout.strip()
            tag = img.split(':')[-1] if ':' in img else ''
            if tag:
                versions['epp'] = tag
        if 'epp' in versions:
            break

    # EPP fallback — search by image name
    if 'epp' not in versions:
        r = kubectl.run(
            ['get', 'deploy', '-A',
             '-o', 'jsonpath={range .items[*]}{.spec.template.spec.containers[0].image}{"\\n"}{end}'],
            check=False)
        if r.returncode == 0:
            for line in r.stdout.strip().splitlines():
                if 'scheduler' in line.lower() or 'epp' in line.lower() or 'endpoint-picker' in line.lower():
                    tag = line.split(':')[-1] if ':' in line else ''
                    if tag and tag != 'latest':
                        versions['epp'] = tag
                        break

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
