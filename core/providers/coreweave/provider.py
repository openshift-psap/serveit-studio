"""
CoreWeave infrastructure provider.

CoreWeave is a GPU cloud provider with InfiniBand networking
and Kubernetes-native infrastructure.
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


class CoreWeaveProvider(BaseProvider):
    """
    CoreWeave infrastructure provider.

    Key characteristics:
    - No pod-per-node limits
    - InfiniBand RDMA via rdma/ib device plugin
    - Vanilla Kubernetes (not OpenShift)
    - Detection via gpu.coreweave.cloud node labels
    """

    def get_provider_id(self) -> str:
        return "coreweave"

    def get_display_name(self) -> str:
        return "CoreWeave"

    def detect(self, kubectl_runner=None) -> bool:
        """
        Detect CoreWeave via node labels.

        CoreWeave nodes have gpu.coreweave.cloud/* labels.
        """
        if not kubectl_runner:
            return False

        try:
            result = kubectl_runner.run(
                ['get', 'nodes',
                 '-l', 'gpu.coreweave.cloud/vendor=NVIDIA',
                 '-o', 'jsonpath={.items[0].metadata.name}'],
                check=False
            )
            if result.returncode == 0 and result.stdout.strip():
                logger.info("Detected CoreWeave from gpu.coreweave.cloud node labels")
                return True
        except Exception as e:
            logger.debug(f"Could not query for CoreWeave node labels: {e}")

        return False

    def load_profile(self, profile_name: str) -> ProviderProfile:
        """
        Load CoreWeave profile from YAML.

        Args:
            profile_name: Profile name ('default')

        Returns:
            ProviderProfile instance
        """
        profiles_dir = Path(__file__).parent / "profiles"
        profile_file = profiles_dir / f"{profile_name}.yaml"

        if not profile_file.exists():
            logger.warning(f"Profile '{profile_name}' not found for CoreWeave, using default")
            profile_file = profiles_dir / "default.yaml"

        with open(profile_file) as f:
            data = yaml.safe_load(f)

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

        network = NetworkConfig(
            rdma_type=data['network']['rdma_type'],
            rdma_device_plugin=data['network']['rdma_device_plugin'],
            requires_cni=data['network'].get('requires_cni', False),
            cni_type=data['network'].get('cni_type'),
            max_bandwidth_gbps=data['network'].get('max_bandwidth_gbps'),
            expected_latency_us=data['network'].get('expected_latency_us'),
            config=data['network'].get('config', {})
        )

        metrics = None
        if 'metrics' in data:
            metrics = MetricsConfig(
                endpoint_url=data['metrics'].get('endpoint_url'),
                custom_queries=data['metrics'].get('custom_queries', {}),
                scrape_interval_seconds=data['metrics'].get('scrape_interval_seconds', 15),
                config=data['metrics'].get('config', {})
            )

        cost_model = None
        if 'cost_model' in data:
            cost_model = CostModel(
                gpu_cost_per_hour=data['cost_model'].get('gpu_cost_per_hour'),
                network_cost_per_gb=data['cost_model'].get('network_cost_per_gb'),
                storage_cost_per_gb_month=data['cost_model'].get('storage_cost_per_gb_month'),
                pricing_factors=data['cost_model'].get('pricing_factors', {})
            )

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
        """No additional constraints on CoreWeave."""
        return True, ""
