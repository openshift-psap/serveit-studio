"""Step 9: EPP tuning — smart weight sweep per architecture."""

import os
import time
from typing import Dict


from core.config_generator import TestConfig
from core.template_manager import TemplateManager

class EPPTuningMixin:
    """Mixin providing EPP tuning methods for RecipeOptimizer."""

    def _benchmark_epp_strategies(self):
        """Step 9: EPP Tuning — smart weight sweep per architecture.

        Tests 3 EPP weight combinations on the best config from each
        architecture (PD and Aggregated), using the optimal concurrency
        from Step 7/8. Swaps only the EPP configmap between tests.
        """
        if not self.config.epp_benchmark:
            return

        self.log("\n" + "=" * 80, 'info')
        self.log("STEP 9: EPP Tuning (Smart Weight Sweep)", 'decision')
        self.log("=" * 80, 'info')

        # Build weight combos based on workload
        has_sla = False
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
            # Use optimal concurrency from Step 10 if available
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

        if self.epp_benchmark_results:
            self.log(f"  EPP tuning already completed (resumed from DB) — skipping re-run", 'info')
            return

        self.epp_benchmark_results = {}

        from core import PrereqManager
        prereq_mgr = PrereqManager(
            namespace=self.config.namespace,
            kubectl_runner=self.orchestrator.deployment_manager.kubectl
        )

        for arch_idx, (arch, base_cfg, concurrency) in enumerate(configs_to_test):
            if self._should_stop():
                break

            self.log(f"\n  --- EPP Tuning: {arch.upper()} (c={concurrency}) ---", 'decision')
            arch_results = []

            for combo_idx, (name, weights) in enumerate(weight_combos):
                if self._should_stop():
                    break

                # Clean up any leftover step9 EPP LWS from previous combo
                try:
                    self.orchestrator.deployment_manager.kubectl.run(
                        ['delete', 'lws', '-l', 'component=inferecipe-test',
                         '-n', self.config.namespace, '--ignore-not-found=true'],
                        check=False
                    )
                    # Wait for pods to fully terminate before deploying new ones
                    import time
                    time.sleep(5)
                except Exception:
                    pass

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
                    rdma_device_resources=base_cfg.rdma_device_resources,
                    rdma_nics_per_node=base_cfg.rdma_nics_per_node,
                    memory_request=base_cfg.memory_request,
                    memory_limit=base_cfg.memory_limit,
                    cpu_request=base_cfg.cpu_request,
                    cpu_limit=base_cfg.cpu_limit,
                    max_num_seqs=base_cfg.max_num_seqs,
                    prefill_max_num_seqs=base_cfg.prefill_max_num_seqs,
                    decode_max_num_seqs=base_cfg.decode_max_num_seqs,
                    max_num_batched_tokens=base_cfg.max_num_batched_tokens,
                    prefill_gpu_memory_utilization=base_cfg.prefill_gpu_memory_utilization,
                    decode_gpu_memory_utilization=base_cfg.decode_gpu_memory_utilization,
                    selected_nodes=base_cfg.selected_nodes,
                    epp_config=epp_cfg,
                )

                result = self.orchestrator.run_test(
                    epp_test_config,
                    cleanup=True,
                    log_callback=lambda msg: self.log(msg, 'info'),
                    stop_check=self._should_stop,
                )

                if result and result.guidellm_success:
                    ttft = result.ttft_p90 or 0
                    tput = result.throughput_p90 or 0
                    self.log(f"  ✅ {name}: TTFT p90={ttft:.1f}ms, Throughput p90={tput:.2f} req/s", 'success')
                    arch_results.append((name, weights, result))
                    self.all_test_results.append((epp_test_config, result))
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

    def _apply_best_epp_config(self):
        """After EPP tuning, deploy the best-performing EPP weights for subsequent steps."""
        if not self.epp_benchmark_results:
            return
        for arch, results in self.epp_benchmark_results.items():
            if not results:
                continue
            best_name, best_weights, _ = min(results, key=lambda x: x[2].ttft_p90 or float('inf'))
            self.log(f"  Applying best EPP config for {arch}: {best_name} "
                     f"(cache={best_weights['prefix_cache_weight']}, kv={best_weights['kv_cache_weight']}, "
                     f"queue={best_weights['queue_weight']})", 'success')
            epp_config = {
                'preset': 'custom',
                'plugins': {
                    'prefix_cache': {'enabled': True, 'weight': best_weights['prefix_cache_weight']},
                    'kv_cache': {'enabled': True, 'weight': best_weights['kv_cache_weight']},
                    'queue': {'enabled': True, 'weight': best_weights['queue_weight']},
                    'slo': {'enabled': False},
                }
            }
            from core import PrereqManager
            mgr = PrereqManager(
                namespace=self.config.namespace,
                kubectl_runner=self.orchestrator.deployment_manager.kubectl
            )
            mgr.update_epp_config(arch, epp_config, log_callback=self.log)

        winner = self.epp_benchmark_results.get('aggregated') or self.epp_benchmark_results.get('pd')
        if winner:
            _, best_w, _ = min(winner, key=lambda x: x[2].ttft_p90 or float('inf'))
            self.config.epp_preset = 'custom'
            if not self.config.epp_config:
                self.config.epp_config = {}
            self.config.epp_config['preset'] = 'custom'
            self.config.epp_config['plugins'] = {
                'prefix_cache': {'enabled': True, 'weight': best_w['prefix_cache_weight']},
                'kv_cache': {'enabled': True, 'weight': best_w['kv_cache_weight']},
                'queue': {'enabled': True, 'weight': best_w['queue_weight']},
                'slo': {'enabled': False},
            }

