"""Cluster resource scanner — scans nodes, GPUs, and network for visualization."""

import base64
import os
import subprocess
import tempfile
from dataclasses import asdict
from typing import Dict


def scan_cluster_resources(cluster: Dict, namespace: str = 'inftune') -> Dict:
    """Scan a cluster's resources using the system scanner.

    For remote clusters, extracts the kubeconfig from the K8s Secret
    and passes it to the scanner. For local clusters, uses in-cluster auth.

    Returns a dict with nodes, summary, and GPU info for visualization.
    """
    kubeconfig_path = None

    if cluster.get('kubeconfig_secret'):
        cmd = 'oc' if _is_oc() else 'kubectl'
        r = subprocess.run(
            [cmd, 'get', 'secret', cluster['kubeconfig_secret'], '-n', namespace,
             '-o', 'jsonpath={.data.kubeconfig}'],
            capture_output=True, text=True, timeout=15
        )
        if r.returncode != 0 or not r.stdout.strip():
            raise RuntimeError('Could not read kubeconfig Secret')
        kubeconfig_data = base64.b64decode(r.stdout.strip()).decode()
        tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.kubeconfig', delete=False)
        tmp.write(kubeconfig_data)
        tmp.close()
        kubeconfig_path = tmp.name

    try:
        from core.system_scanner import SystemScanner
        scanner = SystemScanner(
            namespace=namespace,
            kubeconfig=kubeconfig_path
        )
        resources = scanner.scan_cluster()

        # Count GPUs in use
        gpus_in_use = 0
        try:
            import json as _json
            r = scanner.kubectl.run(['get', 'pods', '--all-namespaces', '-o', 'json'], check=False)
            if r.returncode == 0:
                pods = _json.loads(r.stdout)
                for pod in pods.get('items', []):
                    if pod.get('status', {}).get('phase') != 'Running':
                        continue
                    for container in pod.get('spec', {}).get('containers', []):
                        reqs = container.get('resources', {}).get('requests', {})
                        gpu_req = reqs.get('nvidia.com/gpu', 0)
                        if gpu_req and str(gpu_req) != '0':
                            gpus_in_use += int(gpu_req)
        except Exception:
            pass

        nodes = []
        for n in resources.nodes:
            nodes.append({
                'name': n.name,
                'gpus': n.gpus,
                'gpu_model': n.gpu_model,
                'gpu_memory_gb': round(n.gpu_memory_mb / 1024, 1) if n.gpu_memory_mb else 0,
                'cpu_cores': n.cpu_cores,
                'memory_gb': n.memory_gb,
                'has_rdma': n.has_rdma,
                'status': n.status,
            })

        return {
            'nodes': nodes,
            'summary': {
                'total_gpus': resources.total_gpus,
                'gpus_in_use': gpus_in_use,
                'gpus_available': resources.total_gpus - gpus_in_use,
                'gpu_node_count': resources.gpu_node_count,
                'node_count': resources.node_count,
                'gpu_model': resources.gpu_model,
                'gpu_vendor': resources.gpu_vendor,
                'gpu_memory_per_gpu_mb': resources.gpu_memory_per_gpu_mb,
                'total_cpu_cores': resources.total_cpu_cores,
                'total_memory_gb': resources.total_memory_gb,
                'has_rdma': resources.has_rdma,
                'cloud_provider': resources.cloud_provider.value if resources.cloud_provider else 'unknown',
                'cpu_model': resources.cpu_model,
            }
        }
    finally:
        if kubeconfig_path:
            os.unlink(kubeconfig_path)


def _is_oc() -> bool:
    try:
        r = subprocess.run(['kubectl', 'api-resources', '--api-group=route.openshift.io'],
                          capture_output=True, text=True, timeout=15)
        return r.returncode == 0 and 'Route' in r.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
