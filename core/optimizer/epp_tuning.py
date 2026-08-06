"""Step 9: EPP tuning — smart weight derivation + optional sweep."""

import math
import os
import time
from typing import Dict, Optional, Tuple


from core.config_generator import TestConfig
from core.optimizer.config import FeasibleSplit
from core.template_manager import TemplateManager

class EPPTuningMixin:
    """Mixin providing EPP tuning methods for RecipeOptimizer."""

    def _get_best_result_prom(self, arch: str) -> Optional[Dict]:
        """Get Prometheus metrics dict from the best Step 6/7 result from the database."""
        import json as _json
        if not self.db_manager or not self.run_id:
            return None

        step_prefix = 'step6-' if arch == 'aggregated' else ('step7-ep-' if arch == 'ep' else 'step7-')
        try:
            with self.db_manager.get_connection() as conn:
                row = conn.execute('''
                    SELECT metrics_json FROM test_configurations
                    WHERE run_id = ? AND architecture = ? AND status = 'completed'
                      AND config_name LIKE ? AND ttft_p90 IS NOT NULL AND ttft_p90 > 0
                    ORDER BY ttft_p90 ASC LIMIT 1
                ''', (self.run_id, arch, f'{step_prefix}%')).fetchone()
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
            config_desc = f"TP={self.aggregated_tp}" if arch == 'aggregated' else ("best EP config" if arch == 'ep' else "best PD split")
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

    def _get_user_baseline_weights(self) -> Dict:
        """Get the user's selected EPP preset weights as the starting point."""
        presets = {
            'balanced': {'prefix_cache_weight': 3.0, 'kv_cache_weight': 2.0, 'queue_weight': 2.0, 'active_request_weight': 2.0},
            'cache_optimized': {'prefix_cache_weight': 5.0, 'kv_cache_weight': 1.0, 'queue_weight': 2.0, 'active_request_weight': 1.0},
            'queue_balanced': {'prefix_cache_weight': 1.0, 'kv_cache_weight': 1.0, 'queue_weight': 3.0, 'active_request_weight': 3.0},
            'latency_aware': {'prefix_cache_weight': 3.0, 'kv_cache_weight': 2.0, 'queue_weight': 2.0, 'active_request_weight': 2.0},
        }
        preset = getattr(self.config, 'epp_preset', 'balanced') or 'balanced'
        if preset == 'custom' and self.config.epp_config:
            plugins = self.config.epp_config.get('plugins', {})
            return {
                'prefix_cache_weight': plugins.get('prefix_cache', {}).get('weight', 3.0),
                'kv_cache_weight': plugins.get('kv_cache', {}).get('weight', 2.0),
                'queue_weight': plugins.get('queue', {}).get('weight', 2.0),
                'active_request_weight': plugins.get('active_request', {}).get('weight', 2.0),
            }
        return presets.get(preset, presets['balanced'])

    def _compute_smart_epp_weights(self, num_pods: int = 1, arch: str = 'aggregated') -> Optional[Dict]:
        """Refine EPP weights starting from the user's preset, adjusted by measured metrics.

        Instead of deriving weights from scratch (which ignores the user's choice),
        starts from the user's selected preset and nudges weights based on measured
        Prometheus metrics from Step 6/7. This respects the user's intent while
        optimizing based on real data.

        Adjustment rules:
          - High cache hit rate + diverse prompts → nudge prefix up
          - High KV pressure → nudge kv up
          - High queue pressure → nudge queue up
          - High active load → nudge active up
          - Low metric → nudge that weight down (but never below 1)
        """
        prefill_tpsg = self.optimal_prefill_tp.tpsg if self.optimal_prefill_tp else None
        decode_tpsg = self.optimal_decode_tp.tpsg if self.optimal_decode_tp else None

        if not prefill_tpsg or not decode_tpsg:
            self.log("🚨 BUG: TPSG data missing — Steps 2-3 should always produce this. "
                     f"prefill_tpsg={prefill_tpsg}, decode_tpsg={decode_tpsg}", 'error')

        isl = self.config.isl
        osl = self.config.osl

        tp = self.optimal_decode_tp.tp if self.optimal_decode_tp else 1
        max_seqs = self._compute_max_num_seqs(tp) or 256

        # Get user's baseline weights as starting point
        base_w = self._get_user_baseline_weights()
        w_prefix = base_w['prefix_cache_weight']
        w_kv = base_w['kv_cache_weight']
        w_queue = base_w['queue_weight']
        w_active = base_w['active_request_weight']
        preset_name = getattr(self.config, 'epp_preset', 'balanced') or 'balanced'

        self.log(f"  Smart EPP Weight Refinement ({arch}, {num_pods} pods):", 'info')
        self.log(f"    Starting from user preset: {preset_name} ({w_prefix}:{w_kv}:{w_queue}:{w_active})", 'info')

        # Measure actual metrics
        measured_hit_rate = self._extract_cache_hit_rate(arch)
        cache_pct = measured_hit_rate if measured_hit_rate is not None else (self.config.prefix_cache_hit_pct or 0)
        cache_source = 'measured' if measured_hit_rate is not None else 'configured'
        kv_pressure, queue_pressure = self._extract_kv_and_queue_metrics(arch)

        prom = self._get_best_result_prom(arch)
        active_running = self._prom_avg(prom, 'vllm_requests_running') if prom else None
        active_load = min(active_running / max(max_seqs, 1), 1.0) if active_running and active_running > 0 else 0

        # Compute adjustment signals (-1 to +1 range)
        # Prefix: high cache hit rate with diverse prompts → increase
        cache_mode = getattr(self.config, 'prefix_cache_mode', 'identical') or 'identical'
        if cache_mode == 'identical':
            cache_routing_value = 0.1  # routing doesn't help for identical
        elif cache_mode == 'shared_prefix':
            cache_routing_value = 0.3
        else:
            cache_routing_value = 0.7  # multi_group: routing has real value

        prefix_signal = (cache_pct / 100.0) * cache_routing_value - 0.3  # centered around 30% hit
        if num_pods > 2:
            prefix_signal *= min(1.0, 2.0 / num_pods)  # dampen for many pods

        # KV: high pressure → increase weight to route away from full pods
        kv_signal = (kv_pressure - 0.3) if kv_pressure > 0 else -0.1  # threshold at 30%

        # Queue: high pressure → increase weight for better load distribution
        queue_signal = (queue_pressure - 0.1) if queue_pressure > 0 else 0  # threshold at 10%

        # Active: high load → increase weight to spread requests
        active_signal = (active_load - 0.3) if active_load > 0 else -0.1

        # Apply adjustments (±1 max per signal)
        adj_prefix = max(-1, min(1, round(prefix_signal * 2)))
        adj_kv = max(-1, min(1, round(kv_signal * 3)))
        adj_queue = max(-1, min(1, round(queue_signal * 3)))
        adj_active = max(-1, min(1, round(active_signal * 2)))

        w_prefix = max(1, min(5, w_prefix + adj_prefix))
        w_kv = max(1, min(5, w_kv + adj_kv))
        w_queue = max(1, min(5, w_queue + adj_queue))
        w_active = max(1, min(5, w_active + adj_active))

        has_sla = getattr(self.config, 'latency_constraint_enabled', False)

        self.log(f"    Cache hit: {cache_pct:.0f}% ({cache_source}), mode={cache_mode}, routing_value={cache_routing_value:.1f} → prefix adj={adj_prefix:+d}", 'info')
        self.log(f"    KV pressure: {kv_pressure:.4f} → kv adj={adj_kv:+d}", 'info')
        self.log(f"    Queue pressure: {queue_pressure:.4f} → queue adj={adj_queue:+d}", 'info')
        self.log(f"    Active load: {active_load:.4f} → active adj={adj_active:+d}", 'info')
        self.log(f"    → Weights: prefix={w_prefix}, kv={w_kv}, queue={w_queue}, active={w_active}", 'success')

        return {
            'prefix_cache_weight': float(w_prefix),
            'kv_cache_weight': float(w_kv),
            'queue_weight': float(w_queue),
            'active_request_weight': float(w_active),
            'slo_enabled': has_sla,
            'slo_weight': 3.0 if has_sla else 0,
        }

    def _refine_epp_from_metrics(self, test_result, arch: str = 'aggregated', base_weights: Dict = None) -> Optional[Dict]:
        """Refine EPP weights from the smart-derived test's own Prometheus metrics.

        After running with smart-derived weights, reads the test's actual metrics
        and applies a second round of ±1 adjustments. Uses the smart-derived
        weights as the starting point (not the user preset).
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
                        'SELECT metrics_json, test_config_json FROM test_configurations WHERE config_name = ? AND run_id = ?',
                        (test_id, self.run_id)
                    ).fetchone()
                    if row and row[0]:
                        full = json.loads(row[0])
                        prom = full.get('prometheus_metrics', {})
            except Exception:
                pass
        if not prom:
            return None

        max_seqs = self._compute_max_num_seqs(self.optimal_decode_tp.tp if self.optimal_decode_tp else 1) or 256

        hits = self._prom_avg(prom, 'vllm_prefix_cache_hits_rate')
        queries = self._prom_avg(prom, 'vllm_prefix_cache_queries_rate')
        actual_hit_pct = (hits / queries * 100) if hits is not None and queries and queries > 0 else 0
        kv_pressure = self._prom_avg(prom, 'vllm_kv_cache_pct') or 0
        requests_waiting = self._prom_avg(prom, 'vllm_requests_waiting') or 0
        requests_running = self._prom_avg(prom, 'vllm_requests_running') or 0
        total_reqs = requests_running + requests_waiting
        queue_pressure = (requests_waiting / total_reqs) if total_reqs > 0 else 0
        active_load = requests_running / max(max_seqs, 1)

        if actual_hit_pct > 0:
            self.log(f"    Measured cache hit rate: {actual_hit_pct:.1f}%", 'info')
        self.log(f"    Measured KV cache: {kv_pressure:.4f}, queue pressure: {queue_pressure:.4f}, active load: {active_load:.4f}", 'info')

        # Get pod count for damping
        num_pods = 1
        if arch == 'pd' and self.pareto_results:
            best_split = min(self.pareto_results, key=lambda x: x[1].ttft_p90 if x[1].ttft_p90 else 1e9)[0]
            num_pods = best_split.prefill_pods + best_split.decode_pods
        elif arch == 'aggregated' and self.aggregated_tp:
            num_pods = self.config.total_gpus // self.aggregated_tp

        # Compute adjustment signals from the test's own metrics
        cache_mode = getattr(self.config, 'prefix_cache_mode', 'identical') or 'identical'
        cache_routing_value = 0.1 if cache_mode == 'identical' else (0.3 if cache_mode == 'shared_prefix' else 0.7)
        prefix_signal = (actual_hit_pct / 100.0) * cache_routing_value - 0.3
        if num_pods > 2:
            prefix_signal *= min(1.0, 2.0 / num_pods)
        kv_signal = (kv_pressure - 0.3) if kv_pressure > 0 else -0.1
        queue_signal = (queue_pressure - 0.1) if queue_pressure > 0 else 0
        active_signal = (active_load - 0.3) if active_load > 0 else -0.1

        adj_prefix = max(-1, min(1, round(prefix_signal * 2)))
        adj_kv = max(-1, min(1, round(kv_signal * 3)))
        adj_queue = max(-1, min(1, round(queue_signal * 3)))
        adj_active = max(-1, min(1, round(active_signal * 2)))

        # Apply adjustments to the smart-derived weights from the first test
        bw = base_weights or self._get_user_baseline_weights()
        w_prefix = max(1, min(5, bw['prefix_cache_weight'] + adj_prefix))
        w_kv = max(1, min(5, bw['kv_cache_weight'] + adj_kv))
        w_queue = max(1, min(5, bw['queue_weight'] + adj_queue))
        w_active = max(1, min(5, bw.get('active_request_weight', 2) + adj_active))

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

        # Collect best configs per architecture from DB (resume-safe)
        configs_to_test = []
        default_concurrency = int(self.config.qps)
        if hasattr(self, 'effective_concurrency') and self.effective_concurrency:
            default_concurrency = self.effective_concurrency

        import json as _json
        def _best_from_db(arch, step_prefix):
            """Find best config for an architecture from DB test results."""
            if not self.db_manager or not self.run_id:
                return None
            try:
                with self.db_manager.get_connection() as conn:
                    rows = conn.execute('''
                        SELECT config_name, tensor_parallelism, decode_tp, prefill_pods, decode_pods,
                               ttft_p90, ttft_p99, throughput_p90, test_config_json
                        FROM test_configurations
                        WHERE run_id = ? AND architecture = ? AND status = 'completed'
                          AND config_name LIKE ? AND ttft_p90 IS NOT NULL AND ttft_p90 > 0
                        ORDER BY ttft_p90 ASC
                        LIMIT 1
                    ''', (self.run_id, arch, f'{step_prefix}%')).fetchall()
                return rows[0] if rows else None
            except Exception:
                return None

        # Best PD config from step7 DB results
        pd_row = _best_from_db('pd', 'step7-')
        if pd_row:
            tc_raw = pd_row[8]
            tc = _json.loads(tc_raw) if tc_raw else {}
            split = FeasibleSplit(
                prefill_pods=tc.get('prefill_replicas', pd_row[3]),
                decode_pods=tc.get('decode_replicas', pd_row[4]),
                prefill_tp=tc.get('prefill_tp', pd_row[1]),
                decode_tp=tc.get('decode_tp', pd_row[2] or pd_row[1]),
                prefill_gpus=tc.get('prefill_replicas', pd_row[3]) * tc.get('prefill_tp', pd_row[1]),
                decode_gpus=tc.get('decode_replicas', pd_row[4]) * tc.get('decode_tp', pd_row[2] or pd_row[1]),
                total_gpus=self.config.total_gpus,
                prefill_pct=0,
            )
            pd_cfg = self._create_pd_config(split)
            configs_to_test.append(('pd', pd_cfg, default_concurrency))
            self.log(f"  PD: {split.prefill_pods}P×TP{split.prefill_tp} + {split.decode_pods}D×TP{split.decode_tp} at c={default_concurrency}", 'info')

        # Best Aggregated config from step6 DB results
        agg_row = _best_from_db('aggregated', 'step6-')
        if agg_row:
            agg_tp = agg_row[1]
            agg_cfg = self._create_aggregated_config(
                tp=agg_tp,
                num_gpus=self.config.total_gpus,
                isl=self.config.isl,
                osl=self.config.osl,
                test_id=f"step11-epp-aggregated",
                use_concurrency=True,
            )
            configs_to_test.append(('aggregated', agg_cfg, default_concurrency))
            self.log(f"  Aggregated: {self.config.total_gpus // agg_tp}×TP{agg_tp} at c={default_concurrency}", 'info')

        # Best EP config from step7-ep DB results
        ep_row = _best_from_db('ep', 'step7-ep-')
        if ep_row:
            tc_raw = ep_row[8]
            tc = _json.loads(tc_raw) if tc_raw else {}
            ep_split = FeasibleSplit(
                prefill_pods=tc.get('prefill_replicas', ep_row[3]),
                decode_pods=tc.get('decode_replicas', ep_row[4]),
                prefill_tp=tc.get('prefill_tp', ep_row[1]),
                decode_tp=tc.get('decode_tp', ep_row[2] or ep_row[1]),
                prefill_gpus=tc.get('prefill_replicas', ep_row[3]) * tc.get('prefill_tp', ep_row[1]),
                decode_gpus=tc.get('decode_replicas', ep_row[4]) * tc.get('decode_tp', ep_row[2] or ep_row[1]),
                total_gpus=self.config.total_gpus,
                prefill_pct=0,
            )
            ep_cfg = self._create_ep_config(ep_split)
            configs_to_test.append(('ep', ep_cfg, default_concurrency))
            self.log(f"  EP: {ep_split.prefill_pods}P+{ep_split.decode_pods}D "
                     f"PTP={ep_split.prefill_tp} DTP={ep_split.decode_tp} at c={default_concurrency}", 'info')

        # Skip single-pod configs — EPP routing is meaningless with 1 backend
        filtered = []
        for arch, cfg, conc in configs_to_test:
            total_pods = 1
            if arch == 'aggregated' and agg_row:
                agg_tp = agg_row[1]
                total_pods = self.config.total_gpus // agg_tp if agg_tp else 1
            elif arch == 'pd' and pd_row:
                total_pods = pd_row[3] + pd_row[4]
            elif arch == 'ep' and ep_row:
                total_pods = ep_row[3] + ep_row[4]
            if total_pods <= 1:
                self.log(f"  ⏩ Skipping {arch.upper()} EPP tuning — single pod, nothing to route", 'info')
                if not self.epp_benchmark_results:
                    self.epp_benchmark_results = {}
                self.epp_benchmark_results[arch] = [('_skipped_single_pod', {}, None)]
                continue
            filtered.append((arch, cfg, conc))
        configs_to_test = filtered

        if not configs_to_test:
            self.log("⚠️  No multi-pod configs for EPP tuning — skipping", 'warning')
            return

        if not self.epp_benchmark_results:
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

            if arch in self.epp_benchmark_results and self.epp_benchmark_results[arch]:
                self.log(f"\n  --- EPP Tuning: {arch.upper()} — already completed (resumed from DB), skipping ---", 'info')
                continue

            self.log(f"\n  --- EPP Tuning: {arch.upper()} (c={concurrency}) ---", 'decision')

            # Compute per-architecture smart weights using measured Step 6/7 data
            arch_pods = 1
            if arch == 'pd' and self.pareto_results:
                best_split = min(self.pareto_results, key=lambda x: x[1].ttft_p90 if x[1].ttft_p90 else 1e9)[0]
                arch_pods = best_split.prefill_pods + best_split.decode_pods
            elif arch == 'aggregated' and self.aggregated_tp:
                arch_pods = self.config.total_gpus // self.aggregated_tp
            elif arch == 'ep' and self.best_ep_config:
                arch_pods = self.best_ep_config.prefill_pods + self.best_ep_config.decode_pods

            prom_data = self._get_best_result_prom(arch)
            if not prom_data:
                self.log(f"  ⚠️  Skipping {arch.upper()} EPP tuning — no Prometheus metrics available from Step 6/7", 'warning')
                self.log(f"     Check that Thanos/Prometheus is accessible and metrics are being scraped", 'warning')
                self.epp_benchmark_results[arch] = [('_skipped_no_metrics', {}, None)]
                continue

            smart_weights = self._compute_smart_epp_weights(num_pods=arch_pods, arch=arch)

            # Baseline TTFT from Step 6/7 (ran with default EPP weights)
            baseline_ttft = None
            if arch == 'aggregated' and self.aggregated_result:
                baseline_ttft = self.aggregated_result.ttft_p90
            elif arch == 'pd' and self.pareto_results:
                best_pd = min(self.pareto_results, key=lambda x: x[1].ttft_p99 or x[1].ttft_p90 or 1e9)
                baseline_ttft = best_pd[1].ttft_p90
            elif arch == 'ep' and self.best_ep_result:
                baseline_ttft = self.best_ep_result.ttft_p90

            if smart_weights:
                base_w = self._get_user_baseline_weights()
                self.log(f"  Preset:  {base_w['prefix_cache_weight']}:{base_w['kv_cache_weight']}:"
                         f"{base_w['queue_weight']}:{base_w.get('active_request_weight', 2)}", 'info')
                self.log(f"  Derived: {smart_weights['prefix_cache_weight']}:{smart_weights['kv_cache_weight']}:"
                         f"{smart_weights['queue_weight']}:{smart_weights.get('active_request_weight', 2)}", 'info')
                weights_unchanged = (
                    smart_weights['prefix_cache_weight'] == base_w['prefix_cache_weight'] and
                    smart_weights['kv_cache_weight'] == base_w['kv_cache_weight'] and
                    smart_weights['queue_weight'] == base_w['queue_weight'] and
                    smart_weights.get('active_request_weight', 2) == base_w.get('active_request_weight', 2)
                )
                if weights_unchanged:
                    self.log(f"  ✅ Smart-derived weights match user preset — skipping EPP test for {arch}.", 'success')
                    self.epp_benchmark_results[arch] = [('_skipped_weights_match', base_w, None)]
                    continue
                self.log(f"  ↔ Weights differ from preset — running EPP test for {arch}", 'info')
                weight_combos = [('smart-derived', smart_weights)]
            else:
                self.log(f"  ⚠️  Smart weight derivation returned None — using fallback combos", 'warning')
                weight_combos = list(fallback_combos)

            self.log(f"  Weight combos: {', '.join(n for n, _ in weight_combos)}", 'info')
            arch_results = []

            for combo_idx, (name, weights) in enumerate(weight_combos):
                if self._should_stop():
                    break

                # Clean up any leftover step9 EPP LWS from previous combo
                try:
                    self.orchestrator.deployment_manager.kubectl.run(
                        ['delete', 'lws', '-l', 'component=serveit-test',
                         '-n', self.config.namespace, '--ignore-not-found=true'],
                        check=False
                    )
                    # Wait for pods to fully terminate before deploying new ones
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
                    selected_dra_classes=getattr(base_cfg, 'selected_dra_classes', None) or getattr(self.config, 'selected_dra_classes', []),
                    dra_gpu_resource_key=getattr(base_cfg, 'dra_gpu_resource_key', None) or getattr(self.config, 'dra_gpu_resource_key', None),
                    gateway_class=getattr(base_cfg, 'gateway_class', None) or getattr(self.config, 'gateway_class', 'istio'),
                    per_node_storage=getattr(base_cfg, 'per_node_storage', None) or getattr(self.config, 'per_node_storage', False),
                    node_nfs_pvcs=getattr(base_cfg, 'node_nfs_pvcs', None) or getattr(self.config, 'node_nfs_pvcs', []),
                    storage_class=getattr(base_cfg, 'storage_class', None) or getattr(self.config, 'storage_class', None),
                    pvc_size=getattr(base_cfg, 'pvc_size', None) or getattr(self.config, 'pvc_size', None),
                    rdma_network_annotation=getattr(base_cfg, 'rdma_network_annotation', None) or getattr(self.config, 'rdma_network_annotation', None),
                    exclusive_pf=getattr(base_cfg, 'exclusive_pf', False) or getattr(self.config, 'exclusive_pf', False),
                    scheduler_image=getattr(base_cfg, 'scheduler_image', None) or getattr(self.config, 'scheduler_image', None),
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
                refined = self._refine_epp_from_metrics(first_result, arch=arch, base_weights=smart_weights)
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
                                ['delete', 'lws', '-l', 'component=serveit-test',
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
                                selected_dra_classes=getattr(base_cfg, 'selected_dra_classes', None) or getattr(self.config, 'selected_dra_classes', []),
                                dra_gpu_resource_key=getattr(base_cfg, 'dra_gpu_resource_key', None) or getattr(self.config, 'dra_gpu_resource_key', None),
                                gateway_class=getattr(base_cfg, 'gateway_class', None) or getattr(self.config, 'gateway_class', 'istio'),
                                per_node_storage=getattr(base_cfg, 'per_node_storage', None) or getattr(self.config, 'per_node_storage', False),
                                node_nfs_pvcs=getattr(base_cfg, 'node_nfs_pvcs', None) or getattr(self.config, 'node_nfs_pvcs', []),
                                storage_class=getattr(base_cfg, 'storage_class', None) or getattr(self.config, 'storage_class', None),
                                pvc_size=getattr(base_cfg, 'pvc_size', None) or getattr(self.config, 'pvc_size', None),
                                rdma_network_annotation=getattr(base_cfg, 'rdma_network_annotation', None) or getattr(self.config, 'rdma_network_annotation', None),
                                exclusive_pf=getattr(base_cfg, 'exclusive_pf', False) or getattr(self.config, 'exclusive_pf', False),
                                scheduler_image=getattr(base_cfg, 'scheduler_image', None) or getattr(self.config, 'scheduler_image', None),
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
                            ['delete', 'lws', '-l', 'component=serveit-test',
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
                        selected_dra_classes=getattr(base_cfg, 'selected_dra_classes', None) or getattr(self.config, 'selected_dra_classes', []),
                        dra_gpu_resource_key=getattr(base_cfg, 'dra_gpu_resource_key', None) or getattr(self.config, 'dra_gpu_resource_key', None),
                        gateway_class=getattr(base_cfg, 'gateway_class', None) or getattr(self.config, 'gateway_class', 'istio'),
                        per_node_storage=getattr(base_cfg, 'per_node_storage', None) or getattr(self.config, 'per_node_storage', False),
                        node_nfs_pvcs=getattr(base_cfg, 'node_nfs_pvcs', None) or getattr(self.config, 'node_nfs_pvcs', []),
                        storage_class=getattr(base_cfg, 'storage_class', None) or getattr(self.config, 'storage_class', None),
                        pvc_size=getattr(base_cfg, 'pvc_size', None) or getattr(self.config, 'pvc_size', None),
                        rdma_network_annotation=getattr(base_cfg, 'rdma_network_annotation', None) or getattr(self.config, 'rdma_network_annotation', None),
                        exclusive_pf=getattr(base_cfg, 'exclusive_pf', False) or getattr(self.config, 'exclusive_pf', False),
                        scheduler_image=getattr(base_cfg, 'scheduler_image', None) or getattr(self.config, 'scheduler_image', None),
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

            valid_arch = [r for r in arch_results if r[2] is not None]
            if valid_arch:
                best_name = min(valid_arch, key=lambda x: x[2].ttft_p90 or float('inf'))[0]
                self.log(f"  Best {arch}: {best_name}", 'success')

    def _apply_best_epp_config(self):
        """After EPP tuning, deploy the best-performing EPP weights for subsequent steps.
        Only applies if the best EPP result beats the Step 6/7 baseline."""
        if not self.epp_benchmark_results:
            return
        for arch, results in self.epp_benchmark_results.items():
            valid = [r for r in results if r[2] is not None]
            if not valid:
                continue
            best_name, best_weights, best_result = min(valid, key=lambda x: x[2].ttft_p90 or float('inf'))

            # Compare against Step 6/7 baseline — only apply if EPP result is better
            baseline_ttft = None
            if arch == 'aggregated' and self.aggregated_result:
                baseline_ttft = self.aggregated_result.ttft_p90
            elif arch == 'pd' and self.pareto_results:
                best_pd = min(self.pareto_results, key=lambda x: x[1].ttft_p90 if x[1].ttft_p90 else 1e9)
                baseline_ttft = best_pd[1].ttft_p90
            elif arch == 'ep' and getattr(self, 'best_ep_result', None):
                baseline_ttft = self.best_ep_result.ttft_p90
            if not baseline_ttft:
                prom = self._get_best_result_prom(arch)
                if not prom:
                    baseline_ttft = best_result.ttft_p90

            epp_ttft = best_result.ttft_p90 or float('inf')
            if baseline_ttft and epp_ttft >= baseline_ttft:
                self.log(f"  ⏩ Keeping original preset for {arch} — smart-derived TTFT {epp_ttft:.0f}ms >= baseline {baseline_ttft:.0f}ms", 'info')
                continue

            self.log(f"  Applying best EPP config for {arch}: {best_name} "
                     f"(cache={best_weights['prefix_cache_weight']}, kv={best_weights['kv_cache_weight']}, "
                     f"queue={best_weights['queue_weight']}) — TTFT {epp_ttft:.0f}ms vs baseline {baseline_ttft:.0f}ms", 'success')
            epp_config = {
                'preset': 'custom',
                'plugins': {
                    'prefix_cache': {'enabled': True, 'weight': best_weights['prefix_cache_weight']},
                    'kv_cache': {'enabled': True, 'weight': best_weights['kv_cache_weight']},
                    'queue': {'enabled': True, 'weight': best_weights['queue_weight']},
                    'active_request': {'enabled': True, 'weight': best_weights.get('active_request_weight', 2)},
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

        # Only update global config if a non-baseline winner was actually applied above
        # (the per-arch loop skips architectures where EPP didn't beat baseline)

