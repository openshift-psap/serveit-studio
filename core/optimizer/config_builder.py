"""Test config creation and vLLM parameter computation."""

import math
from typing import List, Tuple, Optional


from core.optimizer.config import FeasibleSplit
from core.config_generator import TestConfig
from core.test_orchestrator import TestResult

class ConfigBuilderMixin:
    """Mixin providing config creation methods for RecipeOptimizer."""

    def _resolve_block_size(self):
        """Resolve block_size: user custom > auto-computed > None (vLLM default).

        When advanced_vllm block_size mode is:
          - 'custom': use user's value
          - 'auto': use ServeIt auto-computed value
          - 'default': None — let vLLM use its own default
        """
        adv = getattr(self.config, 'advanced_vllm', None) or {}
        bs_setting = adv.get('block_size', {})
        if isinstance(bs_setting, dict):
            mode = bs_setting.get('mode', 'auto')
            if mode == 'custom' and bs_setting.get('value') is not None:
                return int(bs_setting['value'])
            if mode == 'default':
                return None
        return self._compute_block_size()

    def _find_pareto_front(self) -> List[Tuple[FeasibleSplit, 'TestResult']]:
        """
        Find Pareto front from P/D split results.

        A configuration is Pareto optimal if no other configuration has
        both lower TTFT P99 and higher throughput P90.
        Uses P99 instead of P90 to penalize configs with unstable tail latency.
        """
        pareto = []

        for i, (split_i, result_i) in enumerate(self.pareto_results):
            ttft_i = result_i.ttft_p99 or result_i.ttft_p90 or result_i.ttft_p50 or 1000000.0
            tput_i = result_i.throughput_p90 or result_i.throughput_p50 or 0.0

            dominated = False
            for j, (split_j, result_j) in enumerate(self.pareto_results):
                if i == j:
                    continue
                ttft_j = result_j.ttft_p99 or result_j.ttft_p90 or result_j.ttft_p50 or 1000000.0
                tput_j = result_j.throughput_p90 or result_j.throughput_p50 or 0.0

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

        # Multi-node: if TP > gpus_per_node, use LWS multi-pod groups
        gpus_per_node = self.cluster_resources.max_gpus_per_node if self.cluster_resources else 8
        agg_lws_size = max(1, tp // gpus_per_node) if tp > gpus_per_node else 1
        agg_gpus_per_pod = min(tp, gpus_per_node) if agg_lws_size > 1 else None

        replicas = num_gpus // tp
        mem, cpu = self._get_pod_resources(tp=tp, total_pods=replicas)

        max_num_seqs = self._compute_max_num_seqs(tp)

        # Only apply stdev when using the full workload ISL/OSL (Steps 7-8),
        # not for calibration tests (Steps 2-3) where ISL or OSL is fixed to 1
        is_calibration = (isl != self.config.isl) or (osl != self.config.osl)
        max_batched = None if is_calibration else self._compute_max_num_batched_tokens(tp, role='aggregated')
        enable_chunked = not is_calibration and max_batched is not None and self.config.max_model_len > max_batched

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
            enable_chunked_prefill=enable_chunked,
            isl_stdev=None if is_calibration else self.config.isl_stdev,
            osl_stdev=None if is_calibration else self.config.osl_stdev,
            isl_min=None if is_calibration else getattr(self.config, 'isl_min', None),
            isl_max=None if is_calibration else getattr(self.config, 'isl_max', None),
            osl_min=None if is_calibration else getattr(self.config, 'osl_min', None),
            osl_max=None if is_calibration else getattr(self.config, 'osl_max', None),
            turns=1 if is_calibration else self.config.turns,
            first_prompt_tokens=None if is_calibration else getattr(self.config, 'first_prompt_tokens', None),
            first_prompt_tokens_stdev=None if is_calibration else getattr(self.config, 'first_prompt_tokens_stdev', None),
            first_prompt_tokens_min=None if is_calibration else getattr(self.config, 'first_prompt_tokens_min', None),
            first_prompt_tokens_max=None if is_calibration else getattr(self.config, 'first_prompt_tokens_max', None),
            first_output_tokens=None if is_calibration else getattr(self.config, 'first_output_tokens', None),
            first_output_tokens_stdev=None if is_calibration else getattr(self.config, 'first_output_tokens_stdev', None),
            prefix_tokens=None if is_calibration else getattr(self.config, 'prefix_tokens', None),
            prefix_count=None if is_calibration else getattr(self.config, 'prefix_count', None),
            turn_delay=None if is_calibration else getattr(self.config, 'turn_delay', None),
            turn_delay_stdev=None if is_calibration else getattr(self.config, 'turn_delay_stdev', None),
            turn_delay_min=None if is_calibration else getattr(self.config, 'turn_delay_min', None),
            turn_delay_max=None if is_calibration else getattr(self.config, 'turn_delay_max', None),
            image=self.config.image,
            scheduler_image=self.config.scheduler_image,
            pvc_name=self.config.pvc_name,
            per_node_storage=getattr(self.config, "per_node_storage", False),
            node_nfs_pvcs=getattr(self.config, "node_nfs_pvcs", None) or [],
            storage_class=getattr(self.config, "storage_class", None),
            pvc_size=getattr(self.config, "pvc_size", None),
            local_disk_path=getattr(self.config, "local_disk_path", None),
            nccl_ib_hca=self.config.nccl_ib_hca,
            max_model_len=self.config.max_model_len,
            gpu_memory_utilization=gpu_memory_utilization,
            gpu_vram_gb=self._gpu_vram_gb,
            max_num_seqs=max_num_seqs,
            optimization_goal='ttft',
            network_type=self.config.network_type,
            rdma_device_resources=self.config.rdma_device_resources or [],
            rdma_nics_per_node=self.config.rdma_nics_per_node or 0,
            rdma_network_annotation=self.config.rdma_network_annotation,
            selected_dra_classes=self.config.selected_dra_classes or [],
            dra_gpu_resource_key=getattr(self.config, 'dra_gpu_resource_key', None),
            gateway_class=self.config.gateway_class,
            exclusive_pf=getattr(self.config, 'exclusive_pf', False),
            lws_size=agg_lws_size if agg_lws_size > 1 else None,
            gpus_per_pod=agg_gpus_per_pod,
            nnodes=agg_lws_size if agg_lws_size > 1 else None,

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
            block_size=self._resolve_block_size(),
            extra_env_vars=self.config.extra_env_vars,
            enable_expert_parallel=False,
            enable_dbo=False,
            dbo_prefill_token_threshold=getattr(self, '_dbo_threshold', 32),
            dbo_decode_token_threshold=getattr(self, '_dbo_threshold', 32),
            enable_eplb=False,
            moe_backend=None,
            all2all_backend=None,
            use_deep_gemm=getattr(self, '_use_deep_gemm', None),
            has_hybrid_attention=getattr(self, '_has_hybrid_attention', False),
        )
        cfg = self._apply_advanced_vllm(cfg)
        cfg = self._auto_tune_model_loader(cfg)
        return cfg

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

    def _compute_gpu_mem_util(self, tp: int, log: bool = True) -> float:
        """Compute gpu_memory_utilization per TP.

        When profiled data from Steps 2-3 is available, uses measured overhead
        to compute a precise U for this TP. Otherwise falls back to a safe default.
        Higher TP = less model weight per GPU = more room, so the fallback
        scales with TP.
        """
        measured = self._get_measured_overhead(tp)
        if measured is not None and measured > 0:
            safe_budget = self._gpu_vram_gb - measured - 2.0
            if safe_budget > 0:
                u = round(safe_budget / self._gpu_vram_gb, 2)
                u = min(u, 0.95)
                if log:
                    self.log(f"   gpu_memory_utilization={u} (profiled: measured overhead={measured:.1f}GB, "
                             f"usable={safe_budget:.0f}/{self._gpu_vram_gb:.0f}GB)")
                return u

        gpus_per_node = 8
        if self.cluster_resources:
            gpu_nodes = [n for n in self.cluster_resources.nodes if n.gpus > 0]
            if gpu_nodes:
                gpus_per_node = max(n.gpus for n in gpu_nodes)
        pods_per_node = max(gpus_per_node // tp, 1)
        reserve_pct = 0.05 + (pods_per_node - 1) * 0.008
        reserve_gb = max(self._gpu_vram_gb * reserve_pct, 5.0)
        u = round((self._gpu_vram_gb - reserve_gb) / self._gpu_vram_gb, 2)
        if log:
            self.log(f"   gpu_memory_utilization={u} (estimated: {pods_per_node} pods/node, "
                     f"reserving {reserve_gb:.1f}GB for overhead)")
        return u

    def _compute_max_num_seqs(self, tp: int, role: str = 'aggregated',
                              gpu_mem_util_override: float = None,
                              num_pods: int = 1) -> Optional[int]:
        """Compute max_num_seqs by evaluating four competing constraints.

        max_num_seqs = min(S_activation, S_kv, S_concurrency, 512)

        S_activation: Compute slot scale from model activation profile.
        S_kv:         KV cache capacity from available VRAM.
        S_concurrency: Role-dependent:
                      - Prefill: concurrency / prefill_pods (only active prefills)
                      - Decode: concurrency × 1.5 (holds ALL sequences post-prefill)
                      - Aggregated: concurrency × 2.0 (handles both phases)
        512:          Hard cap for CUDA graph compilation.
        """
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

        dtype_bytes = 1 if getattr(self, '_model_dtype', 'fp16') == 'fp8' else 2
        gpu_vram = self._gpu_vram_gb

        # --- S_activation: compute slot scale ---
        slot_weight = num_layers * num_kv_heads * head_dim * dtype_bytes
        effective_weight = slot_weight / tp
        if gpu_vram >= 120: arch_coeff = 1.2
        elif gpu_vram >= 70: arch_coeff = 1.0
        elif gpu_vram >= 40: arch_coeff = 0.8
        else: arch_coeff = 0.6
        s_activation = int((gpu_vram * (1024 ** 3) * arch_coeff) / effective_weight)

        # --- S_kv: KV cache capacity ---
        gpu_mem_util = gpu_mem_util_override or self._compute_gpu_mem_util(tp, log=False)
        model_weight_gb = self._estimate_model_size_gb() / tp
        overhead_gb = 4.0
        available_kv_gb = max(0, gpu_vram * gpu_mem_util - model_weight_gb - overhead_gb)
        kv_per_seq_bytes = (2 * num_layers * (num_kv_heads // max(tp, 1)) * head_dim
                           * self.config.max_model_len * dtype_bytes)
        s_kv = int(available_kv_gb * (1024 ** 3) / kv_per_seq_bytes) if kv_per_seq_bytes > 0 else 512

        # --- S_concurrency: role-dependent ---
        concurrency = getattr(self, 'effective_concurrency', int(self.config.qps))
        if role == 'prefill':
            # Prefill only handles active prefills — a fraction of total users.
            # At any time, only concurrency/prefill_pods users are prefilling per pod.
            s_concurrency = max(64, int(concurrency / max(num_pods, 1) * 2.0))
        elif role == 'decode':
            # Decode holds ALL sequences that completed prefill.
            # Needs capacity for full concurrency across fewer decode pods.
            s_concurrency = max(64, int(concurrency / max(num_pods, 1) * 3.0))
        else:
            # Aggregated: handles both phases
            s_concurrency = max(64, int(concurrency * 2.0))

        # --- Final: min of all constraints, aligned to 32 for warp scheduling ---
        raw = min(s_activation, s_kv, s_concurrency, 512)
        max_seqs = max(64, (raw // 32) * 32 or 64)

        self.log(f"   max_num_seqs(TP={tp}, {role}): {max_seqs} "
                 f"(S_act={s_activation}, S_kv={s_kv}, S_conc={s_concurrency}, cap=512, "
                 f"model_len={self.config.max_model_len}, VRAM={gpu_vram:.0f}GB)")
        return max_seqs

    def _compute_moe_dp_chunk_size(self, tp: int, max_num_seqs: int = None) -> Optional[int]:
        """Compute MoE dispatch chunk size by balancing 4 constraints.

        moe_dp_chunk_size = min(S_sequences, S_expert_capacity, S_dispatch, 512)

        S_sequences:       Can't dispatch more tokens than max_num_seqs allows.
                          Uses the already-computed max_num_seqs (which itself
                          balances activation, KV capacity, and concurrency).
        S_expert_capacity: GPU activation memory per expert limits how many
                          tokens each expert can process per dispatch.
        S_dispatch:        Balance all2all communication cost vs GPU utilization.
                          Scales with sqrt(num_experts * batch / TP) because
                          more experts and more tokens need larger chunks to
                          amortize dispatch overhead, but more TP GPUs increase
                          per-chunk communication volume.
        512:               Hard cap — per-chunk latency and activation memory
                          allocation overhead dominate above 512.
        """
        if not self._is_moe or not self._model_config:
            return None

        num_experts = self._num_experts or 8
        top_k = self._model_config.get('num_experts_per_tok', 2)

        # S_sequences: bound by max_num_seqs (already balances 4 constraints)
        s_sequences = max_num_seqs or self._compute_max_num_seqs(tp)

        # S_expert_capacity: activation memory per expert per token
        # Each dispatched token activates top_k experts. Per expert, the token
        # passes through gate_proj (up) and down_proj: intermediate_size * 2 * dtype_bytes.
        # Activation budget ≈ (VRAM - model_weights - KV_reserved - overhead) / num_experts
        intermediate = self._model_config.get('moe_intermediate_size',
                       self._model_config.get('intermediate_size', 14336))
        dtype_bytes = 1 if getattr(self, '_model_dtype', 'fp16') == 'fp8' else 2
        act_per_token_per_expert = intermediate * 2 * dtype_bytes
        model_weight_gb = self._estimate_model_size_gb() / tp
        overhead_gb = 4.0
        activation_budget_gb = max(0, self._gpu_vram_gb - model_weight_gb - overhead_gb) * 0.15
        expert_budget_bytes = (activation_budget_gb * 1024**3) / max(num_experts, 1)
        s_expert_capacity = int(expert_budget_bytes / act_per_token_per_expert) if act_per_token_per_expert > 0 else 512

        # S_dispatch: balance all2all overhead vs utilization
        # Larger chunks amortize dispatch latency but increase per-chunk
        # communication volume across TP GPUs. sqrt() balances the tradeoff.
        # Divide by TP because each all2all round involves TP GPUs.
        batch_tokens = s_sequences * top_k
        s_dispatch = int(math.sqrt(num_experts * batch_tokens / max(tp, 1)))

        # Final: min of all, aligned to 64 for GPU warp efficiency
        raw = min(s_sequences, s_expert_capacity, s_dispatch, 512)
        chunk = max(128, (raw // 64) * 64 or 128)

        winner = ('S_seq' if raw == s_sequences else
                  'S_expert' if raw == s_expert_capacity else
                  'S_dispatch' if raw == s_dispatch else 'cap')

        self.log(f"   moe_dp_chunk_size(TP={tp}): {chunk} "
                 f"(S_seq={s_sequences}, S_expert={s_expert_capacity}, "
                 f"S_dispatch={s_dispatch}, cap=512, "
                 f"experts={num_experts}, top_k={top_k}, bound={winner})")
        return chunk

    def _compute_ep_memory_gb(self, max_num_batched_tokens: int) -> tuple:
        """Compute EP memory overhead from model architecture.

        Returns (nvshmem_env_size_str, total_ep_overhead_gb).

        vLLM passes max_num_batched_tokens to DeepEP's
        get_low_latency_rdma_size_hint as the buffer token count.
        NVSHMEM_SYMMETRIC_SIZE is capped at 16G — CUDA VMM (enabled via
        NVSHMEM_DISABLE_CUDA_VMM=0) grows the heap dynamically beyond that.
        The EP reserve must account for the full RDMA + NVL allocation.
        """
        if not self._model_config:
            return '16G', 20.0

        hidden = self._model_config.get('hidden_size', 4096)
        num_experts = self._num_experts or 8
        max_tokens = max_num_batched_tokens or self.config.max_model_len
        num_scales = hidden // 128

        dispatch_msg = 16 + max(hidden * 2, hidden + num_scales * 4)
        combine_msg = num_scales * 4 + hidden * 2

        send_buf = max(max_tokens * dispatch_msg,
                       num_experts * max_tokens * combine_msg)
        recv_buf = max(num_experts * max_tokens * dispatch_msg,
                       num_experts * max_tokens * combine_msg)
        signal_buf = ((num_experts * 4 + 127) // 128) * 128

        rdma_gb = (send_buf + recv_buf + signal_buf) * 2 / (1024**3)

        # NVL buffer (VLLM_DEEPEP_BUFFER_SIZE_MB default 1024 = 1GB)
        nvl_gb = 1.0

        # NVSHMEM env var: initial heap, capped at 16G (VMM grows beyond)
        nvshmem_env_gb = min(max(math.ceil(rdma_gb * 1.25 + 0.5), 2), 16)

        # Total EP memory: actual RDMA + NVL + overhead
        total_gb = rdma_gb + nvl_gb + 2.0

        self.log(f"   EP memory: RDMA={rdma_gb:.1f}GB + NVL={nvl_gb:.0f}GB + overhead=2GB "
                 f"= {total_gb:.1f}GB (NVSHMEM_SYMMETRIC_SIZE={nvshmem_env_gb}G, "
                 f"hidden={hidden}, experts={num_experts}, batch={max_tokens})")
        return f'{nvshmem_env_gb}G', total_gb

    def _compute_max_num_batched_tokens(self, tp: int, role: str = 'aggregated') -> Optional[int]:
        """Compute max_num_batched_tokens tuned for ISL/OSL and architecture role.

        For PD prefill: process the entire prompt in as few iterations as possible.
          Formula: min(ISL, kv_available_tokens × 0.8)
          The prefill pod only does prefill — no decode interference — so we can
          use large batches. Safety margin of 0.8 for activation memory.

        For PD decode: optimize for decode throughput, small batches are fine.
          Formula: min(OSL × max_num_seqs_cap, kv_available_tokens × 0.8)

        For aggregated: balance prefill and decode sharing the same engine.
          Scale batch latency target with ISL: short prompts use 200ms,
          long prompts use up to 2s to reduce iteration overhead.
        """
        if not self.optimal_prefill_tp or self.optimal_prefill_tp.tpsg <= 0:
            return None

        isl = self.config.isl
        osl = self.config.osl

        # Floor: must be >= vLLM's max_tokens_per_mm_item for multimodal models
        floor = 2048
        if self._model_config:
            vcfg = self._model_config.get('vision_config') or self._model_config.get('visual')
            if vcfg and isinstance(vcfg, dict):
                patch = vcfg.get('patch_size', 16)
                img_size = vcfg.get('image_size', 0) or vcfg.get('resolution', 0)
                if img_size > 0:
                    floor = max(floor, (img_size // patch) ** 2)
                else:
                    default_out = vcfg.get('default_output_length', 0)
                    if default_out > 0:
                        floor = max(floor, default_out * 10)
                    else:
                        floor = max(floor, 4096)

        if role == 'prefill':
            # PD prefill: cap at optimal GEMM saturation point for inter-request interleaving.
            # Setting this to ISL defeats chunked prefill — the scheduler does one monolithic
            # pass instead of interleaving chunks from multiple requests.
            #
            # For ISL <= 16K: use ISL (single pass is fine, no queuing benefit from chunking)
            # For ISL > 16K: cap at 16K × TP to allow chunked prefill interleaving.
            #   At TP2 on H200: 16K tokens already achieves >90% Tensor Core utilization.
            #   Going from 16K to 100K gives zero marginal GEMM efficiency but ruins scheduling.
            #
            # Memory ceiling from KV capacity as safety check.
            gemm_cap = 16384  # fixed per scheduler step — TP splits heads, not sequence length
            kv_bytes = self._get_profiled_kv_cache_bytes(tp)
            if kv_bytes:
                num_layers = self._model_config.get('num_hidden_layers', 32) if self._model_config else 32
                num_kv_heads = (self._model_config.get('num_key_value_heads', 32) if self._model_config else 32)
                head_dim = (self._model_config.get('head_dim',
                            self._model_config.get('hidden_size', 4096) //
                            self._model_config.get('num_attention_heads', 32)) if self._model_config else 128)
                kv_heads_per_gpu = max(1, num_kv_heads // tp)
                bytes_per_token = 2 * num_layers * kv_heads_per_gpu * head_dim * 2
                kv_cap_tokens = int(kv_bytes / bytes_per_token) if bytes_per_token > 0 else isl
                mem_budget = int(kv_cap_tokens * 0.8)
            else:
                mem_budget = gemm_cap

            if isl <= 16384:
                result = max(floor, min(isl, mem_budget, self.config.max_model_len))
            else:
                result = max(floor, min(gemm_cap, mem_budget, self.config.max_model_len))

            self.log(f"   max_num_batched_tokens(TP={tp}, prefill): {result} "
                     f"(ISL={isl}, gemm_cap={gemm_cap}, mem_budget={mem_budget}, floor={floor})")
            return result

        elif role == 'decode':
            # PD decode: small batches, optimize for decode throughput
            result = max(floor, min(osl * 64, self.config.max_model_len))
            self.log(f"   max_num_batched_tokens(TP={tp}, decode): {result} "
                     f"(OSL={osl})")
            return result

        else:
            # Aggregated: scale batch latency with ISL
            # Short ISL (<4K): 200ms target (low latency)
            # Long ISL (>32K): up to 2s target (reduce iteration overhead)
            tpsg = self.optimal_prefill_tp.tpsg
            if isl < 4000:
                target_s = 0.2
            elif isl < 32000:
                target_s = 0.2 + (isl - 4000) / 28000 * 0.8  # 0.2s to 1.0s
            else:
                target_s = 1.0 + min((isl - 32000) / 68000, 1.0)  # 1.0s to 2.0s
            budget = int(tpsg * tp * target_s)
            # Cap at 16K for aggregated to prevent decode ITL spikes when
            # a large prefill enters the shared engine
            agg_cap = 16384
            result = max(floor, min(budget, agg_cap, self.config.max_model_len))
            self.log(f"   max_num_batched_tokens(TP={tp}, aggregated): {result} "
                     f"(TPSG={tpsg:.0f} × TP={tp} × {target_s:.1f}s = {budget}, "
                     f"cap={agg_cap}, ISL={isl}, floor={floor})")
            return result

    def _apply_advanced_vllm(self, cfg: TestConfig) -> TestConfig:
        """Apply user's advanced vLLM overrides to a TestConfig."""
        # When custom settings are disabled, strip auto-computed values
        # so vLLM uses its own defaults (upstream llm-d behavior).
        # Exception: max_model_len is ALWAYS set — without it vLLM uses
        # max_position_embeddings (e.g., 40960) which wastes 95%+ of KV
        # cache memory on empty slots, making it impossible to serve the
        # user's workload at their requested concurrency.
        if not getattr(self.config, 'advanced_vllm_custom_enabled', True):
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
            'model_loader_extra_config': 'model_loader_extra_config',
            'cpu_offload_gb': 'cpu_offload_gb',
            'weight_cpu_offload_gb': 'weight_cpu_offload_gb',
            'http_timeout_keep_alive': 'http_timeout_keep_alive',
            'prefix_cache_retention': 'prefix_cache_retention',
            'ssm_conv_state_layout': 'ssm_conv_state_layout',
            'num_speculative_tokens': 'speculative_num_tokens',
        }
        for key, attr in val_fields.items():
            setting = adv.get(key)
            if not setting:
                continue
            if setting.get('mode') == 'off':
                setattr(cfg, attr, None)
            elif setting.get('mode') == 'custom' and setting.get('value') is not None:
                val = setting['value']
                if attr in ('max_model_len', 'max_num_seqs', 'max_num_batched_tokens', 'pipeline_parallel_size', 'block_size',
                            'cpu_offload_gb', 'weight_cpu_offload_gb', 'http_timeout_keep_alive', 'prefix_cache_retention',
                            'speculative_num_tokens'):
                    val = int(val)
                elif attr == 'gpu_memory_utilization':
                    val = float(val)
                setattr(cfg, attr, val)

        # Disk KV cache offloading — only when local_disk_path is set (hostPath NVMe)
        disk_offload = adv.get('disk_offload_kv') or adv.get('disk-offload-kv')
        local_disk = getattr(self.config, 'local_disk_path', None)
        if disk_offload and disk_offload.get('mode') == 'custom' and local_disk:
            val = disk_offload.get('value', {})
            cfg.disk_offload_kv_path = f"{local_disk}/kv-cache"
            if isinstance(val, dict):
                cfg.disk_offload_kv_read_threads = int(val.get('read_threads', 32))
                cfg.disk_offload_kv_write_threads = int(val.get('write_threads', 16))

        toggle_fields = {
            'enable_prefix_caching': 'enable_prefix_caching',
            'enable_chunked_prefill': 'enable_chunked_prefill',
            'disable_custom_all_reduce': 'disable_custom_all_reduce',
            'enable_auto_tool_choice': 'enable_auto_tool_choice',
            'enable_expert_parallel': 'enable_expert_parallel',
            'enable_dbo': 'enable_dbo',
            'enable_eplb': 'enable_eplb',
            'trust_remote_code': 'trust_remote_code',
            'disable_log_requests': 'disable_log_requests',
            'vllm_debug_logs': 'vllm_debug_logs',
            'nccl_debug_logs': 'nccl_debug_logs',
            'enable_bidirectional_kv': 'enable_bidirectional_kv',
        }
        for key, attr in toggle_fields.items():
            setting = adv.get(key)
            if setting and setting.get('mode') in ('on', 'off'):
                setattr(cfg, attr, setting['mode'] == 'on')

        # EP requires TP > 1 and a MoE model
        if cfg.tensor_parallelism <= 1 or not self._is_moe:
            cfg.enable_expert_parallel = False

        return cfg

    @staticmethod
    def _auto_tune_model_loader(cfg: TestConfig) -> TestConfig:
        """Set model loader threads to match pod CPU allocation when not explicitly configured."""
        if not cfg.model_loader_extra_config and cfg.cpu_limit:
            import json as _json
            num_threads = int(cfg.cpu_limit)
            cfg.model_loader_extra_config = _json.dumps({
                "enable_multithread_load": True,
                "num_threads": num_threads
            })
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
            '--speculative-config.num_speculative_tokens': ('speculative_num_tokens', int),
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

        # Compute gpu_memory_utilization separately for prefill and decode.
        # Prefill pods: maximize compute — use full profiled allocation.
        # Decode pods: need headroom for NIXL KV transfer receive buffers
        # (~5% extra reserve on top of normal overhead).
        prefill_gmu = self._compute_gpu_mem_util(split.prefill_tp)
        decode_gmu_raw = self._compute_gpu_mem_util(split.decode_tp, log=False)
        nixl_reserve = 0.05  # 5% extra for KV transfer buffers
        decode_gmu = round(min(decode_gmu_raw, decode_gmu_raw - nixl_reserve), 2)
        decode_gmu = max(decode_gmu, 0.80)  # floor at 0.80

        self.log(f"   Prefill gpu_memory_utilization={prefill_gmu:.2f}")
        self.log(f"   Decode  gpu_memory_utilization={decode_gmu:.2f} "
                 f"(base={decode_gmu_raw:.2f} - {nixl_reserve:.0%} NIXL reserve)")

        prefill_max_num_seqs = self._compute_max_num_seqs(
            split.prefill_tp, role='prefill',
            gpu_mem_util_override=prefill_gmu, num_pods=split.prefill_pods)
        decode_max_num_seqs = self._compute_max_num_seqs(
            split.decode_tp, role='decode',
            gpu_mem_util_override=decode_gmu, num_pods=split.decode_pods)
        max_batched = self._compute_max_num_batched_tokens(split.prefill_tp, role='prefill')

        # vLLM requires chunked prefill when max_model_len > max_num_batched_tokens
        enable_chunked = max_batched is not None and self.config.max_model_len > max_batched

        total_pods = split.prefill_pods + split.decode_pods
        min_tp = min(split.prefill_tp, split.decode_tp)
        mem, cpu = self._get_pod_resources(tp=min_tp, total_pods=total_pods)

        # Multi-node: if TP > gpus_per_node, use LWS multi-pod groups
        gpus_per_node = self.cluster_resources.max_gpus_per_node if self.cluster_resources else 8
        prefill_lws = max(1, split.prefill_tp // gpus_per_node) if split.prefill_tp > gpus_per_node else 1
        decode_lws = max(1, split.decode_tp // gpus_per_node) if split.decode_tp > gpus_per_node else 1
        pd_lws_size = max(prefill_lws, decode_lws)
        pd_gpus_per_pod = min(max(split.prefill_tp, split.decode_tp), gpus_per_node) if pd_lws_size > 1 else None

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
            gpu_memory_utilization=prefill_gmu,
            prefill_gpu_memory_utilization=prefill_gmu,
            decode_gpu_memory_utilization=decode_gmu,
            gpu_vram_gb=self._gpu_vram_gb,
            prefill_max_num_seqs=prefill_max_num_seqs,
            decode_max_num_seqs=decode_max_num_seqs,
            max_num_batched_tokens=max_batched,
            enable_chunked_prefill=enable_chunked,
            kv_cache_memory_bytes=self._get_profiled_kv_cache_bytes(split.decode_tp),
            isl_stdev=self.config.isl_stdev,
            osl_stdev=self.config.osl_stdev,
            isl_min=getattr(self.config, 'isl_min', None),
            isl_max=getattr(self.config, 'isl_max', None),
            osl_min=getattr(self.config, 'osl_min', None),
            osl_max=getattr(self.config, 'osl_max', None),
            turns=self.config.turns,
            first_prompt_tokens=getattr(self.config, 'first_prompt_tokens', None),
            first_prompt_tokens_stdev=getattr(self.config, 'first_prompt_tokens_stdev', None),
            first_prompt_tokens_min=getattr(self.config, 'first_prompt_tokens_min', None),
            first_prompt_tokens_max=getattr(self.config, 'first_prompt_tokens_max', None),
            first_output_tokens=getattr(self.config, 'first_output_tokens', None),
            first_output_tokens_stdev=getattr(self.config, 'first_output_tokens_stdev', None),
            prefix_tokens=getattr(self.config, 'prefix_tokens', None),
            prefix_count=getattr(self.config, 'prefix_count', None),
            turn_delay=getattr(self.config, 'turn_delay', None),
            turn_delay_stdev=getattr(self.config, 'turn_delay_stdev', None),
            turn_delay_min=getattr(self.config, 'turn_delay_min', None),
            turn_delay_max=getattr(self.config, 'turn_delay_max', None),
            image=self.config.image,
            scheduler_image=self.config.scheduler_image,
            pvc_name=self.config.pvc_name,
            per_node_storage=getattr(self.config, "per_node_storage", False),
            node_nfs_pvcs=getattr(self.config, "node_nfs_pvcs", None) or [],
            storage_class=getattr(self.config, "storage_class", None),
            pvc_size=getattr(self.config, "pvc_size", None),
            local_disk_path=getattr(self.config, "local_disk_path", None),
            nccl_ib_hca=self.config.nccl_ib_hca,
            optimization_goal='ttft',
            test_duration=self.config.test_duration,
            network_type=self.config.network_type,
            rdma_device_resources=self.config.rdma_device_resources or [],
            rdma_nics_per_node=self.config.rdma_nics_per_node or 0,
            rdma_network_annotation=self.config.rdma_network_annotation,
            selected_dra_classes=self.config.selected_dra_classes or [],
            dra_gpu_resource_key=getattr(self.config, 'dra_gpu_resource_key', None),
            gateway_class=self.config.gateway_class,
            exclusive_pf=getattr(self.config, 'exclusive_pf', False),

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
            block_size=self._resolve_block_size(),
            extra_env_vars=self.config.extra_env_vars,
            enable_expert_parallel=False,
            enable_dbo=False,
            dbo_prefill_token_threshold=getattr(self, '_dbo_threshold', 32),
            dbo_decode_token_threshold=getattr(self, '_dbo_threshold', 32),
            enable_eplb=False,
            moe_backend=None,
            all2all_backend=None,
            use_deep_gemm=getattr(self, '_use_deep_gemm', None),
            has_hybrid_attention=getattr(self, '_has_hybrid_attention', False),
            lws_size=pd_lws_size if pd_lws_size > 1 else None,
            gpus_per_pod=pd_gpus_per_pod,
            nnodes=pd_lws_size if pd_lws_size > 1 else None,
        )
        cfg = self._apply_advanced_vllm(cfg)
        cfg = self._auto_tune_model_loader(cfg)
        return cfg

    def _create_ep_config(self, split: 'FeasibleSplit') -> TestConfig:
        """Create EP (Expert Parallelism) architecture test config.

        EP uses the PD prefill/decode split with EP-specific flags enabled.
        Same template structure as PD but with --enable-expert-parallel,
        --enable-eplb, --moe-backend, --all2all-backend, and NVSHMEM env vars.
        """
        concurrency = self.effective_concurrency

        # Compute moe_dp_chunk_size
        decode_max_num_seqs_prelim = self._compute_max_num_seqs(
            split.decode_tp, role='decode', num_pods=split.decode_pods)
        chunk_size = self._compute_moe_dp_chunk_size(split.decode_tp, decode_max_num_seqs_prelim) if self._is_moe else 384

        # Compute NVSHMEM symmetric heap from model architecture.
        max_batched = self._compute_max_num_batched_tokens(split.prefill_tp, role='prefill') or self.config.max_model_len
        nvshmem_size, ep_mem_gb = self._compute_ep_memory_gb(max_batched)

        # EPLB redundant experts: must equal ep_ranks for divisibility
        ep_ranks = split.decode_tp * split.decode_pods
        num_redundant = ep_ranks
        eplb_gb = 0.5
        if self._model_config:
            num_layers = self._model_config.get('num_hidden_layers', 32)
            intermediate = self._model_config.get('moe_intermediate_size',
                           self._model_config.get('intermediate_size', 14336))
            hidden = self._model_config.get('hidden_size', 4096)
            dtype_bytes = 1 if getattr(self, '_model_dtype', 'fp16') == 'fp8' else 2
            bytes_per_expert = 3 * hidden * intermediate * dtype_bytes
            eplb_gb = max(num_layers * bytes_per_expert * num_redundant / max(ep_ranks, 1) / (1024**3), 0.5)

        # EP gpu_memory_utilization: start from profiled base, subtract EP overhead,
        # then ADD BACK weight savings from expert distribution.
        # Without EP: all experts on every GPU (weight = total/TP).
        # With EP: experts distributed across ep_ranks (weight = non_expert/TP + expert/EP).
        # Savings = expert_weight × (1/TP - 1/EP) per GPU — positive when EP > TP.
        expert_weight_gb = 0.0
        if self._model_config and self._num_experts > 1:
            num_layers_cfg = self._model_config.get('num_hidden_layers', 32)
            interleave = self._model_config.get('interleave_moe_layer_step', 1)
            moe_layers = num_layers_cfg // interleave if interleave > 1 else num_layers_cfg
            expert_weight_gb = moe_layers * self._num_experts * bytes_per_expert / (1024**3)

        decode_ep_savings = expert_weight_gb * (1.0 / split.decode_tp - 1.0 / ep_ranks)
        decode_ep_savings = max(decode_ep_savings, 0)
        decode_ep_savings_pct = round(decode_ep_savings / self._gpu_vram_gb, 2)

        # Decode: full RDMA overhead (low-latency mode) + NIXL + EPLB
        decode_ep_overhead_pct = round((ep_mem_gb + eplb_gb) / self._gpu_vram_gb, 2)
        nixl_reserve = 0.05

        # Prefill: high-throughput mode uses small NVL buffers (~1GB),
        # not the large RDMA buffers. Only EPLB + NVL overhead.
        prefill_ep_overhead_gb = 1.0 + eplb_gb
        prefill_ep_overhead_pct = round(prefill_ep_overhead_gb / self._gpu_vram_gb, 2)

        prefill_gmu_raw = self._compute_gpu_mem_util(split.prefill_tp)
        decode_gmu_raw = self._compute_gpu_mem_util(split.decode_tp, log=False)

        prefill_gmu = round(max(prefill_gmu_raw - prefill_ep_overhead_pct, 0.70), 2)
        prefill_gmu = min(prefill_gmu, 0.95)
        decode_gmu = round(max(decode_gmu_raw - decode_ep_overhead_pct - nixl_reserve + decode_ep_savings_pct, 0.70), 2)
        decode_gmu = min(decode_gmu, 0.95)

        self.log(f"   EPLB: {num_redundant} redundant experts "
                 f"(expert={bytes_per_expert / 1024**2:.0f}MB, ep_ranks={ep_ranks})")
        self.log(f"   Decode EP overhead: {ep_mem_gb + eplb_gb:.1f}GB = {decode_ep_overhead_pct:.0%} "
                 f"(RDMA={ep_mem_gb:.1f}GB + EPLB={eplb_gb:.1f}GB)")
        self.log(f"   Prefill EP overhead: {prefill_ep_overhead_gb:.1f}GB = {prefill_ep_overhead_pct:.0%} "
                 f"(NVL=1GB + EPLB={eplb_gb:.1f}GB)")
        self.log(f"   EP weight savings (decode): {decode_ep_savings:.1f}GB = +{decode_ep_savings_pct:.0%} "
                 f"(experts={expert_weight_gb:.0f}GB, 1/{split.decode_tp} → 1/{ep_ranks})")
        self.log(f"   Prefill gmu={prefill_gmu:.2f} (base={prefill_gmu_raw:.2f} - {prefill_ep_overhead_pct:.0%} EP)")
        self.log(f"   Decode  gmu={decode_gmu:.2f} (base={decode_gmu_raw:.2f} - {decode_ep_overhead_pct:.0%} EP "
                 f"- {nixl_reserve:.0%} NIXL + {decode_ep_savings_pct:.0%} savings)")

        prefill_max_num_seqs = self._compute_max_num_seqs(
            split.prefill_tp, role='prefill',
            gpu_mem_util_override=prefill_gmu, num_pods=split.prefill_pods)
        decode_max_num_seqs = self._compute_max_num_seqs(
            split.decode_tp, role='decode',
            gpu_mem_util_override=decode_gmu, num_pods=split.decode_pods)
        self._compute_max_num_batched_tokens(split.prefill_tp, role='prefill')

        total_pods = split.prefill_pods + split.decode_pods
        min_tp = min(split.prefill_tp, split.decode_tp)
        mem, cpu = self._get_pod_resources(tp=min_tp, total_pods=total_pods)

        dbo_threshold = getattr(self, '_dbo_threshold', 32)

        # EP needs extra max_model_len headroom for routing metadata overhead
        ep_max_model_len = min(
            int(self.config.max_model_len * 1.10),
            self._model_config.get('max_position_embeddings', 1048576) if self._model_config else 1048576
        )

        # Multi-node: if TP > gpus_per_node, use LWS multi-pod groups
        gpus_per_node = self.cluster_resources.max_gpus_per_node if self.cluster_resources else 8
        prefill_lws_size = max(1, split.prefill_tp // gpus_per_node) if split.prefill_tp > gpus_per_node else 1
        decode_lws_size = max(1, split.decode_tp // gpus_per_node) if split.decode_tp > gpus_per_node else 1
        lws_size = max(prefill_lws_size, decode_lws_size)
        gpus_per_pod = min(split.prefill_tp, gpus_per_node) if lws_size > 1 else None

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

            max_model_len=ep_max_model_len,
            gpu_memory_utilization=prefill_gmu,
            prefill_gpu_memory_utilization=prefill_gmu,
            decode_gpu_memory_utilization=decode_gmu,
            gpu_vram_gb=self._gpu_vram_gb,
            prefill_max_num_seqs=prefill_max_num_seqs,
            decode_max_num_seqs=decode_max_num_seqs,
            max_num_batched_tokens=max(max_batched or 0, ep_max_model_len),
            kv_cache_memory_bytes=self._get_profiled_kv_cache_bytes(split.decode_tp),
            isl_stdev=self.config.isl_stdev,
            osl_stdev=self.config.osl_stdev,
            isl_min=getattr(self.config, 'isl_min', None),
            isl_max=getattr(self.config, 'isl_max', None),
            osl_min=getattr(self.config, 'osl_min', None),
            osl_max=getattr(self.config, 'osl_max', None),
            turns=self.config.turns,
            first_prompt_tokens=getattr(self.config, 'first_prompt_tokens', None),
            first_prompt_tokens_stdev=getattr(self.config, 'first_prompt_tokens_stdev', None),
            first_prompt_tokens_min=getattr(self.config, 'first_prompt_tokens_min', None),
            first_prompt_tokens_max=getattr(self.config, 'first_prompt_tokens_max', None),
            first_output_tokens=getattr(self.config, 'first_output_tokens', None),
            first_output_tokens_stdev=getattr(self.config, 'first_output_tokens_stdev', None),
            prefix_tokens=getattr(self.config, 'prefix_tokens', None),
            prefix_count=getattr(self.config, 'prefix_count', None),
            turn_delay=getattr(self.config, 'turn_delay', None),
            turn_delay_stdev=getattr(self.config, 'turn_delay_stdev', None),
            turn_delay_min=getattr(self.config, 'turn_delay_min', None),
            turn_delay_max=getattr(self.config, 'turn_delay_max', None),
            image=self.config.image,
            scheduler_image=self.config.scheduler_image,
            pvc_name=self.config.pvc_name,
            per_node_storage=getattr(self.config, "per_node_storage", False),
            node_nfs_pvcs=getattr(self.config, "node_nfs_pvcs", None) or [],
            storage_class=getattr(self.config, "storage_class", None),
            pvc_size=getattr(self.config, "pvc_size", None),
            local_disk_path=getattr(self.config, "local_disk_path", None),
            nccl_ib_hca=self.config.nccl_ib_hca,
            optimization_goal='throughput',
            test_duration=self.config.test_duration,
            stop_mode=self.config.stop_mode,
            max_requests=self.config.max_requests,
            network_type=self.config.network_type,
            rdma_device_resources=self.config.rdma_device_resources or [],
            rdma_nics_per_node=self.config.rdma_nics_per_node or 0,
            rdma_network_annotation=self.config.rdma_network_annotation,
            selected_dra_classes=self.config.selected_dra_classes or [],
            dra_gpu_resource_key=getattr(self.config, 'dra_gpu_resource_key', None),
            gateway_class=self.config.gateway_class,
            exclusive_pf=getattr(self.config, 'exclusive_pf', False),

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
            block_size=self._resolve_block_size(),
            extra_env_vars=self.config.extra_env_vars,
            enable_expert_parallel=(max(split.prefill_tp, split.decode_tp) > 1),
            enable_dbo=(max(split.prefill_tp, split.decode_tp) > 1),
            dbo_prefill_token_threshold=dbo_threshold,
            dbo_decode_token_threshold=dbo_threshold,
            enable_eplb=(max(split.prefill_tp, split.decode_tp) > 1),
            num_redundant_experts=num_redundant,
            moe_backend=None,
            all2all_backend='deepep_high_throughput' if max(split.prefill_tp, split.decode_tp) > 1 else None,
            moe_dp_chunk_size=chunk_size if self._is_moe else None,
            nvshmem_symmetric_size=nvshmem_size,
            use_deep_gemm=getattr(self, '_use_deep_gemm', None),
            has_hybrid_attention=getattr(self, '_has_hybrid_attention', False),
            lws_size=lws_size if lws_size > 1 else None,
            gpus_per_pod=gpus_per_pod,
            nnodes=lws_size if lws_size > 1 else None,
        )
        cfg = self._apply_advanced_vllm(cfg)
        cfg = self._auto_tune_model_loader(cfg)
        return cfg

