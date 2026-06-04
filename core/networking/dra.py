"""
Dynamic Resource Allocation (DRA) creator for DRANET.

Creates ResourceClaimTemplates for:
- IBM Cloud DRANET (bypasses pod-per-node constraints)
- GPU+NIC affinity (same PCIe root)
- Multi-rail RDMA configurations
"""

import logging
from typing import Dict, List, Any, Optional

from .base import (
    BaseNetworkCreator,
    NetworkConfig,
    NetworkResource,
    NetworkType,
    RDMAType
)

logger = logging.getLogger(__name__)


class DRANetworkCreator(BaseNetworkCreator):
    """
    Dynamic Resource Allocation creator for DRANET.

    Creates ResourceClaimTemplates that:
    - Request GPU + NIC pairs
    - Ensure PCIe affinity (GPU and NIC on same root)
    - Configure RDMA networking with routing
    - Support multi-rail configurations (up to 8 rails for H100)
    """

    def get_network_type(self) -> NetworkType:
        """Get network type."""
        return NetworkType.DRA

    def create_network_resources(
        self,
        namespace: str,
        base_name: str,
        num_resources: int = None
    ) -> List[NetworkResource]:
        """
        Create network resources for DRA.

        When using the DRA admission webhook (recommended), no ResourceClaimTemplates
        are created - the webhook handles everything automatically based on the
        dra.llm-d.io/gpu-nic-pair resource request.

        Args:
            namespace: Kubernetes namespace
            base_name: Base name (e.g., "gpu-nic")
            num_resources: Number of rails (defaults to config.num_rails)

        Returns:
            Empty list (webhook mode) or List of ResourceClaimTemplates (legacy mode)
        """
        import os

        # Check if using webhook mode (default)
        use_webhook = os.getenv('DRA_USE_WEBHOOK', 'true').lower() == 'true'

        if use_webhook:
            self.logger.info(
                "Using DRA admission webhook mode - no ResourceClaimTemplates created. "
                "Webhook will auto-generate based on dra.llm-d.io/gpu-nic-pair resource requests."
            )
            return []

        # Legacy manual mode (for clusters without webhook)
        self.logger.info("Using DRA legacy mode - creating ResourceClaimTemplates manually")
        self.logger.info(f"DEBUG create_network_resources: num_resources parameter={num_resources}")
        self.logger.info(f"DEBUG create_network_resources: self.config.num_rails={self.config.num_rails}")

        if num_resources is None:
            num_resources = self.config.num_rails

        self.logger.info(f"DEBUG create_network_resources: final num_resources={num_resources}")

        resources = []

        for rail_num in range(num_resources):
            template_name = f"{base_name}-rail{rail_num}-template"

            # Build ResourceClaimTemplate spec
            spec = self._build_resource_claim_template_spec(rail_num)

            # Build metadata
            metadata = {
                'name': template_name,
                'namespace': namespace,
                'labels': {
                    'app': base_name,
                    'rail-number': str(rail_num),
                    **self.config.labels
                },
                'annotations': {
                    'description': self._get_description(rail_num),
                    **self.config.annotations
                }
            }

            # Create NetworkResource
            resource = NetworkResource(
                resource_type=NetworkType.DRA,
                api_version='resource.k8s.io/v1',
                kind='ResourceClaimTemplate',
                metadata=metadata,
                spec=spec,
                name=template_name,
                namespace=namespace,
                description=self._get_description(rail_num)
            )

            resources.append(resource)
            self.logger.debug(f"Created DRA ResourceClaimTemplate: {template_name}")

        return resources

    def _build_resource_claim_template_spec(self, rail_num: int) -> Dict[str, Any]:
        """
        Build ResourceClaimTemplate spec.

        Args:
            rail_num: Rail number (0-7 for H100)

        Returns:
            Spec dictionary
        """
        # Calculate IP subnet for this rail
        # Rail 0 = 10.0.x.x, Rail 1 = 10.1.x.x, etc.
        ip_subnet = f"{self.config.ip_prefix.rstrip('.')}.{rail_num}."

        # Build network interface configuration
        interface_config = {
            'name': f'net{rail_num + 1}',
            'mtu': self.config.mtu
        }

        # Build routing configuration
        routes = self._build_routes(rail_num)
        rules = self._build_rules(rail_num)

        # Build DRA opaque parameters
        opaque_params = {
            'interface': interface_config,
            'routes': routes,
            'rules': rules
        }

        # Build device requests
        gpu_request = {
            'name': 'gpu',
            'exactly': {
                'allocationMode': 'ExactCount',
                'count': 1,
                'deviceClassName': 'gpu.nvidia.com'
            }
        }

        nic_request = {
            'name': 'nic',
            'exactly': {
                'allocationMode': 'ExactCount',
                'count': 1,
                'deviceClassName': 'dranet',
                'selectors': [
                    {
                        'cel': {
                            # CEL expression: filter NICs with RDMA + matching IP subnet
                            'expression': self._build_cel_expression(ip_subnet)
                        }
                    }
                ]
            }
        }

        # Build constraint (GPU+NIC must be on same PCIe root)
        constraint = None
        if self.config.pcie_affinity:
            constraint = {
                'matchAttribute': 'resource.kubernetes.io/pcieRoot',
                'requests': ['gpu', 'nic']
            }

        # Build spec
        spec = {
            'metadata': {},
            'spec': {
                'devices': {
                    'requests': [gpu_request, nic_request],
                    'config': [
                        {
                            'opaque': {
                                'driver': 'dra.net',
                                'parameters': opaque_params
                            },
                            'requests': ['nic']
                        }
                    ]
                }
            }
        }

        # Add constraint if enabled
        if constraint:
            spec['spec']['devices']['constraints'] = [constraint]

        return spec

    def _build_cel_expression(self, ip_subnet: str) -> str:
        """
        Build CEL expression for NIC selection.

        Args:
            ip_subnet: IP subnet prefix (e.g., "10.0.")

        Returns:
            CEL expression string
        """
        # Select NICs with RDMA enabled
        # NOTE: Kubernetes DRA typed attributes are objects with .bool/.string/.int fields
        # Must use has() check and field accessor: device.attributes["dra.net/rdma"].bool
        # IBM Cloud DRANET RDMA devices don't have ipv4 attributes, so we only filter by RDMA capability
        if self.config.rdma_enabled:
            return 'has(device.attributes["dra.net/rdma"].bool) && device.attributes["dra.net/rdma"].bool == true'
        else:
            # For non-RDMA, just select any dranet device
            return 'device.driver == "dra.net"'

    def _build_routes(self, rail_num: int) -> List[Dict[str, Any]]:
        """
        Build routing table entries.

        Args:
            rail_num: Rail number

        Returns:
            List of route dicts
        """
        routes = []

        # Calculate per-rail gateway (e.g., 10.0.0.1 for rail 0)
        rail_gateway = f"{self.config.ip_prefix.rstrip('.')}.{rail_num}.0.1"

        # Add route for local subnet (in custom routing table 100+rail_num)
        local_subnet = f"{self.config.ip_prefix.rstrip('.')}.{rail_num}.0.0/16"
        routes.append({
            'destination': local_subnet,
            'scope': 253,  # RT_SCOPE_LINK
            'table': 100 + rail_num
        })

        # Add routes to other rail subnets (via this rail's gateway)
        for other_rail in range(self.config.num_rails):
            if other_rail != rail_num:
                other_subnet = f"{self.config.ip_prefix.rstrip('.')}.{other_rail}.0.0/16"
                routes.append({
                    'destination': other_subnet,
                    'gateway': rail_gateway
                })

        # Default route via this rail's gateway (in custom table 100+rail_num)
        routes.append({
            'destination': '0.0.0.0/0',
            'gateway': rail_gateway,
            'table': 100 + rail_num
        })

        # Add custom routes from config
        routes.extend(self.config.routes)

        return routes

    def _build_rules(self, rail_num: int) -> List[Dict[str, Any]]:
        """
        Build routing policy rules.

        Args:
            rail_num: Rail number

        Returns:
            List of rule dicts
        """
        rules = []

        # Add rule: traffic from local subnet uses custom routing table 100+rail_num
        local_subnet = f"{self.config.ip_prefix.rstrip('.')}.{rail_num}.0.0/16"
        rules.append({
            'priority': 32765,
            'source': local_subnet,
            'table': 100 + rail_num
        })

        # Add custom rules from config
        rules.extend(self.config.rules)

        return rules

    def _get_description(self, rail_num: int) -> str:
        """
        Get human-readable description.

        Args:
            rail_num: Rail number

        Returns:
            Description string
        """
        rdma_info = ""
        if self.config.rdma_enabled:
            rdma_info = f" with {self.config.rdma_type.value.upper()} RDMA"

        return f"GPU+NIC Rail {rail_num}{rdma_info} - DRA ResourceClaimTemplate - Auto-generated by ServeIt Studio"

    def get_pod_annotations(self, resource_names: List[str]) -> Dict[str, str]:
        """
        Get pod annotations (not used for DRA).

        Args:
            resource_names: List of template names (unused)

        Returns:
            Empty dict (DRA uses resourceClaims, not annotations)
        """
        return {}

    def get_pod_resource_claims(self, resource_names: List[str]) -> List[Dict[str, Any]]:
        """
        Get pod resourceClaims for DRA.

        When using webhook mode, returns empty list (webhook injects claims).
        In legacy mode, returns manual resourceClaim definitions.

        Args:
            resource_names: List of ResourceClaimTemplate names

        Returns:
            List of resourceClaim definitions (empty in webhook mode)
        """
        import os

        # Webhook mode: let webhook inject resourceClaims
        if os.getenv('DRA_USE_WEBHOOK', 'true').lower() == 'true':
            return []

        # Legacy mode: manually inject resourceClaims
        claims = []

        for i, template_name in enumerate(resource_names):
            claim_name = f"gpu-nic-rail{i}"

            claims.append({
                'name': claim_name,
                'resourceClaimTemplateName': template_name
            })

        return claims

    def get_container_resource_claims(self, num_rails: int = None) -> List[Dict[str, str]]:
        """
        Get container-level resource claims.

        When using webhook mode, returns empty list (webhook handles claims).
        In legacy mode, returns manual claim references.

        Args:
            num_rails: Number of rails (defaults to config.num_rails)

        Returns:
            List of claim references for container.resources.claims (empty in webhook mode)
        """
        import os

        # Webhook mode: container resources use dra.llm-d.io/gpu-nic-pair, not claims
        if os.getenv('DRA_USE_WEBHOOK', 'true').lower() == 'true':
            return []

        # Legacy mode: manually inject container claims
        if num_rails is None:
            num_rails = self.config.num_rails

        claims = []

        for rail_num in range(num_rails):
            claims.append({
                'name': f"gpu-nic-rail{rail_num}"
            })

        return claims

    def get_resource_requirements(self) -> Dict[str, Any]:
        """
        Get resource requirements for DRA.

        In webhook mode: Returns dra.llm-d.io/gpu-nic-pair resource request
        that triggers the admission webhook to inject DRA resourceClaims.

        In legacy mode: Returns empty dict (uses manual resourceClaims).

        Returns:
            Flat resource requirements dict (will be added to both limits and requests)
        """
        import os

        # Webhook mode: request webhook resource to trigger mutation
        if os.getenv('DRA_USE_WEBHOOK', 'true').lower() == 'true':
            return {
                'dra.llm-d.io/gpu-nic-pair': str(self.config.num_rails)
            }

        # Legacy mode: empty (uses manual resourceClaims)
        return {}
