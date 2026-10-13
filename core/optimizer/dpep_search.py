"""DPEP (Data Parallel + Expert Parallel) configuration search and testing.

Each GPU runs an independent vLLM instance (TP=1) with:
- Full attention weights replicated locally (~12GB for GLM-5.3)
- Only its assigned subset of MoE experts (~690/N GB)
- --enable-ep-weight-filter so each GPU only loads its experts
- No TP all-reduce — communication is only the expert all-to-all
"""


class DPEPSearchMixin:
    """Mixin providing DPEP configuration search for RecipeOptimizer."""

    def _calculate_dpep_configs(self):
        """Steps 4-5: Enumerate DPEP configurations.

        Explores DP sizes (GPUs per pod) and pod counts within the GPU budget.
        Resource calculation accounts for expert distribution: each GPU holds
        attention (~small) + experts/DP_size, not the full model.
        """
        total_gpus = self.config.total_gpus
        gpus_per_node = (self.cluster_resources.max_gpus_per_node
                         if self.cluster_resources else 8)

        # Estimate per-GPU memory for DPEP
        # Attention weights are replicated; experts are distributed across DP ranks
        model_weight_gb = self._estimate_model_size_gb()
        attention_gb = self._estimate_attention_weight_gb()
        expert_gb = model_weight_gb - attention_gb

        self.log("  DPEP model analysis:", 'info')
        self.log(f"    Total weights: {model_weight_gb:.0f} GB (fp8)", 'info')
        self.log(f"    Attention (replicated per GPU): ~{attention_gb:.0f} GB", 'info')
        self.log(f"    Experts (distributed): ~{expert_gb:.0f} GB", 'info')
        self.log(f"    GPU VRAM: {self._gpu_vram_gb} GB", 'info')

        # Valid DP sizes: single-node DP groups only (multi-node DP groups are
        # not supported by the templates or the production architecture).
        valid_dp_sizes = []
        for dp in sorted({gpus_per_node, 4, 8, 16, 32}):
            if dp > gpus_per_node:
                continue  # multi-node DP group — unsupported
            if dp > total_gpus:
                continue
            # Per-GPU: attention (replicated) + experts/DP + overhead
            per_gpu_gb = attention_gb + expert_gb / dp + 7  # 7GB overhead
            if per_gpu_gb <= self._gpu_vram_gb * 0.92:  # leave headroom
                valid_dp_sizes.append(dp)
                self.log(
                    f"    DP={dp}: {per_gpu_gb:.0f} GB/GPU "
                    f"(attention={attention_gb:.0f} + experts={expert_gb/dp:.0f}) "
                    f"✓ fits", 'success')
            else:
                self.log(
                    f"    DP={dp}: {per_gpu_gb:.0f} GB/GPU — exceeds VRAM", 'warning')

        if not valid_dp_sizes:
            self.log("  ⚠️  No valid DPEP configurations — "
                     "model too large for single-GPU attention", 'warning')
            self._dpep_candidates = []
            return

        # Generate candidates: each DP size with feasible pod counts.
        # Each candidate deploys num_pods prefill pods + num_pods decode pods,
        # each pod holding dp_size GPUs → 2 × dp_size × num_pods GPUs total.
        from core.optimizer.config import FeasibleSplit
        self._dpep_candidates = []
        for dp_size in valid_dp_sizes:
            max_pods = total_gpus // (2 * dp_size)
            for num_pods in range(1, max_pods + 1):
                gpus_used = 2 * dp_size * num_pods
                split = FeasibleSplit(
                    prefill_pods=num_pods,
                    decode_pods=num_pods,
                    prefill_tp=1,
                    decode_tp=1,
                    prefill_gpus=num_pods * dp_size,
                    decode_gpus=num_pods * dp_size,
                    total_gpus=gpus_used,
                    prefill_pct=50,
                )
                # Tag the split so _create_ep_config can delegate DPEP rebuilds
                # (Step 11 calibrated load, concurrency sweep)
                split.dpep_dp_size = dp_size
                self._dpep_candidates.append({
                    'dp_size': dp_size,
                    'num_pods': num_pods,
                    'gpus_used': gpus_used,
                    'split': split,
                })

        self.log(f"\n  DPEP candidates ({len(self._dpep_candidates)}):", 'info')
        for c in self._dpep_candidates:
            label = (f"{c['num_pods']}×DP{c['dp_size']}"
                     if c['num_pods'] > 1 else f"DP{c['dp_size']}")
            self.log(f"    ✓ {label} = {c['gpus_used']} GPUs", 'success')

    def _test_dpep_configs(self):
        """Step 7: Test DPEP configurations at full workload."""
        if not getattr(self, '_dpep_candidates', []):
            self.log("  ⚠️  No DPEP candidates to test", 'warning')
            return

        concurrency = self.config.qps
        test_num = 0
        total = len(self._dpep_candidates)

        for candidate in self._dpep_candidates:
            if self._should_stop():
                return
            test_num += 1
            dp_size = candidate['dp_size']
            num_pods = candidate['num_pods']
            label = (f"{num_pods}×DP{dp_size}"
                     if num_pods > 1 else f"DP{dp_size}")

            self.log(f"\n  Test {test_num}/{total}: {label} "
                     f"({candidate['gpus_used']} GPUs)", 'info')

            test_id = f"step7-dpep-{num_pods}xdp{dp_size}"

            # Skip if already completed (resume support)
            existing = self._check_completed_test(test_id)
            if existing:
                self.log("    ⏩ Resuming from DB (already completed)", 'info')
                result = existing
            else:
                test_config = self._create_dpep_test_config(
                    dp_size=dp_size, num_pods=num_pods, test_id=test_id,
                    concurrency=concurrency,
                )
                if test_config is None:
                    self.log("    ⚠️  Config creation failed, skipping", 'warning')
                    continue

                result = self.orchestrator.run_test(
                    test_config, cleanup=True,
                    log_callback=lambda msg, _tid=test_id: self.log(
                        f"    {msg}" if not msg.startswith(('  ', '\n', '=')) else msg, 'info'),
                    stop_check=self._should_stop,
                )
                self.all_test_results.append((test_config, result))
                self._save_test_to_database(test_config, result)

            if result and result.guidellm_success:
                ttft = result.ttft_p90 or 1e6
                tput = result.throughput_mean or 0
                self.log(f"    ✅ {label}: TTFT p90={ttft:.0f}ms, "
                         f"Throughput={tput:.3f} req/s", 'success')
                self.ep_results.append((candidate['split'], result))
            else:
                self.log(f"    ❌ {label}: test failed", 'warning')

        # Select best DPEP config (by throughput) — downstream Step 11 /
        # sweep / reporting all read best_ep_config + ep_results splits
        if self.ep_results:
            best_split, best_result = max(
                self.ep_results,
                key=lambda x: x[1].throughput_p90 if x[1].throughput_p90 else 0.0
            )
            self.best_ep_config = best_split
            self.best_ep_result = best_result
            best_dp = best_split.dpep_dp_size
            best_ttft = best_result.ttft_p90 or best_result.ttft_p50 or 0
            best_tput = best_result.throughput_mean or best_result.throughput_p90 or 0
            self.log("", 'info')
            self.log("✅ Best DPEP Configuration (by throughput):", 'success')
            self.log(f"  {best_split.prefill_pods}×DP{best_dp} prefill + "
                     f"{best_split.decode_pods}×DP{best_dp} decode "
                     f"({best_split.total_gpus} GPUs)", 'info')
            self.log(f"  TTFT p90: {best_ttft:.1f}ms, Throughput mean: {best_tput:.2f} req/s", 'info')

    def _create_dpep_test_config(self, dp_size, num_pods, test_id, concurrency):
        """Create a TestConfig for a DPEP configuration."""
        from core.config_generator import TestConfig

        gpus_per_node = (self.cluster_resources.max_gpus_per_node
                         if self.cluster_resources else 8)

        # Pod resources: compute DPEP-specific (experts distributed, not TP-split)
        # The shared _get_pod_resources assumes TP-split weights, which breaks for DPEP.
        # With ep-weight-filter, per-GPU weights are ~34GB (not 713/TP). Skip the TP-based check.
        model_weight_gb = self._estimate_model_size_gb()
        attention_gb = self._estimate_attention_weight_gb()
        expert_gb = model_weight_gb - attention_gb
        per_gpu_weights = attention_gb + expert_gb / dp_size
        # Pod memory: per-GPU weights + KV + overhead, capped at VRAM
        pod_mem_gb = min(per_gpu_weights + 30, self._gpu_vram_gb * 0.92)
        mem = pod_mem_gb
        cpu = getattr(self.config, 'cpu_per_pod', None) or 32

        cfg = TestConfig(
            test_id=test_id,
            architecture='ep',
            model_name=self.config.model_name,

            namespace=self.config.namespace,
            isl=self.config.isl,
            osl=self.config.osl,
            num_users=concurrency,
            request_type=self.config.rate_type,
            request_rate=concurrency,

            # DPEP: TP=1, DP=dp_size, EP across all DP ranks
            tensor_parallelism=1,
            replicas=num_pods,
            data_parallel_size=dp_size,
            data_parallel_size_local=min(dp_size, gpus_per_node),
            enable_expert_parallel=True,
            enable_ep_weight_filter=True,

            # Use prefill TP for backward compat (it's TP=1)
            prefill_tp=1,
            decode_tp=1,
            prefill_replicas=num_pods,
            decode_replicas=num_pods,

            # Each pod requests dp_size GPUs (DP workers inside, TP=1)
            gpus_per_pod=dp_size,

            max_model_len=self.config.max_model_len,
            gpu_memory_utilization=0.92,
            gpu_vram_gb=self._gpu_vram_gb,

            isl_stdev=self.config.isl_stdev,
            osl_stdev=self.config.osl_stdev,
            isl_min=getattr(self.config, 'isl_min', None),
            isl_max=getattr(self.config, 'isl_max', None),
            osl_min=getattr(self.config, 'osl_min', None),
            osl_max=getattr(self.config, 'osl_max', None),
            turns=self.config.turns,
            first_prompt_tokens=self._clamp_first_prompt_tokens(
                self.config.isl, self.config.osl),
            prefix_tokens=getattr(self.config, 'prefix_tokens', None),
            prefix_count=getattr(self.config, 'prefix_count', None),

            block_size=self._resolve_block_size(),

            memory_request=mem,
            memory_limit=mem,
            cpu_request=cpu,
            cpu_limit=cpu,
            all2all_backend='deepep_high_throughput'
            if getattr(self, '_network_type', '') == 'shared_device'
            else None,
            moe_backend='deep_gemm' if getattr(self, '_use_deep_gemm', None) else None,
        )

        # Apply advanced vLLM overrides, then correct for DPEP semantics:
        # - No weight offload — pods hold distributed experts (~34GB/GPU)
        # - No CPU KV offload — that's a PD-prefix-cache feature
        # - Production per-role all2all backends unless the user set them:
        #   prefill = deepep_high_throughput, decode = deepep_low_latency
        cfg = self._apply_advanced_vllm(cfg)
        cfg.cpu_offload_gb = 0
        cfg.weight_cpu_offload_gb = 0
        # Production defaults for DPEP — only when the user hasn't explicitly
        # Disabled the setting (mode 'off' means "leave to vLLM default")
        adv = getattr(self.config, 'advanced_vllm', None) or {}

        def _disabled(key):
            s = adv.get(key)
            return bool(s and s.get('mode') == 'off')

        if not cfg.prefill_all2all_backend and not _disabled('all2all_backend') \
                and not _disabled('prefill_all2all_backend'):
            cfg.prefill_all2all_backend = 'deepep_high_throughput'
        if not cfg.decode_all2all_backend and not _disabled('all2all_backend') \
                and not _disabled('decode_all2all_backend'):
            cfg.decode_all2all_backend = 'deepep_low_latency'
        if not cfg.moe_backend and not _disabled('moe_backend'):
            cfg.moe_backend = 'deep_gemm'
        cfg = self._auto_tune_model_loader(cfg)

        return cfg

    def _estimate_attention_weight_gb(self):
        """Estimate attention weight size (replicated per GPU in DPEP).

        For MoE models with MLA, attention is a small fraction of total
        parameters. For GLM-5.3: ~12GB out of 713GB total.
        """
        if not self._model_config:
            return 15.0  # conservative default

        hidden = self._model_config.get('hidden_size', 4096)
        num_layers = self._model_config.get('num_hidden_layers', 32)
        num_heads = self._model_config.get('num_attention_heads', 32)
        head_dim = self._model_config.get('head_dim',
                                          hidden // max(num_heads, 1))

        # Attention projections: Q, K, V, O (MLA uses latent, smaller)
        kv_lora_rank = self._model_config.get('kv_lora_rank', 0)
        if kv_lora_rank:
            # MLA: compressed KV, attention weights are smaller
            # Q: hidden × (num_heads × head_dim)
            # O: (num_heads × head_dim) × hidden
            # KV projections to latent: hidden × (kv_lora_rank + rope_dim)
            q_params = hidden * num_heads * head_dim
            o_params = num_heads * head_dim * hidden
            kv_params = hidden * (kv_lora_rank + 64) * 2  # up + down projections
            per_layer = q_params + o_params + kv_params
        else:
            # Standard attention
            per_layer = hidden * (num_heads * head_dim) * 4

        total_params = per_layer * num_layers
        # fp8 = 1 byte per param
        return total_params / 1e9

    def _check_completed_test(self, test_id):
        """Check if a test already completed (for resume support)."""
        for cfg, result in self.all_test_results:
            if cfg.test_id == test_id and result and result.guidellm_success:
                return result
        return None
