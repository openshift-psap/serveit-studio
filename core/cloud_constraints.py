"""
Cloud Provider Constraints

Defines infrastructure-specific constraints for different cloud providers.
This module detects the cloud provider and applies provider-specific limits.
"""

import logging
from dataclasses import dataclass
from typing import Optional
from enum import Enum

logger = logging.getLogger(__name__)


class CloudProvider(Enum):
    """Supported cloud providers."""
    UNKNOWN = "unknown"
    IBM_CLOUD = "ibm-cloud"
    COREWEAVE = "coreweave"
    AWS = "aws"
    GCP = "gcp"
    AZURE = "azure"
    ON_PREM = "on-prem"


@dataclass
class PodConstraints:
    """Pod deployment constraints for a cloud provider."""
    max_prefill_pods_per_node: Optional[int] = None  # None = no limit
    max_decode_pods_per_node: Optional[int] = None   # None = no limit
    max_total_pods_per_node: Optional[int] = None    # None = no limit
    description: str = ""


class CloudConstraints:
    """Cloud provider constraint definitions."""

    # Cloud-specific constraints
    CONSTRAINTS = {
        CloudProvider.IBM_CLOUD: PodConstraints(
            max_prefill_pods_per_node=1,
            max_decode_pods_per_node=1,
            max_total_pods_per_node=1,
            description="IBM Cloud: Max 1 PD pod (prefill OR decode) per node"
        ),
        CloudProvider.COREWEAVE: PodConstraints(
            description="CoreWeave: No pod-per-node constraints"
        ),
        CloudProvider.AWS: PodConstraints(
            description="AWS: No special PD constraints"
        ),
        CloudProvider.GCP: PodConstraints(
            description="GCP: No special PD constraints"
        ),
        CloudProvider.AZURE: PodConstraints(
            description="Azure: No special PD constraints"
        ),
        CloudProvider.ON_PREM: PodConstraints(
            description="On-premises: No special PD constraints"
        ),
        CloudProvider.UNKNOWN: PodConstraints(
            description="Unknown cloud provider: No special PD constraints"
        ),
    }

    @staticmethod
    def detect_cloud_provider(kubectl_runner=None) -> CloudProvider:
        """
        Detect cloud provider from cluster infrastructure.

        Args:
            kubectl_runner: Optional KubectlRunner instance for API calls

        Returns:
            CloudProvider enum
        """
        # Try OpenShift infrastructure resource first (most reliable)
        if kubectl_runner:
            try:
                result = kubectl_runner.run(
                    ['get', 'infrastructure', 'cluster', '-o', 'jsonpath={.status.platform}'],
                    check=False
                )
                if result.returncode == 0:
                    platform = result.stdout.strip()
                    if platform == "IBMCloud":
                        logger.info("Detected IBM Cloud from infrastructure.config.openshift.io/cluster")
                        return CloudProvider.IBM_CLOUD
                    elif platform in ["AWS", "aws"]:
                        logger.info("Detected AWS from infrastructure.config.openshift.io/cluster")
                        return CloudProvider.AWS
                    elif platform in ["GCP", "gcp"]:
                        logger.info("Detected GCP from infrastructure.config.openshift.io/cluster")
                        return CloudProvider.GCP
                    elif platform in ["Azure", "azure"]:
                        logger.info("Detected Azure from infrastructure.config.openshift.io/cluster")
                        return CloudProvider.AZURE
            except Exception as e:
                logger.debug(f"Could not query infrastructure resource: {e}")

        # Try CoreWeave detection via node labels (vanilla K8s, no OpenShift)
        if kubectl_runner:
            try:
                result = kubectl_runner.run(
                    ['get', 'nodes', '-l', 'gpu.coreweave.cloud/vendor=NVIDIA',
                     '-o', 'jsonpath={.items[0].metadata.name}'],
                    check=False
                )
                if result.returncode == 0 and result.stdout.strip():
                    logger.info("Detected CoreWeave from gpu.coreweave.cloud node labels")
                    return CloudProvider.COREWEAVE
            except Exception as e:
                logger.debug(f"Could not query for CoreWeave node labels: {e}")

        logger.info("Cloud provider not detected, assuming on-premises or unknown")
        return CloudProvider.UNKNOWN

    @staticmethod
    def get_constraints(provider: CloudProvider) -> PodConstraints:
        """Get constraints for a cloud provider."""
        return CloudConstraints.CONSTRAINTS.get(provider, CloudConstraints.CONSTRAINTS[CloudProvider.UNKNOWN])


def validate_pd_config(
    cloud_provider: CloudProvider,
    prefill_pods: int,
    decode_pods: int,
    num_nodes: int,
    prefill_tp: int,
    decode_tp: int
) -> tuple[bool, str]:
    """
    Validate if a PD configuration is valid for the given cloud provider.

    Args:
        cloud_provider: Detected cloud provider
        prefill_pods: Number of prefill pods
        decode_pods: Number of decode pods
        num_nodes: Number of nodes in cluster
        prefill_tp: Prefill tensor parallelism
        decode_tp: Decode tensor parallelism

    Returns:
        (is_valid, reason) - True if valid, False with reason if invalid
    """
    constraints = CloudConstraints.get_constraints(cloud_provider)

    # Calculate pods per node (ceil division)
    prefill_pods_per_node = (prefill_pods + num_nodes - 1) // num_nodes
    decode_pods_per_node = (decode_pods + num_nodes - 1) // num_nodes

    # Check prefill constraint
    if constraints.max_prefill_pods_per_node is not None:
        if prefill_pods_per_node > constraints.max_prefill_pods_per_node:
            return False, f"Prefill pods per node ({prefill_pods_per_node}) exceeds limit ({constraints.max_prefill_pods_per_node})"

    # Check decode constraint
    if constraints.max_decode_pods_per_node is not None:
        if decode_pods_per_node > constraints.max_decode_pods_per_node:
            return False, f"Decode pods per node ({decode_pods_per_node}) exceeds limit ({constraints.max_decode_pods_per_node})"

    # Check total pods constraint (IBM Cloud: prefill + decode pods cannot coexist on same node)
    if constraints.max_total_pods_per_node is not None:
        # For IBM Cloud: if both prefill and decode exist, they can't share nodes
        if prefill_pods > 0 and decode_pods > 0:
            total_pods_needed = prefill_pods + decode_pods
            if total_pods_needed > num_nodes * constraints.max_total_pods_per_node:
                return False, f"Total PD pods ({total_pods_needed}) exceeds node capacity ({num_nodes} nodes × {constraints.max_total_pods_per_node} pod/node)"

    return True, ""
