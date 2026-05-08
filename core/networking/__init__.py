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
    if network_type != 'dra' and rdma_device_resources:
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
]
