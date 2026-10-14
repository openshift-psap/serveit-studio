"""Cluster resource scanner — scans nodes, GPUs, and network for visualization."""

import base64
import os
import subprocess
import tempfile
from typing import Dict


def scan_cluster_resources(cluster: Dict, namespace: str = 'serveit', proxy: str = None) -> Dict:
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
            capture_output=True, text=True, timeout=60
        )
        if r.returncode != 0 or not r.stdout.strip():
            raise RuntimeError('Could not read kubeconfig Secret')
        kubeconfig_data = base64.b64decode(r.stdout.strip()).decode()
        tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.kubeconfig', delete=False)
        tmp.write(kubeconfig_data)
        tmp.close()
        kubeconfig_path = tmp.name

    try:
        # Pass proxy to scanner via env — scoped to the scanner's subprocess calls only
        proxy_url = proxy or cluster.get('proxy')

        from core.system_scanner import SystemScanner
        scanner = SystemScanner(
            namespace=namespace,
            kubeconfig=kubeconfig_path
        )
        # Inject proxy into the scanner's kubectl env (not os.environ)
        if proxy_url and hasattr(scanner, 'kubectl') and hasattr(scanner.kubectl, '_env'):
            scanner.kubectl._env['HTTPS_PROXY'] = proxy_url
            scanner.kubectl._env['https_proxy'] = proxy_url

        try:
            resources = scanner.scan_cluster()
        except Exception as scan_err:
            # Permission denied or no nodes — return empty result
            return {
                'nodes': [],
                'summary': {
                    'total_gpus': 0,
                    'gpus_in_use': 0,
                    'gpus_available': 0,
                    'gpu_node_count': 0,
                    'node_count': 0,
                    'gpu_model': 'unknown',
                    'gpu_vendor': 'unknown',
                    'gpu_memory_per_gpu_mb': 0,
                    'total_cpu_cores': 0,
                    'total_memory_gb': 0,
                    'has_rdma': False,
                    'cloud_provider': 'unknown',
                    'cpu_model': 'unknown',
                },
                'scan_warning': f'Limited permissions: {str(scan_err)[:200]}',
            }

        # Check for missing infrastructure components (using the scanner's kubectl which respects kubeconfig)
        from core.cluster_scan_service import get_infra_warnings
        warnings = get_infra_warnings(scanner.kubectl)

        # Count GPUs in use (device plugin + DRA ResourceClaims)
        from core.cluster_scan_service import count_gpus_in_use
        gpus_in_use = count_gpus_in_use(scanner.kubectl)

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

        result = {
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
        if warnings:
            result['infra_warnings'] = warnings

        # Scan infrastructure component versions
        from core.cluster_scan_service import scan_versions_safe, zero_gpu_warning
        versions = scan_versions_safe(scanner.kubectl)
        if versions:
            result['infra_versions'] = versions

        # GPU warning
        gpu_warning = zero_gpu_warning(resources.total_gpus, versions)
        if gpu_warning:
            result['summary']['gpu_warning'] = gpu_warning

        return result
    finally:
        if kubeconfig_path:
            os.unlink(kubeconfig_path)


def _is_oc() -> bool:
    return False
