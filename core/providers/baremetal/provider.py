"""
Bare metal infrastructure provider.

Default fallback provider for non-cloud deployments.
Supports multiple network profiles: InfiniBand, RoCE, TCP.
"""

import yaml
import logging
from pathlib import Path
from typing import Optional

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


class BaremetalProvider(BaseProvider):
    """
    Bare metal infrastructure provider.

    Key characteristics:
    - No pod-per-node limits (full flexibility)
    - Multiple network profiles (InfiniBand, RoCE, TCP)
    - Default fallback when no cloud provider is detected
    """

    def get_provider_id(self) -> str:
        """Return provider ID."""
        return "baremetal"

    def get_display_name(self) -> str:
        """Return human-readable name."""
        return "Bare Metal"

    def detect(self, kubectl_runner=None) -> bool:
        """
        Bare metal is the fallback provider, always returns True.

        In practice, this is only called if no other provider is detected.

        Returns:
            Always True (fallback provider)
        """
        return True

    def load_profile(self, profile_name: str) -> ProviderProfile:
        """
        Load bare metal profile from YAML.

        Args:
            profile_name: Profile name ('infiniband', 'roce', 'tcp', or 'default')

        Returns:
            ProviderProfile instance
        """
        profiles_dir = Path(__file__).parent / "profiles"
        profile_file = profiles_dir / f"{profile_name}.yaml"

        if not profile_file.exists():
            logger.warning(f"Profile '{profile_name}' not found for bare metal, using default")
            # Try common profile names
            for fallback in ['infiniband', 'roce', 'default']:
                fallback_file = profiles_dir / f"{fallback}.yaml"
                if fallback_file.exists():
                    profile_file = fallback_file
                    logger.info(f"Using fallback profile: {fallback}")
                    break

        if not profile_file.exists():
            # Create a minimal default profile on-the-fly
            logger.warning("No profile files found, using built-in defaults")
            return self._create_default_profile()

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
            supports_multi_pod_per_node=data['constraints'].get('supports_multi_pod_per_node', True),
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

    def _create_default_profile(self) -> ProviderProfile:
        """
        Create a minimal default profile when no YAML files are found.

        Returns:
            Default ProviderProfile
        """
        return ProviderProfile(
            name='default',
            description='Bare metal default profile (no constraints)',
            constraints=ProviderConstraints(
                max_prefill_pods_per_node=None,
                max_decode_pods_per_node=None,
                max_total_pods_per_node=None,
                supports_rdma=True,
                supports_pod_affinity=True,
                supports_node_affinity=True,
                supports_multi_pod_per_node=True,
                description='No pod-per-node constraints'
            ),
            network=NetworkConfig(
                rdma_type='infiniband',
                rdma_device_plugin='rdma/rdma_shared_device_a',
                requires_cni=False,
                max_bandwidth_gbps=400.0,
                expected_latency_us=1.0
            ),
            template_overrides={},
            enabled_architectures=['aggregated', 'pd', 'ep'],
            config={'recommended_ratios': ['1:1', '1:2', '1:4', '2:1']}
        )
