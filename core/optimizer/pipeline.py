"""RecipeOptimizer — main pipeline orchestrator."""

import os
import sys
import time
import json
import math
import logging
from typing import Dict, List, Tuple, Optional, Any, Callable
from dataclasses import field

from core.optimizer.config import (
    RecipeOptimizerConfig, OptimalTP, FeasibleSplit, EPConfig
)
from core.config_generator import TestConfig
from core.test_orchestrator import TestOrchestrator, TestResult
from core.system_scanner import SystemScanner
from core.database_manager import DatabaseManager
from core.template_manager import TemplateManager
from core.cloud_constraints import CloudProvider
from core.networking import detect_rdma_device_resources
from core.test_planner import calculate_engine_memory_config
from core.optimizer.tp_calibration import TPCalibrationMixin
from core.optimizer.pd_search import PDSearchMixin
from core.optimizer.latency_search import LatencySearchMixin
from core.optimizer.config_builder import ConfigBuilderMixin
from core.optimizer.epp_tuning import EPPTuningMixin
from core.optimizer.dataset import DatasetMixin
from core.optimizer.speculative import SpeculativeMixin
from core.optimizer.cache_sweep import CacheSweepMixin

logger = logging.getLogger(__name__)

class RecipeOptimizer(
    TPCalibrationMixin,
    PDSearchMixin,
    LatencySearchMixin,
    ConfigBuilderMixin,
    EPPTuningMixin,
    DatasetMixin,
    SpeculativeMixin,
    CacheSweepMixin,
):
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
        db_path: str = '/mnt/storage/serveit.db',
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
        self.scanner = SystemScanner(namespace=config.namespace, kubeconfig=config.kubeconfig)
        self.orchestrator = TestOrchestrator(
            namespace=config.namespace,
            kubeconfig=config.kubeconfig,
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

        # Auto-detect Multus NAD annotation for sriov_multinic
        if self.config.network_type == 'sriov_multinic' and not self.config.rdma_network_annotation:
            self.config.rdma_network_annotation = self._detect_rdma_network_annotation()
            if self.config.rdma_network_annotation:
                self.log(f"Auto-detected RDMA network annotation: {self.config.rdma_network_annotation}")

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
        self._model_dtype = 'fp16'
        try:
            from transformers import AutoConfig
            self.log(f"Loading model config: {self.config.model_name}")
            hf_kwargs = {}
            if self.config.hf_token:
                hf_kwargs['token'] = self.config.hf_token
            self._model_config = AutoConfig.from_pretrained(
                self.config.model_name, trust_remote_code=True, **hf_kwargs
            ).to_dict()

            # Multimodal models nest LLM params under text_config
            if 'text_config' in self._model_config and isinstance(self._model_config['text_config'], dict):
                tcfg = self._model_config['text_config']
                for k in ['hidden_size', 'intermediate_size', 'num_hidden_layers',
                           'num_attention_heads', 'num_key_value_heads', 'vocab_size',
                           'num_local_experts', 'n_routed_experts', 'num_experts',
                           'moe_intermediate_size', 'shared_expert_intermediate_size',
                           'n_shared_experts', 'num_experts_per_tok',
                           'intermediate_size_mlp', 'interleave_moe_layer_step',
                           'max_position_embeddings', 'attention_chunk_size',
                           'num_nextn_predict_layers']:
                    if k in tcfg and k not in self._model_config:
                        self._model_config[k] = tcfg[k]

            # Detect dtype from quantization config
            qcfg = self._model_config.get('quantization_config', {})
            if qcfg:
                quant_method = (qcfg.get('quant_method', '') or '').lower()
                quant_format = (qcfg.get('format', '') or qcfg.get('quant_type', '') or '').lower()
                # 4-bit quantization (MXFP4, W4A16, GPTQ-4bit, AWQ-4bit)
                if 'mxfp4' in quant_method or 'nvfp4' in quant_method:
                    self._model_dtype = 'fp4'
                elif 'w4a16' in quant_format or 'w4a16' in quant_method:
                    self._model_dtype = 'int4'
                elif qcfg.get('bits') == 4 or qcfg.get('num_bits') == 4:
                    self._model_dtype = 'int4'
                # 8-bit quantization (FP8, W8A8)
                elif 'fp8' in quant_method or 'fp8' in quant_format:
                    self._model_dtype = 'fp8'
                elif 'w8a8' in quant_format:
                    self._model_dtype = 'fp8'
                elif quant_method == 'compressed-tensors':
                    groups = qcfg.get('config_groups', {})
                    for g in groups.values():
                        w = g.get('weights', {}) if isinstance(g, dict) else {}
                        if w.get('num_bits') == 8 and w.get('type', '').lower() == 'float':
                            self._model_dtype = 'fp8'
                            break
                        elif w.get('num_bits') == 4:
                            self._model_dtype = 'int4'
                            break
            if self._model_dtype == 'fp16':
                torch_dtype = self._model_config.get('torch_dtype', '')
                if torch_dtype == 'float16':
                    self._model_dtype = 'fp16'
                elif torch_dtype == 'bfloat16':
                    self._model_dtype = 'bf16'

            self.log(f"Model config loaded: {self._model_config.get('num_hidden_layers')} layers, "
                     f"{self._model_config.get('num_key_value_heads')} KV heads, "
                     f"hidden_size={self._model_config.get('hidden_size')}, "
                     f"max_pos={self._model_config.get('max_position_embeddings')}, "
                     f"dtype={self._model_dtype}")
            estimated_b = self._estimate_params_from_config()
            if estimated_b:
                self._model_size_b = estimated_b
                self.log(f"Model size from config: {self._model_size_b:.1f}B parameters")

            # Sanity check: parse model name for parameter count (e.g., "550B", "70B")
            import re
            name_match = re.search(r'(\d+)[Bb]', self.config.model_name.split('/')[-1])
            if name_match:
                name_b = float(name_match.group(1))
                if estimated_b and estimated_b > name_b * 3:
                    self.log(f"Model size from config ({estimated_b:.0f}B) seems too high vs name ({name_b:.0f}B) — using name", 'warning')
                    self._model_size_b = name_b
                elif not estimated_b:
                    self._model_size_b = name_b
                    self.log(f"Model size from name: {name_b:.0f}B parameters")
        except Exception as e:
            self.log(f"Could not load model config: {e}. Using defaults.", 'warning')

        # Auto-detect MoE and speculative decoding (MTP) capability
        self._is_moe = False
        self._num_experts = 0
        self._dbo_threshold = 32
        self._supports_mtp = False
        if self._model_config:
            num_experts = (self._model_config.get('num_local_experts')
                          or self._model_config.get('n_routed_experts')
                          or self._model_config.get('num_experts') or 0)
            if num_experts > 1:
                self._is_moe = True
                self._num_experts = num_experts
                self._dbo_threshold = self._compute_dbo_threshold(num_experts)
                self.log(f"MoE model detected ({num_experts} experts) — expert parallel will be enabled")
                self.log(f"  DBO token threshold: {self._dbo_threshold} (based on {num_experts} experts)")
            if self._model_config.get('num_nextn_predict_layers'):
                self._supports_mtp = True
                self.log(f"MTP support detected ({self._model_config['num_nextn_predict_layers']} prediction layers)")
            else:
                mtp_archs = ['Glm4ForCausalLM', 'DeepseekV3ForCausalLM']
                model_archs = self._model_config.get('architectures', [])
                if any(a in mtp_archs for a in model_archs):
                    self._supports_mtp = True
                    self.log(f"MTP support detected (architecture: {model_archs[0]})")

        # Detect hybrid attention (mixed attention types across layers)
        self._has_hybrid_attention = False
        if self._model_config:
            has_chunk = self._model_config.get('attention_chunk_size') is not None
            has_sliding = self._model_config.get('sliding_window') is not None
            hybrid_archs = ['Qwen3NextForCausalLM', 'Llama4ForConditionalGeneration',
                            'Qwen3_5ForCausalLM', 'Bamba2ForCausalLM']
            model_archs = self._model_config.get('architectures', [])
            has_hybrid_arch = any(a in hybrid_archs for a in model_archs)
            if has_chunk or has_hybrid_arch:
                self._has_hybrid_attention = True
                reason = []
                if has_chunk:
                    reason.append(f"attention_chunk_size={self._model_config['attention_chunk_size']}")
                if has_hybrid_arch:
                    reason.append(f"architecture={model_archs[0]}")
                self.log(f"  Hybrid attention detected ({', '.join(reason)})")
                self.log(f"    PD disaggregation requires NixlConnector with HMA support (vLLM >= v0.19)")

        # Detect DeepGemm compatibility from quantization config
        self._use_deep_gemm = None  # None = let vLLM decide
        if self._model_config and self._model_dtype == 'fp8':
            qcfg = self._model_config.get('quantization_config', {})
            quant_method = (qcfg.get('quant_method', '') or '').lower()
            if quant_method == 'compressed-tensors':
                # compressed-tensors uses per-channel weight scales which
                # DeepGemm doesn't support (requires per-tensor)
                self._use_deep_gemm = False
                self.log("  DeepGemm: disabled (compressed-tensors per-channel quantization)")
            elif quant_method == 'fp8' or not quant_method:
                # Standard FP8 per-tensor quantization — DeepGemm compatible
                self._use_deep_gemm = True
                self.log("  DeepGemm: enabled (per-tensor FP8 quantization)")

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

        # Step 10: Latency-bounded throughput maximization
        self.latency_bounded_result = None

        # Step 11: Calibrated Load validation (only when user didn't enable achievable QPS)
        # Sustainable throughput in req/s (for logging/reporting)
        self.sustainable_throughput_rps: Optional[float] = None
        # Sustainable concurrency in concurrent-user units (for test configs)
        self.achievable_concurrency: Optional[int] = None
        self.calibrated_pd_result: Optional[TestResult] = None
        self.calibrated_agg_result: Optional[TestResult] = None

        # EP (Expert Parallelism) results — populated by ThroughputStrategy/BalancedStrategy
        self.ep_configs: List[FeasibleSplit] = []
        self.ep_results: List[Tuple[FeasibleSplit, TestResult]] = []
        self.best_ep_result: Optional[TestResult] = None
        self.best_ep_config: Optional[FeasibleSplit] = None
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
        from core.optimization_strategies import (
            TTFTStrategy, ThroughputStrategy, BalancedStrategy,
            AggregatedOnlyStrategy, PDOnlyStrategy, EPOnlyStrategy,
            SingleTestStrategy,
        )
        strategies = {
            'ttft': TTFTStrategy,
            'throughput': ThroughputStrategy,
            'balanced': BalancedStrategy,
            'aggregated_only': AggregatedOnlyStrategy,
            'pd_only': PDOnlyStrategy,
            'ep_only': EPOnlyStrategy,
            'single_test': SingleTestStrategy,
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

    def _save_sweep_progress(self):
        """Incrementally save concurrency_sweep and cache_sweep to optimal_config."""
        if not self.db_manager or not self.run_id:
            return
        try:
            import json as _json
            with self.db_manager.get_connection() as conn:
                row = conn.execute('SELECT optimal_config FROM optimization_runs WHERE id=?', (self.run_id,)).fetchone()
                opt = _json.loads(row[0]) if row and row[0] else {}
                if hasattr(self, 'concurrency_sweep_results') and self.concurrency_sweep_results:
                    opt['concurrency_sweep'] = self.concurrency_sweep_results
                if hasattr(self, 'cache_sweep_results') and self.cache_sweep_results:
                    opt['cache_sweep'] = self.cache_sweep_results
                conn.execute('UPDATE optimization_runs SET optimal_config=? WHERE id=?',
                             (_json.dumps(opt), self.run_id))
        except Exception:
            pass

    def _is_memory_failure(self, result) -> bool:
        """Check if a test failure was caused by OOM or insufficient memory."""
        if not result:
            return False
        oom_patterns = ['out of memory', 'oom', 'no available memory for the cache blocks',
                        'cudaErrorMemoryAllocation', 'CUDA_OOM', 'OOM_KILLED',
                        'Cannot allocate memory', 'oom-kill']
        # Check error_message
        err = (result.error_message or '').lower()
        if any(p.lower() in err for p in oom_patterns):
            return True
        # Check pod_errors_json
        pod_errs = (result.pod_errors_json or '').lower()
        if any(p.lower() in pod_errs for p in oom_patterns):
            return True
        return False

    def _check_pod_errors(self, test_config: TestConfig, test_result: TestResult):
        """Check for pod errors after a test. Raise on critical errors, log NIXL warnings."""
        if test_result.nixl_errors > 0:
            self.log(f"  ⚠️  NIXL transfer errors: {test_result.nixl_errors} (non-critical)", 'warning')

        if not test_result.pod_errors_detected:
            return
        try:
            from core.pod_error_scanner import PodErrorsDetected
        except ImportError:
            return
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

    def _check_request_errors(self, test_config: TestConfig, test_result: TestResult):
        """Check request error rate and stop if overload is too high.

        High error rates (>2%) indicate 503 overload from EPP — the cluster
        cannot handle the requested concurrency. Results under heavy overload
        are unreliable for comparison, so the run stops with guidance to either
        lower concurrency or enable Auto-Scale Concurrency.
        """
        total = test_result.request_total or 0
        errored = test_result.request_errored or 0
        if total == 0 or errored == 0:
            return
        error_pct = errored / total * 100
        if error_pct > 2.0:
            self.log(f"🚨 {errored}/{total} requests failed ({error_pct:.1f}%) — cluster overloaded (503 from EPP)", 'error')
            self.log(f"   Results under heavy overload are unreliable for performance comparison.", 'error')
            self.log(f"   To fix, start a new run with one of these options:", 'error')
            self.log(f"   1. Lower the concurrent users in Workload Configuration", 'error')
            self.log(f"   2. Enable 'Auto-Scale Concurrency' to let the optimizer find sustainable load", 'error')
            if self.db_manager and self.run_id:
                try:
                    with self.db_manager.get_connection() as conn:
                        conn.execute(
                            'UPDATE test_configurations SET status = ? WHERE run_id = ? AND config_name = ?',
                            ('failed', self.run_id, test_config.test_id)
                        )
                except Exception:
                    pass
            from core.pod_error_scanner import PodErrorsDetected
            raise PodErrorsDetected(
                scan_result={'request_error_rate': error_pct, 'errored': errored, 'total': total,
                             'reason': 'overload_503'},
                test_id=test_config.test_id
            )
        elif error_pct > 0.5:
            self.log(f"   ⚠️  {errored}/{total} requests errored ({error_pct:.1f}%) — "
                     f"minor overload, results may have inflated latency", 'warning')
        elif errored > 0:
            self.log(f"   ⚠️  {errored}/{total} requests errored ({error_pct:.1f}%) — acceptable", 'warning')

    def _recalculate_achievable_concurrency(self):
        """Recalculate achievable concurrency from actual Step 6/7 throughput.

        The initial estimate from TPSG calibration (single-pod, 1 user) is often
        too conservative for MoE models where batch efficiency scales with load.
        After Step 7, we have real throughput data at the actual concurrency.
        Use Little's Law: sustainable_concurrency = throughput × avg_latency.
        """
        best_tput = 0
        best_e2e = 0
        source = None

        for results, label in [
            (self.ep_results, 'EP'),
            (self.pareto_results, 'PD'),
        ]:
            for cfg, result in results:
                tput = result.throughput_mean or result.throughput_p90 or 0
                if tput > best_tput:
                    best_tput = tput
                    e2e_s = ((result.ttft_p90 or 0) + (result.tpot_p90 or 10) * self.config.osl) / 1000
                    best_e2e = max(e2e_s, 0.1)
                    source = label

        if self.aggregated_result:
            tput = self.aggregated_result.throughput_mean or self.aggregated_result.throughput_p90 or 0
            if tput > best_tput:
                best_tput = tput
                e2e_s = ((self.aggregated_result.ttft_p90 or 0) + (self.aggregated_result.tpot_p90 or 10) * self.config.osl) / 1000
                best_e2e = max(e2e_s, 0.1)
                source = 'Aggregated'

        if best_tput <= 0:
            return

        measured_concurrency = int(best_tput * best_e2e / self.config.headroom)
        old = self.achievable_concurrency
        requested = int(self.config.qps)

        if old is not None and measured_concurrency > old:
            self.achievable_concurrency = min(measured_concurrency, requested)
            self.log(f"  📊 Recalculated achievable concurrency from measured throughput:", 'info')
            self.log(f"     Best throughput: {best_tput:.1f} req/s ({source}), avg E2E: {best_e2e:.1f}s", 'info')
            self.log(f"     Old estimate (TPSG): {old} → Measured: {measured_concurrency} → Using: {self.achievable_concurrency}", 'info')
        elif old is None and measured_concurrency < requested:
            self.achievable_concurrency = measured_concurrency
            self.log(f"  📊 Computed achievable concurrency from measured throughput: {measured_concurrency}", 'info')

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

                    # Backfill artifacts for completed tests missing from disk
                    from pathlib import Path
                    import json as _json2
                    import hashlib as _hashlib
                    backfilled = 0
                    for name, row in self.completed_tests.items():
                        artifact_dir = Path(f"/mnt/storage/test-artifacts/{name}")
                        if (artifact_dir / "test-result.json").exists():
                            continue
                        artifact_dir.mkdir(parents=True, exist_ok=True)
                        backfilled += 1
                        try:
                            tc_raw = row.get('test_config_json')
                            if tc_raw:
                                with open(artifact_dir / "test-config.json", 'w') as f:
                                    f.write(tc_raw)
                            result_summary = {
                                'test_id': name, 'architecture': row.get('architecture'),
                                'ttft_p90': row.get('ttft_p90'), 'ttft_p95': row.get('ttft_p95'),
                                'ttft_p99': row.get('ttft_p99'), 'throughput_p90': row.get('throughput_p90'),
                                'throughput_p95': row.get('throughput_p95'), 'throughput_p99': row.get('throughput_p99'),
                                'status': row.get('status'),
                            }
                            with open(artifact_dir / "test-result.json", 'w') as f:
                                _json2.dump(result_summary, f, indent=2, default=str)
                            metrics_raw = row.get('metrics_json')
                            if metrics_raw:
                                with open(artifact_dir / "metrics-prometheus.json", 'w') as f:
                                    f.write(metrics_raw)

                            # Try to recover guidellm raw JSON from workload pod
                            raw_file = artifact_dir / "guidellm-raw.json"
                            if not raw_file.exists():
                                try:
                                    remote_path = f"/tmp/guidellm-{name}.json"
                                    kubectl = self.orchestrator.deployment_manager.kubectl
                                    workload_pod = self.orchestrator._guidellm_pod_name
                                    md5_r = kubectl.run(
                                        ['exec', workload_pod, '-n', self.config.namespace,
                                         '--', 'md5sum', remote_path], check=False)
                                    if md5_r.returncode == 0:
                                        remote_md5 = md5_r.stdout.strip().split()[0]
                                        import subprocess as _sp
                                        cp_env = os.environ.copy()
                                        cp_env['KUBECONFIG'] = os.path.expanduser(kubectl.kubeconfig)
                                        for attempt in range(3):
                                            _sp.run(
                                                [kubectl.kubectl_cmd, 'cp',
                                                 f'{workload_pod}:{remote_path}',
                                                 str(raw_file), '-n', self.config.namespace],
                                                env=cp_env, check=False, timeout=120)
                                            if raw_file.exists() and raw_file.stat().st_size > 0:
                                                local_md5 = _hashlib.md5(raw_file.read_bytes()).hexdigest()
                                                if local_md5 == remote_md5:
                                                    break
                                                raw_file.unlink(missing_ok=True)
                                        else:
                                            raw_file.unlink(missing_ok=True)
                                except Exception:
                                    pass
                        except Exception:
                            pass
                    if backfilled:
                        self.log(f"   📁 Backfilled artifacts for {backfilled} tests", 'info')

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

                    # Restore EPP benchmark results from completed step11-epp tests
                    epp_tests = {k: v for k, v in self.completed_tests.items() if k.startswith('step11-epp-')}
                    if epp_tests:
                        for test_name, row_data in epp_tests.items():
                            suffix = test_name.replace('step11-epp-', '')
                            if suffix.startswith('aggregated-'):
                                arch = 'aggregated'
                                combo_name = suffix[len('aggregated-'):]
                            elif suffix.startswith('pd-'):
                                arch = 'pd'
                                combo_name = suffix[len('pd-'):]
                            else:
                                continue
                            result = self._make_test_result_from_db(row_data)
                            weights = {}
                            tc_raw = row_data.get('test_config_json')
                            if tc_raw:
                                try:
                                    tc = _json.loads(tc_raw)
                                    ec = tc.get('epp_config', {})
                                    plugins = ec.get('plugins', {})
                                    if plugins:
                                        weights = {
                                            'prefix_cache_weight': plugins.get('prefix_cache', {}).get('weight', 3.0),
                                            'kv_cache_weight': plugins.get('kv_cache', {}).get('weight', 2.0),
                                            'queue_weight': plugins.get('queue', {}).get('weight', 2.0),
                                            'slo_enabled': False,
                                        }
                                except Exception:
                                    pass
                            if not weights:
                                manifest_raw = row_data.get('manifests_yaml')
                                if manifest_raw:
                                    try:
                                        manifests = _json.loads(manifest_raw)
                                        for mk, mv in manifests.items():
                                            if 'configmap' in mk:
                                                import re as _re2
                                                w_matches = _re2.findall(r'weight:\s*([\d.]+)', mv)
                                                if len(w_matches) >= 3:
                                                    weights = {
                                                        'prefix_cache_weight': float(w_matches[0]),
                                                        'kv_cache_weight': float(w_matches[1]) if 'kv-cache' in mv else 0,
                                                        'queue_weight': float(w_matches[1] if 'kv-cache' not in mv else w_matches[2]),
                                                        'slo_enabled': False,
                                                    }
                                    except Exception:
                                        pass
                            if not weights:
                                preset_weights = {'cache-heavy': (5,1,1), 'queue-heavy': (1,1,5), 'kv-heavy': (2,5,1), 'equal': (2,2,2)}
                                pw = preset_weights.get(combo_name, (3,2,2))
                                weights = {'prefix_cache_weight': pw[0], 'kv_cache_weight': pw[1], 'queue_weight': pw[2], 'slo_enabled': False}
                            if arch not in self.epp_benchmark_results:
                                self.epp_benchmark_results[arch] = []
                            self.epp_benchmark_results[arch].append((combo_name, weights, result))
                        self.log(f"   📋 Restored EPP tuning results: {', '.join(f'{a}({len(r)})' for a, r in self.epp_benchmark_results.items())}", 'info')
                    # Rebuild optimizer state from completed tests
                    self._rebuild_state_from_db()

        except Exception as e:
            self.log(f"⚠️  Could not load previous results: {e}", 'warning')

    def _rebuild_state_from_db(self):
        """Rebuild in-memory optimizer state from completed tests in the DB.

        On resume, Steps 2-7 are skipped but later steps (EPP, Step 10) depend
        on state set during those steps. This reconstructs that state from DB rows.
        """
        import re as _re

        # 1. Rebuild decode_tp_results + optimal_decode_tp from step2 tests
        if not self.decode_tp_results:
            step2 = [(name, row) for name, row in self.completed_tests.items()
                     if name.startswith('step2-trial')]
            if step2:
                candidates = []
                for name, row in step2:
                    tp = row.get('tensor_parallelism', 1)
                    tput = row.get('throughput_p90') or row.get('throughput_p50') or 0
                    ttft = row.get('ttft_p90') or 0
                    tpsg = (tput * self.config.osl) / tp if tp > 0 else 0
                    candidates.append({'tp': tp, 'tpsg': tpsg, 'ttft_p90': ttft, 'throughput_p90': tput})
                self.decode_tp_results = candidates
                use_ttft = self.config.objective == 'ttft'
                if use_ttft:
                    valid = [c for c in candidates if c['ttft_p90'] and c['ttft_p90'] < 1e6]
                    best = min(valid, key=lambda c: c['ttft_p90']) if valid else max(candidates, key=lambda c: c['tpsg'])
                else:
                    best = max(candidates, key=lambda c: c['tpsg'])
                self.optimal_decode_tp = OptimalTP(
                    tp=best['tp'], tpsg=best['tpsg'],
                    ttft_p90=best['ttft_p90'], throughput_p90=best['throughput_p90'])
                self.log(f"   🔄 Rebuilt decode_tp_results ({len(candidates)} TPs), optimal: TP={best['tp']} TPSG={best['tpsg']:.0f}", 'info')

        # 2. Rebuild prefill_tp_results + optimal_prefill_tp from step3 tests
        if not self.prefill_tp_results:
            step3 = [(name, row) for name, row in self.completed_tests.items()
                     if name.startswith('step3-trial')]
            if step3:
                candidates = []
                for name, row in step3:
                    tp = row.get('tensor_parallelism', 1)
                    tput = row.get('throughput_p90') or row.get('throughput_p50') or 0
                    ttft = row.get('ttft_p90') or 0
                    tpsg = (tput * self.config.isl) / tp if tp > 0 else 0
                    candidates.append({'tp': tp, 'tpsg': tpsg, 'ttft_p90': ttft, 'throughput_p90': tput})
                self.prefill_tp_results = candidates
                use_ttft = self.config.objective == 'ttft'
                if use_ttft:
                    valid = [c for c in candidates if c['ttft_p90'] and c['ttft_p90'] < 1e6]
                    best = min(valid, key=lambda c: c['ttft_p90']) if valid else max(candidates, key=lambda c: c['tpsg'])
                else:
                    best = max(candidates, key=lambda c: c['tpsg'])
                self.optimal_prefill_tp = OptimalTP(
                    tp=best['tp'], tpsg=best['tpsg'],
                    ttft_p90=best['ttft_p90'], throughput_p90=best['throughput_p90'])
                self.log(f"   🔄 Rebuilt prefill_tp_results ({len(candidates)} TPs), optimal: TP={best['tp']} TPSG={best['tpsg']:.0f}", 'info')

        # 3. Rebuild aggregated_result + aggregated_tp from step6 tests
        if not self.aggregated_result:
            step6 = [(name, row) for name, row in self.completed_tests.items()
                     if name.startswith('step6-agg-')]
            if step6:
                use_ttft = self.config.objective == 'ttft'
                if use_ttft:
                    best_name, best_row = min(step6, key=lambda x: x[1].get('ttft_p99') or x[1].get('ttft_p90') or 1e9)
                else:
                    best_name, best_row = max(step6, key=lambda x: x[1].get('throughput_p90') or 0)
                self.aggregated_result = self._make_test_result_from_db(best_row)
                self.aggregated_tp = best_row.get('tensor_parallelism', 8)
                self.aggregated_gpus = self.config.total_gpus
                self.log(f"   🔄 Rebuilt aggregated_result: TP={self.aggregated_tp}, "
                         f"TTFT={best_row.get('ttft_p90', 0):.1f}ms", 'info')

        # 4. Rebuild pareto_results from step7 PD tests
        if not self.pareto_results:
            step7 = [(name, row) for name, row in self.completed_tests.items()
                     if name.startswith('step7-') and not name.startswith('step7-ep-')]
            if step7:
                for name, row in step7:
                    tc_raw = row.get('test_config_json')
                    if not tc_raw:
                        continue
                    try:
                        tc = _json.loads(tc_raw)
                        split = FeasibleSplit(
                            prefill_pods=tc.get('prefill_replicas', 1),
                            decode_pods=tc.get('decode_replicas', 1),
                            prefill_tp=tc.get('prefill_tp', 1),
                            decode_tp=tc.get('decode_tp', 1),
                            prefill_gpus=tc.get('prefill_replicas', 1) * tc.get('prefill_tp', 1),
                            decode_gpus=tc.get('decode_replicas', 1) * tc.get('decode_tp', 1),
                            total_gpus=self.config.total_gpus,
                            prefill_pct=0,
                        )
                        split.prefill_pct = (split.prefill_gpus / max(split.total_gpus, 1)) * 100
                        result = self._make_test_result_from_db(row)
                        self.pareto_results.append((split, result))
                    except Exception:
                        continue
                self.log(f"   🔄 Rebuilt pareto_results ({len(self.pareto_results)} PD splits)", 'info')

        # 5. Rebuild EP results from step7-ep tests
        if not self.ep_results:
            step7_ep = [(name, row) for name, row in self.completed_tests.items()
                        if name.startswith('step7-ep-')]
            if step7_ep:
                for name, row in step7_ep:
                    tc_raw = row.get('test_config_json')
                    if not tc_raw:
                        continue
                    try:
                        tc = _json.loads(tc_raw)
                        split = FeasibleSplit(
                            prefill_pods=tc.get('prefill_replicas', 1),
                            decode_pods=tc.get('decode_replicas', 1),
                            prefill_tp=tc.get('prefill_tp', 1),
                            decode_tp=tc.get('decode_tp', 1),
                            prefill_gpus=tc.get('prefill_replicas', 1) * tc.get('prefill_tp', 1),
                            decode_gpus=tc.get('decode_replicas', 1) * tc.get('decode_tp', 1),
                            total_gpus=self.config.total_gpus,
                            prefill_pct=0,
                        )
                        result = self._make_test_result_from_db(row)
                        self.ep_results.append((split, result))
                    except Exception:
                        continue
                if self.ep_results:
                    best_split, best_result = max(self.ep_results,
                        key=lambda x: x[1].throughput_p90 if x[1].throughput_p90 else 0)
                    self.best_ep_config = best_split
                    self.best_ep_result = best_result
                    self.log(f"   🔄 Rebuilt ep_results ({len(self.ep_results)} configs)", 'info')

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

    def _estimate_safe_concurrency(self, tp: int) -> int:
        """Estimate max safe concurrent requests for a single TP-group pod.

        Calculates how many KV cache slots fit in available GPU memory after
        model weights and overhead. Used to cap calibration concurrency so
        Steps 2-3 don't OOM on small TP values.
        """
        gpu_vram = getattr(self, '_gpu_vram_gb', 80.0)
        total_vram = gpu_vram * tp
        model_gb = self._estimate_model_size_gb()
        overhead_gb = 5.0  # CUDA graphs, activations, NCCL buffers
        available_for_kv = max(0, total_vram - model_gb - overhead_gb)

        if not self._model_config:
            return int(self.config.qps)

        # Use ISL+OSL as effective sequence length for the concurrency estimate.
        # Calibration tests now set max_model_len to ISL+OSL (even when auto-tuning
        # is off), so vLLM's actual KV slot size matches this.
        effective_seq_len = self.config.isl + self.config.osl

        num_layers = self._model_config.get('num_hidden_layers', 32)
        num_kv_heads = self._model_config.get('num_key_value_heads',
                       self._model_config.get('num_attention_heads', 32))
        head_dim = self._model_config.get('head_dim',
                   self._model_config.get('hidden_size', 4096) //
                   self._model_config.get('num_attention_heads', 32))
        kv_heads_per_gpu = max(1, num_kv_heads // tp)

        # KV cache per sequence in GB: 2(K+V) × layers × kv_heads/tp × head_dim × seq_len × 2 bytes
        kv_per_seq_gb = (2 * num_layers * kv_heads_per_gpu * head_dim * effective_seq_len * 2) / (1024**3)

        if kv_per_seq_gb <= 0:
            return int(self.config.qps)

        max_concurrent = int(available_for_kv / kv_per_seq_gb)
        result = min(int(self.config.qps), max(1, int(max_concurrent * 0.9)))

        self.log(f"   Calibration concurrency for TP={tp}: {result} "
                 f"(user={int(self.config.qps)}, kv_cap={max_concurrent}, "
                 f"seq_len={effective_seq_len}, kv/seq={kv_per_seq_gb:.2f}GB, "
                 f"available={available_for_kv:.0f}GB)")
        return result

    def _detect_network_type(self) -> str:
        """
        Detect network type by querying actual cluster resources.

        Checks for DRA device classes, then RDMA resources, then NAD.
        """
        import os

        force_nad = os.getenv('INFTUNE_FORCE_NAD', 'false').lower() == 'true'
        if force_nad:
            return 'nad'

        if not self.cluster_resources:
            return 'nad'

        # Check for DRA: either gpu-nic-pair in allocatable or dranet device class
        try:
            # Method 1: gpu-nic-pair resource in allocatable
            r = self.scanner.kubectl.run(
                ['get', 'nodes', '-l', 'nvidia.com/gpu.present=true',
                 '-o', 'jsonpath={.items[0].status.allocatable}'], check=False)
            if r.returncode == 0 and r.stdout.strip():
                import json as _j
                alloc = _j.loads(r.stdout)
                if 'dra.llm-d.io/gpu-nic-pair' in alloc:
                    self.log("Network: DRA gpu-nic-pair detected in allocatable")
                    return 'dra'

            # Method 2: dranet or gpu.nvidia.com device classes exist
            r = self.scanner.kubectl.run(
                ['get', 'deviceclass', '-o', 'jsonpath={.items[*].metadata.name}'], check=False)
            if r.returncode == 0 and r.stdout.strip():
                classes = r.stdout.strip().split()
                has_dranet = any('dranet' in c for c in classes)
                has_gpu_dra = any('gpu.nvidia.com' in c for c in classes)
                if has_dranet or has_gpu_dra:
                    self.log(f"Network: DRA detected (device classes: {', '.join(c for c in classes if 'dranet' in c or 'gpu' in c)})")
                    return 'dra'
        except Exception:
            pass

        # SR-IOV multi-nic: rdma/roce_gdr + multi-nic NAD
        if self.cluster_resources.has_rdma:
            try:
                r = self.scanner.kubectl.run(
                    ['get', 'net-attach-def', '-n', self.config.namespace,
                     '-o', 'jsonpath={.items[*].metadata.name}'], check=False)
                if r.returncode == 0 and ('multi-nic-inference' in r.stdout or 'multi-nic-compute' in r.stdout):
                    self.log("Network: SR-IOV multi-nic detected (rdma + multi-nic NAD)")
                    return 'sriov_multinic'
            except Exception:
                pass

        # SharedDevice: has RDMA resources but no DRA or multi-nic
        if self.cluster_resources.has_rdma:
            self.log("Network: RDMA detected (shared_device mode)")
            return 'shared_device'

        # NAD (Multus CNI)
        try:
            r = self.scanner.kubectl.run(
                ['api-resources', '--api-group=k8s.cni.cncf.io'], check=False)
            if r.returncode == 0 and 'network-attachment-definitions' in r.stdout:
                self.log("Network: NAD (Multus) detected")
                return 'nad'
        except Exception:
            pass

        self.log("Network: No RDMA or NAD detected, using pod network (eth0)")
        return 'eth0'

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
        if not getattr(self.config, 'epp_custom_enabled', True):
            return {'preset': 'default'}
        import math
        block_size = self._compute_block_size()
        lru_capacity = self._compute_lru_capacity(block_size)
        plugins = None
        if self.config.epp_preset == 'custom' and self.config.epp_config:
            plugins = self.config.epp_config.get('plugins', self.config.epp_config)
        return {
            'preset': self.config.epp_preset,
            'plugins': plugins,
            'maxPrefixBlocksToMatch': math.ceil(self.config.isl / block_size),
            'lruCapacityPerServer': lru_capacity,
            'nonCachedTokens': min(16, max(1, self.config.isl // 100)),
        }

    def _compute_lru_capacity(self, block_size: int) -> int:
        """Compute EPP LRU cache capacity from GPU VRAM and model architecture.

        lruCapacity = (gpu_vram_gb * kv_cache_fraction * 1024^3) /
                      (block_size * 2 * num_layers * kv_heads_per_gpu * head_dim * 2)

        This is the number of KV cache blocks that fit in one GPU's available
        memory, which is how many prefix entries the EPP should track.
        """
        kv_cache_fraction = 0.5

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

        tp = self.optimal_decode_tp.tp if self.optimal_decode_tp else 1
        kv_heads_per_gpu = max(num_kv_heads // tp, 1)

        # Bytes per block: block_size tokens × 2 (K+V) × num_layers × kv_heads_per_gpu × head_dim × 2 bytes (fp16)
        bytes_per_block = block_size * 2 * num_layers * kv_heads_per_gpu * head_dim * 2
        if bytes_per_block <= 0:
            return 31250

        available_bytes = self._gpu_vram_gb * kv_cache_fraction * (1024 ** 3)
        capacity = int(available_bytes / bytes_per_block)

        return max(capacity, 1024)

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

    def _detect_rdma_network_annotation(self) -> Optional[str]:
        """Detect Multus NetworkAttachmentDefinition for sriov_multinic mode."""
        if not self.scanner:
            return None
        try:
            import json as _j
            r = self.scanner.kubectl.run(
                ['get', 'net-attach-def', '-n', self.config.namespace,
                 '-o', 'json'], check=False)
            if r.returncode != 0:
                # Try openshift-sriov-network-operator namespace
                r = self.scanner.kubectl.run(
                    ['get', 'net-attach-def', '-n', 'openshift-sriov-network-operator',
                     '-o', 'json'], check=False)
            if r.returncode == 0 and r.stdout.strip():
                items = _j.loads(r.stdout).get('items', [])
                # Prefer multi-nic-inference over multi-nic-compute
                for preferred in ['multi-nic-inference', 'multi-nic-compute']:
                    for item in items:
                        name = item['metadata']['name']
                        ns = item['metadata']['namespace']
                        if name == preferred:
                            return _j.dumps([{"name": name, "namespace": ns}])
                # Fallback: first NAD with multi-nic in name
                for item in items:
                    name = item['metadata']['name']
                    ns = item['metadata']['namespace']
                    if 'multi-nic' in name:
                        return _j.dumps([{"name": name, "namespace": ns}])
        except Exception:
            pass
        return None

    def _get_pod_resources(self, tp: int, total_pods: int) -> tuple:
        """
        Calculate memory and CPU per pod for a specific deployment.

        Queries the cluster for actual free resources (allocatable minus
        currently used by running pods), then divides by pods_per_node.
        Uses min across nodes (not average) so every test gets the same
        resource envelope regardless of scheduling. Results are cached
        on first call so all tests in a run use consistent values.

        Args:
            tp: Tensor parallelism for this deployment
            total_pods: Total number of pods being deployed

        Returns:
            (memory_str, cpu_str) — e.g. ("64Gi", "16")
        """
        import math

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

        pods_from_deployment = math.ceil(total_pods / num_gpu_nodes)
        pods_from_tp = max_gpus_per_node // tp if tp > 0 else 1
        pods_per_node = max(pods_from_deployment, pods_from_tp, 1)

        # Use cached free resources — consistent across all tests in a run.
        # On resume, load from DB; on first run, query cluster and persist.
        if not hasattr(self, '_cached_free_mem_gb') or self._cached_free_mem_gb is None:
            self._cached_free_mem_gb = None
            self._cached_free_cpus = None

            # Try loading from DB first (resume case)
            if self.db_manager and self.run_id:
                try:
                    with self.db_manager.get_connection() as conn:
                        row = conn.execute(
                            'SELECT config_json FROM optimization_runs WHERE id = ?',
                            (self.run_id,)
                        ).fetchone()
                        if row and row['config_json']:
                            import json as _json2
                            saved = _json2.loads(row['config_json'])
                            if saved.get('_resource_pool_mem_gb') and saved.get('_resource_pool_cpus'):
                                self._cached_free_mem_gb = saved['_resource_pool_mem_gb']
                                self._cached_free_cpus = saved['_resource_pool_cpus']
                                logger.info(
                                    f"Loaded resource pool from DB: {self._cached_free_mem_gb:.0f}Gi memory, "
                                    f"{self._cached_free_cpus:.0f} CPUs (consistent with previous tests)")
                except Exception as e:
                    logger.debug(f"Could not load resource pool from DB: {e}")
            try:
                if hasattr(self, 'scanner') and self.scanner and hasattr(self.scanner, 'kubectl'):
                    import json as _json
                    import re as _re

                    def _parse_mem(s):
                        if not s:
                            return 0
                        s = str(s)
                        if s.endswith('Ki'):
                            return int(s[:-2]) / (1024 * 1024)
                        if s.endswith('Mi'):
                            return int(s[:-2]) / 1024
                        if s.endswith('Gi'):
                            return int(s[:-2])
                        if s.endswith('Ti'):
                            return int(s[:-2]) * 1024
                        m = _re.match(r'^(\d+)$', s)
                        return int(m.group(1)) / (1024 ** 3) if m else 0

                    def _parse_cpu(s):
                        if not s:
                            return 0
                        s = str(s)
                        if s.endswith('m'):
                            return int(s[:-1]) / 1000
                        return int(s)

                    selected = set(self.config.selected_nodes) if self.config.selected_nodes else None

                    r = self.scanner.kubectl.run(['get', 'nodes', '-o', 'json'], check=False)
                    node_alloc = {}
                    if r.returncode == 0:
                        for nd in _json.loads(r.stdout).get('items', []):
                            name = nd['metadata']['name']
                            if selected and name not in selected:
                                continue
                            alloc = nd.get('status', {}).get('allocatable', {})
                            if int(alloc.get('nvidia.com/gpu', '0')) <= 0:
                                continue
                            node_alloc[name] = {
                                'mem_gb': _parse_mem(alloc.get('memory', '0')),
                                'cpu': _parse_cpu(alloc.get('cpu', '0')),
                                'used_mem_gb': 0, 'used_cpu': 0,
                            }

                    if node_alloc:
                        r = self.scanner.kubectl.run(['get', 'pods', '-A', '-o', 'json'], check=False)
                        if r.returncode == 0:
                            for pod in _json.loads(r.stdout).get('items', []):
                                phase = pod.get('status', {}).get('phase', '')
                                if phase not in ('Running', 'Pending'):
                                    continue
                                node = pod.get('spec', {}).get('nodeName', '')
                                if node not in node_alloc:
                                    continue
                                for c in pod['spec'].get('containers', []):
                                    req = c.get('resources', {}).get('requests', {})
                                    node_alloc[node]['used_mem_gb'] += _parse_mem(req.get('memory', '0'))
                                    node_alloc[node]['used_cpu'] += _parse_cpu(req.get('cpu', '0'))

                        free_per_node = []
                        for name, res in node_alloc.items():
                            free_mem = max(res['mem_gb'] - res['used_mem_gb'], 0)
                            free_cpu = max(res['cpu'] - res['used_cpu'], 0)
                            free_per_node.append((free_mem, free_cpu, name))
                            logger.debug(f"  {name}: alloc={res['mem_gb']:.0f}Gi/{res['cpu']:.0f}cpu "
                                         f"used={res['used_mem_gb']:.0f}Gi/{res['used_cpu']:.0f}cpu "
                                         f"free={free_mem:.0f}Gi/{free_cpu:.0f}cpu")

                        if free_per_node:
                            self._cached_free_mem_gb = min(m for m, _, _ in free_per_node)
                            self._cached_free_cpus = min(c for _, c, _ in free_per_node)
                            min_mem_node = min(free_per_node, key=lambda x: x[0])[2]
                            min_cpu_node = min(free_per_node, key=lambda x: x[1])[2]
                            logger.info(
                                f"Node free resources (min): {self._cached_free_mem_gb:.0f}Gi memory "
                                f"(bottleneck: {min_mem_node}), {self._cached_free_cpus:.0f} CPUs "
                                f"(bottleneck: {min_cpu_node})")

                            # Persist to DB so resume uses the same values
                            if self.db_manager and self.run_id:
                                try:
                                    with self.db_manager.get_connection() as conn:
                                        row = conn.execute(
                                            'SELECT config_json FROM optimization_runs WHERE id = ?',
                                            (self.run_id,)
                                        ).fetchone()
                                        if row and row['config_json']:
                                            import json as _json3
                                            cfg = _json3.loads(row['config_json'])
                                            cfg['_resource_pool_mem_gb'] = self._cached_free_mem_gb
                                            cfg['_resource_pool_cpus'] = self._cached_free_cpus
                                            conn.execute(
                                                'UPDATE optimization_runs SET config_json = ? WHERE id = ?',
                                                (_json3.dumps(cfg), self.run_id)
                                            )
                                            logger.info("Saved resource pool to DB for resume consistency")
                                except Exception as e:
                                    logger.debug(f"Could not save resource pool to DB: {e}")
            except Exception as e:
                logger.debug(f"Could not query node resources: {e}")

        if not mem_override:
            if self._cached_free_mem_gb:
                usable_memory_gb = self._cached_free_mem_gb * 0.80
            else:
                avg_node_memory_gb = sum(n.memory_gb for n in gpu_nodes) / num_gpu_nodes
                usable_memory_gb = avg_node_memory_gb * 0.85
            memory_per_pod_gb = int(usable_memory_gb / pods_per_node)
            mem_str = f"{memory_per_pod_gb}Gi"
        else:
            mem_str = mem_override

        if not cpu_override:
            if self._cached_free_cpus:
                usable_cpus = self._cached_free_cpus * 0.75
            else:
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

        try:
            # Single test: skip TP calibration, go straight to strategy
            if self.config.objective == 'single_test':
                strategy = self._get_strategy()
                strategy.execute()
                return self._build_results()

            # Generate datasets for the workload
            if self.config.prefix_cache_hit_pct > 0 and self.config.workload_mode == 'synthetic':
                self._generate_prefix_cache_dataset()
            elif self.config.workload_mode == 'synthetic':
                self._generate_random_dataset()
                self.config.workload_mode = 'dataset'
                self.config.dataset_source = self.random_dataset_path
                self.config.dataset_column = 'prompt'
                self.config.dataset_max_output = self.config.osl
                self.log("   Workload switched to random dataset mode for reproducible testing", 'info')

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

            # Return results
            return self._build_results()
        finally:
            self.orchestrator.cleanup()

    def _get_valid_tp_options(self, role: str = 'aggregated') -> List[int]:
        """
        Get valid TP options based on cluster GPUs per node and model size.

        Args:
            role: 'prefill', 'decode', or 'aggregated'. Determines the
                  gpu_memory_utilization budget used for min TP calculation
                  (prefill=0.80, decode/aggregated=0.90).

        Returns powers of 2 up to max GPUs per node, filtered to exclude:
        - TP values too small to fit the model in VRAM
        - TP values that break FP8 block quantization (partition < block_n=128)
        """
        if role == 'prefill':
            gmu = 0.80
        elif role == 'decode':
            gmu = 0.85  # 0.90 base - 5% NIXL KV transfer reserve
        else:
            gmu = 0.90
        if self.cluster_resources:
            tp_options = self.cluster_resources.get_tp_options()
            seq_len = self.config.isl + self.config.osl if hasattr(self.config, 'isl') else 0
            reserve_pct = getattr(self.config, 'memory_reserve_pct', 0.0)
            min_tp = self.cluster_resources.estimate_model_gpu_requirement(
                model_size_gb=self._estimate_model_size_gb(),
                dtype=self._model_dtype,
                is_moe=self._is_moe,
                model_config=self._model_config,
                seq_len=seq_len,
                min_concurrency=4,
                extra_reserve_pct=reserve_pct,
                gpu_memory_utilization=gmu
            )
            tp_options = [tp for tp in tp_options if tp >= min_tp]
            # Cap TP by the instance's GPU limit
            tp_options = [tp for tp in tp_options if tp <= self.config.total_gpus]
        else:
            tp_options = list(self.config.tp_options)

        max_tp_fp8 = self._fp8_max_tp()
        if max_tp_fp8 < 9999:
            before = len(tp_options)
            tp_options = [tp for tp in tp_options if tp <= max_tp_fp8]
            if len(tp_options) < before:
                self.log(f"  FP8 block quantization: max TP={max_tp_fp8} "
                         f"(shared/MoE intermediate too small for higher TP)", 'warning')

        return tp_options or self.config.tp_options

    def _fp8_max_tp(self) -> int:
        """Max TP that satisfies FP8 block quantization for this model.

        FP8 block-quantized weights need partition_size >= block_n (128).
        For MoE models, both the routed experts (moe_intermediate_size) and
        the shared expert (shared_expert_intermediate_size) are TP-sharded.
        The shared expert is always TP-sharded even with EP.
        Returns max safe TP, or a large value if no constraint applies.
        """
        if not (self._model_config and self._model_dtype == 'fp8'):
            return 9999
        fp8_block_n = 128
        dims = [self._model_config.get('intermediate_size', 0)]
        if self._is_moe:
            dims.append(self._model_config.get('moe_intermediate_size', 0))
            dims.append(self._model_config.get('shared_expert_intermediate_size', 0))
        smallest = min(d for d in dims if d > 0) if any(d > 0 for d in dims) else 0
        return smallest // fp8_block_n if smallest > 0 else 9999

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
        if not layers and cfg.get('layers_block_type'):
            layers = len(cfg['layers_block_type'])
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
        dense_intermediate = cfg.get('intermediate_size_mlp', intermediate)
        interleave_step = cfg.get('interleave_moe_layer_step', 1)

        if num_experts > 1 and moe_intermediate:
            # MoE with separate expert FFN size (Qwen-MoE, DeepSeek)
            ffn_per_expert = hidden * moe_intermediate * 3
            shared_ffn = hidden * intermediate * 3 if intermediate else 0
            router_params = hidden * num_experts
            moe_layer = attn_params + ffn_per_expert * num_experts + shared_ffn * num_shared_experts + router_params
            dense_layer = attn_params + hidden * dense_intermediate * 3
        elif num_experts > 1:
            # MoE where intermediate_size IS the per-expert size (Mixtral, Llama-4)
            ffn_per_expert = hidden * intermediate * 3
            router_params = hidden * num_experts
            moe_layer = attn_params + ffn_per_expert * num_experts + router_params
            dense_layer = attn_params + hidden * dense_intermediate * 3
        else:
            moe_layer = 0
            dense_layer = attn_params + hidden * intermediate * 3

        # Handle interleaved MoE/dense layers (e.g. Llama-4: every other layer is MoE)
        if num_experts > 1 and interleave_step > 1:
            moe_layers = layers // interleave_step
            dense_layers = layers - moe_layers
            total_layer_params = moe_layers * moe_layer + dense_layers * dense_layer
        elif num_experts > 1:
            total_layer_params = layers * moe_layer
        else:
            total_layer_params = layers * dense_layer

        embed_params = vocab * hidden * 2
        total = total_layer_params + embed_params
        total_b = total / 1e9

        if num_experts > 1:
            self.log(f"  MoE model: {num_experts} experts, ~{total_b:.1f}B total parameters")

        return round(total_b, 1)

    def _compute_dbo_threshold(self, num_experts: int) -> int:
        """Compute DBO token threshold based on expert count.

        More experts = more all-to-all communication = overlap pays off
        sooner = lower threshold. Upstream uses 32 for DeepSeek (256 experts).
        """
        if num_experts >= 128:
            threshold = 32   # DeepSeek-class, heavy all-to-all
        elif num_experts >= 32:
            threshold = 48   # Medium MoE
        else:
            threshold = 64   # Small MoE (Mixtral), less all-to-all benefit
        return threshold

    def _estimate_model_size_gb(self) -> float:
        """Estimate model weight size in GB for VRAM planning.

        Uses _model_size_b (set from config or name parsing).
        Multipliers account for raw weights + mixed-precision layers:
          MoE FP4:   0.55x (experts ~97% at 0.5B/param, attention stays FP16)
          Dense FP4: 0.7x  (FFN ~60% at 0.5B/param, attention ~30% stays FP16)
          FP8:       1.1x
          FP16:      2.2x
          FP32:      4.4x
        """
        params_b = self._model_size_b
        if self._model_dtype in ('fp4', 'int4'):
            return params_b * (0.55 if self._is_moe else 0.7)
        if self._model_dtype == 'fp8':
            return params_b * (0.85 if self._is_moe else 1.1)
        if self._model_dtype in ('fp16', 'bf16'):
            return params_b * 2.2
        return params_b * 4.4


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
            'calibration_analysis': getattr(self, '_calibration_analysis', None),
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
                    'throughput_p90': result.throughput_p90,
                    'throughput_mean': result.throughput_mean or result.throughput_p90,
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
                    'throughput_mean': result.throughput_mean or result.throughput_p90,
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
                'throughput_mean': (self.aggregated_result.throughput_mean or self.aggregated_result.throughput_p90) if self.aggregated_result else None,
            } if self.aggregated_result else None,
            # Step 10: Latency-bounded throughput maximization
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
            # Step 11: Calibrated Load results
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
            'concurrency_sweep': getattr(self, 'concurrency_sweep_results', None),
            'cache_sweep': getattr(self, 'cache_sweep_results', None),
            # EP results (populated by ThroughputStrategy/BalancedStrategy)
            'ep_configurations': [
                {
                    'prefill_tp': ep_cfg.prefill_tp,
                    'decode_tp': ep_cfg.decode_tp,
                    'prefill_pods': ep_cfg.prefill_pods,
                    'decode_pods': ep_cfg.decode_pods,
                    'total_gpus': ep_cfg.total_gpus,
                    'ttft_p90': result.ttft_p90,
                    'throughput_mean': result.throughput_mean or result.throughput_p90,
                }
                for ep_cfg, result in self.ep_results
            ],
            'best_ep': {
                'prefill_tp': self.best_ep_config.prefill_tp,
                'decode_tp': self.best_ep_config.decode_tp,
                'prefill_pods': self.best_ep_config.prefill_pods,
                'decode_pods': self.best_ep_config.decode_pods,
                'total_gpus': self.best_ep_config.total_gpus,
                'ttft_p90': self.best_ep_result.ttft_p90,
                'throughput_mean': self.best_ep_result.throughput_mean or self.best_ep_result.throughput_p90,
            } if self.best_ep_result and self.best_ep_config else None,
            'calibrated_ep_result': {
                'ttft_p90': self.calibrated_ep_result.ttft_p90,
                'throughput_p90': self.calibrated_ep_result.throughput_p90,
            } if self.calibrated_ep_result else None,
            # Optimization goal for report rendering
            'optimization_goal': self.config.objective,
            # Step 9: EPP tuning results (per architecture)
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

