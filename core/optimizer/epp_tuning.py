"""Step 9: EPP tuning — smart weight derivation + optional sweep."""

import math
import os
import time
from typing import Dict, Optional, Tuple


from core.config_generator import TestConfig
from core.template_manager import TemplateManager

class EPPTuningMixin:
    """Mixin providing EPP tuning methods for RecipeOptimizer."""

    def _get_best_result_prom(self, arch: str) -> Optional[Dict]:
        """Get Prometheus metrics dict from the best Step 6/7 result from the database."""
        import json as _json
        if not self.db_manager or not self.run_id:
            return None

        test_id = None
        if arch == 'aggregated' and self.aggregated_result:
            test_id = getattr(self.aggregated_result, 'test_id', None)
        elif arch == 'pd' and self.pareto_results:
            best = min(self.pareto_results, key=lambda x: x[1].ttft_p99 or x[1].ttft_p90 or 1e9)
            test_id = getattr(best[1], 'test_id', None)
        if not test_id:
            return None

        try:
            with self.db_manager.get_connection() as conn:
                row = conn.execute(
                    'SELECT metrics_json FROM test_configurations WHERE config_name = ? AND run_id = ?',
                    (test_id, self.run_id)
                ).fetchone()
                if row and row[0]:
                    full = _json.loads(row[0])
                    prom = full.get('prometheus_metrics', {})
                    if prom and any(v is not None for v in prom.values()):
                        return prom
        except Exception:
            pass
        return None

    def _prom_avg(self, prom: Dict, key: str) -> Optional[float]:
        """Extract avg value from a Prometheus metric."""
        val = prom.get(key)
        if val is None:
            return None
        if isinstance(val, dict):
            return val.get('avg')
        return val if isinstance(val, (int, float)) else None

    def _extract_cache_hit_rate(self, arch: str) -> Optional[float]:
        """Extract actual prefix cache hit rate from Prometheus metrics."""
        prom = self._get_best_result_prom(arch)
        if not prom:
            return None
        hits = self._prom_avg(prom, 'vllm_prefix_cache_hits_rate')
        queries = self._prom_avg(prom, 'vllm_prefix_cache_queries_rate')
        if hits is not None and queries and queries > 0:
            rate = (hits / queries) * 100
            config_desc = f"TP={self.aggregated_tp}" if arch == 'aggregated' else "best PD split"
            self.log(f"    Measured {arch} cache hit rate: {rate:.1f}% (from {config_desc})", 'info')
            return rate
        return None

    def _extract_kv_and_queue_metrics(self, arch: str) -> Tuple[float, float]:
        """Extract KV cache pressure and queue pressure from Prometheus metrics.

        Returns (kv_pressure, queue_pressure) using real vLLM metrics:
        - kv_pressure: avg KV cache utilization (0-1) — higher means more memory pressure
        - queue_pressure: avg requests waiting / (running + waiting) — higher means more queuing
        """
        prom = self._get_best_result_prom(arch)
        if not prom:
            return 0, 0

        kv_pct = self._prom_avg(prom, 'vllm_kv_cache_pct')
        requests_waiting = self._prom_avg(prom, 'vllm_requests_waiting')
        requests_running = self._prom_avg(prom, 'vllm_requests_running')
        queue_time = self._prom_avg(prom, 'vllm_queue_time_rate')
        prefill_time = self._prom_avg(prom, 'vllm_prefill_time_rate')
        decode_time = self._prom_avg(prom, 'vllm_decode_time_rate')

        kv_pressure = 0.0
        if kv_pct is not None and kv_pct > 0:
            kv_pressure = min(kv_pct, 1.0)

        queue_pressure = 0.0
        if requests_waiting is not None and requests_running is not None:
            total = requests_running + requests_waiting
            if total > 0:
                queue_pressure = requests_waiting / total
        elif queue_time is not None and prefill_time is not None and decode_time is not None:
            total_time = prefill_time + decode_time + queue_time
            if total_time > 0:
                queue_pressure = queue_time / total_time

        if kv_pressure > 0 or queue_pressure > 0:
            self.log(f"    Measured {arch}: KV cache={kv_pressure:.4f}, queue pressure={queue_pressure:.4f}", 'info')

        return kv_pressure, queue_pressure

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
        kv_pressure, queue_pressure = self._extract_kv_and_queue_metrics(arch)

        prefix_time_impact_raw = (isl * cache_pct / 100.0) / prefill_tpsg

        cache_mode = getattr(self.config, 'prefix_cache_mode', 'identical') or 'identical'
        if cache_mode == 'identical':
            diversity = 0.1
        elif cache_mode == 'shared_prefix':
            diversity = 0.3
        elif cache_mode == 'multi_group':
            n_groups = getattr(self.config, 'prefix_cache_groups', 5) or 5
            diversity = min(0.7, max(0.2, n_groups / max(num_pods * 3, 1)))
        else:
            diversity = 1.0

        # With many pods, cache affinity causes queue imbalance — dampen prefix weight
        # 1-2 pods: no damping (can't really imbalance), 3-4 pods: moderate, 8+: full damping
        if num_pods <= 2:
            pod_damping = 1.0
        else:
            pod_damping = min(1.0, 2.0 / num_pods)
        prefix_time_impact = prefix_time_impact_raw * diversity * pod_damping

        total_tpsg = prefill_tpsg + decode_tpsg

        if kv_pressure > 0:
            kv_eviction_cost = (isl / prefill_tpsg) * kv_pressure
            kv_source = f"kv_cache={kv_pressure:.4f} (measured)"
        else:
            kv_utilization = min(concurrency / max(max_seqs, 1), 1.0)
            kv_eviction_cost = (isl / prefill_tpsg) * (kv_utilization ** 2)
            kv_source = f"({concurrency}/{max_seqs})²={kv_utilization**2:.4f} (estimated)"

        # Queue cost floor: prevents queue weight from dropping to zero when measured pressure is low
        # With 2 pods: floor=0.15 (low risk), 4 pods: 0.25, 8+ pods: 0.25 (cap)
        queue_floor = 0.15 if num_pods <= 2 else min(0.25, 1.0 / max(num_pods, 1))
        if queue_pressure > 0:
            queue_wait_cost = (isl + osl) / total_tpsg * max(queue_pressure, queue_floor)
            queue_source = f"queue_pressure=max({queue_pressure:.4f}, floor={queue_floor:.2f}) (measured)"
        else:
            queue_wait_cost = (isl + osl) / total_tpsg * queue_floor
            queue_source = f"floor={queue_floor:.2f} ({num_pods} pods, estimated)"

        # Active request cost: proportional to how loaded pods are (running requests vs capacity)
        prom = self._get_best_result_prom(arch)
        active_running = self._prom_avg(prom, 'vllm_requests_running') if prom else None
        if active_running is not None and active_running > 0:
            active_load = min(active_running / max(max_seqs, 1), 1.0)
            active_cost = (osl / decode_tpsg) * active_load
            active_source = f"running={active_running:.1f}/{max_seqs} (measured)"
        else:
            active_load = min(concurrency / max(num_pods * max_seqs, 1), 1.0)
            active_cost = (osl / decode_tpsg) * active_load
            active_source = f"{concurrency}/{num_pods}×{max_seqs} (estimated)"

        # SLO cost: only when latency SLA is enabled, proportional to tail latency overshoot
        has_sla = getattr(self.config, 'latency_constraint_enabled', False)
        slo_cost = 0.0
        slo_source = "disabled"
        if has_sla:
            sla_ms = getattr(self.config, 'latency_constraint_ms', 500) or 500
            ttft_p99 = self._prom_avg(prom, 'vllm_ttft_p99') if prom else None
            if ttft_p99 is not None and ttft_p99 > 0:
                overshoot = max(0, (ttft_p99 * 1000 - sla_ms) / sla_ms)
                slo_cost = (isl + osl) / total_tpsg * min(overshoot, 1.0)
                slo_source = f"p99={ttft_p99*1000:.0f}ms vs SLA={sla_ms}ms (measured)"
            else:
                slo_cost = (isl + osl) / total_tpsg * 0.3
                slo_source = f"SLA={sla_ms}ms (estimated)"

        total = prefix_time_impact + kv_eviction_cost + queue_wait_cost + active_cost + slo_cost
        if total <= 0:
            return None

        scale = 9 if has_sla else 7
        w_prefix = max(1, min(5, round(prefix_time_impact / total * scale)))
        w_kv = max(1, min(5, round(kv_eviction_cost / total * scale)))
        w_queue = max(1, min(5, round(queue_wait_cost / total * scale)))
        w_active = max(1, min(5, round(active_cost / total * scale)))
        w_slo = max(1, min(5, round(slo_cost / total * scale))) if has_sla else 0

        self.log(f"  Smart EPP Weight Derivation ({arch}, {num_pods} pods):", 'info')
        self.log(f"    Prefix impact:  ISL={isl} × {cache_pct:.0f}% ({cache_source}) / TPSG={prefill_tpsg:.0f} = {prefix_time_impact_raw:.4f} × diversity={diversity:.2f} ({cache_mode}) × pod_damping={pod_damping:.2f} = {prefix_time_impact:.4f} GPU-sec", 'info')
        self.log(f"    KV pressure:    ISL={isl} / {prefill_tpsg:.0f} × {kv_source} = {kv_eviction_cost:.4f} GPU-sec", 'info')
        self.log(f"    Queue cost:     ({isl}+{osl}) / {total_tpsg:.0f} × {queue_source} = {queue_wait_cost:.4f} GPU-sec", 'info')
        self.log(f"    Active request: OSL={osl} / {decode_tpsg:.0f} × {active_source} = {active_cost:.4f} GPU-sec", 'info')
        if has_sla:
            self.log(f"    SLO cost:       ({isl}+{osl}) / {total_tpsg:.0f} × {slo_source} = {slo_cost:.4f} GPU-sec", 'info')
        self.log(f"    → Weights: prefix={w_prefix}, kv={w_kv}, queue={w_queue}, active={w_active}" +
                 (f", slo={w_slo}" if has_sla else ""), 'success')

        return {
            'prefix_cache_weight': float(w_prefix),
            'kv_cache_weight': float(w_kv),
            'queue_weight': float(w_queue),
            'active_request_weight': float(w_active),
            'slo_enabled': has_sla,
            'slo_weight': float(w_slo) if has_sla else 0,
        }

    def _refine_epp_from_metrics(self, test_result, arch: str = 'aggregated') -> Optional[Dict]:
        """Refine EPP weights from the Smart EPP test's own metrics.

        After running with derived weights, reads actual cache hit rate,
        KV utilization variance, and queue depth variance from the test.
        Returns refined weights or None if metrics unavailable.
        """
        if not test_result:
            return None

        import json
        test_id = getattr(test_result, 'test_id', None)
        prom = None
        if self.db_manager and self.run_id and test_id:
            try:
                with self.db_manager.get_connection() as conn:
                    row = conn.execute(
                        'SELECT metrics_json FROM test_configurations WHERE config_name = ? AND run_id = ?',
                        (test_id, self.run_id)
                    ).fetchone()
                    if row and row[0]:
                        full = json.loads(row[0])
                        prom = full.get('prometheus_metrics', {})
            except Exception:
                pass
        if not prom:
            return None
        metrics = prom

        prefill_tpsg = self.optimal_prefill_tp.tpsg if self.optimal_prefill_tp else None
        decode_tpsg = self.optimal_decode_tp.tpsg if self.optimal_decode_tp else None
        if not prefill_tpsg or not decode_tpsg:
            return None

        isl = self.config.isl
        osl = self.config.osl

        hits = self._prom_avg(metrics, 'vllm_prefix_cache_hits_rate')
        queries = self._prom_avg(metrics, 'vllm_prefix_cache_queries_rate')
        actual_hit_pct = (hits / queries * 100) if hits is not None and queries and queries > 0 else 0
        kv_pressure = self._prom_avg(metrics, 'vllm_kv_cache_pct') or 0
        requests_waiting = self._prom_avg(metrics, 'vllm_requests_waiting') or 0
        requests_running = self._prom_avg(metrics, 'vllm_requests_running') or 0
        total_reqs = requests_running + requests_waiting
        queue_pressure = (requests_waiting / total_reqs) if total_reqs > 0 else 0
        active_load = requests_running / max(self._compute_max_num_seqs(self.optimal_decode_tp.tp if self.optimal_decode_tp else 1) or 256, 1)

        if actual_hit_pct > 0:
            self.log(f"    Measured cache hit rate: {actual_hit_pct:.1f}%", 'info')
        self.log(f"    Measured KV cache: {kv_pressure:.4f}, queue pressure: {queue_pressure:.4f}, active load: {active_load:.4f}", 'info')

        total_tpsg = prefill_tpsg + decode_tpsg

        # Get pod count for damping
        num_pods = 1
        if arch == 'pd' and self.pareto_results:
            best_split = min(self.pareto_results, key=lambda x: x[1].ttft_p90 if x[1].ttft_p90 else 1e9)[0]
            num_pods = best_split.prefill_pods + best_split.decode_pods
        elif arch == 'aggregated' and self.aggregated_tp:
            num_pods = self.config.total_gpus // self.aggregated_tp

        pod_damping = 1.0 if num_pods <= 2 else min(1.0, 2.0 / num_pods)
        prefix_impact = (isl * actual_hit_pct / 100.0) / prefill_tpsg * pod_damping
        kv_impact = (isl / prefill_tpsg) * min(kv_pressure, 1.0)
        queue_floor = max(0.15, 1.0 / max(num_pods, 1))
        queue_impact = (isl + osl) / total_tpsg * max(queue_pressure, queue_floor)
        active_impact = (osl / decode_tpsg) * min(active_load, 1.0)

        total = prefix_impact + kv_impact + queue_impact + active_impact
        if total <= 0:
            return None

        w_prefix = max(1, min(5, round(prefix_impact / total * 7)))
        w_kv = max(1, min(5, round(kv_impact / total * 7)))
        w_queue = max(1, min(5, round(queue_impact / total * 7)))
        w_active = max(1, min(5, round(active_impact / total * 7)))

        self.log(f"    → Refined weights: prefix={w_prefix}, kv={w_kv}, queue={w_queue}, active={w_active}", 'success')

        return {
            'prefix_cache_weight': float(w_prefix),
            'kv_cache_weight': float(w_kv),
            'queue_weight': float(w_queue),
            'active_request_weight': float(w_active),
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
                best_pd = min(self.pareto_results, key=lambda x: x[1].ttft_p99 or x[1].ttft_p90 or 1e9)
                baseline_ttft = best_pd[1].ttft_p90

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
                        'active_request': {'enabled': True, 'weight': weights.get('active_request_weight', 2)},
                        'slo': {'enabled': weights.get('slo_enabled', False), 'weight': weights.get('slo_weight', 0)},
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
                            'active_request_enabled': True,
                            'active_request_weight': weights.get('active_request_weight', 2),
                            'slo_enabled': weights.get('slo_enabled', False),
                            'slo_weight': weights.get('slo_weight', 0),
                            'max_prefix_blocks': epp_cfg.get('maxPrefixBlocksToMatch', 256),
                            'lru_capacity': epp_cfg.get('lruCapacityPerServer', 31250),
                            'non_cached_tokens': epp_cfg.get('nonCachedTokens', 16),
                        })
                        manifests = {'epp-configmap': cm_yaml}
                        lws_manifests = tmgr.render_config(base_cfg)
                        for mk, mv in lws_manifests.items():
                            if 'service' not in mk:
                                manifests[mk] = mv
                        epp_test_config._epp_manifests = _json.dumps(manifests)
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
                                'active_request': {'enabled': True, 'weight': rweights.get('active_request_weight', 2)},
                                'slo': {'enabled': rweights.get('slo_enabled', False), 'weight': rweights.get('slo_weight', 0)},
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
                            'active_request': {'enabled': True, 'weight': 2.0},
                            'slo': {'enabled': has_sla, 'weight': 3.0 if has_sla else 0},
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

