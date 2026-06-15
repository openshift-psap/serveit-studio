"""
Modular networking system for ServeIt Studio.

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
    rdma_network_annotation: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Produce template values from network configuration.

    Args:
        network_type: 'dra', 'nad', 'shared_device', 'sriov_multinic', or 'eth0'
        rdma_device_resources: List of RDMA device plugin resource keys from
            node allocatable.
        rdma_nics_per_node: Physical NIC count per node from scanner.
        rdma_network_annotation: Multus NAD annotation JSON for sriov_multinic.

    Returns:
        Dict with keys:
          gpu_resource_key: K8s resource key for GPU allocation
          extra_device_resources: List of {key, value} for additional device plugins
          use_anti_affinity: Whether PD pods need anti-affinity scheduling
          rdma_nics_per_node: Physical NIC count
          rdma_network_annotation: Multus annotation string (if sriov_multinic)
    """
    if rdma_device_resources is None:
        rdma_device_resources = []

    values: Dict[str, Any] = {}

    if network_type == 'dra' and rdma_device_resources and 'dra.llm-d.io/gpu-nic-pair' in rdma_device_resources:
        values['gpu_resource_key'] = 'dra.llm-d.io/gpu-nic-pair'
    else:
        values['gpu_resource_key'] = 'nvidia.com/gpu'

    values['extra_device_resources'] = []
    values['rdma_nics_per_node'] = rdma_nics_per_node
    values['rdma_network_annotation'] = None

    if network_type == 'nad' and rdma_device_resources:
        for resource_key in rdma_device_resources:
            values['extra_device_resources'].append({
                'key': resource_key,
                'value': '1'
            })
    elif network_type == 'sriov_multinic' and rdma_device_resources:
        for resource_key in rdma_device_resources:
            values['extra_device_resources'].append({
                'key': resource_key,
                'value': '1'
            })
        values['rdma_network_annotation'] = rdma_network_annotation

    values['use_anti_affinity'] = (
        network_type == 'nad'
        and len(rdma_device_resources) > 0
    )

    return values


def scan_available_networks(kubectl_runner, namespace: str = None) -> List[Dict[str, Any]]:
    """Scan the cluster and return ALL available network types.

    Always includes eth0 (pod network). Checks for NAD CRDs,
    DRA device classes, and shared RDMA device plugins.

    Args:
        kubectl_runner: KubectlRunner instance for cluster queries
        namespace: Target namespace for NAD discovery (scans target ns only)

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

    networks.append({
        'id': 'shared_device',
        'name': 'Shared Device Plugin',
        'description': 'RDMA via pre-configured device plugin. Pods request RDMA resources directly — no CRDs needed.',
        'available': shared_available,
        'reason': '' if shared_available else 'No rdma/* resources found in node allocatable',
        'rdma': True,
    })

    # 5. SR-IOV multi-nic (multi-nic-cni operator)
    sriov_multinic_available = False
    if nad_available and shared_available and namespace:
        try:
            r = kubectl_runner.run(['get', 'net-attach-def', '-n', namespace,
                '-o', 'jsonpath={.items[*].metadata.name}'], check=False)
            if r.returncode == 0 and ('multi-nic-inference' in r.stdout or 'multi-nic-compute' in r.stdout):
                sriov_multinic_available = True
        except Exception:
            pass
    networks.append({
        'id': 'sriov_multinic',
        'name': 'SR-IOV',
        'description': 'RoCE RDMA via SR-IOV. Creates network interfaces per pod for GPU-aware RDMA routing.',
        'available': sriov_multinic_available,
        'reason': '' if sriov_multinic_available else 'multi-nic-cni NADs not found',
        'rdma': True,
    })

    # Scan available NADs in the target namespace
    available_nads = []
    if nad_available and namespace:
        try:
            r = kubectl_runner.run(['get', 'net-attach-def', '-n', namespace,
                '-o', 'json'], check=False)
            if r.returncode == 0:
                import json
                items = json.loads(r.stdout).get('items', [])
                seen = set()
                for item in items:
                    nad_name = item['metadata']['name']
                    nad_ns = item['metadata']['namespace']
                    if nad_name not in seen:
                        available_nads.append({'name': nad_name, 'namespace': nad_ns})
                        seen.add(nad_name)
        except Exception:
            pass

    # Scan SR-IOV policies for the NAD network type
    sriov_policies = []
    try:
        from .sriov import detect_sriov_policies
        sriov_policies = detect_sriov_policies(kubectl_runner)
    except Exception:
        pass

    # Attach NAD list and SR-IOV policies to each RDMA network type
    for net in networks:
        if net['rdma'] and net['available']:
            net['available_nads'] = available_nads
            if sriov_policies:
                net['sriov_policies'] = sriov_policies

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
