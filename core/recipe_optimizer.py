"""
Recipe-based exhaustive optimization.

Implements the step-by-step recipe approach:
- Step 2: Exhaustively test ALL valid decode TP values (powers of 2 up to GPUs/node)
- Step 3: Exhaustively test ALL valid prefill TP values (same search space)
- Steps 4-5: Mathematical calculation of ideal P/D ratio and feasible splits
- Step 6: Search for best aggregated configuration (full workload at each TP)
- Step 7: Exhaustively test feasible P/D splits near the ideal ratio
- Step 8: Architecture comparison (PD vs Aggregated, no new tests)
- Step 9: Latency-bounded throughput maximization (Optuna search, conditional)
- Step 10: Calibrated load validation (conditional)

TP values that can't fit the model (based on model size and GPU VRAM) are skipped.
"""

import logging
import os
from typing import Dict, Any, List, Optional, Callable, Tuple
from dataclasses import dataclass, field
from .config_generator import TestConfig
from .test_orchestrator import TestOrchestrator, TestResult
from .system_scanner import SystemScanner
from .test_planner import calculate_engine_memory_config
from .cloud_constraints import CloudProvider
from .database_manager import DatabaseManager
from .template_manager import TemplateManager
from .networking import detect_rdma_device_resources

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
    tp_pair_top_n: int = 2  # Top-N prefill/decode TPs to cross-product (1=fast, 2=thorough)
    pd_search_mode: str = 'smart'  # 'smart' (calculated ~3/pair) or 'exhaustive' (all splits)

    # EPP configuration
    epp_preset: str = 'balanced'  # 'balanced', 'cache_optimized', 'queue_balanced', 'latency_aware', 'custom'
    epp_benchmark: bool = False  # Benchmark multiple EPP strategies
    epp_config: Optional[Dict] = None  # Custom plugin weights and parameters

    # Infrastructure
    thanos_url: Optional[str] = None
    image: str = 'ghcr.io/llm-d/llm-d-cuda:v0.5.1'
    pvc_name: str = 'inferecipe-model-cache'
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

    # Optimization objective for Step 7
    objective: str = 'balanced'  # 'ttft', 'throughput', or 'balanced'

    # If True, scale down concurrent users to achievable QPS when GPUs are insufficient
    use_achievable_qps: bool = False

    # Latency-bounded throughput maximization (Step 9)
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
    prefix_cache_seed: Optional[int] = None  # Deterministic seed, stored in DB for reproducibility

    # Advanced vLLM settings (user overrides)
    advanced_vllm: Optional[Dict] = None

    def to_dict(self) -> dict:
        """Serialize config to dict for DB persistence (excludes secrets)."""
        from dataclasses import fields as dc_fields
        d = {}
        for f in dc_fields(self):
            v = getattr(self, f.name)
            if f.name == 'hf_token':
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


class RecipeOptimizer:
    """
    Recipe-based exhaustive optimizer.

    Workflow:
    1. Step 2: Exhaustively test all valid decode TP values
    2. Step 3: Exhaustively test all valid prefill TP values
    3. Steps 4-5: Calculate ideal P/D ratio and feasible splits
    4. Step 6: Search for best aggregated configuration
    5. Step 7: Exhaustively test P/D splits near ideal ratio
    6. Step 8: Compare PD vs Aggregated (no new tests)
    """

    def __init__(
        self,
        config: RecipeOptimizerConfig,
        log_callback: Optional[Callable[[str, str], None]] = None,
        run_id: Optional[int] = None,
        db_path: str = '/mnt/storage/inferecipe.db',
        stop_check: Optional[Callable[[], bool]] = None
    ):
        """
        Initialize recipe-based optimizer.

        Args:
            config: Recipe configuration
            log_callback: Optional callback for logging (message, level)
            run_id: Optional database run ID for immediate persistence
            db_path: Path to SQLite database
            stop_check: Optional callable that returns True if optimization should stop
        """
        self.config = config
        self.log_callback = log_callback
        self.run_id = run_id
        self.db_path = db_path
        self.stop_check = stop_check
        self.stopped = False

        # Initialize database manager if run_id provided
        self.db_manager: Optional[DatabaseManager] = None
        if run_id is not None:
            self.db_manager = DatabaseManager(db_path=db_path)
            self.log(f"Database persistence enabled (run_id={run_id})", 'info')

        # Initialize components
        self.scanner = SystemScanner(namespace=config.namespace)
        self.orchestrator = TestOrchestrator(
            namespace=config.namespace,
            thanos_url=config.thanos_url,
            deployment_timeout=3600,
            test_duration=config.test_duration
        )

        # Scan cluster
        self.cluster_resources = self.scanner.scan_cluster()

        # Auto-detect network type if not provided
        if self.config.network_type is None:
            self.config.network_type = self._detect_network_type()
            self.log(f"Auto-detected network type: {self.config.network_type}")

        # Auto-detect RDMA device resources if not provided
        if self.config.rdma_device_resources is None:
            self.config.rdma_device_resources = self._detect_rdma_device_resources()
            if self.config.rdma_device_resources:
                self.log(f"Auto-detected RDMA device resources: {self.config.rdma_device_resources}")

        # Auto-detect physical NIC count per node
        if self.config.rdma_nics_per_node is None:
            self.config.rdma_nics_per_node = self._detect_rdma_nics_per_node()
            if self.config.rdma_nics_per_node:
                self.log(f"Auto-detected RDMA NICs per node: {self.config.rdma_nics_per_node}")

        # Memory and CPU per pod are calculated dynamically per deployment
        # based on actual TP and total_pods (see _get_pod_resources).
        # Users can still override via config.memory_per_pod / cpu_per_pod.
        if self.config.memory_per_pod or self.config.cpu_per_pod:
            self.log(
                f"Using user-specified resources: "
                f"memory={self.config.memory_per_pod or 'auto'}, "
                f"cpu={self.config.cpu_per_pod or 'auto'}"
            )

        # GPU VRAM from cluster scan (fallback to 80 GB for A100/H100)
        self._gpu_vram_gb = 80.0
        if self.cluster_resources and self.cluster_resources.gpu_memory_per_gpu_mb > 0:
            self._gpu_vram_gb = self.cluster_resources.gpu_memory_per_gpu_mb / 1024
            self.log(f"GPU VRAM per GPU: {self._gpu_vram_gb:.0f} GB (from cluster scan)")

        # Load model config from HuggingFace for accurate memory calculations
        self._model_config = None
        self._model_size_b = 8.0
        self._model_dtype = 'fp8'
        try:
            import re
            match = re.search(r'(\d+)[Bb]', self.config.model_name)
            if match:
                self._model_size_b = float(match.group(1))
            self._model_dtype = 'fp8' if 'fp8' in self.config.model_name.lower() else 'fp16'

            from transformers import AutoConfig
            self.log(f"Loading model config: {self.config.model_name}")
            hf_kwargs = {}
            if self.config.hf_token:
                hf_kwargs['token'] = self.config.hf_token
            self._model_config = AutoConfig.from_pretrained(
                self.config.model_name, trust_remote_code=True, **hf_kwargs
            ).to_dict()
            self.log(f"Model config loaded: {self._model_config.get('num_hidden_layers')} layers, "
                     f"{self._model_config.get('num_key_value_heads')} KV heads, "
                     f"hidden_size={self._model_config.get('hidden_size')}, "
                     f"max_pos={self._model_config.get('max_position_embeddings')}")
            estimated_b = self._estimate_params_from_config()
            if estimated_b:
                self._model_size_b = estimated_b
                self.log(f"Model size from config: {self._model_size_b:.1f}B parameters")
        except Exception as e:
            self.log(f"Could not load model config: {e}. Using defaults.", 'warning')

        # Set HF_TOKEN in process environment so guidellm and other subprocesses inherit it
        if self.config.hf_token and not os.environ.get('HF_TOKEN'):
            os.environ['HF_TOKEN'] = self.config.hf_token

        # Compute stdev-adjusted max_model_len so vLLM can handle the longest sequences
        # guidellm generates (mean + 2*stdev covers 97.7% of the distribution)
        computed_max_model_len, _ = calculate_engine_memory_config(
            isl=config.isl,
            osl=config.osl,
            num_users=int(config.qps),
            model_size_b=self._model_size_b,
            dtype=self._model_dtype,
            gpu_vram_gb=self._gpu_vram_gb,
            model_config=self._model_config,
            tensor_parallelism=1,  # max_model_len is independent of TP
            isl_stdev=config.isl_stdev,
            osl_stdev=config.osl_stdev
        )
        if computed_max_model_len != self.config.max_model_len:
            self.log(f"Adjusted max_model_len: {self.config.max_model_len} → {computed_max_model_len}"
                     + (f" (includes stdev: ISL±{config.isl_stdev}, OSL±{config.osl_stdev})"
                        if config.isl_stdev or config.osl_stdev else ""))
            self.config.max_model_len = computed_max_model_len

        # Results storage
        self.optimal_decode_tp: Optional[OptimalTP] = None
        self.optimal_prefill_tp: Optional[OptimalTP] = None
        self.ideal_prefill_pct: float = 50.0
        self.feasible_splits: List[FeasibleSplit] = []
        self.pareto_results: List[Tuple[FeasibleSplit, TestResult]] = []
        self.epp_benchmark_results: Dict = {}

        # Calibration results for all TPs (populated in steps 2-3)
        self.decode_tp_results: List[Dict[str, Any]] = []  # [{tp, tpsg, ttft_p90, throughput_p90}]
        self.prefill_tp_results: List[Dict[str, Any]] = []

        # Constraint notes (e.g., asymmetric TP not supported)
        self.constraint_notes: List[str] = []

        # Effective concurrency for Steps 7-8 (may be scaled down if use_achievable_qps is enabled)
        # Always in concurrent-user units, never req/s
        self.effective_concurrency: int = int(config.qps)

        # Step 6: Aggregated configuration search (full-workload tests at each TP)
        self.aggregated_search_results: List[Tuple[int, TestResult]] = []
        self.aggregated_result: Optional[TestResult] = None
        self.aggregated_tp: Optional[int] = None
        self.aggregated_gpus: Optional[int] = None

        # Step 9: Latency-bounded throughput maximization
        self.latency_bounded_result = None

        # Step 10: Calibrated Load validation (only when user didn't enable achievable QPS)
        # Sustainable throughput in req/s (for logging/reporting)
        self.sustainable_throughput_rps: Optional[float] = None
        # Sustainable concurrency in concurrent-user units (for test configs)
        self.achievable_concurrency: Optional[int] = None
        self.calibrated_pd_result: Optional[TestResult] = None
        self.calibrated_agg_result: Optional[TestResult] = None

        # EP (Expert Parallelism) results — populated by ThroughputStrategy/BalancedStrategy
        self.ep_configs: List[EPConfig] = []
        self.ep_results: List[Tuple[EPConfig, TestResult]] = []
        self.best_ep_result: Optional[TestResult] = None
        self.best_ep_config: Optional[EPConfig] = None
        self.calibrated_ep_result: Optional[TestResult] = None

        # Store ALL test results for database insertion
        self.all_test_results: List[Tuple[TestConfig, TestResult]] = []

        # Resume: load completed tests from DB
        self.completed_tests: Dict[str, Dict[str, Any]] = {}
        if self.db_manager and self.run_id:
            self._load_completed_tests()

    def _should_stop(self) -> bool:
        """Check if optimization should stop."""
        if self.stopped:
            return True
        if self.stop_check and self.stop_check():
            self.stopped = True
            self.log("🛑 Optimization stopped by user", 'warning')
            return True
        return False

    def _get_strategy(self):
        """Get the optimization strategy for the configured objective."""
        from .optimization_strategies import (
            TTFTStrategy, ThroughputStrategy, BalancedStrategy,
            AggregatedOnlyStrategy, PDOnlyStrategy, EPOnlyStrategy,
        )
        strategies = {
            'ttft': TTFTStrategy,
            'throughput': ThroughputStrategy,
            'balanced': BalancedStrategy,
            'aggregated_only': AggregatedOnlyStrategy,
            'pd_only': PDOnlyStrategy,
            'ep_only': EPOnlyStrategy,
        }
        cls = strategies.get(self.config.objective, TTFTStrategy)
        self.log(f"Using {cls.__name__} for objective '{self.config.objective}'", 'info')
        return cls(self)

    def log(self, message: str, level: str = 'info'):
        """Log message via callback or logger."""
        if self.log_callback:
            self.log_callback(message, level)
        else:
            log_func = getattr(logger, level, logger.info)
            log_func(message)

    def _render_manifests_json(self, test_config: TestConfig) -> Optional[str]:
        """
        Render LWS templates for a test config and return as JSON string.

        Returns JSON dict of manifest_name -> rendered_yaml, e.g.:
          Aggregated: {"lws": "...", "service": "..."}
          PD: {"prefill": "...", "decode": "...", "prefill-service": "...", "decode-service": "..."}
        """
        try:
            import json
            tmgr = TemplateManager()
            manifests = tmgr.render_config(test_config)
            return json.dumps(manifests)
        except Exception as e:
            self.log(f"  ⚠️  Failed to render templates for DB: {e}", 'warning')
            return None

    def _save_epp_test_to_database(self, test_config: TestConfig, test_result: TestResult):
        """Save EPP tuning test result with configmap YAML as manifest."""
        if self.db_manager and self.run_id:
            try:
                manifests_yaml = getattr(test_config, '_epp_manifests', None)
                self.db_manager.insert_test_result(
                    run_id=self.run_id,
                    test_config=test_config,
                    test_result=test_result,
                    manifests_yaml=manifests_yaml
                )
                self.log(f"  💾 Saved to database (test_id={test_config.test_id})", 'info')
            except Exception as e:
                self.log(f"  ⚠️  Database save failed: {e}", 'warning')

    def _save_test_to_database(self, test_config: TestConfig, test_result: TestResult):
        """
        Save test result to database immediately after test completes.

        Args:
            test_config: Test configuration
            test_result: Test result from orchestrator
        """
        if self.db_manager and self.run_id:
            try:
                manifests_yaml = self._render_manifests_json(test_config)
                self.db_manager.insert_test_result(
                    run_id=self.run_id,
                    test_config=test_config,
                    test_result=test_result,
                    manifests_yaml=manifests_yaml
                )
                self.log(f"  💾 Saved to database (test_id={test_config.test_id})", 'info')
            except Exception as e:
                self.log(f"  ⚠️  Database save failed: {e}", 'warning')

    def _check_pod_errors(self, test_config: TestConfig, test_result: TestResult):
        """Check for pod errors after a test and raise if found."""
        if not test_result.pod_errors_detected:
            return
        from .pod_error_scanner import PodErrorsDetected
        if self.db_manager and self.run_id:
            try:
                self.db_manager.save_pod_errors(
                    run_id=self.run_id,
                    test_id=test_config.test_id,
                    errors_json=test_result.pod_errors_json,
                    architecture=test_config.architecture
                )
            except Exception as e:
                self.log(f"  ⚠️  Failed to save pod errors: {e}", 'warning')
        self.log("🚨 Critical pod errors detected — stopping for investigation", 'error')
        self.log("   Pods left running. Investigate then resume from the Resume page.", 'error')
        import json as _json
        raise PodErrorsDetected(
            scan_result=_json.loads(test_result.pod_errors_json) if isinstance(test_result.pod_errors_json, str) else test_result.pod_errors_json,
            test_id=test_config.test_id
        )

    def _save_constraint_notes(self):
        """Save constraint notes to the database immediately so they persist even if the run fails."""
        if self.db_manager and self.run_id and self.constraint_notes:
            try:
                import json as _json
                notes_json = _json.dumps(self.constraint_notes)
                with self.db_manager.get_connection() as conn:
                    conn.execute(
                        'UPDATE optimization_runs SET constraint_notes = ? WHERE id = ?',
                        (notes_json, self.run_id)
                    )
            except Exception as e:
                self.log(f"  ⚠️  Failed to save constraint notes: {e}", 'warning')

    def _load_completed_tests(self):
        """
        Load completed tests from the database for resume capability.

        Populates self.completed_tests with config_name -> result data mapping.
        Also restores profiled vLLM memory data into all_test_results so that
        _compute_gpu_mem_util and _compute_max_num_seqs work on resume.
        """
        try:
            import json as _json
            with self.db_manager.get_connection() as conn:
                conn.row_factory = __import__('sqlite3').Row
                rows = conn.execute(
                    'SELECT * FROM test_configurations WHERE run_id = ? AND status = ?',
                    (self.run_id, 'completed')
                ).fetchall()

                for row in rows:
                    row_dict = dict(row)
                    self.completed_tests[row['config_name']] = row_dict

                    # Restore profiled vLLM data from metrics_json into all_test_results
                    metrics_raw = row_dict.get('metrics_json')
                    if metrics_raw:
                        try:
                            metrics = _json.loads(metrics_raw)
                            avail_kv = metrics.get('vllm_available_kv_gb')
                            overhead = metrics.get('vllm_fixed_overhead_gb')
                            if avail_kv is not None or overhead is not None:
                                result = self._make_test_result_from_db(row_dict)
                                result.vllm_available_kv_gb = avail_kv
                                result.vllm_fixed_overhead_gb = overhead
                                result.vllm_gpu_blocks = metrics.get('vllm_gpu_blocks')
                                tp = row_dict.get('tensor_parallelism', 1)
                                config = TestConfig(
                                    test_id=row_dict['config_name'],
                                    architecture='aggregated',
                                    model_name=self.config.model_name,
                                    tensor_parallelism=tp,
                                    namespace=self.config.namespace,
                                )
                                self.all_test_results.append((config, result))
                                self.log(f"   📊 Restored profiled data for TP={tp}: "
                                         f"avail_kv={avail_kv}GB, overhead={overhead}GB")
                        except Exception:
                            pass

                if self.completed_tests:
                    self.log(f"📋 Found {len(self.completed_tests)} completed tests from previous run", 'info')
                    for name in sorted(self.completed_tests.keys()):
                        self.log(f"   ✅ {name}", 'info')

                    # Infer total_gpus from completed step6 test names
                    # All step6 tests use the same GPU count; take the max to be safe.
                    # e.g. "step6-agg-tp2-16r" → tp=2, replicas=16 → total_gpus=32
                    import re as _re
                    max_inferred = 0
                    for name in self.completed_tests:
                        m = _re.match(r'step6-agg-tp(\d+)-(\d+)r', name)
                        if m:
                            max_inferred = max(max_inferred, int(m.group(1)) * int(m.group(2)))
                    if max_inferred > 0 and max_inferred != self.config.total_gpus:
                        self.log(f"   ⚠️  Config total_gpus={self.config.total_gpus} "
                                 f"but completed tests used {max_inferred} — correcting", 'warning')
                        self.config.total_gpus = max_inferred
        except Exception as e:
            self.log(f"⚠️  Could not load previous results: {e}", 'warning')

    def _make_test_result_from_db(self, row: Dict[str, Any]) -> TestResult:
        """Reconstruct a TestResult from a database row."""
        # Restore extended metrics from metrics_json if available
        mj = {}
        mj_raw = row.get('metrics_json')
        if mj_raw:
            try:
                mj = _json.loads(mj_raw)
            except Exception:
                pass

        return TestResult(
            test_id=row.get('config_name', 'unknown'),
            architecture=row.get('architecture', 'unknown'),
            metrics_collected=True,
            deployment_start_time=row.get('started_at', ''),
            deployment_success=True,
            deployment_ready=True,
            guidellm_success=True,
            # Core percentiles (from direct columns)
            ttft_p50=row.get('ttft_p50'),
            ttft_p90=row.get('ttft_p90'),
            ttft_p95=row.get('ttft_p95'),
            ttft_p99=row.get('ttft_p99'),
            itl_p50=row.get('itl_p50'),
            itl_p90=row.get('itl_p90'),
            itl_p95=row.get('itl_p95'),
            itl_p99=row.get('itl_p99'),
            throughput_p50=row.get('throughput_p50'),
            throughput_p90=row.get('throughput_p90'),
            throughput_p95=row.get('throughput_p95'),
            throughput_p99=row.get('throughput_p99'),
            gpu_utilization=row.get('gpu_utilization'),
            kv_cache_usage=row.get('kv_cache_usage'),
            # Extended guidellm metrics (from metrics_json)
            ttft_mean=mj.get('ttft_mean'),
            ttft_min=mj.get('ttft_min'),
            ttft_max=mj.get('ttft_max'),
            ttft_std_dev=mj.get('ttft_std_dev'),
            ttft_p25=mj.get('ttft_p25'),
            ttft_p75=mj.get('ttft_p75'),
            itl_mean=mj.get('itl_mean'),
            itl_min=mj.get('itl_min'),
            itl_max=mj.get('itl_max'),
            itl_std_dev=mj.get('itl_std_dev'),
            throughput_mean=mj.get('throughput_mean'),
            tpot_mean=mj.get('tpot_mean'),
            tpot_p50=mj.get('tpot_p50'),
            tpot_p90=mj.get('tpot_p90'),
            tpot_p95=mj.get('tpot_p95'),
            tpot_p99=mj.get('tpot_p99'),
            e2e_latency_mean=mj.get('e2e_latency_mean'),
            e2e_latency_p50=mj.get('e2e_latency_p50'),
            e2e_latency_p90=mj.get('e2e_latency_p90'),
            e2e_latency_p95=mj.get('e2e_latency_p95'),
            e2e_latency_p99=mj.get('e2e_latency_p99'),
            output_tps_mean=mj.get('output_tps_mean'),
            output_tps_p50=mj.get('output_tps_p50'),
            output_tps_p90=mj.get('output_tps_p90'),
            output_tps_p95=mj.get('output_tps_p95'),
            output_tps_p99=mj.get('output_tps_p99'),
            prompt_tokens_mean=mj.get('prompt_tokens_mean'),
            output_tokens_mean=mj.get('output_tokens_mean'),
            concurrency_mean=mj.get('concurrency_mean'),
            concurrency_p50=mj.get('concurrency_p50'),
            concurrency_p90=mj.get('concurrency_p90'),
            request_total=mj.get('request_total'),
            request_successful=mj.get('request_successful'),
            request_incomplete=mj.get('request_incomplete'),
            request_errored=mj.get('request_errored'),
            benchmark_duration_s=mj.get('benchmark_duration_s'),
            warmup_duration_s=mj.get('warmup_duration_s'),
        )

    def _detect_network_type(self) -> str:
        """
        Detect network type based on cloud provider.

        Returns:
            'dra' for IBM Cloud (DRANET), 'nad' for bare metal or other providers
        """
        import os

        # Check for manual override
        force_nad = os.getenv('INFE_RECIPE_FORCE_NAD', 'false').lower() == 'true'
        if force_nad:
            return 'nad'

        if self.cluster_resources:
            if self.cluster_resources.cloud_provider == CloudProvider.IBM_CLOUD:
                return 'dra'
            if self.cluster_resources.cloud_provider == CloudProvider.COREWEAVE:
                return 'shared_device'

        return 'nad'

    def _detect_rdma_device_resources(self) -> List[str]:
        if not self.cluster_resources:
            return []
        return detect_rdma_device_resources(
            self.cluster_resources.nodes, self.config.network_type or 'nad'
        )

    def _compute_block_size(self) -> int:
        """Compute optimal vLLM KV cache block size.

        block_size = next_power_of_2(sqrt(ISL + OSL)), clamped to [8, 512].
        For PD goals (ttft, balanced, pd_only), floor is 128 because NIXL
        transfers KV cache in blocks — larger blocks reduce transfer count.
        """
        import math
        seq_len = self.config.isl + self.config.osl
        raw = math.sqrt(seq_len)
        from core.utils import next_power_of_2
        bs = next_power_of_2(max(1, int(raw)))
        pd_goals = ('ttft', 'balanced', 'pd_only')
        floor = 128 if self.config.objective in pd_goals else 8
        return max(floor, min(512, bs))

    def _build_epp_config(self) -> Optional[Dict]:
        """Build EPP config dict for prereq_manager from optimizer config."""
        import math
        block_size = self._compute_block_size()
        return {
            'preset': self.config.epp_preset,
            'plugins': self.config.epp_config if self.config.epp_preset == 'custom' else None,
            'maxPrefixBlocksToMatch': math.ceil(self.config.isl / block_size),
            'lruCapacityPerServer': 31250,
            'nonCachedTokens': min(16, max(1, self.config.isl // 100)),
        }

    def _detect_rdma_nics_per_node(self) -> int:
        """
        Get physical NIC count per node from scanner results.
        """
        if not self.cluster_resources:
            return 0

        min_nics = None
        for node in self.cluster_resources.nodes:
            if not node.has_rdma:
                continue
            for nic in node.network_interfaces:
                if nic.type in ('InfiniBand', 'RoCE', 'RDMA'):
                    if min_nics is None or nic.count < min_nics:
                        min_nics = nic.count

        return min_nics or 0

    def _get_pod_resources(self, tp: int, total_pods: int) -> tuple:
        """
        Calculate memory and CPU per pod for a specific deployment.

        Uses the actual TP and total_pods to determine how many pods will
        land on each node, then divides node resources proportionally:
          pods_per_node = ceil(total_pods / num_gpu_nodes)
          memory = (node_memory * 0.85) / pods_per_node
          cpu    = (node_cpus   * 0.80) / pods_per_node

        If the user specified overrides (config.memory_per_pod / cpu_per_pod),
        those are returned instead.

        Args:
            tp: Tensor parallelism for this deployment
            total_pods: Total number of pods being deployed

        Returns:
            (memory_str, cpu_str) — e.g. ("64Gi", "16")
        """
        import math

        # Use user overrides if specified
        mem_override = self.config.memory_per_pod
        cpu_override = self.config.cpu_per_pod
        if mem_override and cpu_override:
            return mem_override, cpu_override

        if not self.cluster_resources:
            logger.warning("No cluster resources, using defaults: 64Gi / 16 CPU")
            return mem_override or '64Gi', cpu_override or '16'

        gpu_nodes = [n for n in self.cluster_resources.nodes if n.gpus > 0]
        if not gpu_nodes:
            logger.warning("No GPU nodes found, using defaults: 64Gi / 16 CPU")
            return mem_override or '64Gi', cpu_override or '16'

        num_gpu_nodes = len(gpu_nodes)
        max_gpus_per_node = max(n.gpus for n in gpu_nodes)

        # How many pods will land on each node?
        # Method 1: from total deployment — ceil(total_pods / num_nodes)
        pods_from_deployment = math.ceil(total_pods / num_gpu_nodes)
        # Method 2: from TP — how many pods CAN fit per node based on GPU count
        pods_from_tp = max_gpus_per_node // tp if tp > 0 else 1
        # Use the actual expected density (the higher of the two is more conservative)
        pods_per_node = max(pods_from_deployment, pods_from_tp, 1)

        # Memory: 85% of node memory / pods_per_node
        if not mem_override:
            avg_node_memory_gb = sum(n.memory_gb for n in gpu_nodes) / num_gpu_nodes
            usable_memory_gb = avg_node_memory_gb * 0.85
            memory_per_pod_gb = int(usable_memory_gb / pods_per_node)
            mem_str = f"{memory_per_pod_gb}Gi"
        else:
            mem_str = mem_override

        # CPU: 80% of node CPUs / pods_per_node
        if not cpu_override:
            avg_node_cpus = sum(n.cpu_cores for n in gpu_nodes) / num_gpu_nodes
            usable_cpus = avg_node_cpus * 0.80
            cpus_per_pod = int(usable_cpus / pods_per_node)
            cpu_str = str(max(cpus_per_pod, 1))
        else:
            cpu_str = cpu_override

        logger.info(
            f"Resource calculation: {total_pods} pods, TP={tp}, "
            f"{num_gpu_nodes} GPU nodes → {pods_per_node} pods/node → "
            f"{mem_str} memory, {cpu_str} CPUs per pod"
        )

        return mem_str, cpu_str

    def clear_previous_results(self):
        """Clear all previous test results for this run (start fresh)."""
        if self.db_manager and self.run_id:
            try:
                with self.db_manager.get_connection() as conn:
                    conn.execute(
                        'DELETE FROM test_configurations WHERE run_id = ?',
                        (self.run_id,)
                    )
                self.completed_tests.clear()
                self.log("🗑️  Cleared all previous test results — starting fresh", 'info')
            except Exception as e:
                self.log(f"⚠️  Failed to clear previous results: {e}", 'warning')

    def optimize(self, resume: bool = True) -> Dict[str, Any]:
        """
        Run complete recipe-based optimization.

        Args:
            resume: If True, skip completed tests from previous runs.
                    If False, clear previous results and start fresh.

        Returns:
            Dictionary with optimization results including Pareto front
        """
        if not resume:
            self.clear_previous_results()

        self.log("=" * 80, 'info')
        self.log("RECIPE-BASED OPTIMIZATION", 'success')
        self.log("=" * 80, 'info')
        self.log(f"Model: {self.config.model_name}", 'info')
        isl_s = f"ISL={self.config.isl}" + (f"(σ={self.config.isl_stdev})" if self.config.isl_stdev else "")
        osl_s = f"OSL={self.config.osl}" + (f"(σ={self.config.osl_stdev})" if self.config.osl_stdev else "")
        turns_s = f", Turns={self.config.turns}" if self.config.turns > 1 else ""
        rate_label = f"Concurrency={int(self.config.qps)}" if self.config.rate_type == 'concurrent' else f"Rate={int(self.config.qps)} req/s ({self.config.rate_type})"
        self.log(f"Workload: {isl_s}, {osl_s}, {rate_label}{turns_s}", 'info')
        self.log(f"Resources: {self.config.total_gpus} GPUs available", 'info')
        bs = self._compute_block_size()
        self.log(f"Block size: {bs} (auto-tuned from seq_len={self.config.isl + self.config.osl}"
                 f"{', prefix caching' if self.config.prefix_cache_hit_pct > 0 else ''})", 'info')
        pd_mode = 'Smart (~3/pair)' if self.config.pd_search_mode == 'smart' else 'Exhaustive (all splits)'
        self.log(f"PD search: {pd_mode}", 'info')
        if self.completed_tests:
            self.log(f"Mode: RESUME ({len(self.completed_tests)} completed tests will be skipped)", 'info')
        else:
            self.log("Mode: FRESH START", 'info')
        self.log("", 'info')

        # Generate prefix cache dataset if configured
        if self.config.prefix_cache_hit_pct > 0 and self.config.workload_mode == 'synthetic':
            self._generate_prefix_cache_dataset()

        # Step 2: Find optimal decode TP
        self.log("STEP 2: Decode TP Optimization", 'decision')
        self.log("-" * 80, 'info')
        self._optimize_decode_tp()
        self.log("", 'info')
        if self._should_stop():
            return self._build_results()

        # Step 3: Find optimal prefill TP
        self.log("STEP 3: Prefill TP Optimization", 'decision')
        self.log("-" * 80, 'info')
        self._optimize_prefill_tp()
        self.log("", 'info')
        if self._should_stop():
            return self._build_results()

        # Steps 4-11: Dispatch to goal-specific strategy
        strategy = self._get_strategy()
        strategy.execute()

        # Step 11: EPP tuning (conditional, after all other steps)
        if self.config.epp_benchmark and not self._should_stop():
            self._benchmark_epp_strategies()

        # Return results
        return self._build_results()

    def _get_valid_tp_options(self) -> List[int]:
        """
        Get valid TP options based on cluster GPUs per node and model size.

        Returns powers of 2 up to max GPUs per node, filtered to exclude
        TP values too small to fit the model.
        """
        if self.cluster_resources:
            tp_options = self.cluster_resources.get_tp_options()
            min_tp = self.cluster_resources.estimate_model_gpu_requirement(
                model_size_gb=self._estimate_model_size_gb(),
                dtype='fp8' if 'fp8' in self.config.model_name.lower() else 'fp16'
            )
            tp_options = [tp for tp in tp_options if tp >= min_tp]
            if tp_options:
                return tp_options

        # Fallback to configured options
        return self.config.tp_options

    def _estimate_params_from_config(self) -> float:
        """Estimate total parameter count (in billions) from loaded model config.

        Handles dense models and MoE architectures. For MoE, uses
        moe_intermediate_size for expert FFN and intermediate_size for
        shared/dense FFN. Supports n_routed_experts, num_local_experts,
        and n_shared_experts fields across Mixtral, Qwen-MoE, and DeepSeek.
        """
        if not self._model_config:
            return 0.0
        cfg = self._model_config

        hidden = cfg.get('hidden_size', 0)
        layers = cfg.get('num_hidden_layers', 0)
        vocab = cfg.get('vocab_size', 0)
        intermediate = cfg.get('intermediate_size', 0)
        num_heads = cfg.get('num_attention_heads', 0)
        num_kv_heads = cfg.get('num_key_value_heads', num_heads)
        if not all([hidden, layers, vocab]):
            return 0.0

        head_dim = hidden // num_heads if num_heads else 128

        # Attention: Q + K + V projections + output projection
        attn_params = hidden * (num_heads * head_dim) + hidden * (num_kv_heads * head_dim) * 2 + (num_heads * head_dim) * hidden

        # MoE detection
        num_experts = cfg.get('num_local_experts') or cfg.get('n_routed_experts') or cfg.get('num_experts') or 1
        num_shared_experts = cfg.get('n_shared_experts', 0)
        moe_intermediate = cfg.get('moe_intermediate_size', 0)

        if num_experts > 1 and moe_intermediate:
            # MoE with separate expert FFN size (Qwen-MoE, DeepSeek)
            ffn_per_expert = hidden * moe_intermediate * 3
            shared_ffn = hidden * intermediate * 3 if intermediate else 0
            router_params = hidden * num_experts
            per_layer = attn_params + ffn_per_expert * num_experts + shared_ffn * num_shared_experts + router_params
        elif num_experts > 1:
            # MoE where intermediate_size IS the per-expert size (Mixtral)
            ffn_per_expert = hidden * intermediate * 3
            router_params = hidden * num_experts
            per_layer = attn_params + ffn_per_expert * num_experts + router_params
        else:
            # Dense model
            per_layer = attn_params + hidden * intermediate * 3

        embed_params = vocab * hidden * 2
        total = layers * per_layer + embed_params
        total_b = total / 1e9

        if num_experts > 1:
            self.log(f"  MoE model: {num_experts} experts, ~{total_b:.1f}B total parameters")

        return round(total_b, 1)

    def _estimate_model_size_gb(self) -> float:
        """Estimate model weight size in GB for VRAM planning.

        Uses _model_size_b (set from config or name parsing).
        FP8: ~1 byte/param, FP16: ~2 bytes/param.
        """
        params_b = self._model_size_b
        if 'fp8' in self.config.model_name.lower():
            return params_b * 1.0
        return params_b * 2.0

    def _optimize_decode_tp(self):
        """
        Step 2: Test ALL valid TP values for decode workload.

        Tests decode-only workload (ISL=1, OSL=target) with every valid TP.
        Objective: lowest TTFT (when objective='ttft') or highest TPSG.
        """
        valid_tp = self._get_valid_tp_options()
        self.log(f"Testing all {len(valid_tp)} valid TP values: {valid_tp}", 'info')
        self.log(f"Workload: ISL=1, OSL={self.config.osl} (decode-focused)", 'info')

        use_ttft = self.config.objective == 'ttft'
        best_tp = None
        best_tpsg = 0.0
        best_ttft = float('inf')
        best_throughput = None
        all_candidates = []

        for i, tp in enumerate(valid_tp):
            if self._should_stop():
                break

            test_id = f"step2-trial{i + 1}-decode-tp{tp}"
            self.log(f"  Test {i + 1}/{len(valid_tp)}: TP={tp}", 'info')

            # Check for completed test from previous run
            if test_id in self.completed_tests:
                row = self.completed_tests[test_id]
                result = self._make_test_result_from_db(row)
                self.log("    ⏩ Resuming from DB (already completed)", 'info')
            else:
                test_config = self._create_aggregated_config(
                    tp=tp,
                    num_gpus=tp,
                    isl=1,
                    osl=self.config.osl,
                    test_id=test_id,
                    use_concurrency=True
                )

                result = self.orchestrator.run_test(
                    test_config,
                    cleanup=True,
                    log_callback=lambda msg: self.log(msg, 'info'),
                    stop_check=self._should_stop
                )

                self.all_test_results.append((test_config, result))
                self._save_test_to_database(test_config, result)
                self._check_pod_errors(test_config, result)

                if not result or not result.guidellm_success:
                    self.log("    ❌ Test failed - STOPPING optimization", 'error')
                    self.log(f"    🔍 Debug: kubectl get pods -n {self.config.namespace} -l test-id={test_id}", 'error')
                    self.log(f"    🧹 Cleanup: kubectl delete lws -n {self.config.namespace} -l test-id={test_id}", 'error')
                    raise RuntimeError(f"Test {test_id} failed - stopping optimization")

            # Calculate TPSG
            if result.throughput_p90 and result.throughput_p90 > 0:
                tpsg = (result.throughput_p90 * self.config.osl) / tp
            elif result.throughput_p50 and result.throughput_p50 > 0:
                tpsg = (result.throughput_p50 * self.config.osl) / tp
            else:
                self.log("    ❌ No throughput metric available", 'error')
                continue

            ttft = result.ttft_p90 if result.ttft_p90 else float('inf')
            all_candidates.append((tp, tpsg, ttft, result.throughput_p90))

            if use_ttft:
                self.log(f"    ✅ TTFT_p90: {ttft:.0f}ms, TPSG: {tpsg:.0f} tokens/s/GPU", 'success')
                if ttft < best_ttft:
                    best_ttft = ttft
                    best_tp = tp
                    best_tpsg = tpsg
                    best_throughput = result.throughput_p90
            else:
                self.log(f"    ✅ TPSG: {tpsg:.0f} tokens/s/GPU", 'success')
                if tpsg > best_tpsg:
                    best_tpsg = tpsg
                    best_tp = tp
                    best_ttft = ttft
                    best_throughput = result.throughput_p90

        # If TTFT-based selection found nothing (all TTFTs were inf, common for
        # decode-only ISL=1 workloads), fall back to highest TPSG
        if best_tp is None and all_candidates:
            self.log("  ⚠️  All TTFT values are inf (normal for ISL=1 decode tests), selecting by highest TPSG", 'warning')
            all_candidates.sort(key=lambda x: x[1], reverse=True)  # sort by TPSG desc
            best_tp, best_tpsg, best_ttft, best_throughput = all_candidates[0]

        if best_tp is None:
            raise RuntimeError("All decode TP tests failed - no valid results")

        # Store all TP results for multi-TP split generation
        self.decode_tp_results = [
            {'tp': tp, 'tpsg': tpsg, 'ttft_p90': ttft, 'throughput_p90': thr}
            for tp, tpsg, ttft, thr in all_candidates
        ]

        self.optimal_decode_tp = OptimalTP(
            tp=best_tp,
            tpsg=best_tpsg,
            ttft_p90=best_ttft,
            throughput_p90=best_throughput
        )

        criterion = "lowest TTFT" if use_ttft else "highest TPSG"
        self.log("", 'info')
        self.log(f"✅ Optimal Decode TP: {self.optimal_decode_tp.tp} (selected by {criterion})", 'success')
        self.log(f"   TTFT_p90: {best_ttft:.0f}ms, TPSG: {self.optimal_decode_tp.tpsg:.0f} tokens/s/GPU", 'info')
        self.log(f"   Tested all {len(valid_tp)} TP values", 'info')

    def _optimize_prefill_tp(self):
        """
        Step 3: Test ALL valid TP values for prefill workload.

        Tests prefill-only workload (ISL=target, OSL=1) with every valid TP.
        Objective: lowest TTFT (when objective='ttft') or highest TPSG.
        """
        valid_tp = self._get_valid_tp_options()
        self.log(f"Testing all {len(valid_tp)} valid TP values: {valid_tp}", 'info')
        self.log(f"Workload: ISL={self.config.isl}, OSL=1 (prefill-focused)", 'info')

        use_ttft = self.config.objective == 'ttft'
        best_tp = None
        best_tpsg = 0.0
        best_ttft = float('inf')
        best_throughput = None
        all_candidates = []

        for i, tp in enumerate(valid_tp):
            if self._should_stop():
                break

            test_id = f"step3-trial{i + 1}-prefill-tp{tp}"
            self.log(f"  Test {i + 1}/{len(valid_tp)}: TP={tp}", 'info')

            # Check for completed test from previous run
            if test_id in self.completed_tests:
                row = self.completed_tests[test_id]
                result = self._make_test_result_from_db(row)
                self.log("    ⏩ Resuming from DB (already completed)", 'info')
            else:
                test_config = self._create_aggregated_config(
                    tp=tp,
                    num_gpus=tp,
                    isl=self.config.isl,
                    osl=1,
                    test_id=test_id,
                    use_concurrency=True
                )

                result = self.orchestrator.run_test(
                    test_config,
                    cleanup=True,
                    log_callback=lambda msg: self.log(msg, 'info'),
                    stop_check=self._should_stop
                )

                self.all_test_results.append((test_config, result))
                self._save_test_to_database(test_config, result)
                self._check_pod_errors(test_config, result)

                if not result or not result.guidellm_success:
                    self.log("    ❌ Test failed - STOPPING optimization", 'error')
                    self.log(f"    🔍 Debug: kubectl get pods -n {self.config.namespace} -l test-id={test_id}", 'error')
                    self.log(f"    🧹 Cleanup: kubectl delete lws -n {self.config.namespace} -l test-id={test_id}", 'error')
                    raise RuntimeError(f"Test {test_id} failed - stopping optimization")

            # Calculate TPSG
            if result.throughput_p90 and result.throughput_p90 > 0:
                tpsg = (result.throughput_p90 * self.config.isl) / tp
            elif result.throughput_p50 and result.throughput_p50 > 0:
                tpsg = (result.throughput_p50 * self.config.isl) / tp
            else:
                self.log("    ❌ No throughput metric available", 'error')
                continue

            ttft = result.ttft_p90 if result.ttft_p90 else float('inf')
            all_candidates.append((tp, tpsg, ttft, result.throughput_p90))

            if use_ttft:
                self.log(f"    ✅ TTFT_p90: {ttft:.0f}ms, TPSG: {tpsg:.0f} tokens/s/GPU", 'success')
                if ttft < best_ttft:
                    best_ttft = ttft
                    best_tp = tp
                    best_tpsg = tpsg
                    best_throughput = result.throughput_p90
            else:
                self.log(f"    ✅ TPSG: {tpsg:.0f} tokens/s/GPU", 'success')
                if tpsg > best_tpsg:
                    best_tpsg = tpsg
                    best_tp = tp
                    best_ttft = ttft
                    best_throughput = result.throughput_p90

        if best_tp is None and all_candidates:
            self.log("  ⚠️  All TTFT values are inf, selecting by highest TPSG", 'warning')
            all_candidates.sort(key=lambda x: x[1], reverse=True)
            best_tp, best_tpsg, best_ttft, best_throughput = all_candidates[0]

        if best_tp is None:
            raise RuntimeError("All prefill TP tests failed - no valid results")

        # Store all TP results for multi-TP split generation
        self.prefill_tp_results = [
            {'tp': tp, 'tpsg': tpsg, 'ttft_p90': ttft, 'throughput_p90': thr}
            for tp, tpsg, ttft, thr in all_candidates
        ]

        self.optimal_prefill_tp = OptimalTP(
            tp=best_tp,
            tpsg=best_tpsg,
            ttft_p90=best_ttft,
            throughput_p90=best_throughput
        )

        criterion = "lowest TTFT" if use_ttft else "highest TPSG"
        self.log("", 'info')
        self.log(f"✅ Optimal Prefill TP: {self.optimal_prefill_tp.tp} (selected by {criterion})", 'success')
        self.log(f"   TTFT_p90: {best_ttft:.0f}ms, TPSG: {self.optimal_prefill_tp.tpsg:.0f} tokens/s/GPU", 'info')
        self.log(f"   Tested all {len(valid_tp)} TP values", 'info')

    def _select_tp_pairs(self):
        """
        Select (prefill_tp, decode_tp) pairs to test in Step 7.

        Builds the cross-product of the top-N prefill TPs and top-N decode TPs,
        then deduplicates.  N is controlled by config.tp_pair_top_n (1=fast, 2=thorough).
        """
        use_ttft = self.config.objective == 'ttft'
        top_n = self.config.tp_pair_top_n

        # Rank prefill TPs: by TTFT (lower=better) for ttft objective, by TPSG otherwise
        if use_ttft:
            prefill_ranked = sorted(self.prefill_tp_results, key=lambda r: r['ttft_p90'] or float('inf'))
        else:
            prefill_ranked = sorted(self.prefill_tp_results, key=lambda r: r['tpsg'], reverse=True)
        # Rank decode TPs: always by TPSG (decode throughput efficiency)
        decode_ranked = sorted(self.decode_tp_results, key=lambda r: r['tpsg'], reverse=True)

        top_prefill = [r['tp'] for r in prefill_ranked[:top_n]]
        top_decode = [r['tp'] for r in decode_ranked[:top_n]]

        prefill_metric = "TTFT" if use_ttft else "TPSG"
        self.log(f"  Top-{top_n} prefill TPs (by {prefill_metric}): {top_prefill}", 'info')
        self.log(f"  Top-{top_n} decode TPs (by TPSG): {top_decode}", 'info')

        # Cross-product of top-N × top-N, deduplicated, primary pair first.
        # NIXL KV transfer constraint: when prefill_tp >= num_kv_heads (KV cache is
        # replicated across prefill TP workers) AND prefill_tp > decode_tp, the
        # handshake fails with AssertionError in _validate_remote_agent_handshake.
        num_kv_heads = (self._model_config or {}).get('num_key_value_heads', 0)
        seen = set()
        skipped = []
        self._selected_tp_pairs = []

        # Primary pair: best prefill × best decode (always first)
        primary = (top_prefill[0], top_decode[0])

        all_pairs = [primary] + [(ptp, dtp) for ptp in top_prefill for dtp in top_decode if (ptp, dtp) != primary]
        for ptp, dtp in all_pairs:
            if (ptp, dtp) in seen:
                continue
            seen.add((ptp, dtp))
            if ptp > dtp and num_kv_heads > 0 and ptp >= num_kv_heads:
                skipped.append((ptp, dtp))
                continue
            self._selected_tp_pairs.append((ptp, dtp))

        if skipped:
            skipped_str = ', '.join(f'(PTP={p}, DTP={d})' for p, d in skipped)
            self.log(f"  ⚠️  Skipped {len(skipped)} pairs due to NIXL sharding constraint:", 'warning')
            self.log(f"     Problem: When Prefill TP ({max(p for p,_ in skipped)}) >= KV Heads ({num_kv_heads}), "
                     f"KV data is highly fragmented ({num_kv_heads // max(p for p,_ in skipped)} head per GPU). "
                     f"NIXL cannot transfer this fragmented data to a smaller Decode TP "
                     f"because the many-to-few mapping logic fails.", 'warning')
            self.log(f"     Affected: [{skipped_str}]", 'warning')
            self.log(f"     Constraint: To run PTP >= {num_kv_heads} with this model, "
                     f"Decode TP must be >= Prefill TP.", 'warning')

        # Fall back to symmetric if everything was filtered
        if not self._selected_tp_pairs:
            for tp in top_prefill:
                self._selected_tp_pairs.append((tp, tp))

        for ptp, dtp in self._selected_tp_pairs:
            label = f"TP={ptp}" if ptp == dtp else f"Prefill TP={ptp}, Decode TP={dtp}"
            tag = " (primary)" if (ptp, dtp) == primary else ""
            self.log(f"  ✅ {label}{tag}", 'success')

    def _usable_gpus_for_tp(self, tp: int) -> int:
        """Count GPUs on nodes that can actually host pods with this TP."""
        if not self.cluster_resources:
            return self.config.total_gpus
        gpu_nodes = [n for n in self.cluster_resources.nodes if n.gpus > 0]
        if not gpu_nodes:
            return self.config.total_gpus
        usable = sum(n.gpus for n in gpu_nodes if n.gpus >= tp)
        return min(usable, self.config.total_gpus)

    def _generate_splits_for_tp_pair(self, prefill_tp: int, decode_tp: int) -> List[FeasibleSplit]:
        """Generate all valid splits for a (prefill_tp, decode_tp) pair."""
        usable_gpus = self._usable_gpus_for_tp(max(prefill_tp, decode_tp))
        if usable_gpus < prefill_tp + decode_tp:
            return []
        splits = []
        for prefill_gpus in range(prefill_tp, usable_gpus, prefill_tp):
            decode_gpus = usable_gpus - prefill_gpus
            if decode_gpus >= decode_tp and decode_gpus % decode_tp == 0:
                prefill_pct = (prefill_gpus / usable_gpus) * 100
                splits.append(FeasibleSplit(
                    prefill_pods=prefill_gpus // prefill_tp,
                    decode_pods=decode_gpus // decode_tp,
                    prefill_tp=prefill_tp,
                    decode_tp=decode_tp,
                    prefill_gpus=prefill_gpus,
                    decode_gpus=decode_gpus,
                    total_gpus=usable_gpus,
                    prefill_pct=prefill_pct
                ))
        return splits

    def _smart_pd_search(self, tp_pairs: List[tuple]) -> List[FeasibleSplit]:
        """Calculate mathematically optimal P/D splits from calibration data.

        For each TP pair, uses measured per-pod throughput from Steps 2-3 to
        compute the balanced prefill/decode ratio, then returns ~3 candidate
        splits around that optimum.
        """
        import math

        prefill_by_tp = {r['tp']: r for r in self.prefill_tp_results}
        decode_by_tp = {r['tp']: r for r in self.decode_tp_results}

        smart_splits = []

        for ptp, dtp in tp_pairs:
            prefill_thr = prefill_by_tp.get(ptp, {}).get('throughput_p90', 0)
            decode_thr = decode_by_tp.get(dtp, {}).get('throughput_p90', 0)

            if prefill_thr <= 0 or decode_thr <= 0:
                self.log(f"  ⚠️  Skipping PTP={ptp}/DTP={dtp}: missing throughput data", 'warning')
                continue

            all_valid = self._generate_splits_for_tp_pair(ptp, dtp)
            if not all_valid:
                continue

            usable_gpus = self._usable_gpus_for_tp(max(ptp, dtp))
            r = decode_thr / prefill_thr
            d_ideal = usable_gpus / (r * ptp + dtp)

            candidates_d = sorted({
                max(1, math.floor(d_ideal) - 1),
                max(1, math.floor(d_ideal)),
                max(1, math.ceil(d_ideal)),
                math.ceil(d_ideal) + 1,
            })

            self.log(f"  Smart search PTP={ptp}/DTP={dtp}:", 'info')
            self.log(f"    Prefill: {prefill_thr:.2f} req/s/pod, Decode: {decode_thr:.2f} req/s/pod", 'info')
            self.log(f"    Balanced ratio P:D = {r:.2f}:1, ideal decode pods = {d_ideal:.1f}", 'info')

            valid_by_decode = {s.decode_pods: s for s in all_valid}
            selected = []
            for d in candidates_d:
                if d in valid_by_decode:
                    selected.append(valid_by_decode[d])

            if len(selected) < 2 and all_valid:
                by_distance = sorted(all_valid, key=lambda s: abs(s.decode_pods - d_ideal))
                for s in by_distance:
                    if s not in selected:
                        selected.append(s)
                    if len(selected) >= 3:
                        break

            for s in selected:
                self.log(f"    -> {s.prefill_pods}P + {s.decode_pods}D "
                         f"({s.prefill_pct:.1f}% prefill)", 'info')

            smart_splits.extend(selected)

        seen = set()
        unique = []
        for s in smart_splits:
            key = (s.prefill_pods, s.decode_pods, s.prefill_tp, s.decode_tp)
            if key not in seen:
                seen.add(key)
                unique.append(s)

        unique.sort(key=lambda s: (s.prefill_tp, s.decode_tp, s.prefill_pct))

        exhaustive_count = sum(len(self._generate_splits_for_tp_pair(p, d)) for p, d in tp_pairs)
        self.log(f"  Smart PD search: {len(unique)} candidates (vs {exhaustive_count} exhaustive)", 'success')
        return unique

    def _calculate_feasible_splits(self):
        """
        Steps 4-5: Calculate ideal P/D ratio and select splits to test.

        Uses optimal TPs from Steps 2-3 to calculate resource requirements,
        enumerate all valid splits, then select those nearest the ideal ratio.
        Supports asymmetric TP — prefill and decode can use different TP values.
        """
        # Step 4: Cluster capacity analysis
        # config.qps is concurrency (simultaneous in-flight requests), NOT requests/sec.
        # GPU cost per request (GPU-seconds): cost = tokens / TPSG
        prefill_tpsg = self.optimal_prefill_tp.tpsg
        decode_tpsg = self.optimal_decode_tp.tpsg

        if prefill_tpsg <= 0 or decode_tpsg <= 0:
            self.log("❌ Cannot calculate GPU splits: TPSG values are zero or negative", 'error')
            self.log(f"   Prefill TPSG: {prefill_tpsg}, Decode TPSG: {decode_tpsg}", 'error')
            self.log("   This indicates benchmark failures. Check gateway configuration and pod health.", 'error')
            raise ValueError(f"Invalid TPSG values: prefill={prefill_tpsg}, decode={decode_tpsg}")

        prefill_cost = self.config.isl / prefill_tpsg
        decode_cost = self.config.osl / decode_tpsg
        total_cost = prefill_cost + decode_cost

        max_throughput_pct = (prefill_cost / total_cost) * 100

        total_gpus = self.config.total_gpus
        sustainable_qps = total_gpus / total_cost / self.config.headroom
        concurrency = self.config.qps

        self.log("Step 4: Cluster Capacity Analysis", 'info')
        self.log(f"  Concurrency (simultaneous requests): {concurrency:.0f}", 'info')
        self.log(f"  GPU cost per request:", 'info')
        self.log(f"    Prefill: {self.config.isl} ISL ÷ {prefill_tpsg:.0f} TPSG = {prefill_cost:.2f} GPU-sec", 'info')
        self.log(f"    Decode:  {self.config.osl} OSL ÷ {decode_tpsg:.0f} TPSG = {decode_cost:.2f} GPU-sec", 'info')
        self.log(f"    Total:   {total_cost:.2f} GPU-sec/request", 'info')
        self.log(f"  Max-throughput prefill ratio: {max_throughput_pct:.1f}%", 'info')

        if self.config.objective == 'ttft':
            prefill_tp = self.optimal_prefill_tp.tp
            decode_tp = self.optimal_decode_tp.tp
            ideal_decode_gpus = min(int(concurrency) * decode_tp,
                                    total_gpus - prefill_tp)
            self.ideal_prefill_pct = ((total_gpus - ideal_decode_gpus) / total_gpus) * 100
            ideal_decode_pods = ideal_decode_gpus // decode_tp
            self.log(f"  Latency-optimal prefill ratio: {self.ideal_prefill_pct:.1f}%"
                     f" (targeting {ideal_decode_pods} decode pods for {int(concurrency)} users)", 'info')
        else:
            self.ideal_prefill_pct = max_throughput_pct

        self.log("", 'info')

        self.log(f"Step 5: Sustainable Throughput (with {self.config.headroom}x headroom)", 'info')
        self.log(f"  Available: {total_gpus} GPUs", 'info')
        self.log(f"  Sustainable QPS: {total_gpus} ÷ {total_cost:.2f} ÷ {self.config.headroom} = {sustainable_qps:.2f} req/s", 'info')

        sustainable_concurrency = max(1, int(total_gpus / self.config.headroom))

        self._gpu_sizing = {
            'concurrency': concurrency,
            'isl': self.config.isl,
            'osl': self.config.osl,
            'prefill_tpsg': round(prefill_tpsg, 1),
            'decode_tpsg': round(decode_tpsg, 1),
            'prefill_cost': round(prefill_cost, 2),
            'decode_cost': round(decode_cost, 2),
            'total_cost': round(total_cost, 2),
            'headroom': self.config.headroom,
            'max_throughput_pct': round(max_throughput_pct, 1),
            'ideal_prefill_pct': round(self.ideal_prefill_pct, 1),
            'sustainable_throughput_rps': round(sustainable_qps, 2),
            'sustainable_concurrency': sustainable_concurrency,
            'total_gpus': total_gpus,
        }

        # Sustainable concurrency: max concurrent users before overload.
        # From the overload check (concurrency > total_gpus / headroom),
        # the boundary is exactly total_gpus / headroom.
        sustainable_concurrency = max(1, int(total_gpus / self.config.headroom))
        implied_throughput = sustainable_qps  # cluster max in req/s

        if concurrency > sustainable_concurrency:
            self.sustainable_throughput_rps = sustainable_qps
            self.achievable_concurrency = sustainable_concurrency
            self.log(f"  Requested concurrency: {concurrency:.0f} users", 'info')
            self.log(f"  Sustainable: {sustainable_concurrency} users ({sustainable_qps:.2f} req/s)", 'info')
            self.log(f"  ⚠️  Load exceeds capacity ({concurrency:.0f} > {sustainable_concurrency} users)", 'warning')
            if self.config.use_achievable_qps:
                self.effective_concurrency = sustainable_concurrency
                self.log(f"  ✅ Scaling down to {sustainable_concurrency} concurrent users for Steps 7-8", 'success')
            else:
                self.effective_concurrency = int(self.config.qps)
                self.log(f"  ℹ️  Using original concurrency ({concurrency:.0f}) for Steps 7-8 — expect overload", 'info')
                if self.config.latency_constraint_enabled:
                    self.log(f"  ℹ️  Step 9 will find max throughput under latency SLA", 'info')
                else:
                    self.log(f"  ℹ️  Step 10 will re-test at sustainable load ({sustainable_concurrency} users)", 'info')
        else:
            self.effective_concurrency = int(self.config.qps)
            self.log(f"  ✅ Cluster can handle the load ({concurrency:.0f} users, capacity: {sustainable_concurrency} users)", 'success')

        self.log("", 'info')

        # Enumerate valid splits
        tp_pairs_to_test = getattr(self, '_selected_tp_pairs', None)
        if tp_pairs_to_test is None:
            tp_pairs_to_test = [(self.optimal_prefill_tp.tp, self.optimal_decode_tp.tp)]

        self.log("Feasible P/D Splits:", 'info')
        for ptp, dtp in tp_pairs_to_test:
            if ptp == dtp:
                self.log(f"  Testing: TP={ptp} (symmetric)", 'info')
            else:
                self.log(f"  Testing: Prefill TP={ptp}, Decode TP={dtp} (asymmetric)", 'info')

        # Generate splits for all selected TP pairs
        all_valid_splits = []
        for ptp, dtp in tp_pairs_to_test:
            tp_splits = self._generate_splits_for_tp_pair(ptp, dtp)
            all_valid_splits.extend(tp_splits)
            label = f"TP={ptp}" if ptp == dtp else f"PTP={ptp}/DTP={dtp}"
            self.log(f"  {label}: {len(tp_splits)} valid splits", 'info')

        self.log(f"  Total valid splits: {len(all_valid_splits)}", 'info')

        # Select splits to test based on search mode
        import re
        resumed_step7 = {name: row for name, row in self.completed_tests.items() if name.startswith('step7-')}

        if self.config.pd_search_mode == 'smart':
            self.log(f"\n  Search mode: Smart (calculated ~3 splits per TP pair)", 'info')
            planned = self._smart_pd_search(tp_pairs_to_test)

            if resumed_step7:
                self.log(f"  Resuming: found {len(resumed_step7)} completed step7 tests", 'info')
                planned_ids = {f"step7-{s.prefill_pods}p{s.decode_pods}d-ptp{s.prefill_tp}-dtp{s.decode_tp}" for s in planned}
                self.feasible_splits = list(planned)
                for name, row in resumed_step7.items():
                    if name not in planned_ids:
                        m = re.match(r'step7-(\d+)p(\d+)d-ptp(\d+)-dtp(\d+)', name)
                        if m:
                            pp, dp, ptp_v, dtp_v = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
                            self.feasible_splits.append(FeasibleSplit(
                                prefill_pods=pp, decode_pods=dp,
                                prefill_tp=ptp_v, decode_tp=dtp_v,
                                prefill_gpus=pp * ptp_v, decode_gpus=dp * dtp_v,
                                total_gpus=pp * ptp_v + dp * dtp_v,
                                prefill_pct=(pp * ptp_v / (pp * ptp_v + dp * dtp_v)) * 100
                            ))
            else:
                self.feasible_splits = planned

            self.feasible_splits.sort(key=lambda s: (s.prefill_tp, s.decode_tp, s.prefill_pct))
        else:
            # Exhaustive mode: test all valid splits (original behavior)
            self.log(f"\n  Search mode: Exhaustive (all valid splits)", 'info')
            max_splits = self.config.max_pd_splits

            if resumed_step7:
                self.log(f"  Resuming: found {len(resumed_step7)} completed step7 tests", 'info')

                split_by_id = {}
                for s in all_valid_splits:
                    tid = f"step7-{s.prefill_pods}p{s.decode_pods}d-ptp{s.prefill_tp}-dtp{s.decode_tp}"
                    split_by_id[tid] = s

                completed_split_ids = set()
                self.feasible_splits = []
                for name in sorted(resumed_step7.keys()):
                    if name in split_by_id:
                        self.feasible_splits.append(split_by_id[name])
                        completed_split_ids.add(name)
                    else:
                        m = re.match(r'step7-(\d+)p(\d+)d-ptp(\d+)-dtp(\d+)', name)
                        if m:
                            pp, dp, ptp_v, dtp_v = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
                            self.feasible_splits.append(FeasibleSplit(
                                prefill_pods=pp, decode_pods=dp,
                                prefill_tp=ptp_v, decode_tp=dtp_v,
                                prefill_gpus=pp * ptp_v, decode_gpus=dp * dtp_v,
                                total_gpus=pp * ptp_v + dp * dtp_v,
                                prefill_pct=(pp * ptp_v / (pp * ptp_v + dp * dtp_v)) * 100
                            ))
                            completed_split_ids.add(name)

                candidates = [s for s in all_valid_splits
                              if f"step7-{s.prefill_pods}p{s.decode_pods}d-ptp{s.prefill_tp}-dtp{s.decode_tp}" not in completed_split_ids]
                if max_splits > 0:
                    remaining_slots = max_splits - len(self.feasible_splits)
                    if remaining_slots > 0:
                        candidates.sort(key=lambda s: abs(s.prefill_pct - self.ideal_prefill_pct))
                        self.feasible_splits.extend(candidates[:remaining_slots])
                else:
                    self.feasible_splits.extend(candidates)

                self.feasible_splits.sort(key=lambda s: (s.prefill_tp, s.decode_tp, s.prefill_pct))
            elif max_splits <= 0 or len(all_valid_splits) <= max_splits:
                self.feasible_splits = all_valid_splits
                self.feasible_splits.sort(key=lambda s: (s.prefill_tp, s.decode_tp, s.prefill_pct))
            else:
                by_pair = {}
                for s in all_valid_splits:
                    key = (s.prefill_tp, s.decode_tp)
                    by_pair.setdefault(key, []).append(s)

                selected = []
                for key, splits in by_pair.items():
                    best = min(splits, key=lambda s: abs(s.prefill_pct - self.ideal_prefill_pct))
                    selected.append(best)

                selected_set = set(id(s) for s in selected)
                remaining = [s for s in all_valid_splits if id(s) not in selected_set]
                remaining.sort(key=lambda s: abs(s.prefill_pct - self.ideal_prefill_pct))
                slots_left = max_splits - len(selected)
                if slots_left > 0:
                    selected.extend(remaining[:slots_left])

                self.feasible_splits = selected
                self.feasible_splits.sort(key=lambda s: (s.prefill_tp, s.decode_tp, s.prefill_pct))

        for split in self.feasible_splits:
            self.log(f"  ✓ {split.prefill_pods}P×TP{split.prefill_tp} + {split.decode_pods}D×TP{split.decode_tp} "
                    f"= {self.config.total_gpus} GPUs ({split.prefill_pct:.1f}% prefill)", 'info')

        self.log(f"\n  Splits to test: {len(self.feasible_splits)}", 'success')

    def _search_aggregated_configs(self):
        """
        Step 6: Search for the best aggregated configuration.

        Tests all valid TP values with the full ISL+OSL workload using
        all available GPUs. This finds the actual best aggregated config
        before PD/EP testing, so the architecture comparison in Step 8
        requires no additional tests.
        """
        valid_tp = self._get_valid_tp_options()
        total_gpus = self.config.total_gpus

        self.log(f"Testing aggregated at full workload: ISL={self.config.isl}, OSL={self.config.osl}", 'info')
        self.log(f"TP values: {valid_tp}, GPUs: {total_gpus}", 'info')

        for i, tp in enumerate(valid_tp):
            if self._should_stop():
                break

            replicas = total_gpus // tp
            if replicas < 1:
                continue
            actual_gpus = tp * replicas

            test_id = f"step6-agg-tp{tp}-{replicas}r"
            self.log(f"  Test {i + 1}/{len(valid_tp)}: TP={tp}, {replicas} replicas ({actual_gpus} GPUs)", 'info')

            if test_id in self.completed_tests:
                row = self.completed_tests[test_id]
                result = self._make_test_result_from_db(row)
                self.log("    ⏩ Resuming from DB (already completed)", 'info')
            else:
                test_config = self._create_aggregated_config(
                    tp=tp,
                    num_gpus=actual_gpus,
                    isl=self.config.isl,
                    osl=self.config.osl,
                    test_id=test_id,
                    use_concurrency=True
                )

                result = self.orchestrator.run_test(
                    test_config,
                    cleanup=True,
                    log_callback=lambda msg: self.log(msg, 'info'),
                    stop_check=self._should_stop
                )

                self.all_test_results.append((test_config, result))
                self._save_test_to_database(test_config, result)

                if not result or not result.guidellm_success:
                    self.log("    ❌ Test failed - STOPPING optimization", 'error')
                    self.log(f"    🔍 Debug: kubectl get pods -n {self.config.namespace} -l test-id={test_id}", 'error')
                    raise RuntimeError(f"Test {test_id} failed - stopping optimization")

            ttft = result.ttft_p90 or result.ttft_p50 or 1000000.0
            throughput = result.throughput_p90 or result.throughput_p50 or 0.0

            self.log(f"    ✅ TTFT p90: {ttft:.1f}ms, Throughput p90: {throughput:.2f} req/s", 'success')
            self.aggregated_search_results.append((tp, result))

        if not self.aggregated_search_results:
            self.log("❌ No aggregated test results!", 'error')
            return

        # Select best based on optimization objective
        if self.config.objective == 'throughput':
            best_tp, best_result = max(
                self.aggregated_search_results,
                key=lambda x: x[1].throughput_p90 if x[1].throughput_p90 else 0.0
            )
            criterion = "highest throughput"
        else:
            best_tp, best_result = min(
                self.aggregated_search_results,
                key=lambda x: x[1].ttft_p90 if x[1].ttft_p90 else 1000000.0
            )
            criterion = "lowest TTFT"

        self.aggregated_result = best_result
        self.aggregated_tp = best_tp
        self.aggregated_gpus = total_gpus

        best_ttft = best_result.ttft_p90 or best_result.ttft_p50 or 0
        best_tput = best_result.throughput_p90 or best_result.throughput_p50 or 0

        self.log("", 'info')
        self.log(f"✅ Best Aggregated: TP={best_tp}, {total_gpus // best_tp} replicas "
                 f"(selected by {criterion})", 'success')
        self.log(f"   TTFT p90: {best_ttft:.1f}ms, Throughput p90: {best_tput:.2f} req/s", 'info')

    def _optimize_pd_splits(self):
        """
        Step 7: Exhaustively test all selected P/D splits.

        Tests each feasible split and identifies the Pareto front
        (configurations where no other is better in both TTFT and throughput).
        """
        if not self.feasible_splits:
            self.log("❌ No feasible splits to test!", 'error')
            return

        self.log(f"Testing all {len(self.feasible_splits)} P/D split configurations...", 'info')
        isl_s = f"ISL={self.config.isl}" + (f"(σ={self.config.isl_stdev})" if self.config.isl_stdev else "")
        osl_s = f"OSL={self.config.osl}" + (f"(σ={self.config.osl_stdev})" if self.config.osl_stdev else "")
        turns_s = f", Turns={self.config.turns}" if self.config.turns > 1 else ""
        rate_label = f"Concurrency={int(self.config.qps)}" if self.config.rate_type == 'concurrent' else f"Rate={int(self.config.qps)} req/s ({self.config.rate_type})"
        self.log(f"Workload: {isl_s}, {osl_s}, {rate_label}{turns_s}", 'info')

        for i, split in enumerate(self.feasible_splits):
            if self._should_stop():
                break

            test_id = f"step7-{split.prefill_pods}p{split.decode_pods}d-ptp{split.prefill_tp}-dtp{split.decode_tp}"
            self.log(f"  Test {i + 1}/{len(self.feasible_splits)}: "
                    f"{split.prefill_pods}P×TP{split.prefill_tp} + "
                    f"{split.decode_pods}D×TP{split.decode_tp} ({split.prefill_pct:.0f}% prefill)", 'info')

            # Check for completed test from previous run
            if test_id in self.completed_tests:
                row = self.completed_tests[test_id]
                result = self._make_test_result_from_db(row)
                self.log("    ⏩ Resuming from DB (already completed)", 'info')
            else:
                test_config = self._create_pd_config(split)

                result = self.orchestrator.run_test(
                    test_config,
                    cleanup=True,
                    log_callback=lambda msg: self.log(msg, 'info'),
                    stop_check=self._should_stop
                )

                self.all_test_results.append((test_config, result))
                self._save_test_to_database(test_config, result)
                self._check_pod_errors(test_config, result)

                if not result or not result.guidellm_success:
                    self.log("    ❌ Test failed - STOPPING optimization", 'error')
                    self.log(f"    🔍 Debug: kubectl get pods -n {self.config.namespace} -l test-id={test_id}", 'error')
                    self.log(f"    🧹 Cleanup: kubectl delete lws -n {self.config.namespace} -l test-id={test_id}", 'error')
                    raise RuntimeError(f"Test {test_id} failed - stopping optimization")

            ttft = result.ttft_p90 if result.ttft_p90 else result.ttft_p50 if result.ttft_p50 else 1000000.0
            throughput = result.throughput_p90 if result.throughput_p90 else result.throughput_p50 if result.throughput_p50 else 0.0

            self.log(f"    ✅ TTFT p90: {ttft:.1f}ms, Throughput p90: {throughput:.2f} req/s", 'success')

            self.pareto_results.append((split, result))

        # Find Pareto front from results
        pareto_front = self._find_pareto_front()

        self.log("", 'info')
        self.log(f"✅ Found {len(pareto_front)} Pareto optimal configurations:", 'success')
        for i, (split, result) in enumerate(pareto_front, 1):
            ttft = result.ttft_p90 or result.ttft_p50 or 0
            throughput = result.throughput_p90 or result.throughput_p50 or 0
            self.log(f"  {i}. {split.prefill_pods}P×TP{split.prefill_tp} + {split.decode_pods}D×TP{split.decode_tp}: "
                    f"TTFT={ttft:.1f}ms, Throughput={throughput:.2f} req/s", 'info')

    def _validate_pd_vs_aggregated(self):
        """
        Step 8: Compare best PD config against best Aggregated from Step 6.

        No new tests — uses the best aggregated result already found in Step 6
        and the best PD result from Step 7.
        """
        if not self.pareto_results:
            self.log("⚠️  No PD results to compare — skipping Step 8", 'warning')
            return

        if not self.aggregated_result:
            self.log("⚠️  No aggregated results to compare — skipping Step 8", 'warning')
            return

        # Best PD by TTFT
        best_split, best_pd_result = min(
            self.pareto_results,
            key=lambda x: x[1].ttft_p90 if x[1].ttft_p90 else 1000000.0
        )

        best_pd_ttft = best_pd_result.ttft_p90 or best_pd_result.ttft_p50 or 0
        best_pd_tput = best_pd_result.throughput_p90 or best_pd_result.throughput_p50 or 0

        agg_ttft = self.aggregated_result.ttft_p90 or self.aggregated_result.ttft_p50 or 1000000.0
        agg_tput = self.aggregated_result.throughput_p90 or self.aggregated_result.throughput_p50 or 0.0

        self.log(f"Best PD: {best_split.prefill_pods}P×TP{best_split.prefill_tp} + "
                f"{best_split.decode_pods}D×TP{best_split.decode_tp}", 'info')
        self.log(f"  TTFT p90: {best_pd_ttft:.1f}ms, Throughput p90: {best_pd_tput:.2f} req/s", 'info')
        self.log(f"Best Aggregated: TP={self.aggregated_tp}, "
                f"{self.aggregated_gpus // self.aggregated_tp} replicas", 'info')
        self.log(f"  TTFT p90: {agg_ttft:.1f}ms, Throughput p90: {agg_tput:.2f} req/s", 'info')
        self.log("", 'info')

        # Compare
        ttft_diff = best_pd_ttft - agg_ttft
        ttft_pct = (ttft_diff / agg_ttft * 100) if agg_ttft > 0 else 0
        tput_diff = best_pd_tput - agg_tput
        tput_pct = (tput_diff / agg_tput * 100) if agg_tput > 0 else 0

        self.log("📊 PD vs Aggregated Comparison:", 'decision')
        self.log(f"  TTFT p90:       PD={best_pd_ttft:.1f}ms vs Agg={agg_ttft:.1f}ms "
                f"({'PD wins by' if ttft_diff < 0 else 'Agg wins by'} {abs(ttft_pct):.1f}%)", 'info')
        self.log(f"  Throughput p90:  PD={best_pd_tput:.2f} vs Agg={agg_tput:.2f} req/s "
                f"({'PD wins by' if tput_diff > 0 else 'Agg wins by'} {abs(tput_pct):.1f}%)", 'info')

        if agg_ttft < best_pd_ttft and agg_tput >= best_pd_tput:
            self.log("", 'info')
            self.log("⚡ AGGREGATED IS BETTER — lower TTFT and equal/higher throughput", 'decision')
        elif agg_ttft < best_pd_ttft:
            self.log("", 'info')
            self.log("⚡ AGGREGATED HAS BETTER TTFT but lower throughput — check the report for trade-offs", 'decision')
        else:
            self.log("", 'info')
            self.log("✅ PD CONFIRMED — PD has equal or better TTFT than Aggregated", 'decision')

    def _should_run_latency_bounded_search(self) -> bool:
        """Check if Step 9 (latency-bounded throughput maximization) should run."""
        return self.config.latency_constraint_enabled

    def _run_latency_bounded_search(self):
        """
        Step 9: Find maximum throughput under a user-defined latency SLA.

        Uses exponential search + bisection over concurrency levels for both
        the best PD and best aggregated configurations.  The starting
        concurrency comes from the calibrated (sustainable) QPS calculation.
        """
        from core.user_defined_tuning import (
            LatencyBinarySearch, LatencyConstraintConfig
        )

        self.log("=" * 60, 'info')
        self.log("Step 9: Latency-Bounded Throughput Maximization", 'info')
        self.log("=" * 60, 'info')

        constraint = LatencyConstraintConfig(
            target_ms=float(self.config.latency_constraint_ms),
            percentile=self.config.latency_constraint_percentile,
        )

        default_c = self.achievable_concurrency if self.achievable_concurrency else int(self.config.qps)
        self.log(f"🎯 Target: TTFT {constraint.percentile.upper()} "
                 f"≤ {constraint.target_ms}ms", 'info')
        self.log(f"   Default starting concurrency: {default_c} concurrent users", 'info')
        self.log("", 'info')

        self.latency_search_results = {}

        def _estimate_starting_c(result, target_ms, percentile):
            """Estimate starting concurrency from a prior benchmark result.

            Uses P90 throughput (most stable measure of actual system capacity)
            scaled by the ratio of target latency to observed latency at the
            target percentile.
            """
            latency_field = f'ttft_{percentile}'
            observed_latency = getattr(result, latency_field, None)
            observed_tput = result.throughput_p90
            if not observed_latency or observed_latency <= 0 or not observed_tput or observed_tput <= 0:
                return None
            ceiling = observed_tput * (target_ms / observed_latency)
            estimated = max(1, int(ceiling * 0.6))
            return estimated

        def run_test_fn(cfg):
            result = self.orchestrator.run_test(
                cfg,
                cleanup=True,
                log_callback=lambda msg: self.log(msg, 'info'),
                stop_check=self._should_stop
            )
            self.all_test_results.append((cfg, result))
            return result

        def save_test_fn(cfg, result):
            self._save_test_to_database(cfg, result)
            self._check_pod_errors(cfg, result)

        # --- Search PD ---
        best_split = None
        if self.pareto_results:
            best_split, best_pd_result = min(
                self.pareto_results,
                key=lambda x: x[1].ttft_p90 if x[1].ttft_p90 else 1e9
            )
            self.log(f"📊 PD config: {best_split.prefill_pods}P×TP{best_split.prefill_tp} + "
                     f"{best_split.decode_pods}D×TP{best_split.decode_tp}", 'info')

            pd_starting_c = _estimate_starting_c(
                best_pd_result,
                constraint.target_ms, constraint.percentile
            )
            step7_latency = getattr(best_pd_result, f'ttft_{constraint.percentile}', None)
            step7_tput = best_pd_result.throughput_p90
            if pd_starting_c is not None:
                self.log(f"   Step 7 measured: throughput P90={step7_tput:.1f} req/s, "
                         f"TTFT {constraint.percentile.upper()}={step7_latency:.0f}ms "
                         f"→ estimated start c={pd_starting_c}", 'info')
            else:
                pd_starting_c = default_c
                self.log(f"   No latency data for {constraint.percentile.upper()}, "
                         f"using default c={default_c}", 'info')

            def create_pd_config(concurrency, test_id):
                cfg = self._create_pd_config(best_split)
                cfg.test_id = test_id
                cfg.num_users = concurrency
                cfg.request_rate = concurrency
                cfg.test_duration = max(180, self.config.test_duration)
                return cfg

            pd_search = LatencyBinarySearch(
                constraint=constraint,
                run_test_fn=run_test_fn,
                create_config_fn=create_pd_config,
                log_fn=self.log,
                stop_check_fn=self._should_stop,
                save_test_fn=save_test_fn,
                completed_tests=self.completed_tests,
                make_result_from_db_fn=self._make_test_result_from_db,
                starting_concurrency=pd_starting_c,
                architecture='pd',
                db_manager=self.db_manager,
                run_id=self.run_id,
            )
            pd_result = pd_search.search()
            if pd_result:
                self.latency_search_results['pd'] = pd_result
            self.log("", 'info')

            if self._should_stop():
                return

        # --- Search Aggregated configs ---
        # Test the primary aggregated TP (selected by objective in Step 6)
        # AND the best-throughput TP if different, since Step 9 maximizes throughput under SLA
        agg_configs_to_test = []
        if self.aggregated_tp:
            agg_configs_to_test.append((self.aggregated_tp, self.aggregated_gpus, f"aggregated-tp{self.aggregated_tp}"))

            if self.aggregated_search_results:
                best_tput_tp, _ = max(
                    self.aggregated_search_results,
                    key=lambda x: x[1].throughput_p90 if x[1].throughput_p90 else 0.0
                )
                if best_tput_tp != self.aggregated_tp:
                    tput_gpus = self.config.total_gpus
                    replicas = tput_gpus // best_tput_tp
                    actual_gpus = best_tput_tp * replicas
                    agg_configs_to_test.append((best_tput_tp, actual_gpus, f"aggregated-tp{best_tput_tp}"))
                    self.log(f"  Also testing best-throughput aggregated TP={best_tput_tp}", 'info')

        for agg_tp, agg_gpus, agg_arch in agg_configs_to_test:
            if self._should_stop():
                break

            self.log(f"📊 Aggregated config: TP={agg_tp}, "
                     f"{agg_gpus} GPUs", 'info')

            # Estimate starting concurrency from Step 6 aggregated result
            agg_starting_c = default_c
            agg_step6_result = None
            for tp_val, res in (self.aggregated_search_results or []):
                if tp_val == agg_tp:
                    agg_step6_result = res
                    break
            if agg_step6_result:
                est = _estimate_starting_c(
                    agg_step6_result,
                    constraint.target_ms, constraint.percentile
                )
                if est is not None:
                    step6_latency = getattr(agg_step6_result, f'ttft_{constraint.percentile}', None)
                    step6_tput = agg_step6_result.throughput_p90
                    self.log(f"   Step 6 measured: throughput P90={step6_tput:.1f} req/s, "
                             f"TTFT {constraint.percentile.upper()}={step6_latency:.0f}ms "
                             f"→ estimated start c={est}", 'info')
                    agg_starting_c = est

            def create_agg_config(concurrency, test_id, _tp=agg_tp, _gpus=agg_gpus):
                cfg = self._create_aggregated_config(
                    tp=_tp,
                    num_gpus=_gpus,
                    isl=self.config.isl,
                    osl=self.config.osl,
                    test_id=test_id,
                    use_concurrency=True
                )
                cfg.num_users = concurrency
                cfg.request_rate = concurrency
                cfg.test_duration = max(180, self.config.test_duration)
                return cfg

            agg_search = LatencyBinarySearch(
                constraint=constraint,
                run_test_fn=run_test_fn,
                create_config_fn=create_agg_config,
                log_fn=self.log,
                stop_check_fn=self._should_stop,
                save_test_fn=save_test_fn,
                completed_tests=self.completed_tests,
                make_result_from_db_fn=self._make_test_result_from_db,
                starting_concurrency=agg_starting_c,
                architecture=agg_arch,
                db_manager=self.db_manager,
                run_id=self.run_id,
            )
            agg_result = agg_search.search()
            if agg_result:
                self.latency_search_results[agg_arch] = agg_result
            self.log("", 'info')

        # --- Summary ---
        if self.latency_search_results:
            self.log("📊 Latency Search Summary:", 'decision')
            for arch, res in self.latency_search_results.items():
                self.log(f"  {arch.upper()}: c={res.optimal_concurrency}, "
                         f"throughput={res.achieved_throughput:.2f} req/s, "
                         f"TTFT {res.target_percentile.upper()}="
                         f"{res.achieved_latency_ms:.1f}ms "
                         f"({res.n_trials} trials)", 'success')

            # Pick overall winner by throughput
            best_arch = max(self.latency_search_results,
                           key=lambda k: self.latency_search_results[k].achieved_throughput)
            best = self.latency_search_results[best_arch]
            self.log(f"  🏆 Winner: {best_arch.upper()} with "
                     f"{best.achieved_throughput:.2f} req/s", 'decision')

            # Store as latency_bounded_result for compatibility with _build_results
            from core.user_defined_tuning import LatencyBoundedResult
            self.latency_bounded_result = LatencyBoundedResult(
                optimal_concurrency=best.optimal_concurrency,
                achieved_throughput=best.achieved_throughput,
                achieved_latency_ms=best.achieved_latency_ms,
                target_latency_ms=best.target_latency_ms,
                target_percentile=best.target_percentile,
                n_trials=sum(r.n_trials for r in self.latency_search_results.values()),
                best_config_source=best_arch,
            )

    def _should_run_step10(self) -> bool:
        """Check if Step 10 (calibrated load validation) should run.

        Step 10 runs when:
        1. The concurrency implies load beyond sustainable QPS
        2. The user did NOT enable 'use_achievable_qps'
        3. We have PD results from Step 7
        4. Step 9 (latency-bounded search) did NOT run — it already
           explores concurrency levels including calibrated load
        """
        return (
            self.achievable_concurrency is not None
            and not self.config.use_achievable_qps
            and not self.config.latency_constraint_enabled
            and len(self.pareto_results) > 0
        )

    def _validate_at_calibrated_load(self):
        """
        Step 10: Re-test best PD and Aggregated at sustainable concurrency.

        Steps 7-8 ran at the user's original concurrency which overloads the cluster.
        This step re-runs the best config at a sustainable level to show
        realistic latency and throughput numbers.
        """
        calibrated_concurrency = self.achievable_concurrency

        # Find best PD config by TTFT from Step 7
        best_split, best_pd_result = min(
            self.pareto_results,
            key=lambda x: x[1].ttft_p90 if x[1].ttft_p90 else 1000000.0
        )

        overloaded_ttft = best_pd_result.ttft_p90 or best_pd_result.ttft_p50 or 0
        overloaded_tput = best_pd_result.throughput_p90 or best_pd_result.throughput_p50 or 0

        self.log(f"Re-testing best PD config at calibrated load ({calibrated_concurrency:.0f} users "
                f"vs original {self.config.qps:.0f} users)", 'info')
        self.log(f"Best PD: {best_split.prefill_pods}P×TP{best_split.prefill_tp} + "
                f"{best_split.decode_pods}D×TP{best_split.decode_tp}", 'info')
        self.log(f"  Step 7 results (overloaded): TTFT={overloaded_ttft:.1f}ms, "
                f"Throughput={overloaded_tput:.2f} req/s", 'info')
        self.log("", 'info')

        # --- Test best PD at calibrated load ---
        test_id = (f"step10-{best_split.prefill_pods}p{best_split.decode_pods}d"
                  f"-ptp{best_split.prefill_tp}-dtp{best_split.decode_tp}")

        if test_id in self.completed_tests:
            row = self.completed_tests[test_id]
            pd_result = self._make_test_result_from_db(row)
            self.log("  ⏩ PD test: resuming from DB (already completed)", 'info')
        else:
            # Create a PD config identical to the best split but with calibrated load
            pd_config = self._create_pd_config(best_split)
            pd_config.test_id = test_id
            pd_config.num_users = int(calibrated_concurrency)
            pd_config.request_rate = int(calibrated_concurrency)

            pd_result = self.orchestrator.run_test(
                pd_config,
                cleanup=True,
                log_callback=lambda msg: self.log(msg, 'info'),
                stop_check=self._should_stop
            )

            self.all_test_results.append((pd_config, pd_result))
            self._save_test_to_database(pd_config, pd_result)
            self._check_pod_errors(pd_config, pd_result)

            if not pd_result or not pd_result.guidellm_success:
                self.log("❌ PD calibrated load test failed", 'error')
                return

        cal_pd_ttft = pd_result.ttft_p90 or pd_result.ttft_p50 or 0
        cal_pd_tput = pd_result.throughput_p90 or pd_result.throughput_p50 or 0
        self.calibrated_pd_result = pd_result

        self.log(f"  ✅ PD at calibrated load: TTFT={cal_pd_ttft:.1f}ms, "
                f"Throughput={cal_pd_tput:.2f} req/s", 'success')

        if self._should_stop():
            self.log("🛑 Optimization stopped — skipping aggregated calibration test", 'warning')
            return

        # --- Test Aggregated at calibrated load ---
        if not self.aggregated_tp:
            self.log("⚠️  No aggregated baseline — skipping aggregated re-test", 'warning')
            return
        agg_tp = self.aggregated_tp
        total_gpus = self.aggregated_gpus
        agg_test_id = f"step10-aggregated-tp{agg_tp}"
        # Backwards compat: check old ID format with total_gpus embedded
        if agg_test_id not in self.completed_tests:
            old_id = f"step10-aggregated-{total_gpus}gpu-tp{agg_tp}"
            if old_id in self.completed_tests:
                agg_test_id = old_id

        if agg_test_id in self.completed_tests:
            row = self.completed_tests[agg_test_id]
            agg_result = self._make_test_result_from_db(row)
            self.log("  ⏩ Aggregated test: resuming from DB (already completed)", 'info')
        else:
            agg_config = self._create_aggregated_config(
                tp=agg_tp,
                num_gpus=total_gpus,
                isl=self.config.isl,
                osl=self.config.osl,
                test_id=agg_test_id,
                use_concurrency=True
            )
            # Override with calibrated load
            agg_config.num_users = int(calibrated_concurrency)
            agg_config.request_rate = int(calibrated_concurrency)

            agg_result = self.orchestrator.run_test(
                agg_config,
                cleanup=True,
                log_callback=lambda msg: self.log(msg, 'info'),
                stop_check=self._should_stop
            )

            self.all_test_results.append((agg_config, agg_result))
            self._save_test_to_database(agg_config, agg_result)
            self._check_pod_errors(agg_config, agg_result)

            if not agg_result or not agg_result.guidellm_success:
                self.log("❌ Aggregated calibrated load test failed", 'error')
                self.log("   PD calibrated results stand", 'warning')
                return

        cal_agg_ttft = agg_result.ttft_p90 or agg_result.ttft_p50 or 0
        cal_agg_tput = agg_result.throughput_p90 or agg_result.throughput_p50 or 0
        self.calibrated_agg_result = agg_result

        self.log(f"  ✅ Aggregated at calibrated load: TTFT={cal_agg_ttft:.1f}ms, "
                f"Throughput={cal_agg_tput:.2f} req/s", 'success')
        self.log("", 'info')

        # --- Compare ---
        self.log(f"📊 Calibrated Load Results ({int(calibrated_concurrency)} users):", 'decision')

        ttft_diff = cal_pd_ttft - cal_agg_ttft
        ttft_pct = (ttft_diff / cal_agg_ttft * 100) if cal_agg_ttft > 0 else 0
        tput_diff = cal_pd_tput - cal_agg_tput
        tput_pct = (tput_diff / cal_agg_tput * 100) if cal_agg_tput > 0 else 0

        self.log(f"  TTFT p90:       PD={cal_pd_ttft:.1f}ms vs Agg={cal_agg_ttft:.1f}ms "
                f"({'PD wins by' if ttft_diff < 0 else 'Agg wins by'} {abs(ttft_pct):.1f}%)", 'info')
        self.log(f"  Throughput p90:  PD={cal_pd_tput:.2f} vs Agg={cal_agg_tput:.2f} req/s "
                f"({'PD wins by' if tput_diff > 0 else 'Agg wins by'} {abs(tput_pct):.1f}%)", 'info')
        self.log("", 'info')

        # Compare with overloaded results
        if overloaded_ttft > 0:
            ttft_improvement = ((overloaded_ttft - cal_pd_ttft) / overloaded_ttft) * 100
            self.log("📉 Impact of load reduction on best PD config:", 'info')
            self.log(f"  TTFT:       {overloaded_ttft:.1f}ms → {cal_pd_ttft:.1f}ms "
                    f"({ttft_improvement:+.1f}%)", 'info')
            self.log(f"  Throughput: {overloaded_tput:.2f} → {cal_pd_tput:.2f} req/s", 'info')

    def _find_pareto_front(self) -> List[Tuple[FeasibleSplit, 'TestResult']]:
        """
        Find Pareto front from P/D split results.

        A configuration is Pareto optimal if no other configuration has
        both lower TTFT and higher throughput.
        """
        pareto = []

        for i, (split_i, result_i) in enumerate(self.pareto_results):
            ttft_i = result_i.ttft_p90 or result_i.ttft_p50 or 1000000.0
            tput_i = result_i.throughput_p90 or result_i.throughput_p50 or 0.0

            dominated = False
            for j, (split_j, result_j) in enumerate(self.pareto_results):
                if i == j:
                    continue
                ttft_j = result_j.ttft_p90 or result_j.ttft_p50 or 1000000.0
                tput_j = result_j.throughput_p90 or result_j.throughput_p50 or 0.0

                # j dominates i if j is better or equal in both and strictly better in one
                if ttft_j <= ttft_i and tput_j >= tput_i and (ttft_j < ttft_i or tput_j > tput_i):
                    dominated = True
                    break

            if not dominated:
                pareto.append((split_i, result_i))

        return pareto

    def _create_aggregated_config(
        self,
        tp: int,
        num_gpus: int,
        isl: int,
        osl: int,
        test_id: str,
        use_concurrency: bool = False
    ) -> TestConfig:
        """Create aggregated architecture test config.

        Args:
            use_concurrency: If True, use concurrent rate type with num_users.
                             All steps use concurrent/num_users to measure under realistic load.
        """
        # Use effective_concurrency for Steps 7-8 (may be scaled down), original for Steps 2-3
        concurrency = self.effective_concurrency if use_concurrency else int(self.config.qps)

        gpu_memory_utilization = self._compute_gpu_mem_util(tp)
        allocated_gb = self._gpu_vram_gb * gpu_memory_utilization
        reserve_gb = self._gpu_vram_gb - allocated_gb
        self.log(f"   Memory: gpu_memory_utilization={gpu_memory_utilization:.4f} "
                 f"→ {allocated_gb:.0f}GB allocated, {reserve_gb:.0f}GB reserved for overhead (per GPU)")

        replicas = num_gpus // tp
        mem, cpu = self._get_pod_resources(tp=tp, total_pods=replicas)

        max_num_seqs = self._compute_max_num_seqs(tp)

        # Only apply stdev when using the full workload ISL/OSL (Steps 7-8),
        # not for calibration tests (Steps 2-3) where ISL or OSL is fixed to 1
        is_calibration = (isl != self.config.isl) or (osl != self.config.osl)
        max_batched = None if is_calibration else self._compute_max_num_batched_tokens(tp)

        cfg = TestConfig(
            test_id=test_id,
            architecture='aggregated',
            model_name=self.config.model_name,
            tensor_parallelism=tp,
            replicas=replicas,
            namespace=self.config.namespace,
            isl=isl,
            osl=osl,
            num_users=concurrency,
            request_type=self.config.rate_type if use_concurrency else 'constant',
            request_rate=concurrency if use_concurrency else 1,
            test_duration=self.config.test_duration,
            stop_mode=self.config.stop_mode,
            max_requests=self.config.max_requests,
            max_num_batched_tokens=max_batched,
            isl_stdev=None if is_calibration else self.config.isl_stdev,
            osl_stdev=None if is_calibration else self.config.osl_stdev,
            turns=1 if is_calibration else self.config.turns,
            image=self.config.image,
            pvc_name=self.config.pvc_name,
            nccl_ib_hca=self.config.nccl_ib_hca,
            max_model_len=self.config.max_model_len,
            gpu_memory_utilization=gpu_memory_utilization,
            gpu_vram_gb=self._gpu_vram_gb,
            max_num_seqs=max_num_seqs,
            optimization_goal='ttft',
            network_type=self.config.network_type,
            rdma_device_resources=self.config.rdma_device_resources or [],
            rdma_nics_per_node=self.config.rdma_nics_per_node or 0,

            memory_request=mem,
            memory_limit=mem,
            cpu_request=cpu,
            cpu_limit=cpu,
            selected_nodes=self.config.selected_nodes or [],
            workload_mode='synthetic' if is_calibration else (self.config.workload_mode or 'synthetic'),
            dataset_source=None if is_calibration else self.config.dataset_source,
            dataset_column=None if is_calibration else self.config.dataset_column,
            dataset_max_output=self.config.dataset_max_output or 256,
            epp_config=self._build_epp_config(),
            block_size=self._compute_block_size(),
        )
        return self._apply_advanced_vllm(cfg) if not is_calibration else cfg

    def _get_measured_overhead(self, tp: int) -> Optional[float]:
        """Get measured vLLM fixed overhead from Steps 2-3 results for a given TP."""
        for config, result in self.all_test_results:
            if (result.vllm_fixed_overhead_gb is not None
                    and getattr(config, 'tensor_parallelism', None) == tp):
                return result.vllm_fixed_overhead_gb
        return None

    def _compute_gpu_mem_util(self, tp: int) -> float:
        """Compute gpu_memory_utilization per TP.

        When profiled data from Steps 2-3 is available, uses measured overhead
        to compute a precise U for this TP. Otherwise falls back to a safe default.
        Higher TP = less model weight per GPU = more room, so the fallback
        scales with TP.
        """
        measured = self._get_measured_overhead(tp)
        if measured is not None and measured > 0:
            # Measured overhead includes model weights + CUDA graphs + workspace.
            # Add a small buffer (2 GiB) for allocation fragmentation.
            safe_budget = self._gpu_vram_gb - measured - 2.0
            if safe_budget > 0:
                u = round(safe_budget / self._gpu_vram_gb, 2)
                u = min(u, 0.95)
                self.log(f"   gpu_memory_utilization={u} (profiled: measured overhead={measured:.1f}GB, "
                         f"usable={safe_budget:.0f}/{self._gpu_vram_gb:.0f}GB)")
                return u

        # Fallback: scale reserve with pod density (lower TP = more pods/node)
        gpus_per_node = 8
        if self.cluster_resources:
            gpu_nodes = [n for n in self.cluster_resources.nodes if n.gpus > 0]
            if gpu_nodes:
                gpus_per_node = max(n.gpus for n in gpu_nodes)
        pods_per_node = max(gpus_per_node // tp, 1)
        reserve_pct = 0.05 + (pods_per_node - 1) * 0.008
        reserve_gb = max(self._gpu_vram_gb * reserve_pct, 5.0)
        u = round((self._gpu_vram_gb - reserve_gb) / self._gpu_vram_gb, 2)
        self.log(f"   gpu_memory_utilization={u} (estimated: {pods_per_node} pods/node, "
                 f"reserving {reserve_gb:.1f}GB for overhead)")
        return u

    def _compute_max_num_seqs(self, tp: int) -> Optional[int]:
        """Compute max_num_seqs from profiled available KV cache memory.

        Uses the 'Available KV cache memory' value measured from Steps 2-3 pod logs
        and the KV cache per sequence (computed from model architecture, TP, max_model_len).
        Returns None if no profile data available (vLLM will use its default).
        """
        measured_kv_gb = None
        for config, result in self.all_test_results:
            if (result.vllm_available_kv_gb is not None
                    and getattr(config, 'tensor_parallelism', None) == tp):
                measured_kv_gb = result.vllm_available_kv_gb
                break

        if measured_kv_gb is None or measured_kv_gb <= 0:
            return None

        # KV cache per sequence: 2 (K+V) × layers × kv_heads/TP × head_dim × max_model_len × 2 bytes
        if self._model_config:
            num_layers = self._model_config.get('num_hidden_layers', 32)
            num_kv_heads = self._model_config.get('num_key_value_heads')
            if num_kv_heads is None:
                num_kv_heads = self._model_config.get('num_attention_heads', 32)
            hidden_size = self._model_config.get('hidden_size', 4096)
            num_attention_heads = self._model_config.get('num_attention_heads', 32)
            head_dim = hidden_size // num_attention_heads
        else:
            num_layers, num_kv_heads, head_dim = 32, 8, 128

        kv_heads_per_gpu = max(num_kv_heads // tp, 1)
        kv_per_seq_gb = (2 * num_layers * kv_heads_per_gpu * head_dim
                         * self.config.max_model_len * 2) / (1024**3)

        if kv_per_seq_gb <= 0:
            return None

        max_seqs = int(measured_kv_gb / kv_per_seq_gb)
        max_seqs = max(max_seqs, 1)
        self.log(f"   max_num_seqs(TP={tp}): {max_seqs} "
                 f"(KV avail={measured_kv_gb:.1f}GB, per_seq={kv_per_seq_gb:.3f}GB)")
        return max_seqs

    def _compute_max_num_batched_tokens(self, tp: int) -> Optional[int]:
        """Compute max_num_batched_tokens from calibration prefill TPSG.

        Limits how many tokens vLLM processes in a single forward pass.
        Uses the measured prefill throughput to compute a batch size that
        completes within a target latency budget (~100ms), preventing
        large batches from causing latency spikes.
        """
        if not self.optimal_prefill_tp or self.optimal_prefill_tp.tpsg <= 0:
            return None

        target_batch_latency_s = 0.2
        tokens_per_second_per_gpu = self.optimal_prefill_tp.tpsg
        batch_budget = int(tokens_per_second_per_gpu * tp * target_batch_latency_s)
        clamped = max(2048, min(batch_budget, self.config.max_model_len))

        self.log(f"   max_num_batched_tokens(TP={tp}): {clamped} "
                 f"(prefill_TPSG={tokens_per_second_per_gpu:.0f} × TP={tp} × {target_batch_latency_s}s "
                 f"= {batch_budget}, clamped to [{2048}, {self.config.max_model_len}])")
        return clamped

    def _apply_advanced_vllm(self, cfg: TestConfig) -> TestConfig:
        """Apply user's advanced vLLM overrides to a TestConfig."""
        adv = self.config.advanced_vllm
        if not adv:
            return cfg
        val_fields = {
            'max_model_len': 'max_model_len',
            'gpu_memory_utilization': 'gpu_memory_utilization',
            'max_num_seqs': 'max_num_seqs',
            'max_num_batched_tokens': 'max_num_batched_tokens',
            'dtype': 'dtype',
            'kv_cache_dtype': 'kv_cache_dtype',
            'pipeline_parallel_size': 'pipeline_parallel_size',
            'tool_call_parser': 'tool_call_parser',
            'block_size': 'block_size',
        }
        for key, attr in val_fields.items():
            setting = adv.get(key)
            if setting and setting.get('mode') == 'custom' and setting.get('value') is not None:
                val = setting['value']
                if attr in ('max_model_len', 'max_num_seqs', 'max_num_batched_tokens', 'pipeline_parallel_size', 'block_size'):
                    val = int(val)
                elif attr == 'gpu_memory_utilization':
                    val = float(val)
                setattr(cfg, attr, val)

        toggle_fields = {
            'enable_prefix_caching': 'enable_prefix_caching',
            'disable_custom_all_reduce': 'disable_custom_all_reduce',
            'enable_auto_tool_choice': 'enable_auto_tool_choice',
            'trust_remote_code': 'trust_remote_code',
            'disable_log_requests': 'disable_log_requests',
            'vllm_debug_logs': 'vllm_debug_logs',
            'nccl_debug_logs': 'nccl_debug_logs',
        }
        for key, attr in toggle_fields.items():
            setting = adv.get(key)
            if setting and setting.get('mode') in ('on', 'off'):
                setattr(cfg, attr, setting['mode'] == 'on')

        return cfg

    def _create_pd_config(self, split: FeasibleSplit) -> TestConfig:
        """Create PD architecture test config."""
        concurrency = self.effective_concurrency

        # gpu_memory_utilization: same for prefill and decode — give vLLM max safe allocation.
        # vLLM handles internal breakdown (model + CUDA graphs + KV cache).
        # If Steps 2-3 measured the actual overhead, log it for visibility.
        gpu_mem_util = self._compute_gpu_mem_util(split.prefill_tp)
        alloc = self._gpu_vram_gb * gpu_mem_util
        reserve = self._gpu_vram_gb - alloc
        self.log(f"   Memory: gpu_memory_utilization={gpu_mem_util:.4f} "
                 f"→ {alloc:.0f}GB allocated, {reserve:.0f}GB reserved for overhead (per GPU)")

        prefill_max_num_seqs = self._compute_max_num_seqs(split.prefill_tp)
        decode_max_num_seqs = self._compute_max_num_seqs(split.decode_tp)
        max_batched = self._compute_max_num_batched_tokens(split.prefill_tp)

        total_pods = split.prefill_pods + split.decode_pods
        # Use the smaller TP for resource calculation (more pods per node = more conservative)
        min_tp = min(split.prefill_tp, split.decode_tp)
        mem, cpu = self._get_pod_resources(tp=min_tp, total_pods=total_pods)

        cfg = TestConfig(
            test_id=f"step7-{split.prefill_pods}p{split.decode_pods}d-ptp{split.prefill_tp}-dtp{split.decode_tp}",
            architecture='pd',
            model_name=self.config.model_name,

            # Workload
            namespace=self.config.namespace,
            isl=self.config.isl,
            osl=self.config.osl,
            num_users=concurrency,

            request_type=self.config.rate_type,
            request_rate=concurrency,

            # TP and replicas (use prefill_tp as default for backward compatibility)
            tensor_parallelism=split.prefill_tp,
            replicas=total_pods,
            prefill_replicas=split.prefill_pods,
            decode_replicas=split.decode_pods,
            prefill_decode_ratio=f"{split.prefill_pods}:{split.decode_pods}",

            # PD-specific TP values (different for prefill and decode)
            prefill_tp=split.prefill_tp,
            decode_tp=split.decode_tp,

            # Infrastructure
            max_model_len=self.config.max_model_len,
            gpu_memory_utilization=gpu_mem_util,
            prefill_gpu_memory_utilization=gpu_mem_util,
            decode_gpu_memory_utilization=gpu_mem_util,
            gpu_vram_gb=self._gpu_vram_gb,
            prefill_max_num_seqs=prefill_max_num_seqs,
            decode_max_num_seqs=decode_max_num_seqs,
            max_num_batched_tokens=max_batched,
            isl_stdev=self.config.isl_stdev,
            osl_stdev=self.config.osl_stdev,
            turns=self.config.turns,
            image=self.config.image,
            pvc_name=self.config.pvc_name,
            nccl_ib_hca=self.config.nccl_ib_hca,
            optimization_goal='ttft',
            test_duration=self.config.test_duration,
            network_type=self.config.network_type,
            rdma_device_resources=self.config.rdma_device_resources or [],
            rdma_nics_per_node=self.config.rdma_nics_per_node or 0,

            memory_request=mem,
            memory_limit=mem,
            cpu_request=cpu,
            cpu_limit=cpu,
            selected_nodes=self.config.selected_nodes or [],
            workload_mode=self.config.workload_mode or 'synthetic',
            dataset_source=self.config.dataset_source,
            dataset_column=self.config.dataset_column,
            dataset_max_output=self.config.dataset_max_output or 256,
            epp_config=self._build_epp_config(),
            block_size=self._compute_block_size(),
        )
        return self._apply_advanced_vllm(cfg)

    def _create_ep_config(
        self,
        tp: int,
        num_gpus: int,
        test_id: str,
        use_concurrency: bool = False
    ) -> TestConfig:
        """Create EP (Expert Parallelism) architecture test config.

        EP uses --enable-eplb for expert-level prefill load balancing.
        Each pod handles both prefill and decode, with EPLB distributing work.

        Args:
            tp: Tensor parallelism per pod
            num_gpus: Total GPUs to use (replicas = num_gpus // tp)
            test_id: Unique test identifier
            use_concurrency: If True, use concurrent rate type with num_users
        """
        concurrency = self.effective_concurrency if use_concurrency else int(self.config.qps)

        gpu_memory_utilization = self._compute_gpu_mem_util(tp)
        allocated_gb = self._gpu_vram_gb * gpu_memory_utilization
        reserve_gb = self._gpu_vram_gb - allocated_gb
        self.log(f"   Memory: gpu_memory_utilization={gpu_memory_utilization:.4f} "
                 f"→ {allocated_gb:.0f}GB allocated, {reserve_gb:.0f}GB reserved for overhead (per GPU)")

        max_num_seqs = self._compute_max_num_seqs(tp)
        max_batched = self._compute_max_num_batched_tokens(tp)

        replicas = num_gpus // tp
        mem, cpu = self._get_pod_resources(tp=tp, total_pods=replicas)

        cfg = TestConfig(
            test_id=test_id,
            architecture='ep',
            model_name=self.config.model_name,
            tensor_parallelism=tp,
            replicas=replicas,
            namespace=self.config.namespace,
            isl=self.config.isl,
            osl=self.config.osl,
            num_users=concurrency,
            request_type=self.config.rate_type if use_concurrency else 'constant',
            request_rate=concurrency if use_concurrency else 1,
            test_duration=self.config.test_duration,
            stop_mode=self.config.stop_mode,
            max_requests=self.config.max_requests,
            max_num_batched_tokens=max_batched,
            isl_stdev=self.config.isl_stdev,
            osl_stdev=self.config.osl_stdev,
            turns=self.config.turns,
            image=self.config.image,
            pvc_name=self.config.pvc_name,
            nccl_ib_hca=self.config.nccl_ib_hca,
            max_model_len=self.config.max_model_len,
            gpu_memory_utilization=gpu_memory_utilization,
            gpu_vram_gb=self._gpu_vram_gb,
            max_num_seqs=max_num_seqs,
            optimization_goal='throughput',
            network_type=self.config.network_type,
            rdma_device_resources=self.config.rdma_device_resources or [],
            rdma_nics_per_node=self.config.rdma_nics_per_node or 0,

            memory_request=mem,
            memory_limit=mem,
            cpu_request=cpu,
            cpu_limit=cpu,
            selected_nodes=self.config.selected_nodes or [],
            workload_mode=self.config.workload_mode or 'synthetic',
            dataset_source=self.config.dataset_source,
            dataset_column=self.config.dataset_column,
            dataset_max_output=self.config.dataset_max_output or 256,
            epp_config=self._build_epp_config(),
            block_size=self._compute_block_size(),
        )
        return self._apply_advanced_vllm(cfg)

    def _benchmark_epp_strategies(self):
        """Step 11: EPP Tuning — smart weight sweep per architecture.

        Tests 3 EPP weight combinations on the best config from each
        architecture (PD and Aggregated), using the optimal concurrency
        from Step 9/10. Swaps only the EPP configmap between tests.
        """
        if not self.config.epp_benchmark:
            return

        self.log("\n" + "=" * 80, 'info')
        self.log("STEP 11: EPP Tuning (Smart Weight Sweep)", 'decision')
        self.log("=" * 80, 'info')

        # Build weight combos based on workload
        has_sla = self.config.latency_constraint_enabled
        isl_osl_ratio = self.config.isl / max(self.config.osl, 1)
        base = self._build_epp_config()

        weight_combos = [
            ('cache-heavy', {'prefix_cache_weight': 5.0, 'kv_cache_weight': 1.0, 'queue_weight': 1.0, 'slo_enabled': has_sla}),
            ('queue-heavy', {'prefix_cache_weight': 1.0, 'kv_cache_weight': 1.0, 'queue_weight': 5.0, 'slo_enabled': has_sla}),
        ]
        if isl_osl_ratio > 10:
            weight_combos.append(('kv-heavy', {'prefix_cache_weight': 2.0, 'kv_cache_weight': 5.0, 'queue_weight': 1.0, 'slo_enabled': has_sla}))
        else:
            weight_combos.append(('equal', {'prefix_cache_weight': 2.0, 'kv_cache_weight': 2.0, 'queue_weight': 2.0, 'slo_enabled': has_sla}))

        # Collect best configs per architecture
        configs_to_test = []

        # Best PD config
        if self.pareto_results:
            best_split, best_pd_result = min(self.pareto_results, key=lambda x: x[1].ttft_p90 if x[1].ttft_p90 else 1e9)
            pd_cfg = self._create_pd_config(best_split)
            # Use optimal concurrency from Step 9 if available
            pd_concurrency = int(self.config.qps)
            for arch_key, sr in getattr(self, 'latency_search_results', {}).items():
                if 'pd' in arch_key and sr and sr.optimal_concurrency:
                    pd_concurrency = sr.optimal_concurrency
                    break
            if hasattr(self, 'effective_concurrency') and self.effective_concurrency and pd_concurrency == int(self.config.qps):
                pd_concurrency = self.effective_concurrency
            configs_to_test.append(('pd', pd_cfg, pd_concurrency))
            self.log(f"  PD: {best_split.prefill_pods}P×TP{best_split.prefill_tp} + {best_split.decode_pods}D×TP{best_split.decode_tp} at c={pd_concurrency}", 'info')

        # Best Aggregated config
        if self.aggregated_result and self.aggregated_tp:
            agg_cfg = self._create_aggregated_config(
                tp=self.aggregated_tp,
                num_gpus=self.config.total_gpus,
                isl=self.config.isl,
                osl=self.config.osl,
                test_id=f"step11-epp-aggregated",
                use_concurrency=True,
            )
            agg_concurrency = int(self.config.qps)
            for arch_key, sr in getattr(self, 'latency_search_results', {}).items():
                if 'aggregated' in arch_key and sr and sr.optimal_concurrency:
                    agg_concurrency = sr.optimal_concurrency
                    break
            if hasattr(self, 'effective_concurrency') and self.effective_concurrency and agg_concurrency == int(self.config.qps):
                agg_concurrency = self.effective_concurrency
            configs_to_test.append(('aggregated', agg_cfg, agg_concurrency))
            self.log(f"  Aggregated: {self.config.total_gpus // self.aggregated_tp}×TP{self.aggregated_tp} at c={agg_concurrency}", 'info')

        if not configs_to_test:
            self.log("⚠️  No successful configs for EPP tuning", 'warning')
            return

        self.log(f"  Weight combos: {', '.join(n for n, _ in weight_combos)}", 'info')

        self.epp_benchmark_results = {}

        from core import PrereqManager
        prereq_mgr = PrereqManager(
            namespace=self.config.namespace,
            kubectl_runner=self.orchestrator.deployment_manager.kubectl
        )

        for arch, base_cfg, concurrency in configs_to_test:
            if self._should_stop():
                break

            self.log(f"\n  --- EPP Tuning: {arch.upper()} (c={concurrency}) ---", 'decision')
            arch_results = []

            for name, weights in weight_combos:
                if self._should_stop():
                    break

                test_id = f"step11-epp-{arch}-{name}"
                self.log(f"  Testing: {name} (cache={weights['prefix_cache_weight']}, kv={weights['kv_cache_weight']}, queue={weights['queue_weight']})", 'info')

                epp_cfg = {
                    'preset': 'custom',
                    'maxPrefixBlocksToMatch': base.get('maxPrefixBlocksToMatch', 256),
                    'lruCapacityPerServer': base.get('lruCapacityPerServer', 31250),
                    'nonCachedTokens': base.get('nonCachedTokens', 16),
                    'plugins': {
                        'prefix_cache': {'enabled': True, 'weight': weights['prefix_cache_weight']},
                        'kv_cache': {'enabled': True, 'weight': weights['kv_cache_weight']},
                        'queue': {'enabled': True, 'weight': weights['queue_weight']},
                        'slo': {'enabled': weights['slo_enabled']},
                    },
                }

                success = prereq_mgr.update_epp_config(
                    architecture=arch,
                    epp_config=epp_cfg,
                    log_callback=lambda msg: self.log(msg, 'info')
                )
                if not success:
                    self.log(f"  ❌ Failed to update EPP config for {name}", 'error')
                    continue

                epp_test_config = TestConfig(
                    test_id=test_id,
                    architecture=base_cfg.architecture,
                    model_name=base_cfg.model_name,
                    namespace=base_cfg.namespace,
                    isl=base_cfg.isl, osl=base_cfg.osl,
                    num_users=concurrency,
                    tensor_parallelism=base_cfg.tensor_parallelism,
                    replicas=base_cfg.replicas,
                    prefill_replicas=base_cfg.prefill_replicas,
                    decode_replicas=base_cfg.decode_replicas,
                    prefill_tp=base_cfg.prefill_tp,
                    decode_tp=base_cfg.decode_tp,
                    max_model_len=base_cfg.max_model_len,
                    gpu_memory_utilization=base_cfg.gpu_memory_utilization,
                    image=base_cfg.image,
                    pvc_name=base_cfg.pvc_name,
                    request_type=base_cfg.request_type,
                    request_rate=concurrency,
                    test_duration=base_cfg.test_duration,
                    workload_mode=base_cfg.workload_mode,
                    dataset_source=base_cfg.dataset_source,
                    block_size=base_cfg.block_size,
                    network_type=base_cfg.network_type,
                    nccl_ib_hca=base_cfg.nccl_ib_hca,
                    epp_config=epp_cfg,
                )

                result = self.orchestrator.run_test(
                    epp_test_config,
                    cleanup=False,
                    log_callback=lambda msg: self.log(msg, 'info'),
                    stop_check=self._should_stop,
                    skip_prereqs=True,
                )

                if result and result.guidellm_success:
                    ttft = result.ttft_p90 or 0
                    tput = result.throughput_p90 or 0
                    self.log(f"  ✅ {name}: TTFT p90={ttft:.1f}ms, Throughput p90={tput:.2f} req/s", 'success')
                    arch_results.append((name, weights, result))
                    try:
                        import json as _json
                        tmgr = TemplateManager()
                        cm_template = f'prereq/gaie-configmap-{arch}.yaml.j2'
                        cm_yaml = tmgr.render_template(cm_template, **{
                            'namespace': self.config.namespace,
                            'gaie_name': f'gaie-{arch}-epp',
                            'config_file': f'{arch}-config.yaml',
                            'prefix_cache_weight': weights['prefix_cache_weight'],
                            'kv_cache_weight': weights['kv_cache_weight'],
                            'queue_weight': weights['queue_weight'],
                            'slo_enabled': weights.get('slo_enabled', False),
                            'max_prefix_blocks': epp_cfg.get('maxPrefixBlocksToMatch', 256),
                            'lru_capacity': epp_cfg.get('lruCapacityPerServer', 31250),
                            'non_cached_tokens': epp_cfg.get('nonCachedTokens', 16),
                        })
                        epp_test_config._epp_manifests = _json.dumps({'epp-configmap': cm_yaml})
                    except Exception:
                        epp_test_config._epp_manifests = None
                    self._save_epp_test_to_database(epp_test_config, result)
                else:
                    self.log(f"  ❌ {name}: benchmark failed", 'error')

            self.epp_benchmark_results[arch] = arch_results

            if arch_results:
                best_name = min(arch_results, key=lambda x: x[2].ttft_p90 or float('inf'))[0]
                self.log(f"  Best {arch}: {best_name}", 'success')

        # Restore user's original EPP config for each architecture tested
        for arch, _, _ in configs_to_test:
            prereq_mgr.update_epp_config(
                architecture=arch,
                epp_config=self._build_epp_config(),
                log_callback=lambda msg: self.log(msg, 'info')
            )

    def _build_results(self) -> Dict[str, Any]:
        """Build optimization results summary."""
        return {
            'optimal_decode_tp': self.optimal_decode_tp.tp if self.optimal_decode_tp else None,
            'optimal_prefill_tp': self.optimal_prefill_tp.tp if self.optimal_prefill_tp else None,
            'decode_tpsg': self.optimal_decode_tp.tpsg if self.optimal_decode_tp else None,
            'prefill_tpsg': self.optimal_prefill_tp.tpsg if self.optimal_prefill_tp else None,
            'constraint_notes': self.constraint_notes,
            'concurrency': self.config.qps,
            'total_gpus_available': self.config.total_gpus,
            'gpu_sizing': getattr(self, '_gpu_sizing', None),
            'feasible_splits_count': len(self.feasible_splits),
            'pareto_front_count': len(self.pareto_results),
            'total_tests_run': len(self.all_test_results),
            'pareto_configurations': [
                {
                    'prefill_pods': split.prefill_pods,
                    'decode_pods': split.decode_pods,
                    'prefill_tp': split.prefill_tp,
                    'decode_tp': split.decode_tp,
                    'ttft_p90': result.ttft_p90,
                    'throughput_p90': result.throughput_p90
                }
                for split, result in self.pareto_results
            ],
            # Step 6: Aggregated search results
            'aggregated_search': [
                {
                    'tp': tp,
                    'replicas': self.config.total_gpus // tp,
                    'total_gpus': self.config.total_gpus,
                    'ttft_p90': result.ttft_p90,
                    'throughput_p90': result.throughput_p90,
                }
                for tp, result in self.aggregated_search_results
            ],
            # Best aggregated (from Step 6)
            'aggregated_result': {
                'tp': self.aggregated_tp,
                'gpus': self.aggregated_gpus,
                'pods': self.aggregated_gpus // self.aggregated_tp if self.aggregated_tp else None,
                'ttft_p90': self.aggregated_result.ttft_p90 if self.aggregated_result else None,
                'throughput_p90': self.aggregated_result.throughput_p90 if self.aggregated_result else None,
            } if self.aggregated_result else None,
            # Step 9: Latency-bounded throughput maximization
            'latency_bounded_result': {
                'optimal_concurrency': self.latency_bounded_result.optimal_concurrency,
                'achieved_throughput': self.latency_bounded_result.achieved_throughput,
                'achieved_latency_ms': self.latency_bounded_result.achieved_latency_ms,
                'target_latency_ms': self.latency_bounded_result.target_latency_ms,
                'target_percentile': self.latency_bounded_result.target_percentile,
                'n_trials': self.latency_bounded_result.n_trials,
                'best_config_source': self.latency_bounded_result.best_config_source,
            } if self.latency_bounded_result else None,
            'latency_search_by_architecture': {
                arch: {
                    'optimal_concurrency': res.optimal_concurrency,
                    'achieved_throughput': res.achieved_throughput,
                    'achieved_latency_ms': res.achieved_latency_ms,
                    'n_trials': res.n_trials,
                }
                for arch, res in getattr(self, 'latency_search_results', {}).items()
            } or None,
            # Step 10: Calibrated Load results
            'sustainable_throughput_rps': self.sustainable_throughput_rps,
            'calibrated_concurrency': self.achievable_concurrency,
            'calibrated_qps': self.sustainable_throughput_rps,  # backwards compat (req/s)
            'calibrated_pd_result': {
                'ttft_p90': self.calibrated_pd_result.ttft_p90,
                'throughput_p90': self.calibrated_pd_result.throughput_p90,
            } if self.calibrated_pd_result else None,
            'calibrated_agg_result': {
                'ttft_p90': self.calibrated_agg_result.ttft_p90,
                'throughput_p90': self.calibrated_agg_result.throughput_p90,
            } if self.calibrated_agg_result else None,
            # EP results (populated by ThroughputStrategy/BalancedStrategy)
            'ep_configurations': [
                {
                    'tp': ep_cfg.tp,
                    'replicas': ep_cfg.replicas,
                    'total_gpus': ep_cfg.total_gpus,
                    'ttft_p90': result.ttft_p90,
                    'throughput_p90': result.throughput_p90
                }
                for ep_cfg, result in self.ep_results
            ],
            'best_ep': {
                'tp': self.best_ep_config.tp,
                'replicas': self.best_ep_config.replicas,
                'total_gpus': self.best_ep_config.total_gpus,
                'ttft_p90': self.best_ep_result.ttft_p90,
                'throughput_p90': self.best_ep_result.throughput_p90,
            } if self.best_ep_result and self.best_ep_config else None,
            'calibrated_ep_result': {
                'ttft_p90': self.calibrated_ep_result.ttft_p90,
                'throughput_p90': self.calibrated_ep_result.throughput_p90,
            } if self.calibrated_ep_result else None,
            # Optimization goal for report rendering
            'optimization_goal': self.config.objective,
            # Step 11: EPP tuning results (per architecture)
            'epp_tuning': {
                arch: [
                    {
                        'name': name,
                        'weights': {'prefix_cache': w['prefix_cache_weight'], 'kv_cache': w['kv_cache_weight'], 'queue': w['queue_weight']},
                        'ttft_p50': r.ttft_p50, 'ttft_p90': r.ttft_p90, 'ttft_p95': r.ttft_p95, 'ttft_p99': r.ttft_p99,
                        'throughput_p50': r.throughput_p50, 'throughput_p90': r.throughput_p90, 'throughput_p95': r.throughput_p95, 'throughput_p99': r.throughput_p99,
                        'itl_p90': r.itl_p90,
                    }
                    for name, w, r in results
                ]
                for arch, results in self.epp_benchmark_results.items()
                if results
            } if self.epp_benchmark_results else None,
            # All test results for database insertion
            'all_test_results': self.all_test_results,
            # Whether the user stopped the optimization early
            'stopped': self.stopped
        }

    def _generate_prefix_cache_dataset(self):
        """Generate a synthetic dataset with controlled prefix cache hit ratio.

        Creates a .jsonl file where prefix_cache_hit_pct% of rows share an
        identical prompt (guaranteeing prefix cache hits) and the rest are
        unique random prompts. The dataset is sized to overflow GPU prefix
        cache so unique prompts don't accidentally get cached.
        """
        import hashlib
        import json as _json
        import random
        from pathlib import Path

        hit_pct = self.config.prefix_cache_hit_pct
        isl = self.config.isl
        osl = self.config.osl

        # Compute deterministic seed from config (includes stdev so different variation = different dataset)
        seed_input = f"{self.config.model_name}:{isl}:{osl}:{hit_pct}:{self.config.isl_stdev or 0}:{self.config.osl_stdev or 0}"
        if self.config.prefix_cache_seed is not None:
            seed = self.config.prefix_cache_seed
        else:
            seed = int(hashlib.sha256(seed_input.encode()).hexdigest()[:8], 16)
            self.config.prefix_cache_seed = seed

        # Calculate pool size: overflow the prefix cache
        gpu_vram_gb = getattr(self, '_gpu_vram_gb', 80.0)
        total_gpus = self.config.total_gpus
        model_size_gb = self._estimate_model_size_gb()
        available_cache_gb = max(1, total_gpus * gpu_vram_gb * 0.9 - model_size_gb)
        # KV cache per token ≈ 2 * num_layers * num_kv_heads * head_dim * dtype_bytes
        # Simplified: ~0.5KB/token for typical models
        cacheable_tokens = available_cache_gb * 1024 * 1024 * 1024 / 512
        cacheable_sequences = max(100, int(cacheable_tokens / isl))
        # Pool needs enough unique rows that they get evicted before cycling back.
        # 1.5x the cacheable count is sufficient — the duplicates fill up the cache,
        # and unique rows rotate through faster than the cache can hold them all.
        pool_size = max(1000, int(cacheable_sequences * 1.5))
        pool_size = min(pool_size, 10000)  # Cap at 10K to keep file size reasonable

        self.log(f"Generating prefix cache dataset: {hit_pct}% hit ratio, {pool_size} rows, seed={seed}", 'info')
        self.log(f"   Estimated cacheable sequences: {cacheable_sequences}", 'info')

        rng = random.Random(seed)

        # Build vocabulary of printable words for prompt generation
        # Use model tokenizer if available, otherwise generate random words
        try:
            from transformers import AutoTokenizer
            hf_home = os.environ.get('HF_HOME') or os.path.join(
                os.environ.get('HOME_STORAGE_DIR', '/mnt/storage'), '.cache', 'huggingface')
            hf_token = self.config.hf_token or os.environ.get('HF_TOKEN')
            tokenizer = AutoTokenizer.from_pretrained(
                self.config.model_name, trust_remote_code=True,
                cache_dir=hf_home, token=hf_token
            )
            vocab = [t for t in tokenizer.get_vocab().keys()
                     if len(t) > 2 and t.isascii() and t.isalpha()]
            if len(vocab) < 500:
                vocab = None
        except Exception:
            vocab = None

        def make_prompt(length_tokens, rng_instance):
            if vocab:
                words = [rng_instance.choice(vocab) for _ in range(int(length_tokens * 1.3))]
                text = ' '.join(words)
            else:
                words = []
                for _ in range(int(length_tokens * 1.3)):
                    wlen = rng_instance.randint(3, 10)
                    words.append(''.join(rng_instance.choices('abcdefghijklmnopqrstuvwxyz', k=wlen)))
                text = ' '.join(words)
            return text

        # Generate the shared prompt (fixed length — must be identical for cache hits)
        shared_rng = random.Random(seed)
        shared_prompt = make_prompt(isl, shared_rng)

        isl_stdev = self.config.isl_stdev or 0
        osl_stdev = self.config.osl_stdev or 0

        # Calculate split
        num_shared = int(pool_size * hit_pct / 100)
        num_unique = pool_size - num_shared

        # Generate dataset
        output_dir = Path(os.environ.get('HOME_STORAGE_DIR', '/mnt/storage')) / 'prefix-cache-datasets'
        output_dir.mkdir(parents=True, exist_ok=True)
        dataset_path = output_dir / f'prefix-cache-{seed}.jsonl'

        if dataset_path.exists():
            self.log(f"   Reusing existing dataset: {dataset_path}", 'info')
        else:
            rows = []
            # Shared rows: fixed prompt and fixed OSL (identical for cache hits)
            for _ in range(num_shared):
                rows.append({"prompt": shared_prompt, "output_tokens_count": osl})
            # Unique rows: vary length around ISL/OSL using stdev if configured
            for i in range(num_unique):
                unique_rng = random.Random(seed + i + 1)
                if isl_stdev > 0:
                    row_isl = max(16, int(unique_rng.gauss(isl, isl_stdev)))
                else:
                    row_isl = isl
                if osl_stdev > 0:
                    row_osl = max(1, int(unique_rng.gauss(osl, osl_stdev)))
                else:
                    row_osl = osl
                rows.append({"prompt": make_prompt(row_isl, unique_rng), "output_tokens_count": row_osl})

            rng.shuffle(rows)

            with open(dataset_path, 'w') as f:
                for row in rows:
                    f.write(_json.dumps(row) + '\n')

            file_size_mb = dataset_path.stat().st_size / (1024 * 1024)
            self.log(f"   Generated {dataset_path} ({file_size_mb:.1f} MB)", 'success')

        # Switch workload mode to dataset for all subsequent tests
        self.config.workload_mode = 'dataset'
        self.config.dataset_source = str(dataset_path)
        self.config.dataset_column = 'prompt'
        self.config.dataset_max_output = osl
        self.log(f"   Workload switched to dataset mode for prefix cache simulation", 'info')

        # Persist seed to DB so resume regenerates the same dataset
        if self.run_id and self.db_manager:
            try:
                with self.db_manager.get_connection() as conn:
                    conn.execute(
                        'UPDATE optimization_runs SET prefix_cache_seed = ?, config_json = ? WHERE id = ?',
                        (seed, _json.dumps(self.config.to_dict()), self.run_id)
                    )
            except Exception as e:
                self.log(f"   Warning: failed to persist seed to DB: {e}", 'warning')
