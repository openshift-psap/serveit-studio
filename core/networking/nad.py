"""
NetworkAttachmentDefinition (NAD) creator for Multus CNI.

Creates NAD resources for:
- IBM Cloud VirtIO RoCE
- Bare metal InfiniBand
- Generic DHCP/static IP configurations
"""

import json
import logging
from typing import Dict, List, Any

from .base import (
    BaseNetworkCreator,
    NetworkConfig,
    NetworkResource,
    NetworkType,
    RDMAType
)

logger = logging.getLogger(__name__)


class NADNetworkCreator(BaseNetworkCreator):
    """
    NetworkAttachmentDefinition creator using Multus CNI.

    Creates NAD resources with:
    - host-device CNI plugin (attach physical NIC to pod)
    - IPAM (DHCP or static)
    - Source-based routing (sbr-custom)
    - Tuning (MTU, etc.)
    """

    def get_network_type(self) -> NetworkType:
        """Get network type."""
        return NetworkType.NAD

    def create_network_resources(
        self,
        namespace: str,
        base_name: str,
        num_resources: int = 1
    ) -> List[NetworkResource]:
        """
        Create NAD resources.

        Args:
            namespace: Kubernetes namespace
            base_name: Base name (e.g., "ibm-rdma")
            num_resources: Number of NADs to create (e.g., 8 for multi-port)

        Returns:
            List of NetworkResource objects
        """
        resources = []

        for i in range(num_resources):
            port_num = i + 1
            nad_name = f"{base_name}-port-{port_num}"

            # Build CNI config
            cni_config = self._build_cni_config(nad_name, port_num)

            # Build metadata
            metadata = {
                'name': nad_name,
                'namespace': namespace,
                'labels': {
                    'app': base_name,
                    'auto-generated': 'true',
                    'port-number': str(port_num),
                    **self.config.labels
                },
                'annotations': {
                    'description': self._get_description(port_num),
                    **self.config.annotations
                }
            }

            # Add RDMA labels if enabled
            if self.config.rdma_enabled:
                metadata['labels']['rdma-enabled'] = 'true'
                metadata['labels']['rdma-type'] = self.config.rdma_type.value

            # Create NetworkResource
            resource = NetworkResource(
                resource_type=NetworkType.NAD,
                api_version='k8s.cni.cncf.io/v1',
                kind='NetworkAttachmentDefinition',
                metadata=metadata,
                spec={'config': json.dumps(cni_config, indent=2)},
                name=nad_name,
                namespace=namespace,
                description=self._get_description(port_num)
            )

            resources.append(resource)
            self.logger.debug(f"Created NAD resource: {nad_name}")

        return resources

    def _build_cni_config(self, nad_name: str, port_num: int) -> Dict[str, Any]:
        """
        Build CNI configuration JSON.

        Args:
            nad_name: NAD resource name
            port_num: Port number (1-8)

        Returns:
            CNI config dictionary
        """
        plugins = []

        # 1. host-device plugin (attach physical NIC)
        host_device = {
            'type': 'host-device',
            'device': self._get_device_name(port_num),
        }

        if self.config.rdma_enabled:
            host_device['isRdma'] = True

        # Add IPAM
        host_device['ipam'] = self._build_ipam_config(port_num)

        plugins.append(host_device)

        # 2. Source-based routing (if gateway specified)
        if self.config.gateway:
            sbr_plugin = {
                'type': 'sbr-custom',
                'gateway': self.config.gateway,
                'addSourceHints': True
            }
            plugins.append(sbr_plugin)

        # 3. Tuning plugin (MTU, etc.)
        tuning_plugin = {
            'type': 'tuning',
            'name': 'rdma-tuning' if self.config.rdma_enabled else 'network-tuning',
            'mtu': self.config.mtu
        }
        plugins.append(tuning_plugin)

        # Build final config
        cni_config = {
            'cniVersion': '0.3.1',
            'name': nad_name,
            'plugins': plugins
        }

        return cni_config

    def _get_device_name(self, port_num: int) -> str:
        """
        Get device name for port number.

        Args:
            port_num: Port number (1-8)

        Returns:
            Device name (e.g., "enp233s0")
        """
        if self.config.device_name:
            # If specific device provided, use it
            # For multi-port, append port number
            if '{port}' in self.config.device_name:
                return self.config.device_name.replace('{port}', str(port_num))
            return self.config.device_name

        # Auto-generate based on RDMA type
        if self.config.rdma_type == RDMAType.VIRTIO_ROCE:
            # IBM Cloud VirtIO NICs
            # Port 1 = enp233s0, Port 2 = enp234s0, etc.
            base_pci = 233 + (port_num - 1)
            return f"enp{base_pci}s0"
        elif self.config.rdma_type == RDMAType.INFINIBAND:
            # InfiniBand devices
            return f"ib{port_num - 1}"
        else:
            # Generic
            return f"eth{port_num - 1}"

    def _build_ipam_config(self, port_num: int) -> Dict[str, Any]:
        """
        Build IPAM configuration.

        Args:
            port_num: Port number

        Returns:
            IPAM config dict
        """
        if self.config.ipam_type == "dhcp":
            return {'type': 'dhcp'}
        elif self.config.ipam_type == "static":
            # Static IP (would need to be configured per port)
            return {
                'type': 'static',
                'addresses': [
                    {
                        'address': f"{self.config.ip_prefix}{port_num}.10/24",
                        'gateway': self.config.gateway
                    }
                ]
            }
        elif self.config.ipam_type == "whereabouts":
            # Whereabouts IP pool
            return {
                'type': 'whereabouts',
                'range': f"{self.config.ip_prefix}0.0/16"
            }
        else:
            # Default to DHCP
            return {'type': 'dhcp'}

    def _get_description(self, port_num: int) -> str:
        """
        Get human-readable description.

        Args:
            port_num: Port number

        Returns:
            Description string
        """
        rdma_info = ""
        if self.config.rdma_enabled:
            rdma_info = f" with {self.config.rdma_type.value.upper()} RDMA support"

        device = self._get_device_name(port_num)
        return f"Port {port_num} - {device}{rdma_info} - Auto-generated by ServeIt Studio"

    def get_pod_annotations(self, resource_names: List[str]) -> Dict[str, str]:
        """
        Get pod annotations for NAD.

        Args:
            resource_names: List of NAD names

        Returns:
            Annotations dict with "k8s.v1.cni.cncf.io/networks"
        """
        if not resource_names:
            return {}

        # Join NAD names with commas
        networks_value = ','.join(resource_names)

        return {
            'k8s.v1.cni.cncf.io/networks': networks_value
        }

    def get_pod_resource_claims(self, resource_names: List[str]) -> List[Dict[str, Any]]:
        """
        Get pod resourceClaims (not used for NAD).

        Args:
            resource_names: List of NAD names (unused)

        Returns:
            Empty list (NAD uses annotations, not resourceClaims)
        """
        return []

    def get_dhcp_daemon_required(self) -> bool:
        """
        Check if DHCP daemon is required.

        Returns:
            True if IPAM type is DHCP
        """
        return self.config.ipam_type == "dhcp"
