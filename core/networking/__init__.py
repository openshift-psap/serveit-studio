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
    selected_dra_classes: Optional[List[str]] = None,
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

    # Pass selected DRA device classes through to template rendering
    values['selected_dra_classes'] = selected_dra_classes or []

    values['extra_device_resources'] = []
    values['rdma_nics_per_node'] = rdma_nics_per_node
    values['rdma_network_annotation'] = None

    if network_type in ('nad', 'nmstate') and rdma_device_resources:
        for resource_key in rdma_device_resources:
            values['extra_device_resources'].append({
                'key': resource_key,
                'value': '1'
            })
        values['rdma_network_annotation'] = rdma_network_annotation
    elif network_type == 'sriov_multinic' and rdma_device_resources:
        for resource_key in rdma_device_resources:
            values['extra_device_resources'].append({
                'key': resource_key,
                'value': '1'
            })
        values['rdma_network_annotation'] = rdma_network_annotation

    values['use_anti_affinity'] = (
        network_type in ('nad', 'nmstate')
        and len(rdma_device_resources) > 0
    )

    return values


def scan_available_networks(kubectl_runner, namespace: str = None) -> List[Dict[str, Any]]:
    """Scan the cluster and return ALL available network types.

    Runs all kubectl queries in parallel for speed. Each query is
    independent — failure of one doesn't block the others.

    Args:
        kubectl_runner: KubectlRunner instance for cluster queries
        namespace: Target namespace for NAD discovery

    Returns:
        List of dicts: {id, name, description, available, reason, rdma}
    """
    import json
    from concurrent.futures import ThreadPoolExecutor, as_completed

    results = {}

    def _query(name, args):
        try:
            r = kubectl_runner.run(args, check=False)
            return name, r
        except Exception:
            return name, None

    # Fire all queries in parallel
    queries = {
        'nad_api': ['api-resources', '--api-group=k8s.cni.cncf.io'],
        'dra_classes': ['get', 'deviceclass', '-o', 'json'],
        'nodes': ['get', 'nodes', '-o', 'json'],
        'nmstate_api': ['api-resources', '--api-group=nmstate.io'],
    }
    if namespace:
        queries['nads'] = ['get', 'net-attach-def', '-n', namespace, '-o', 'json']
        queries['sriov_policies'] = ['get', 'sriovnetworknodepolicies', '-n',
                                     'openshift-sriov-network-operator', '-o', 'json']

    with ThreadPoolExecutor(max_workers=len(queries)) as pool:
        futures = {pool.submit(_query, name, args): name for name, args in queries.items()}
        for f in as_completed(futures):
            name, r = f.result()
            results[name] = r

    # Parse results
    nad_r = results.get('nad_api')
    nad_available = nad_r and nad_r.returncode == 0 and 'network-attachment-definitions' in nad_r.stdout

    dra_r = results.get('dra_classes')
    dra_device_classes = []
    dra_available = False
    if dra_r and dra_r.returncode == 0 and dra_r.stdout.strip():
        try:
            dra_items = json.loads(dra_r.stdout).get('items', [])
        except Exception:
            dra_items = []
        for item in dra_items:
            name = item.get('metadata', {}).get('name', '')
            if not name:
                continue
            # Classify by inspecting the CEL selector expression — works regardless of naming
            selectors = item.get('spec', {}).get('selectors', [])
            cel_expr = ' '.join(
                s.get('cel', {}).get('expression', '') for s in selectors
            ).lower()
            # NIC: driver is dra.net, or expression references nic pairing
            has_nic = ('dra.net' in cel_expr or '-nic-' in cel_expr or
                       '"nic"' in cel_expr or "'nic'" in cel_expr or 'rdma' in cel_expr)
            # GPU: expression references gpu but not compute-domain (which is internal infra)
            has_gpu = ('gpu' in cel_expr and 'compute-domain' not in cel_expr)
            if has_gpu and has_nic:
                kind = 'gpu_nic_pair'
            elif has_gpu:
                kind = 'gpu'
            elif has_nic:
                kind = 'nic'
            else:
                kind = 'unknown'
            dra_device_classes.append({'name': name, 'kind': kind})
        dra_device_classes = sorted(dra_device_classes, key=lambda x: x['name'])
        dra_available = any(c['kind'] in ('gpu_nic_pair', 'nic') for c in dra_device_classes)

    nodes_r = results.get('nodes')
    shared_available = False
    shared_resources = set()
    if nodes_r and nodes_r.returncode == 0:
        try:
            for node in json.loads(nodes_r.stdout).get('items', []):
                for key in node.get('status', {}).get('allocatable', {}):
                    if key.startswith('rdma/') or key == 'nvidia.com/roce':
                        shared_resources.add(key)
                        shared_available = True
        except Exception:
            pass

    nads_r = results.get('nads')
    available_nads = []
    sriov_multinic_available = False
    if nads_r and nads_r.returncode == 0:
        try:
            seen = set()
            for item in json.loads(nads_r.stdout).get('items', []):
                nad_name = item['metadata']['name']
                nad_ns = item['metadata']['namespace']
                if nad_name not in seen:
                    available_nads.append({'name': nad_name, 'namespace': nad_ns})
                    seen.add(nad_name)
                if nad_name in ('multi-nic-inference', 'multi-nic-compute'):
                    sriov_multinic_available = True
        except Exception:
            pass

    nmstate_r = results.get('nmstate_api')
    nmstate_available = nmstate_r and nmstate_r.returncode == 0 and 'nodenetworkstate' in nmstate_r.stdout.lower()

    sriov_policies = []
    sriov_r = results.get('sriov_policies')
    if sriov_r and sriov_r.returncode == 0:
        try:
            for item in json.loads(sriov_r.stdout).get('items', []):
                name = item['metadata']['name']
                if name == 'default':
                    continue
                spec = item.get('spec', {})
                sriov_policies.append({
                    'name': name,
                    'resourceName': spec.get('resourceName', ''),
                    'numVfs': spec.get('numVfs', 0),
                    'mtu': spec.get('mtu', 1500),
                    'isRdma': spec.get('isRdma', False),
                    'deviceType': spec.get('deviceType', 'netdevice'),
                    'vendor': spec.get('nicSelector', {}).get('vendor', ''),
                    'deviceID': spec.get('nicSelector', {}).get('deviceID', ''),
                })
        except Exception:
            pass

    # Build network list
    networks = [
        {'id': 'eth0', 'name': 'Pod Network (TCP)',
         'description': 'Standard Kubernetes pod networking. No RDMA — uses TCP for GPU communication.',
         'available': True, 'reason': '', 'rdma': False},
        {'id': 'nad', 'name': 'NAD (Multus CNI)',
         'description': 'Network Attachment Definitions via Multus. Supports SR-IOV, host-device, and macvlan plugins for RDMA.',
         'available': nad_available, 'reason': '' if nad_available else 'Multus CNI not installed', 'rdma': True},
        {'id': 'dra', 'name': 'DRA (DRANET)',
         'description': 'Dynamic Resource Allocation with GPU+NIC PCIe affinity.',
         'available': dra_available, 'reason': '' if dra_available else 'No DRA device classes found', 'rdma': True,
         'device_classes': dra_device_classes},
        {'id': 'shared_device', 'name': 'Shared Device Plugin',
         'description': 'RDMA via pre-configured device plugin. Pods request RDMA resources directly.',
         'available': shared_available, 'reason': '' if shared_available else 'No rdma/* resources found', 'rdma': True,
         'shared_resources': sorted(shared_resources)},
        {'id': 'sriov_multinic', 'name': 'SR-IOV',
         'description': 'RoCE RDMA via SR-IOV. Creates network interfaces per pod for GPU-aware RDMA routing.',
         'available': sriov_multinic_available or (shared_available and bool(sriov_policies)),
         'reason': '' if (sriov_multinic_available or sriov_policies) else 'No SR-IOV NADs or policies found', 'rdma': True},
        {'id': 'nmstate', 'name': 'NMState',
         'description': 'RDMA via kubernetes-nmstate. Configures host network interfaces declaratively.',
         'available': nmstate_available, 'reason': '' if nmstate_available else 'kubernetes-nmstate API not found', 'rdma': True},
    ]

    for net in networks:
        if net['rdma'] and net['available']:
            if net['id'] in ('nad', 'nmstate', 'sriov_multinic'):
                net['available_nads'] = available_nads
            if net['id'] == 'sriov_multinic' and sriov_policies:
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
