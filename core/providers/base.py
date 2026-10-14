"""
Base provider interface for cloud/infrastructure providers.

This module defines the core abstractions that all providers must implement.
Designed to support current features (constraint validation) and future features
(metrics collection, cost analysis, search space optimization).
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Any, Tuple
from pathlib import Path
import logging

logger = logging.getLogger(__name__)


@dataclass
class ProviderConstraints:
    """Infrastructure constraints for a provider."""
    # Pod scheduling constraints
    max_prefill_pods_per_node: Optional[int] = None
    max_decode_pods_per_node: Optional[int] = None
    max_total_pods_per_node: Optional[int] = None

    # Feature support
    supports_rdma: bool = True
    supports_pod_affinity: bool = True
    supports_node_affinity: bool = True
    supports_multi_pod_per_node: bool = True

    # Custom provider-specific constraints
    custom_constraints: Dict[str, Any] = field(default_factory=dict)

    # Human-readable description
    description: str = ""


@dataclass
class NetworkConfig:
    """Network configuration for a provider."""
    rdma_type: str  # 'infiniband', 'roce', 'tcp', 'virtio-roce', 'none'
    rdma_device_plugin: str  # k8s device plugin name (e.g., 'nvidia.com/rdma_shared_device_a')

    # CNI requirements
    requires_cni: bool = False
    cni_type: Optional[str] = None  # 'multus', 'whereabouts', etc.

    # Network performance
    max_bandwidth_gbps: Optional[float] = None
    expected_latency_us: Optional[float] = None  # microseconds

    # Additional network config
    config: Dict[str, Any] = field(default_factory=dict)


@dataclass
class MetricsConfig:
    """
    Metrics collection configuration (FUTURE).

    Defines how to collect provider-specific metrics from Prometheus/Thanos.
    """
    # Prometheus/Thanos endpoint
    endpoint_url: Optional[str] = None

    # Provider-specific metric queries
    custom_queries: Dict[str, str] = field(default_factory=dict)

    # Metric collection interval
    scrape_interval_seconds: int = 15

    # Additional config
    config: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CostModel:
    """
    Cost model for a provider (FUTURE).

    Used for cost-aware optimization and results ranking.
    """
    # Pricing ($/hour per GPU)
    gpu_cost_per_hour: Optional[float] = None

    # Network pricing
    network_cost_per_gb: Optional[float] = None

    # Storage pricing
    storage_cost_per_gb_month: Optional[float] = None

    # Provider-specific pricing factors
    pricing_factors: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SearchSpace:
    """
    Hyperparameter search space constraints (FUTURE).

    Defines provider-specific bounds for optimization.
    """
    # Prefill/Decode ratio constraints
    allowed_pd_ratios: List[str] = field(default_factory=lambda: ["1:1", "1:2", "1:4", "2:1"])

    # Tensor parallelism constraints
    min_tp: int = 1
    max_tp: int = 8
    allowed_tp_values: Optional[List[int]] = None  # If set, restrict to these values

    # Batch size constraints
    min_batch_size: int = 1
    max_batch_size: int = 512

    # Search space restrictions
    restrictions: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ProviderProfile:
    """A specific deployment profile for a provider."""
    name: str
    description: str

    # Core configuration
    constraints: ProviderConstraints
    network: NetworkConfig

    # Template overrides
    template_overrides: Dict[str, Any] = field(default_factory=dict)

    # Enabled architectures for this profile
    enabled_architectures: List[str] = field(default_factory=lambda: ["aggregated", "pd", "ep"])

    # Future: Metrics and cost configuration
    metrics: Optional[MetricsConfig] = None
    cost_model: Optional[CostModel] = None
    search_space: Optional[SearchSpace] = None

    # Additional profile-specific config
    config: Dict[str, Any] = field(default_factory=dict)


class BaseProvider(ABC):
    """
    Abstract base class for infrastructure providers.

    Each provider implements:
    1. Detection logic (identify if this provider is active)
    2. Constraint validation (check if configs are valid for this provider)
    3. Profile management (load different deployment profiles)
    4. FUTURE: Metrics collection, cost analysis, search space integration
    """

    def __init__(self, profile: str = "default"):
        """
        Initialize provider with a specific profile.

        Args:
            profile: Profile name (e.g., 'default', 'dranet', 'infiniband')
        """
        self.profile_name = profile
        self.profile = self.load_profile(profile)
        self.provider_dir = Path(__file__).parent / self.get_provider_id()

        logger.info(f"Initialized {self.get_display_name()} provider with profile: {profile}")

    # ============================================================================
    # REQUIRED METHODS (must be implemented by all providers)
    # ============================================================================

    @abstractmethod
    def get_provider_id(self) -> str:
        """
        Return unique provider identifier.

        Examples: 'ibm_cloud', 'aws', 'gcp', 'azure', 'baremetal'
        """
        pass

    @abstractmethod
    def get_display_name(self) -> str:
        """
        Return human-readable provider name.

        Examples: 'IBM Cloud', 'AWS', 'Google Cloud', 'Bare Metal'
        """
        pass

    @abstractmethod
    def detect(self, kubectl_runner=None) -> bool:
        """
        Detect if this provider is active in the current cluster.

        Args:
            kubectl_runner: Optional KubectlRunner for API queries

        Returns:
            True if this provider is detected
        """
        pass

    @abstractmethod
    def load_profile(self, profile_name: str) -> ProviderProfile:
        """
        Load a specific deployment profile from YAML.

        Args:
            profile_name: Name of the profile to load

        Returns:
            ProviderProfile instance
        """
        pass

    # ============================================================================
    # PROFILE MANAGEMENT
    # ============================================================================

    def get_available_profiles(self) -> List[str]:
        """Return list of available profile names for this provider."""
        profiles_dir = self.provider_dir / "profiles"
        if not profiles_dir.exists():
            return ["default"]

        return [f.stem for f in profiles_dir.glob("*.yaml")]

    def get_constraints(self) -> ProviderConstraints:
        """Get constraints for the current profile."""
        return self.profile.constraints

    # ============================================================================
    # CONSTRAINT VALIDATION (Current Feature)
    # ============================================================================

    def validate_pd_config(
        self,
        prefill_pods: int,
        decode_pods: int,
        num_nodes: int,
        prefill_tp: int,
        decode_tp: int
    ) -> Tuple[bool, str]:
        """
        Validate if a PD configuration is valid for this provider.

        Args:
            prefill_pods: Number of prefill pods
            decode_pods: Number of decode pods
            num_nodes: Number of nodes in cluster
            prefill_tp: Prefill tensor parallelism
            decode_tp: Decode tensor parallelism

        Returns:
            (is_valid, reason) - True if valid, False with reason if invalid
        """
        constraints = self.get_constraints()

        # Calculate pods per node (ceil division)
        prefill_pods_per_node = (prefill_pods + num_nodes - 1) // num_nodes
        decode_pods_per_node = (decode_pods + num_nodes - 1) // num_nodes

        # Check prefill constraint
        if constraints.max_prefill_pods_per_node is not None:
            if prefill_pods_per_node > constraints.max_prefill_pods_per_node:
                return False, (
                    f"Prefill pods per node ({prefill_pods_per_node}) exceeds "
                    f"limit ({constraints.max_prefill_pods_per_node})"
                )

        # Check decode constraint
        if constraints.max_decode_pods_per_node is not None:
            if decode_pods_per_node > constraints.max_decode_pods_per_node:
                return False, (
                    f"Decode pods per node ({decode_pods_per_node}) exceeds "
                    f"limit ({constraints.max_decode_pods_per_node})"
                )

        # Check total pods constraint
        if constraints.max_total_pods_per_node is not None:
            if prefill_pods > 0 and decode_pods > 0:
                total_pods_needed = prefill_pods + decode_pods
                max_capacity = num_nodes * constraints.max_total_pods_per_node
                if total_pods_needed > max_capacity:
                    return False, (
                        f"Total PD pods ({total_pods_needed}) exceeds node capacity "
                        f"({num_nodes} nodes × {constraints.max_total_pods_per_node} pod/node = {max_capacity})"
                    )

        # Provider-specific validation
        return self._validate_custom_constraints(
            prefill_pods, decode_pods, num_nodes, prefill_tp, decode_tp
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
        Override this for provider-specific validation logic.

        Returns:
            (is_valid, reason)
        """
        return True, ""

    # ============================================================================
    # TEMPLATE MANAGEMENT
    # ============================================================================

    # ============================================================================
    # FUTURE: METRICS COLLECTION
    # ============================================================================

    # ============================================================================
    # FUTURE: COST ANALYSIS
    # ============================================================================

    def get_cost_model(self) -> Optional[CostModel]:
        """
        Get cost model for this provider (FUTURE).

        Returns:
            CostModel if configured, None otherwise
        """
        return self.profile.cost_model

    # ============================================================================
    # FUTURE: SEARCH SPACE INTEGRATION
    # ============================================================================
