"""
Base classes and interfaces for network creation.

Provides abstract interface for creating different types of Kubernetes network resources:
- NAD (NetworkAttachmentDefinition) via Multus CNI
- DRA (Dynamic Resource Allocation) via ResourceClaimTemplates
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Any

logger = logging.getLogger(__name__)


class NetworkType(Enum):
    """Supported network types."""
    NAD = "nad"  # NetworkAttachmentDefinition (Multus CNI)
    DRA = "dra"  # Dynamic Resource Allocation (DRANET)
    SHARED_DEVICE = "shared_device"  # Shared RDMA device plugin (e.g., CoreWeave rdma/ib)


class RDMAType(Enum):
    """RDMA transport types."""
    INFINIBAND = "infiniband"
    ROCE = "roce"
    VIRTIO_ROCE = "virtio-roce"  # IBM Cloud VirtIO RoCE
    TCP = "tcp"  # Fallback (no RDMA)


@dataclass
class NetworkConfig:
    """
    Network configuration for a deployment.

    This is a high-level configuration that gets translated into
    provider-specific network resources (NAD or DRA).
    """
    # Network type
    network_type: NetworkType = NetworkType.NAD

    # RDMA configuration
    rdma_type: RDMAType = RDMAType.VIRTIO_ROCE
    rdma_enabled: bool = True

    # Device/interface configuration
    device_name: Optional[str] = None  # e.g., "enp233s0" for NAD
    device_plugin: Optional[str] = None  # e.g., "nvidia.com/roce"

    # Network parameters
    mtu: int = 9000
    ipam_type: str = "dhcp"  # dhcp, static, whereabouts

    # Routing configuration
    gateway: Optional[str] = None
    routes: List[Dict[str, Any]] = field(default_factory=list)
    rules: List[Dict[str, Any]] = field(default_factory=list)

    # DRA-specific (DRANET)
    num_rails: int = 8  # Number of GPU+NIC pairs (H100 = 8)
    ip_prefix: str = "10.0."  # IP prefix for rail selection
    pcie_affinity: bool = True  # Ensure GPU+NIC on same PCIe root

    # NAD-specific (Multus)
    cni_plugins: List[str] = field(default_factory=lambda: ["host-device", "sbr-custom", "tuning"])

    # Additional config
    annotations: Dict[str, str] = field(default_factory=dict)
    labels: Dict[str, str] = field(default_factory=dict)
    extra_config: Dict[str, Any] = field(default_factory=dict)


@dataclass
class NetworkResource:
    """
    A generated network resource (NAD or DRA).

    Contains the Kubernetes resource definition and metadata.
    """
    # Resource type
    resource_type: NetworkType

    # K8s resource
    api_version: str
    kind: str
    metadata: Dict[str, Any]
    spec: Dict[str, Any]

    # Helper info
    name: str
    namespace: str
    description: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Convert to Kubernetes resource dict."""
        return {
            'apiVersion': self.api_version,
            'kind': self.kind,
            'metadata': self.metadata,
            'spec': self.spec
        }


class BaseNetworkCreator(ABC):
    """
    Abstract base class for network creators.

    Each implementation handles a specific network type:
    - NADNetworkCreator: NetworkAttachmentDefinition (Multus)
    - DRANetworkCreator: ResourceClaimTemplate (DRANET)
    """

    def __init__(self, config: NetworkConfig):
        """
        Initialize network creator.

        Args:
            config: High-level network configuration
        """
        self.config = config
        self.logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

    @abstractmethod
    def get_network_type(self) -> NetworkType:
        """
        Get the network type this creator handles.

        Returns:
            NetworkType enum value
        """
        pass

    @abstractmethod
    def create_network_resources(
        self,
        namespace: str,
        base_name: str,
        num_resources: int = 1
    ) -> List[NetworkResource]:
        """
        Create network resources.

        Args:
            namespace: Kubernetes namespace
            base_name: Base name for resources (e.g., "llm-d-rdma")
            num_resources: Number of resources to create (e.g., 8 rails for DRA)

        Returns:
            List of NetworkResource objects
        """
        pass

    @abstractmethod
    def get_pod_annotations(self, resource_names: List[str]) -> Dict[str, str]:
        """
        Get pod annotations for using these network resources.

        For NAD: Returns {"k8s.v1.cni.cncf.io/networks": "net1,net2"}
        For DRA: Returns {} (uses resourceClaims instead)

        Args:
            resource_names: List of network resource names

        Returns:
            Dictionary of pod annotations
        """
        pass

    @abstractmethod
    def get_pod_resource_claims(self, resource_names: List[str]) -> List[Dict[str, Any]]:
        """
        Get pod resourceClaims for using these network resources.

        For NAD: Returns [] (uses annotations instead)
        For DRA: Returns list of resourceClaim dicts

        Args:
            resource_names: List of network resource names

        Returns:
            List of resourceClaim definitions
        """
        pass

    def validate_config(self) -> tuple[bool, str]:
        """
        Validate network configuration.

        Returns:
            (is_valid, error_message)
        """
        if self.config.rdma_enabled and self.config.rdma_type == RDMAType.TCP:
            return False, "RDMA enabled but rdma_type is TCP"

        if self.config.network_type == NetworkType.DRA and self.config.num_rails < 1:
            return False, "DRA requires at least 1 rail"

        if self.config.network_type == NetworkType.NAD and not self.config.device_name:
            return False, "NAD requires device_name (e.g., 'enp233s0')"

        return True, ""

    def get_resource_requirements(self) -> Dict[str, Any]:
        """
        Get pod resource requirements for this network.

        Returns:
            Dictionary with limits/requests for network devices
        """
        requirements = {}

        if self.config.device_plugin:
            # e.g., nvidia.com/roce: 1
            requirements[self.config.device_plugin] = 1

        return requirements
