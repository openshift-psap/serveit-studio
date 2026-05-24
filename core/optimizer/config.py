"""Optimizer configuration dataclasses."""

"""
Recipe-based exhaustive optimization.

Implements the step-by-step recipe approach:
- Step 2: Exhaustively test ALL valid decode TP values (powers of 2 up to GPUs/node)
- Step 3: Exhaustively test ALL valid prefill TP values (same search space)
- Steps 4-5: Mathematical calculation of ideal P/D ratio and feasible splits
- Step 6: Search for best aggregated configuration (full workload at each TP)
- Step 7: Exhaustively test feasible P/D splits near the ideal ratio
- Step 8: Architecture comparison (PD vs Aggregated, no new tests)
- Step 9: EPP Tuning (conditional, smart weight sweep)
- Step 10: Latency-bounded throughput maximization (conditional)
- Step 11: Calibrated load validation (conditional)

TP values that can't fit the model (based on model size and GPU VRAM) are skipped.
"""

import logging
import os
from typing import Dict, Any, List, Optional, Callable, Tuple
from dataclasses import dataclass, field
from core.config_generator import TestConfig
from core.test_orchestrator import TestOrchestrator, TestResult
from core.system_scanner import SystemScanner
from core.test_planner import calculate_engine_memory_config
from core.cloud_constraints import CloudProvider
from core.database_manager import DatabaseManager
from core.template_manager import TemplateManager
from core.networking import detect_rdma_device_resources

logger = logging.getLogger(__name__)


@dataclass
class RecipeOptimizerConfig:
    """Configuration for recipe-based exhaustive optimization."""
    # Model and workload
    model_name: str
    namespace: str
    isl: int  # Input Sequence Length
    osl: int  # Output Sequence Length
    qps: float  # Concurrency or requests-per-second depending on rate_type
    rate_type: str = 'concurrent'  # 'concurrent', 'constant', or 'poisson'

    # Resources
    total_gpus: int = 16
    max_model_len: int = 8192
    gpu_memory_utilization: float = 0.95  # Dynamic, calculated based on max_model_len
    test_duration: int = 300
    stop_mode: str = 'duration'  # 'duration' or 'max_requests'
    max_requests: Optional[int] = None  # Alternative to test_duration
    isl_stdev: Optional[int] = None  # ISL standard deviation (for guidellm)
    osl_stdev: Optional[int] = None  # OSL standard deviation (for guidellm)
    turns: int = 1  # Number of conversation turns (1 = single-turn)

    # Step 7: P/D split search
    max_pd_splits: int = 0  # 0 = full coverage, >0 = limit splits
    tp_pair_top_n: int = 4  # Top-N prefill/decode TPs to cross-product (1=fast, 4=full)
    pd_search_mode: str = 'smart'  # 'smart' (calculated ~3/pair) or 'exhaustive' (all splits)

    # EPP configuration
    epp_custom_enabled: bool = True  # False = use llm-d default EPP config
    epp_preset: str = 'balanced'  # 'balanced', 'cache_optimized', 'queue_balanced', 'latency_aware', 'custom'
    epp_benchmark: bool = False  # Benchmark multiple EPP strategies
    epp_config: Optional[Dict] = None  # Custom plugin weights and parameters

    # Infrastructure
    thanos_url: Optional[str] = None
    image: str = 'ghcr.io/llm-d/llm-d-cuda:v0.6.0'
    scheduler_image: str = 'ghcr.io/llm-d/llm-d-inference-scheduler:v0.7.1'
    pvc_name: str = 'inftune-model-cache'
    nccl_ib_hca: str = 'mlx'
    hf_token: Optional[str] = None

    # Networking (auto-detected if not set)
    network_type: Optional[str] = None  # 'dra' or 'nad', auto-detected from cloud provider
    rdma_device_resources: Optional[List[str]] = None  # RDMA resource keys from node allocatable, auto-detected
    rdma_nics_per_node: Optional[int] = None  # Physical NICs per node, auto-detected

    # Resources (auto-calculated if not set)
    memory_per_pod: Optional[str] = None  # e.g., '191Gi', auto-calculated from cluster resources
    cpu_per_pod: Optional[str] = None  # e.g., '16', auto-calculated from cluster resources

    # Headroom for resource sizing
    headroom: float = 1.3

    # TP options to explore
    tp_options: List[int] = field(default_factory=lambda: [1, 2, 4, 8])
    allow_asymmetric_tp: bool = False

    # Optimization objective for Step 7
    objective: str = 'balanced'  # 'ttft', 'throughput', or 'balanced'

    # If True, scale down concurrent users to achievable concurrency when cluster capacity is insufficient
    use_achievable_qps: bool = False

    # Latency-bounded throughput maximization (Step 10)
    latency_constraint_enabled: bool = False
    latency_constraint_ms: int = 500
    latency_constraint_percentile: str = 'p90'  # p50, p90, p95, p99

    # Node pinning (optional)
    selected_nodes: List[str] = field(default_factory=list)

    # Dataset workload (alternative to synthetic ISL/OSL)
    workload_mode: str = 'synthetic'
    dataset_source: Optional[str] = None
    dataset_column: Optional[str] = None
    dataset_max_output: int = 256

    # Prefix cache simulation (0 = disabled, 1-100 = hit ratio percentage)
    prefix_cache_hit_pct: int = 0
    prefix_cache_mode: str = 'identical'  # 'identical' = N% same prompt, 'shared_prefix' = all prompts share N% prefix, 'multi_group' = N groups
    prefix_cache_groups: int = 5  # Number of distinct prompt groups for multi_group mode
    prefix_cache_seed: Optional[int] = None  # Deterministic seed, stored in DB for reproducibility

    # Speculative decoding
    speculative_config_method: Optional[str] = None  # 'mtp', 'draft', None
    speculative_config_num_tokens: int = 3
    speculative_config_enabled: bool = False  # User override (auto-detected if False)

    # Advanced vLLM settings (user overrides)
    advanced_vllm_custom_enabled: bool = True  # False = use llm-d upstream defaults
    advanced_vllm: Optional[Dict] = None
    extra_env_vars: Optional[List[Dict[str, str]]] = None

    # Cluster connectivity
    kubeconfig: Optional[str] = None  # Path to kubeconfig file for remote clusters

    # Single test mode (only used when objective='single_test')
    single_test_architecture: Optional[str] = None  # 'aggregated', 'pd', 'ep'
    single_test_tp: Optional[int] = None
    single_test_replicas: Optional[int] = None
    single_test_prefill_tp: Optional[int] = None
    single_test_decode_tp: Optional[int] = None
    single_test_prefill_pods: Optional[int] = None
    single_test_decode_pods: Optional[int] = None

    def to_dict(self) -> dict:
        """Serialize config to dict for DB persistence (excludes secrets)."""
        from dataclasses import fields as dc_fields
        d = {}
        for f in dc_fields(self):
            v = getattr(self, f.name)
            if f.name in ('hf_token', 'kubeconfig'):
                d[f.name] = '***' if v else None
            else:
                d[f.name] = v
        return d

    @classmethod
    def from_dict(cls, d: dict) -> 'RecipeOptimizerConfig':
        """Reconstruct config from saved dict. Unknown keys are ignored."""
        from dataclasses import fields as dc_fields
        valid = {f.name for f in dc_fields(cls)}
        filtered = {k: v for k, v in d.items() if k in valid and k != 'hf_token'}
        return cls(**filtered)


@dataclass
class OptimalTP:
    """Result from TP optimization."""
    tp: int
    tpsg: float  # Tokens per second per GPU
    ttft_p90: Optional[float] = None
    throughput_p90: Optional[float] = None
    test_result: Optional[TestResult] = None


@dataclass
class FeasibleSplit:
    """A feasible P/D split configuration."""
    prefill_pods: int
    decode_pods: int
    prefill_tp: int
    decode_tp: int
    prefill_gpus: int
    decode_gpus: int
    total_gpus: int
    prefill_pct: float


@dataclass
class EPConfig:
    """An EP (Expert Parallelism) configuration."""
    tp: int
    replicas: int
    total_gpus: int

