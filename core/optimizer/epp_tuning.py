"""Step 9: EPP tuning — smart weight derivation + optional sweep."""

import math
import os
import time
from typing import Dict, Optional, Tuple


from core.config_generator import TestConfig
from core.template_manager import TemplateManager

class EPPTuningMixin:
    """Mixin providing EPP tuning methods for RecipeOptimizer."""

    def _extract_cache_hit_rate(self, arch: str) -> Optional[float]:
        """Extract actual prefix cache hit rate from the winning Step 6/7 config.

        Returns measured hit rate (0-100) or None if not available.
        Uses the BEST config per architecture (same config EPP tuning will deploy):
          aggregated → best from Step 6 (self.aggregated_result)
          pd → best TTFT from Step 7 Pareto front

        This is important because different configs have different cache hit rates
        (more pods = cache spread across more pods = lower per-pod hit rate).
        """
        import json as _json

        result = None
        if arch == 'aggregated' and self.aggregated_result and self.aggregated_result.metrics_json_content:
            result = self.aggregated_result
        elif arch == 'pd' and self.pareto_results:
            best = min(self.pareto_results, key=lambda x: x[1].ttft_p90 if x[1].ttft_p90 else 1e9)
            if best[1].metrics_json_content:
                result = best[1]

        if not result:
            return None

        try:
            metrics = _json.loads(result.metrics_json_content)
            hits = None
            queries = None
            for key, val in metrics.items():
                if 'prefix_cache_hits' in key:
                    hits = val.get('avg') if isinstance(val, dict) else val
                elif 'prefix_cache_queries' in key:
                    queries = val.get('avg') if isinstance(val, dict) else val
            if hits is not None and queries and queries > 0:
                rate = (hits / queries) * 100
                config_desc = f"TP={self.aggregated_tp}" if arch == 'aggregated' else "best PD split"
                self.log(f"    Measured {arch} cache hit rate: {rate:.1f}% (from {config_desc})", 'info')
                return rate
        except Exception:
            pass
        return None

    def _extract_kv_and_queue_metrics(self, arch: str) -> Tuple[float, float]:
        """Extract KV utilization and queue depth variance from the winning Step 6/7 config.

        Returns (kv_variance, queue_variance) or (0, 0) if unavailable.
        Uses the same winning config as _extract_cache_hit_rate.
        """
        import json as _json

        result = None
        if arch == 'aggregated' and self.aggregated_result and self.aggregated_result.metrics_json_content:
            result = self.aggregated_result
        elif arch == 'pd' and self.pareto_results:
            best = min(self.pareto_results, key=lambda x: x[1].ttft_p90 if x[1].ttft_p90 else 1e9)
            if best[1].metrics_json_content:
                result = best[1]

        if not result:
            return 0, 0

        try:
            metrics = _json.loads(result.metrics_json_content)
            kv_values = []
            queue_values = []
            for key, val in metrics.items():
                if 'kv_cache_usage_perc' in key:
                    if isinstance(val, dict):
                        for v in val.values():
                            if isinstance(v, (int, float)) and v > 0:
                                kv_values.append(v)
                    elif isinstance(val, (int, float)) and val > 0:
                        kv_values.append(val)
                elif 'queue_size' in key and 'per_pod' in key:
                    if isinstance(val, dict):
                        for v in val.values():
                            if isinstance(v, (int, float)):
                                queue_values.append(v)

            kv_var = 0
            if len(kv_values) >= 2:
                kv_mean = sum(kv_values) / len(kv_values)
                kv_var = sum((v - kv_mean) ** 2 for v in kv_values) / len(kv_values)

            q_var = 0
            if len(queue_values) >= 2:
                q_mean = sum(queue_values) / len(queue_values)
                q_var = sum((v - q_mean) ** 2 for v in queue_values) / len(queue_values)

            return kv_var, q_var
        except Exception:
            return 0, 0

    def _compute_smart_epp_weights(self, num_pods: int = 1, arch: str = 'aggregated') -> Optional[Dict]:
        """Derive EPP weights from calibration data + measured Step 6/7 metrics.

        Uses actual cache hit rate from Step 6/7 when available (Prometheus),
        falls back to user-configured prefix_cache_hit_pct.

        Each weight is proportional to the time impact of optimal routing:
          prefix ∝ time saved by cache hit (ISL × actual_hit_pct / prefill_TPSG)
          kv     ∝ cost of KV eviction (ISL / prefill_TPSG × utilization²)
          queue  ∝ wait time per queue imbalance ((ISL+OSL) / total_TPSG / pods)
        """
        prefill_tpsg = self.optimal_prefill_tp.tpsg if self.optimal_prefill_tp else None
        decode_tpsg = self.optimal_decode_tp.tpsg if self.optimal_decode_tp else None

        if not prefill_tpsg or not decode_tpsg:
            self.log("  Smart EPP: no calibration TPSG data, falling back to presets", 'warning')
            return None

        isl = self.config.isl
        osl = self.config.osl
        concurrency = getattr(self, 'effective_concurrency', int(self.config.qps))

        tp = self.optimal_decode_tp.tp if self.optimal_decode_tp else 1
        max_seqs = self._compute_max_num_seqs(tp) or 256

        # Try measured cache hit rate from Step 6/7, fall back to config
        measured_hit_rate = self._extract_cache_hit_rate(arch)
        cache_pct = measured_hit_rate if measured_hit_rate is not None else (self.config.prefix_cache_hit_pct or 0)
        cache_source = 'measured' if measured_hit_rate is not None else 'configured'

        # Try measured KV/queue variance from Step 6/7
        kv_var, q_var = self._extract_kv_and_queue_metrics(arch)

        prefix_time_impact_raw = (isl * cache_pct / 100.0) / prefill_tpsg

        cache_mode = getattr(self.config, 'prefix_cache_mode', 'identical') or 'identical'
        if cache_mode == 'identical':
            diversity = 0.1
        elif cache_mode == 'shared_prefix':
            diversity = 0.3
        elif cache_mode == 'multi_group':
            n_groups = getattr(self.config, 'prefix_cache_groups', 5) or 5
            diversity = min(1.0, max(0.3, n_groups / max(num_pods, 1)))
        else:
            diversity = 1.0

        prefix_time_impact = prefix_time_impact_raw * diversity

        if kv_var > 0:
            kv_eviction_cost = (isl / prefill_tpsg) * min(kv_var, 1.0)
        else:
            kv_utilization = min(concurrency / max(max_seqs, 1), 1.0)
            kv_eviction_cost = (isl / prefill_tpsg) * (kv_utilization ** 2)

        total_tpsg = prefill_tpsg + decode_tpsg
        if q_var > 0:
            queue_wait_cost = (isl + osl) / total_tpsg * min(q_var + 0.1, 1.0)
        else:
            queue_wait_cost = (isl + osl) / total_tpsg / max(num_pods, 1)

        total = prefix_time_impact + kv_eviction_cost + queue_wait_cost
        if total <= 0:
            return None

        w_prefix = max(1, min(5, round(prefix_time_impact / total * 7)))
        w_kv = max(1, min(5, round(kv_eviction_cost / total * 7)))
        w_queue = max(1, min(5, round(queue_wait_cost / total * 7)))

        self.log(f"  Smart EPP Weight Derivation ({arch}):", 'info')
        self.log(f"    Prefix impact: ISL={isl} × {cache_pct:.0f}% ({cache_source}) / TPSG={prefill_tpsg:.0f} = {prefix_time_impact_raw:.4f} × diversity={diversity} ({cache_mode}) = {prefix_time_impact:.4f} GPU-sec", 'info')
        if kv_var > 0:
            self.log(f"    KV pressure:   ISL={isl} / {prefill_tpsg:.0f} × kv_variance={kv_var:.4f} = {kv_eviction_cost:.4f} GPU-sec (measured)", 'info')
        else:
            self.log(f"    KV pressure:   ISL={isl} / {prefill_tpsg:.0f} × ({concurrency}/{max_seqs})² = {kv_eviction_cost:.4f} GPU-sec (estimated)", 'info')
        if q_var > 0:
            self.log(f"    Queue cost:    ({isl}+{osl}) / {total_tpsg:.0f} × queue_variance={q_var:.4f} = {queue_wait_cost:.4f} GPU-sec (measured)", 'info')
        else:
            self.log(f"    Queue cost:    ({isl}+{osl}) / {total_tpsg:.0f} / {num_pods} pods = {queue_wait_cost:.4f} GPU-sec (estimated)", 'info')
        self.log(f"    → Weights: prefix={w_prefix}, kv={w_kv}, queue={w_queue}", 'success')

        return {
            'prefix_cache_weight': float(w_prefix),
            'kv_cache_weight': float(w_kv),
            'queue_weight': float(w_queue),
            'slo_enabled': False,
        }

    def _refine_epp_from_metrics(self, test_result, arch: str = 'aggregated') -> Optional[Dict]:
        """Refine EPP weights from the Smart EPP test's own metrics.

        After running with derived weights, reads actual cache hit rate,
        KV utilization variance, and queue depth variance from the test.
        Returns refined weights or None if metrics unavailable.
        """
        if not test_result or not test_result.metrics_json_content:
            return None

        try:
            import json
            metrics = json.loads(test_result.metrics_json_content)
        except Exception:
            return None

        prefill_tpsg = self.optimal_prefill_tp.tpsg if self.optimal_prefill_tp else None
        decode_tpsg = self.optimal_decode_tp.tpsg if self.optimal_decode_tp else None
        if not prefill_tpsg or not decode_tpsg:
            return None

        cache_hits = None
        cache_queries = None
        kv_values = []
        queue_values = []

        for key, val in metrics.items():
            if 'prefix_cache_hits' in key and 'avg' in str(val):
                cache_hits = val.get('avg') if isinstance(val, dict) else val
            elif 'prefix_cache_queries' in key and 'avg' in str(val):
                cache_queries = val.get('avg') if isinstance(val, dict) else val
            elif 'kv_cache_usage_perc' in key:
                if isinstance(val, dict):
                    for v in val.values():
                        if isinstance(v, (int, float)) and v > 0:
                            kv_values.append(v)
                elif isinstance(val, (int, float)) and val > 0:
                    kv_values.append(val)
            elif 'queue_size' in key and 'per_pod' in key:
                if isinstance(val, dict):
                    for v in val.values():
                        if isinstance(v, (int, float)):
                            queue_values.append(v)

        if cache_hits is None and not kv_values and not queue_values:
            self.log("  Smart EPP: no Prometheus metrics available for refinement", 'info')
            return None

        isl = self.config.isl
        osl = self.config.osl

        actual_hit_pct = 0
        if cache_hits is not None and cache_queries and cache_queries > 0:
            actual_hit_pct = (cache_hits / cache_queries) * 100
            self.log(f"    Measured cache hit rate: {actual_hit_pct:.1f}%", 'info')

        kv_variance = 0
        if len(kv_values) >= 2:
            kv_mean = sum(kv_values) / len(kv_values)
            kv_variance = sum((v - kv_mean) ** 2 for v in kv_values) / len(kv_values)
            self.log(f"    KV utilization variance: {kv_variance:.4f} (mean={kv_mean:.2f}%)", 'info')

        queue_variance = 0
        if len(queue_values) >= 2:
            q_mean = sum(queue_values) / len(queue_values)
            queue_variance = sum((v - q_mean) ** 2 for v in queue_values) / len(queue_values)
            self.log(f"    Queue depth variance: {queue_variance:.4f} (mean={q_mean:.1f})", 'info')

        prefix_impact = (isl * actual_hit_pct / 100.0) / prefill_tpsg
        kv_impact = (isl / prefill_tpsg) * min(kv_variance, 1.0)
        queue_impact = (isl + osl) / (prefill_tpsg + decode_tpsg) * min(queue_variance + 0.1, 1.0)

        total = prefix_impact + kv_impact + queue_impact
        if total <= 0:
            return None

        w_prefix = max(1, min(5, round(prefix_impact / total * 7)))
        w_kv = max(1, min(5, round(kv_impact / total * 7)))
        w_queue = max(1, min(5, round(queue_impact / total * 7)))

        self.log(f"    → Refined weights: prefix={w_prefix}, kv={w_kv}, queue={w_queue}", 'success')

        return {
            'prefix_cache_weight': float(w_prefix),
            'kv_cache_weight': float(w_kv),
            'queue_weight': float(w_queue),
            'slo_enabled': False,
        }

    def _benchmark_epp_strategies(self):
        """Step 9: EPP Tuning — smart weight derivation + validation.

        Derives EPP weights mathematically from calibration data (1 test),
        then optionally refines from measured metrics (1 more test on OpenShift).
        Falls back to preset sweep if calibration data is unavailable.
        """
        if not self.config.epp_benchmark:
            return

        self.log("\n" + "=" * 80, 'info')
        self.log("STEP 9: EPP Tuning (Smart Weight Derivation)", 'decision')
        self.log("=" * 80, 'info')

        has_sla = False
        base = self._build_epp_config()

        # Preset fallback combos (used when smart derivation fails)
        isl_osl_ratio = self.config.isl / max(self.config.osl, 1)
        fallback_combos = [
            ('cache-heavy', {'prefix_cache_weight': 5.0, 'kv_cache_weight': 1.0, 'queue_weight': 1.0, 'slo_enabled': has_sla}),
            ('queue-heavy', {'prefix_cache_weight': 1.0, 'kv_cache_weight': 1.0, 'queue_weight': 5.0, 'slo_enabled': has_sla}),
        ]
        if isl_osl_ratio > 10:
            fallback_combos.append(('kv-heavy', {'prefix_cache_weight': 2.0, 'kv_cache_weight': 5.0, 'queue_weight': 1.0, 'slo_enabled': has_sla}))
        else:
            fallback_combos.append(('equal', {'prefix_cache_weight': 2.0, 'kv_cache_weight': 2.0, 'queue_weight': 2.0, 'slo_enabled': has_sla}))

        # Collect best configs per architecture
        configs_to_test = []

        # Best PD config
        if self.pareto_results:
            best_split, best_pd_result = min(self.pareto_results, key=lambda x: x[1].ttft_p99 or x[1].ttft_p90 or 1e9)
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

        if self.epp_benchmark_results:
            self.log(f"  EPP tuning already completed (resumed from DB) — skipping re-run", 'info')
            return

        self.epp_benchmark_results = {}

        from core import PrereqManager
        prereq_mgr = PrereqManager(
            namespace=self.config.namespace,
            kubectl_runner=self.orchestrator.deployment_manager.kubectl,
            scheduler_image=getattr(self.config, 'scheduler_image', None)
        )

        for arch_idx, (arch, base_cfg, concurrency) in enumerate(configs_to_test):
            if self._should_stop():
                break

            self.log(f"\n  --- EPP Tuning: {arch.upper()} (c={concurrency}) ---", 'decision')

            # Compute per-architecture smart weights using measured Step 6/7 data
            arch_pods = 1
            if arch == 'pd' and self.pareto_results:
                best_split = min(self.pareto_results, key=lambda x: x[1].ttft_p90 if x[1].ttft_p90 else 1e9)[0]
                arch_pods = best_split.prefill_pods + best_split.decode_pods
            elif arch == 'aggregated' and self.aggregated_tp:
                arch_pods = self.config.total_gpus // self.aggregated_tp

            smart_weights = self._compute_smart_epp_weights(num_pods=arch_pods, arch=arch)

            # Baseline TTFT from Step 6/7 (ran with default EPP weights)
            baseline_ttft = None
            if arch == 'aggregated' and self.aggregated_result:
                baseline_ttft = self.aggregated_result.ttft_p90
            elif arch == 'pd' and self.pareto_results:
                baseline_ttft = min(self.pareto_results, key=lambda x: x[1].ttft_p90 if x[1].ttft_p90 else 1e9)[1].ttft_p90

            if smart_weights:
                weight_combos = [('smart-derived', smart_weights)]
            else:
                weight_combos = list(fallback_combos)

            self.log(f"  Weight combos: {', '.join(n for n, _ in weight_combos)}", 'info')
            arch_results = []

            for combo_idx, (name, weights) in enumerate(weight_combos):
                if self._should_stop():
                    break

                # Clean up any leftover step9 EPP LWS from previous combo
                try:
                    self.orchestrator.deployment_manager.kubectl.run(
                        ['delete', 'lws', '-l', 'component=inftune-test',
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
                    prefill_decode_ratio=base_cfg.prefill_decode_ratio,
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

            # Smart EPP: try to refine from measured metrics after first test
            if smart_weights and arch_results and not self._should_stop():
                first_result = arch_results[0][2]
                self.log(f"\n  Attempting metrics-based refinement...", 'info')
                refined = self._refine_epp_from_metrics(first_result, arch=arch)
                if refined and (refined['prefix_cache_weight'] != smart_weights['prefix_cache_weight'] or
                                refined['kv_cache_weight'] != smart_weights['kv_cache_weight'] or
                                refined['queue_weight'] != smart_weights['queue_weight']):
                    self.log(f"  Weights changed — running validation test", 'info')
                    # Run the refined combo through the same test loop
                    refined_combos = [('smart-refined', refined)]
                    for rname, rweights in refined_combos:
                        if self._should_stop():
                            break
                        try:
                            self.orchestrator.deployment_manager.kubectl.run(
                                ['delete', 'lws', '-l', 'component=inftune-test',
                                 '-n', self.config.namespace, '--ignore-not-found=true'], check=False)
                            time.sleep(5)
                        except Exception:
                            pass
                        rtest_id = f"step11-epp-{arch}-{rname}"
                        self.log(f"  Testing: {rname} (cache={rweights['prefix_cache_weight']}, kv={rweights['kv_cache_weight']}, queue={rweights['queue_weight']})", 'info')
                        repp_cfg = {
                            'preset': 'custom',
                            'maxPrefixBlocksToMatch': base.get('maxPrefixBlocksToMatch', 256),
                            'lruCapacityPerServer': base.get('lruCapacityPerServer', 31250),
                            'nonCachedTokens': base.get('nonCachedTokens', 16),
                            'plugins': {
                                'prefix_cache': {'enabled': True, 'weight': rweights['prefix_cache_weight']},
                                'kv_cache': {'enabled': True, 'weight': rweights['kv_cache_weight']},
                                'queue': {'enabled': True, 'weight': rweights['queue_weight']},
                                'slo': {'enabled': False},
                            },
                        }
                        rsuccess = prereq_mgr.update_epp_config(architecture=arch, epp_config=repp_cfg,
                                                                 log_callback=lambda msg: self.log(msg, 'info'))
                        if rsuccess:
                            rtest_config = TestConfig(
                                test_id=rtest_id, architecture=base_cfg.architecture,
                                model_name=base_cfg.model_name, namespace=base_cfg.namespace,
                                isl=base_cfg.isl, osl=base_cfg.osl, num_users=concurrency,
                                tensor_parallelism=base_cfg.tensor_parallelism, replicas=base_cfg.replicas,
                                prefill_replicas=base_cfg.prefill_replicas, decode_replicas=base_cfg.decode_replicas,
                                prefill_tp=base_cfg.prefill_tp, decode_tp=base_cfg.decode_tp,
                                max_model_len=base_cfg.max_model_len, gpu_memory_utilization=base_cfg.gpu_memory_utilization,
                                image=base_cfg.image, pvc_name=base_cfg.pvc_name,
                                request_type=base_cfg.request_type, request_rate=concurrency,
                                test_duration=base_cfg.test_duration, workload_mode=base_cfg.workload_mode,
                                dataset_source=base_cfg.dataset_source, block_size=base_cfg.block_size,
                                network_type=base_cfg.network_type, nccl_ib_hca=base_cfg.nccl_ib_hca,
                                rdma_device_resources=base_cfg.rdma_device_resources,
                                rdma_nics_per_node=base_cfg.rdma_nics_per_node,
                                memory_request=base_cfg.memory_request, memory_limit=base_cfg.memory_limit,
                                cpu_request=base_cfg.cpu_request, cpu_limit=base_cfg.cpu_limit,
                                max_num_seqs=base_cfg.max_num_seqs,
                                prefill_max_num_seqs=base_cfg.prefill_max_num_seqs,
                                decode_max_num_seqs=base_cfg.decode_max_num_seqs,
                                max_num_batched_tokens=base_cfg.max_num_batched_tokens,
                                prefill_gpu_memory_utilization=base_cfg.prefill_gpu_memory_utilization,
                                decode_gpu_memory_utilization=base_cfg.decode_gpu_memory_utilization,
                                selected_nodes=base_cfg.selected_nodes,
                                prefill_decode_ratio=base_cfg.prefill_decode_ratio, epp_config=repp_cfg,
                            )
                            rresult = self.orchestrator.run_test(rtest_config, cleanup=True,
                                                                 log_callback=lambda msg: self.log(msg, 'info'),
                                                                 stop_check=self._should_stop)
                            if rresult and rresult.guidellm_success:
                                self.log(f"  ✅ {rname}: TTFT p90={rresult.ttft_p90:.1f}ms, Throughput p90={rresult.throughput_p90:.2f} req/s", 'success')
                                arch_results.append((rname, rweights, rresult))
                                self.all_test_results.append((rtest_config, rresult))
                                self._save_epp_test_to_database(rtest_config, rresult)
                            else:
                                self.log(f"  ❌ {rname}: benchmark failed", 'error')
                else:
                    self.log(f"  Metrics confirm derived weights — no refinement needed", 'success')

            # A/B guardrail: if smart-derived is worse than Step 6/7 baseline, test balanced
            if (baseline_ttft and arch_results and not self._should_stop()):
                best_smart_ttft = min(r[2].ttft_p90 or float('inf') for r in arch_results)
                if best_smart_ttft > baseline_ttft * 1.05:
                    self.log(f"\n  ⚠️  Smart TTFT ({best_smart_ttft:.0f}ms) > baseline ({baseline_ttft:.0f}ms) — testing balanced fallback", 'warning')
                    balanced_w = {'prefix_cache_weight': 2.0, 'kv_cache_weight': 2.0, 'queue_weight': 2.0, 'slo_enabled': False}
                    fb_test_id = f"step11-epp-{arch}-balanced-fallback"
                    fb_epp_cfg = {
                        'preset': 'custom',
                        'maxPrefixBlocksToMatch': base.get('maxPrefixBlocksToMatch', 256),
                        'lruCapacityPerServer': base.get('lruCapacityPerServer', 31250),
                        'nonCachedTokens': base.get('nonCachedTokens', 16),
                        'plugins': {
                            'prefix_cache': {'enabled': True, 'weight': 2.0},
                            'kv_cache': {'enabled': True, 'weight': 2.0},
                            'queue': {'enabled': True, 'weight': 2.0},
                            'slo': {'enabled': False},
                        },
                    }
                    try:
                        self.orchestrator.deployment_manager.kubectl.run(
                            ['delete', 'lws', '-l', 'component=inftune-test',
                             '-n', self.config.namespace, '--ignore-not-found=true'], check=False)
                        time.sleep(5)
                    except Exception:
                        pass
                    prereq_mgr.update_epp_config(architecture=arch, epp_config=fb_epp_cfg,
                                                  log_callback=lambda msg: self.log(msg, 'info'))
                    fb_config = TestConfig(
                        test_id=fb_test_id, architecture=base_cfg.architecture,
                        model_name=base_cfg.model_name, namespace=base_cfg.namespace,
                        isl=base_cfg.isl, osl=base_cfg.osl, num_users=concurrency,
                        tensor_parallelism=base_cfg.tensor_parallelism, replicas=base_cfg.replicas,
                        prefill_replicas=base_cfg.prefill_replicas, decode_replicas=base_cfg.decode_replicas,
                        prefill_tp=base_cfg.prefill_tp, decode_tp=base_cfg.decode_tp,
                        max_model_len=base_cfg.max_model_len, gpu_memory_utilization=base_cfg.gpu_memory_utilization,
                        image=base_cfg.image, pvc_name=base_cfg.pvc_name,
                        request_type=base_cfg.request_type, request_rate=concurrency,
                        test_duration=base_cfg.test_duration, workload_mode=base_cfg.workload_mode,
                        dataset_source=base_cfg.dataset_source, block_size=base_cfg.block_size,
                        network_type=base_cfg.network_type, nccl_ib_hca=base_cfg.nccl_ib_hca,
                        rdma_device_resources=base_cfg.rdma_device_resources,
                        rdma_nics_per_node=base_cfg.rdma_nics_per_node,
                        memory_request=base_cfg.memory_request, memory_limit=base_cfg.memory_limit,
                        cpu_request=base_cfg.cpu_request, cpu_limit=base_cfg.cpu_limit,
                        max_num_seqs=base_cfg.max_num_seqs,
                        prefill_max_num_seqs=base_cfg.prefill_max_num_seqs,
                        decode_max_num_seqs=base_cfg.decode_max_num_seqs,
                        max_num_batched_tokens=base_cfg.max_num_batched_tokens,
                        prefill_gpu_memory_utilization=base_cfg.prefill_gpu_memory_utilization,
                        decode_gpu_memory_utilization=base_cfg.decode_gpu_memory_utilization,
                        selected_nodes=base_cfg.selected_nodes,
                        prefill_decode_ratio=base_cfg.prefill_decode_ratio, epp_config=fb_epp_cfg,
                    )
                    fb_result = self.orchestrator.run_test(fb_config, cleanup=True,
                                                           log_callback=lambda msg: self.log(msg, 'info'),
                                                           stop_check=self._should_stop)
                    if fb_result and fb_result.guidellm_success:
                        self.log(f"  ✅ balanced-fallback: TTFT p90={fb_result.ttft_p90:.1f}ms, Throughput p90={fb_result.throughput_p90:.2f} req/s", 'success')
                        arch_results.append(('balanced-fallback', balanced_w, fb_result))
                        self.all_test_results.append((fb_config, fb_result))
                        self._save_epp_test_to_database(fb_config, fb_result)
                    else:
                        self.log(f"  ❌ balanced-fallback: benchmark failed", 'error')

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
                kubectl_runner=self.orchestrator.deployment_manager.kubectl,
                scheduler_image=getattr(self.config, 'scheduler_image', None)
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

