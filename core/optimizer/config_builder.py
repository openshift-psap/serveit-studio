"""Test config creation and vLLM parameter computation."""

import os
import math
from typing import List, Tuple, Optional, Dict


from core.optimizer.config import FeasibleSplit
from core.config_generator import TestConfig
from core.test_orchestrator import TestResult

class ConfigBuilderMixin:
    """Mixin providing config creation methods for RecipeOptimizer."""

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
        use_concurrency: bool = False,
        concurrency_override: int = None
    ) -> TestConfig:
        """Create aggregated architecture test config.

        Args:
            use_concurrency: If True, use concurrent rate type with num_users.
                             All steps use concurrent/num_users to measure under realistic load.
            concurrency_override: If set, use this instead of effective_concurrency.
                                  Used by Steps 2-3 to cap concurrency based on KV cache capacity.
        """
        if concurrency_override is not None:
            concurrency = concurrency_override
        else:
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
            enable_expert_parallel=False,  # Single-node: replicate experts (no NCCL all-to-all overhead)
            enable_dbo=False,  # Requires multi-node EP (LWS size>=2) with DeepEP/NVSHMEM
            dbo_prefill_token_threshold=getattr(self, '_dbo_threshold', 32),
            dbo_decode_token_threshold=getattr(self, '_dbo_threshold', 32),
            enable_eplb=False,  # Requires multi-node EP with NVSHMEM
            moe_backend=None,  # deep_gemm requires DeepEP; single-node uses NCCL
            all2all_backend=None,  # DeepEP backends require multi-node; single-node uses NCCL
        )
        if not is_calibration or not getattr(self.config, 'advanced_vllm_custom_enabled', True):
            cfg = self._apply_advanced_vllm(cfg)

        return cfg

    def _image_supports_moe_backend(self) -> bool:
        """Check if the vLLM image supports --moe-backend (requires llm-d v0.6.0+)."""
        import re
        tag = (self.config.image or '').rsplit(':', 1)[-1] if ':' in (self.config.image or '') else ''
        m = re.match(r'v?(\d+)\.(\d+)\.(\d+)', tag)
        if m:
            major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
            return (major, minor, patch) >= (0, 6, 0)
        return False

    def _get_profiled_kv_cache_bytes(self, tp: int) -> Optional[int]:
        """Get profiled KV cache memory in bytes for decode pods at a given TP.

        Uses vllm_available_kv_gb from Steps 2-3 calibration. This is the
        actual KV cache memory after model loading and overhead — per GPU.
        Multiplied by TP to get the total per-pod budget.
        Returns None if no profile data available for this TP.
        """
        for config, result in self.all_test_results:
            if (result.vllm_available_kv_gb is not None
                    and getattr(config, 'tensor_parallelism', None) == tp):
                # vllm_available_kv_gb is per-GPU; multiply by TP for per-pod total
                total_kv_gb = result.vllm_available_kv_gb * tp
                return int(total_kv_gb * (1024 ** 3))
        return None

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
        # When custom settings are disabled, strip auto-computed values
        # so vLLM uses its own defaults (upstream llm-d behavior).
        # Exception: max_model_len is ALWAYS set — without it vLLM uses
        # max_position_embeddings (e.g., 40960) which wastes 95%+ of KV
        # cache memory on empty slots, making it impossible to serve the
        # user's workload at their requested concurrency.
        if not getattr(self.config, 'advanced_vllm_custom_enabled', True):
            cfg.gpu_memory_utilization = None
            cfg.prefill_gpu_memory_utilization = None
            cfg.decode_gpu_memory_utilization = None
            cfg.max_num_seqs = None
            cfg.prefill_max_num_seqs = None
            cfg.decode_max_num_seqs = None
            cfg.max_num_batched_tokens = None
            return cfg

        adv = self.config.advanced_vllm
        if not adv:
            return cfg

        # Raw text mode: parse flags from text, set known ones on TestConfig,
        # put unknown ones in extra_vllm_args. Skip form fields entirely.
        if adv.get('_mode') == 'raw' and adv.get('_raw_text'):
            return self._apply_raw_vllm_args(cfg, adv['_raw_text'])

        # Form mode: apply structured overrides
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
            'reasoning_parser': 'reasoning_parser',
            'chat_template_content_format': 'chat_template_content_format',
            'moe_backend': 'moe_backend',
            'all2all_backend': 'all2all_backend',
            'dbo_prefill_token_threshold': 'dbo_prefill_token_threshold',
            'dbo_decode_token_threshold': 'dbo_decode_token_threshold',
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
            'enable_expert_parallel': 'enable_expert_parallel',
            'enable_dbo': 'enable_dbo',
            'enable_eplb': 'enable_eplb',
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

    def _apply_raw_vllm_args(self, cfg: TestConfig, raw_text: str) -> TestConfig:
        """Parse raw text flags: set known flags on TestConfig, extras go to extra_vllm_args."""
        known_flags = {
            '--max-model-len': ('max_model_len', int),
            '--block-size': ('block_size', int),
            '--max-num-seqs': ('max_num_seqs', int),
            '--max-num-batched-tokens': ('max_num_batched_tokens', int),
            '--pipeline-parallel-size': ('pipeline_parallel_size', int),
            '--gpu-memory-utilization': ('gpu_memory_utilization', float),
            '--dtype': ('dtype', str),
            '--kv-cache-dtype': ('kv_cache_dtype', str),
            '--tool-call-parser': ('tool_call_parser', str),
            '--reasoning-parser': ('reasoning_parser', str),
            '--chat-template-content-format': ('chat_template_content_format', str),
            '--moe-backend': ('moe_backend', str),
            '--all2all-backend': ('all2all_backend', str),
            '--dbo-prefill-token-threshold': ('dbo_prefill_token_threshold', int),
            '--dbo-decode-token-threshold': ('dbo_decode_token_threshold', int),
        }
        known_toggles = {
            '--enable-prefix-caching': ('enable_prefix_caching', True),
            '--disable-custom-all-reduce': ('disable_custom_all_reduce', True),
            '--enable-auto-tool-choice': ('enable_auto_tool_choice', True),
            '--enable-expert-parallel': ('enable_expert_parallel', True),
            '--enable-dbo': ('enable_dbo', True),
            '--enable-eplb': ('enable_eplb', True),
            '--trust-remote-code': ('trust_remote_code', True),
            '--disable-log-requests': ('disable_log_requests', True),
        }
        # Disable all toggles first — only enable what's in the raw text
        for _, (attr, _) in known_toggles.items():
            setattr(cfg, attr, False)

        extra = []
        for line in raw_text.strip().split('\n'):
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            parts = line.split(None, 1)
            flag = parts[0]
            value = parts[1] if len(parts) > 1 else None

            if flag in known_toggles:
                attr, val = known_toggles[flag]
                setattr(cfg, attr, val)
            elif flag in known_flags and value is not None:
                attr, cast = known_flags[flag]
                try:
                    setattr(cfg, attr, cast(value))
                except (ValueError, TypeError):
                    extra.append(line)
            else:
                extra.append(line)

        if extra:
            cfg.extra_vllm_args = ' \\\n                  '.join(extra)

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
            kv_cache_memory_bytes=self._get_profiled_kv_cache_bytes(split.decode_tp),
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
            enable_expert_parallel=False,  # Single-node: replicate experts (no NCCL all-to-all overhead)
            enable_dbo=False,  # Requires multi-node EP (LWS size>=2) with DeepEP/NVSHMEM
            dbo_prefill_token_threshold=getattr(self, '_dbo_threshold', 32),
            dbo_decode_token_threshold=getattr(self, '_dbo_threshold', 32),
            enable_eplb=False,  # Requires multi-node EP with NVSHMEM
            moe_backend=None,  # deep_gemm requires DeepEP; single-node uses NCCL
            all2all_backend=None,  # DeepEP backends require multi-node; single-node uses NCCL
        )
        return self._apply_advanced_vllm(cfg)

    def _create_ep_config(self, split: 'FeasibleSplit') -> TestConfig:
        """Create EP (Expert Parallelism) architecture test config.

        EP uses the PD prefill/decode split with EP-specific flags enabled.
        Same template structure as PD but with --enable-expert-parallel,
        --enable-eplb, --moe-backend, --all2all-backend, and NVSHMEM env vars.
        """
        concurrency = self.effective_concurrency

        gpu_mem_util = self._compute_gpu_mem_util(split.prefill_tp)
        alloc = self._gpu_vram_gb * gpu_mem_util
        reserve = self._gpu_vram_gb - alloc
        self.log(f"   Memory: gpu_memory_utilization={gpu_mem_util:.4f} "
                 f"→ {alloc:.0f}GB allocated, {reserve:.0f}GB reserved for overhead (per GPU)")

        prefill_max_num_seqs = self._compute_max_num_seqs(split.prefill_tp)
        decode_max_num_seqs = self._compute_max_num_seqs(split.decode_tp)
        max_batched = self._compute_max_num_batched_tokens(split.prefill_tp)

        total_pods = split.prefill_pods + split.decode_pods
        min_tp = min(split.prefill_tp, split.decode_tp)
        mem, cpu = self._get_pod_resources(tp=min_tp, total_pods=total_pods)

        dbo_threshold = getattr(self, '_dbo_threshold', 32)

        cfg = TestConfig(
            test_id=f"step7-ep-{split.prefill_pods}p{split.decode_pods}d-ptp{split.prefill_tp}-dtp{split.decode_tp}",
            architecture='ep',
            model_name=self.config.model_name,

            namespace=self.config.namespace,
            isl=self.config.isl,
            osl=self.config.osl,
            num_users=concurrency,
            request_type=self.config.rate_type,
            request_rate=concurrency,

            tensor_parallelism=split.prefill_tp,
            replicas=total_pods,
            prefill_replicas=split.prefill_pods,
            decode_replicas=split.decode_pods,
            prefill_decode_ratio=f"{split.prefill_pods}:{split.decode_pods}",
            prefill_tp=split.prefill_tp,
            decode_tp=split.decode_tp,

            max_model_len=self.config.max_model_len,
            gpu_memory_utilization=gpu_mem_util,
            prefill_gpu_memory_utilization=gpu_mem_util,
            decode_gpu_memory_utilization=gpu_mem_util,
            gpu_vram_gb=self._gpu_vram_gb,
            prefill_max_num_seqs=prefill_max_num_seqs,
            decode_max_num_seqs=decode_max_num_seqs,
            max_num_batched_tokens=max_batched,
            kv_cache_memory_bytes=self._get_profiled_kv_cache_bytes(split.decode_tp),
            isl_stdev=self.config.isl_stdev,
            osl_stdev=self.config.osl_stdev,
            turns=self.config.turns,
            image=self.config.image,
            pvc_name=self.config.pvc_name,
            nccl_ib_hca=self.config.nccl_ib_hca,
            optimization_goal='throughput',
            test_duration=self.config.test_duration,
            stop_mode=self.config.stop_mode,
            max_requests=self.config.max_requests,
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
            enable_expert_parallel=(max(split.prefill_tp, split.decode_tp) > 1),
            enable_dbo=(max(split.prefill_tp, split.decode_tp) > 1),
            dbo_prefill_token_threshold=dbo_threshold,
            dbo_decode_token_threshold=dbo_threshold,
            enable_eplb=(max(split.prefill_tp, split.decode_tp) > 1),
            moe_backend='deep_gemm' if max(split.prefill_tp, split.decode_tp) > 1 and self._image_supports_moe_backend() else None,
            all2all_backend=None,
        )
        return self._apply_advanced_vllm(cfg)

