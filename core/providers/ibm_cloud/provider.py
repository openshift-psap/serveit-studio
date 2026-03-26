"""
IBM Cloud infrastructure provider.

Implements IBM Cloud-specific constraints, detection, and configuration.
Supports two profiles:
  - default: Standard IBM Cloud (1 pod per node limit)
  - dranet: DRANET bypass (removes pod-per-node limit)
"""

import yaml
import logging
from pathlib import Path
from typing import Optional, Tuple

from ..base import (
    BaseProvider,
    ProviderProfile,
    ProviderConstraints,
    NetworkConfig,
    MetricsConfig,
    CostModel,
    SearchSpace
)

logger = logging.getLogger(__name__)


class IBMCloudProvider(BaseProvider):
    """
    IBM Cloud infrastructure provider.

    Key characteristics:
    - Default: Max 1 PD pod (prefill OR decode) per node
    - DRANET profile: Bypasses pod-per-node limit using network virtualization
    - RDMA: Exposed as nvidia.com/roce via VirtIO
    - Detection: Via OpenShift infrastructure.config.openshift.io/cluster resource
    """

    def get_provider_id(self) -> str:
        """Return provider ID."""
        return "ibm_cloud"

    def get_display_name(self) -> str:
        """Return human-readable name."""
        return "IBM Cloud"

    def detect(self, kubectl_runner=None) -> bool:
        """
        Detect IBM Cloud via OpenShift infrastructure resource.

        Args:
            kubectl_runner: KubectlRunner for API queries

        Returns:
            True if IBM Cloud is detected
        """
        if not kubectl_runner:
            return False

        try:
            result = kubectl_runner.run(
                ['get', 'infrastructure', 'cluster',
                 '-o', 'jsonpath={.status.platform}'],
                check=False
            )
            if result.returncode == 0:
                platform = result.stdout.strip()
                if platform == "IBMCloud":
                    logger.info("Detected IBM Cloud from infrastructure.config.openshift.io/cluster")
                    return True
        except Exception as e:
            logger.debug(f"Could not query infrastructure resource: {e}")

        return False

    @staticmethod
    def detect_dranet(kubectl_runner) -> bool:
        """
        Detect if DRANET is installed on the cluster.

        DRANET bypasses IBM Cloud's 1 pod per node constraint by providing
        network virtualization via DRA (Dynamic Resource Allocation).

        Detection methods (in order):
        1. Check for dranet DeviceClass
        2. Check for DaemonSet in kube-system (requires RBAC)

        Args:
            kubectl_runner: KubectlRunner for API queries

        Returns:
            True if DRANET is available
        """
        if not kubectl_runner:
            return False

        # Method 1: Check for dranet DeviceClass (no RBAC needed)
        try:
            result = kubectl_runner.run(
                ['get', 'deviceclass', 'dranet', '--ignore-not-found'],
                check=False
            )
            if result.returncode == 0 and result.stdout.strip():
                logger.info("Detected DRANET DeviceClass - can bypass IBM Cloud pod-per-node constraints")
                return True
        except Exception as e:
            logger.debug(f"Could not query for DRANET DeviceClass: {e}")

        # Method 2: Check for DaemonSet (may require RBAC)
        try:
            result = kubectl_runner.run(
                ['get', 'daemonset', 'dranet', '-n', 'kube-system', '--ignore-not-found'],
                check=False
            )
            if result.returncode == 0 and result.stdout.strip():
                logger.info("Detected DRANET DaemonSet - can bypass IBM Cloud pod-per-node constraints")
                return True
        except Exception as e:
            logger.debug(f"Could not query for DRANET DaemonSet: {e}")

        return False

    def load_profile(self, profile_name: str) -> ProviderProfile:
        """
        Load IBM Cloud profile from YAML.

        Args:
            profile_name: Profile name ('default' or 'dranet')

        Returns:
            ProviderProfile instance
        """
        profiles_dir = Path(__file__).parent / "profiles"
        profile_file = profiles_dir / f"{profile_name}.yaml"

        if not profile_file.exists():
            logger.warning(f"Profile '{profile_name}' not found for IBM Cloud, using default")
            profile_file = profiles_dir / "default.yaml"

        with open(profile_file) as f:
            data = yaml.safe_load(f)

        # Parse constraints
        constraints = ProviderConstraints(
            max_prefill_pods_per_node=data['constraints'].get('max_prefill_pods_per_node'),
            max_decode_pods_per_node=data['constraints'].get('max_decode_pods_per_node'),
            max_total_pods_per_node=data['constraints'].get('max_total_pods_per_node'),
            supports_rdma=data['constraints'].get('supports_rdma', True),
            supports_pod_affinity=data['constraints'].get('supports_pod_affinity', True),
            supports_node_affinity=data['constraints'].get('supports_node_affinity', True),
            supports_multi_pod_per_node=data['constraints'].get('supports_multi_pod_per_node', False),
            description=data.get('description', '')
        )

        # Parse network config
        network = NetworkConfig(
            rdma_type=data['network']['rdma_type'],
            rdma_device_plugin=data['network']['rdma_device_plugin'],
            requires_cni=data['network'].get('requires_cni', False),
            cni_type=data['network'].get('cni_type'),
            max_bandwidth_gbps=data['network'].get('max_bandwidth_gbps'),
            expected_latency_us=data['network'].get('expected_latency_us'),
            config=data['network'].get('config', {})
        )

        # Parse metrics config (FUTURE)
        metrics = None
        if 'metrics' in data:
            metrics = MetricsConfig(
                endpoint_url=data['metrics'].get('endpoint_url'),
                custom_queries=data['metrics'].get('custom_queries', {}),
                scrape_interval_seconds=data['metrics'].get('scrape_interval_seconds', 15),
                config=data['metrics'].get('config', {})
            )

        # Parse cost model (FUTURE)
        cost_model = None
        if 'cost_model' in data:
            cost_model = CostModel(
                gpu_cost_per_hour=data['cost_model'].get('gpu_cost_per_hour'),
                network_cost_per_gb=data['cost_model'].get('network_cost_per_gb'),
                storage_cost_per_gb_month=data['cost_model'].get('storage_cost_per_gb_month'),
                pricing_factors=data['cost_model'].get('pricing_factors', {})
            )

        # Parse search space (FUTURE)
        search_space = None
        if 'search_space' in data:
            search_space = SearchSpace(
                allowed_pd_ratios=data['search_space'].get('allowed_pd_ratios', ["1:1", "1:2", "1:4", "2:1"]),
                min_tp=data['search_space'].get('min_tp', 1),
                max_tp=data['search_space'].get('max_tp', 8),
                allowed_tp_values=data['search_space'].get('allowed_tp_values'),
                min_batch_size=data['search_space'].get('min_batch_size', 1),
                max_batch_size=data['search_space'].get('max_batch_size', 512),
                restrictions=data['search_space'].get('restrictions', {})
            )

        return ProviderProfile(
            name=data['name'],
            description=data['description'],
            constraints=constraints,
            network=network,
            template_overrides=data.get('template_overrides', {}),
            enabled_architectures=data.get('enabled_architectures', ['aggregated', 'pd', 'ep']),
            metrics=metrics,
            cost_model=cost_model,
            search_space=search_space,
            config=data.get('config', {})
        )

    def _validate_custom_constraints(
        self,
        prefill_pods: int,
        decode_pods: int,
        num_nodes: int,
        prefill_tp: int,
        decode_tp: int
    ) -> Tuple[bool, str]:
        """
        IBM Cloud-specific validation.

        DRANET profile bypasses standard pod-per-node constraints.

        Args:
            prefill_pods: Number of prefill pods
            decode_pods: Number of decode pods
            num_nodes: Number of nodes
            prefill_tp: Prefill tensor parallelism
            decode_tp: Decode tensor parallelism

        Returns:
            (is_valid, reason)
        """
        # Check if DRANET is enabled
        if self.profile.config.get('dranet_enabled', False):
            logger.debug("DRANET enabled - bypassing standard pod-per-node constraints")
            # DRANET allows multiple pods per node, no additional validation needed
            return True, ""

        # Standard IBM Cloud: constraints already validated in base class
        # No additional custom validation needed
        return True, ""

    def get_recommended_pd_ratios(self) -> list[str]:
        """
        Get recommended PD ratios based on profile.

        Returns:
            List of recommended PD ratios
        """
        return self.profile.config.get('recommended_ratios', ['1:1'])

    def is_dranet_enabled(self) -> bool:
        """
        Check if DRANET is enabled in the current profile.

        Returns:
            True if DRANET is enabled
        """
        return self.profile.config.get('dranet_enabled', False)
