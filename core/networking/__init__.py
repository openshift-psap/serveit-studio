"""
Modular networking system for Inftune Studio.

Supports multiple network types:
- NAD (NetworkAttachmentDefinition) - Multus CNI
- DRA (Dynamic Resource Allocation) - DRANET
- SharedDevice - Shared RDMA device plugin (e.g., rdma/ib)
"""

from __future__ import annotations
from typing import Dict, List, Any, Optional, TYPE_CHECKING

from .base import BaseNetworkCreator, NetworkConfig, NetworkType, RDMAType, NetworkResource
from .nad import NADNetworkCreator
from .dra import DRANetworkCreator
from .shared_device import SharedDeviceNetworkCreator

if TYPE_CHECKING:
    from ..system_scanner import NodeResources


def detect_rdma_device_resources(
    nodes: List[NodeResources],
    network_type: str,
) -> List[str]:
    """
    Detect RDMA device plugin resource keys from node allocatable.

    Reads actual rdma/* keys present on all RDMA-capable nodes.
    DRA handles GPU+NIC pairing internally, so returns empty for DRA.

    Args:
        nodes: List of scanned NodeResources
        network_type: 'dra', 'nad', or 'shared_device'

    Returns:
        Sorted list of RDMA resource keys common to all RDMA nodes
        (e.g., ['rdma/ib'] or ['rdma/ib-1', 'rdma/ib-2', ...])
    """
    if network_type == 'dra':
        return []

    resource_sets = []
    for node in nodes:
        if node.has_rdma and node.rdma_devices:
            resource_sets.append(set(node.rdma_devices))

    if not resource_sets:
        return []

    common_resources = resource_sets[0]
    for rs in resource_sets[1:]:
        common_resources &= rs

    return sorted(common_resources)


def compute_network_values(
    network_type: str,
    rdma_device_resources: Optional[List[str]] = None,
    rdma_nics_per_node: int = 0,
) -> Dict[str, Any]:
    """
    Produce template values from network configuration.

    Args:
        network_type: 'dra', 'nad', or 'shared_device'
        rdma_device_resources: List of RDMA device plugin resource keys from
            node allocatable. Layout depends on NicClusterPolicy config:
            - Single pool: ['rdma/ib'] — one shared resource for all NICs
            - Per-NIC: ['rdma/ib-1', 'rdma/ib-2', ...] — separate resource per NIC
            Physical NICs are divided evenly across resources.
        rdma_nics_per_node: Physical NIC count per node from scanner.

    Returns:
        Dict with keys:
          gpu_resource_key: K8s resource key for GPU allocation
          extra_device_resources: List of {key, value} for additional device plugins
          use_anti_affinity: Whether PD pods need anti-affinity scheduling
          rdma_nics_per_node: Physical NIC count (for downstream TP-based calculation)
    """
    if rdma_device_resources is None:
        rdma_device_resources = []

    values: Dict[str, Any] = {}

    if network_type == 'dra':
        values['gpu_resource_key'] = 'dra.llm-d.io/gpu-nic-pair'
    else:
        values['gpu_resource_key'] = 'nvidia.com/gpu'

    values['extra_device_resources'] = []
    values['rdma_nics_per_node'] = rdma_nics_per_node
    # NAD mode: request exclusive RDMA NICs per pod (SR-IOV VFs)
    # DRA/shared_device: RDMA is shared, don't request per-pod resources
    if network_type == 'nad' and rdma_device_resources:
        for resource_key in rdma_device_resources:
            values['extra_device_resources'].append({
                'key': resource_key,
                'value': '1'
            })

    # NAD with device plugin = IBM Cloud exclusive NICs → anti-affinity needed
    # NAD without device plugin = baremetal → no anti-affinity
    # DRA / SharedDevice → no anti-affinity
    values['use_anti_affinity'] = (
        network_type == 'nad'
        and len(rdma_device_resources) > 0
    )

    return values


def scan_available_networks(kubectl_runner) -> List[Dict[str, Any]]:
    """Scan the cluster and return ALL available network types.

    Always includes eth0 (pod network). Checks for NAD CRDs,
    DRA device classes, and shared RDMA device plugins.

    Args:
        kubectl_runner: KubectlRunner instance for cluster queries

    Returns:
        List of dicts: {id, name, description, available, reason, rdma}
    """
    networks = []

    # 1. eth0 — always available
    networks.append({
        'id': 'eth0',
        'name': 'Pod Network (TCP)',
        'description': 'Standard Kubernetes pod networking. No RDMA — uses TCP for GPU communication. Works everywhere but slower for multi-node.',
        'available': True,
        'reason': '',
        'rdma': False,
    })

    # 2. NAD (Multus CNI)
    try:
        r = kubectl_runner.run(['api-resources', '--api-group=k8s.cni.cncf.io'], check=False)
        nad_available = r.returncode == 0 and 'network-attachment-definitions' in r.stdout
    except Exception:
        nad_available = False
    networks.append({
        'id': 'nad',
        'name': 'NAD (Multus CNI)',
        'description': 'Network Attachment Definitions via Multus. Supports SR-IOV, host-device, and macvlan plugins for RDMA.',
        'available': nad_available,
        'reason': '' if nad_available else 'Multus CNI not installed (k8s.cni.cncf.io API not found)',
        'rdma': True,
    })

    # 3. DRA (DRANET) — look for gpu-nic-pair or dranet-specific device classes
    dra_available = False
    try:
        r = kubectl_runner.run(['get', 'deviceclass', '-o', 'jsonpath={.items[*].metadata.name}'], check=False)
        if r.returncode == 0 and r.stdout.strip():
            class_names = r.stdout.strip().split()
            dra_available = any('nic' in c or 'dranet' in c or 'dra-net' in c or 'gpu-nic' in c
                                for c in class_names)
    except Exception:
        pass
    networks.append({
        'id': 'dra',
        'name': 'DRA (DRANET)',
        'description': 'Dynamic Resource Allocation with GPU+NIC PCIe affinity. Automatically pairs GPUs with closest network interface.',
        'available': dra_available,
        'reason': '' if dra_available else 'No DRA device classes found on cluster',
        'rdma': True,
    })

    # 4. SharedDevice (RDMA device plugin)
    shared_available = False
    shared_resource = ''
    try:
        r = kubectl_runner.run(['get', 'nodes', '-o',
            'jsonpath={.items[*].status.allocatable}'], check=False)
        if r.returncode == 0:
            import json
            # Parse each node's allocatable to find rdma/* resources
            for node_alloc_str in r.stdout.strip().split(' '):
                try:
                    alloc = json.loads(node_alloc_str) if node_alloc_str.startswith('{') else {}
                except Exception:
                    alloc = {}
                for key in alloc:
                    if key.startswith('rdma/'):
                        shared_available = True
                        shared_resource = key
                        break
                if shared_available:
                    break
    except Exception:
        pass
    if not shared_available:
        # Also check via node JSON
        try:
            r = kubectl_runner.run(['get', 'nodes', '-o', 'json'], check=False)
            if r.returncode == 0:
                import json
                data = json.loads(r.stdout)
                for node in data.get('items', []):
                    alloc = node.get('status', {}).get('allocatable', {})
                    for key in alloc:
                        if key.startswith('rdma/'):
                            shared_available = True
                            shared_resource = key
                            break
                    if shared_available:
                        break
        except Exception:
            pass

    resource_label = f' ({shared_resource})' if shared_resource else ''
    networks.append({
        'id': 'shared_device',
        'name': f'Shared Device Plugin{resource_label}',
        'description': 'RDMA via pre-configured device plugin. Pods request RDMA resources directly — no CRDs needed.',
        'available': shared_available,
        'reason': '' if shared_available else 'No rdma/* resources found in node allocatable',
        'rdma': True,
    })

    return networks


__all__ = [
    'BaseNetworkCreator',
    'NetworkConfig',
    'NetworkType',
    'RDMAType',
    'NetworkResource',
    'NADNetworkCreator',
    'DRANetworkCreator',
    'SharedDeviceNetworkCreator',
    'detect_rdma_device_resources',
    'compute_network_values',
    'scan_available_networks',
]
