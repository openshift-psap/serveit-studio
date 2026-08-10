"""
Shared device plugin network creator.

For environments like CoreWeave where RDMA is provided via a shared
Kubernetes device plugin (e.g., rdma/ib) rather than NADs or DRA.
No network CRDs are needed — the device plugin handles RDMA access.
"""

import logging
from typing import Dict, List, Any

from .base import BaseNetworkCreator, NetworkType, NetworkResource

logger = logging.getLogger(__name__)


class SharedDeviceNetworkCreator(BaseNetworkCreator):
    """
    Network creator for shared RDMA device plugin environments.

    Multiple pods share RDMA devices on the same node via a device plugin
    (e.g., rdma/ib on CoreWeave). No NetworkAttachmentDefinitions or
    DRA ResourceClaims are needed.
    """

    def get_network_type(self) -> NetworkType:
        return NetworkType.SHARED_DEVICE

    def create_network_resources(
        self,
        namespace: str,
        base_name: str,
        num_resources: int = 1
    ) -> List[NetworkResource]:
        return []

    def get_pod_annotations(self, resource_names: List[str]) -> Dict[str, str]:
        return {}

    def get_pod_resource_claims(self, resource_names: List[str]) -> List[Dict[str, Any]]:
        return []

    def validate_config(self) -> tuple[bool, str]:
        if not self.config.device_plugin:
            return False, "SharedDevice requires device_plugin (e.g., 'rdma/ib')"
        return True, ""
