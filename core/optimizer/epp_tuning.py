"""Step 9: EPP tuning — smart weight derivation + optional sweep."""

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
        # Single test runs store their one config under a single-* test id —
        # fall back to it when the recipe steps produced no rows
        prefixes = [step_prefix, 'single-']
        try:
            with self.db_manager.get_connection() as conn:
                row = None
                for prefix in prefixes:
                    row = conn.execute('''
                        SELECT metrics_json FROM test_configurations
                        WHERE run_id = ? AND architecture = ? AND status = 'completed'
                          AND config_name LIKE ? AND ttft_p90 IS NOT NULL AND ttft_p90 > 0
                        ORDER BY ttft_p90 ASC LIMIT 1
                    ''', (self.run_id, arch, f'{prefix}%')).fetchone()
                    if row and row[0]:
                        break
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

    def _merge_auto_plugins(self, core_plugins: Dict) -> Dict:
        """Carry forward Auto EPP plugin decisions into Smart EPP configs.

        Smart EPP refines the four core weight values (prefix, kv, queue,
        active) based on measured metrics. But the Auto derivation also makes
        binary enable/disable decisions for adapter-affinity scorers
        (session-affinity, header-label-affinity for LoRA routing) and
        optional scorers (no-hit-lru, load-aware). Without this merge, every
        Smart EPP config rewrite silently drops those plugins.

        The core weights are always taken from `core_plugins` (the refined
        values). The affinity/optional plugins are carried forward from the
        Auto derivation stored on config.epp_auto_weights.
        """
        auto = getattr(self.config, 'epp_auto_weights', None) or {}
        merged = dict(core_plugins)
        plugin_map = {
            'session_aware': 'session_aware',
            'header_label_affinity': 'header_label_affinity',
            'lora_affinity': 'lora_affinity',
            'no_hit_lru': 'no_hit_lru',
            'load_aware': 'load_aware',
            'token_load': 'token_load',
        }
        for auto_key, plugin_key in plugin_map.items():
            if auto.get(f'{auto_key}_enabled'):
                merged[plugin_key] = {
                    'enabled': True,
                    'weight': auto.get(f'{auto_key}_weight', 2.0),
                }
        return merged

    def _get_user_baseline_weights(self) -> Dict:
        """Get the user's selected EPP preset weights as the starting point."""
        presets = {
            'balanced': {'prefix_cache_weight': 3.0, 'kv_cache_weight': 2.0, 'queue_weight': 2.0, 'active_request_weight': 2.0},
            'cache_optimized': {'prefix_cache_weight': 5.0, 'kv_cache_weight': 1.0, 'queue_weight': 2.0, 'active_request_weight': 1.0},
            'queue_balanced': {'prefix_cache_weight': 1.0, 'kv_cache_weight': 1.0, 'queue_weight': 3.0, 'active_request_weight': 3.0},
            'latency_aware': {'prefix_cache_weight': 3.0, 'kv_cache_weight': 2.0, 'queue_weight': 2.0, 'active_request_weight': 2.0},
        }
        preset = getattr(self.config, 'epp_preset', 'balanced') or 'balanced'
        if preset == 'auto':
            # Auto: use the workload-derived weights (computed at prereq time
            # and stored on the config by _derive_auto_epp_weights).
            # IMPORTANT: check the _enabled flags — the weight values for
            # disabled scorers are placeholders (1.0), NOT real weights.
            # Returning the placeholder causes the smart tuner to "enable"
            # the scorer via adjustment, deploying configs the baseline
            # didn't have.
            auto = getattr(self.config, 'epp_auto_weights', None)
            if auto:
                return {
                    'prefix_cache_weight': auto.get('prefix_cache_weight', 3.0),
                    'kv_cache_weight': (auto.get('kv_cache_weight', 2.0)
                                         if auto.get('kv_cache_enabled') else 0),
                    'queue_weight': auto.get('queue_weight', 2.0),
                    'active_request_weight': (auto.get('active_request_weight', 2.0)
                                              if auto.get('active_request_enabled') else 0),
                }
            # Fallback if auto weights weren't pre-computed (e.g. resume from
            # older config) — derive now
            from core.prereq_manager import PrereqManager
            pm = PrereqManager.__new__(PrereqManager)
            derived = pm._derive_auto_epp_weights(self.config)
            return {
                'prefix_cache_weight': derived['prefix_cache_weight'],
                'kv_cache_weight': derived['kv_cache_weight'],
                'queue_weight': derived['queue_weight'],
                'active_request_weight': derived['active_request_weight'],
            }
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
            if getattr(self.config, 'objective', '') == 'single_test':
                # Single test mode skips calibration (Steps 2-3) — no TPSG
                # data is expected. max_seqs falls back to 256.
                self.log("  ℹ️  No TPSG calibration data (single test mode) — "
                         "using max_seqs fallback", 'info')
            else:
                self.log("🚨 BUG: TPSG data missing — Steps 2-3 should always produce this. "
                         f"prefill_tpsg={prefill_tpsg}, decode_tpsg={decode_tpsg}", 'error')

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

        w_prefix = max(0.1, min(5, w_prefix + adj_prefix))
        # Scorers at 0 are DISABLED — do not adjust or re-enable them
        w_kv = (max(0.1, min(5, w_kv + adj_kv)) if w_kv > 0 else 0)
        w_queue = max(0.1, min(5, w_queue + adj_queue))
        w_active = (max(0.1, min(5, w_active + adj_active)) if w_active > 0 else 0)

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
            num_pods = self.config.total_gpus // (self.aggregated_tp * (self.aggregated_pp or 1))

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

        # Apply adjustments to the smart-derived weights from the first test.
        # Scorers at 0 are DISABLED — do not adjust or re-enable them.
        bw = base_weights or self._get_user_baseline_weights()
        w_prefix = max(0.1, min(5, bw['prefix_cache_weight'] + adj_prefix))
        w_kv = (max(0.1, min(5, bw['kv_cache_weight'] + adj_kv)) if bw['kv_cache_weight'] > 0 else 0)
        w_queue = max(0.1, min(5, bw['queue_weight'] + adj_queue))
        w_active = (max(0.1, min(5, bw.get('active_request_weight', 2) + adj_active)) if bw.get('active_request_weight', 2) > 0 else 0)

        self.log(f"    → Refined weights: prefix={w_prefix}, kv={w_kv}, queue={w_queue}, active={w_active}", 'success')

        result = {
            'prefix_cache_weight': float(w_prefix),
            'kv_cache_weight': float(w_kv),
            'queue_weight': float(w_queue),
            'active_request_weight': float(w_active),
            'slo_enabled': False,
        }

        # LoRA-affinity feedback: read the test's own adapter-routing
        # telemetry (vllm_lora_adapter_info) and adjust the weight by the
        # regime the measurements actually show:
        #   churn high            -> capacity problem, weight can't help
        #   full residency + low
        #   affinity              -> signal not biting, raise the weight
        #   high affinity + hot-pod
        #   waiting queues        -> over-concentration, lower the weight
        if getattr(self.config, 'lora_enabled', False):
            lora_info = prom.get('vllm_lora_adapter_info') or {}
            aff = lora_info.get('adapter_pod_affinity') or {}
            lpods = lora_info.get('pods') or {}
            if aff and lpods:
                total = len(aff)
                conc = sum(1 for v in aff.values() if len(v) == 1)
                affinity_pct = 100.0 * conc / max(total, 1)
                churn_vals = [p.get('residency_set_changes', 0) for p in lpods.values()]
                avg_churn = sum(churn_vals) / max(len(churn_vals), 1)
                waiting = sum(len(p.get('waiting_adapters_last', []) or []) for p in lpods.values())
                self.log(f"    LoRA routing: affinity={affinity_pct:.0f}% single-pod, "
                         f"churn={avg_churn:.0f}/pod, waiting-queue={waiting}", 'info')
                auto_w = getattr(self.config, 'epp_auto_weights', None) or {}
                cur_w = float(auto_w.get('lora_affinity_weight', 3.0) or 3.0)
                if avg_churn > 20:
                    self.log("    → adapter churn high — residency is thrashing (capacity); "
                             "lora-affinity weight cannot fix this, keeping", 'warning')
                elif affinity_pct < 50 and cur_w < 5:
                    result['lora_affinity_weight'] = min(5.0, cur_w + 1.0)
                    self.log(f"    → full residency but affinity low — signal not biting; "
                             f"lora-affinity {cur_w} → {result['lora_affinity_weight']}", 'info')
                elif affinity_pct > 90 and waiting > max(2, total // 4):
                    result['lora_affinity_weight'] = max(1.0, cur_w - 1.0)
                    self.log(f"    → over-concentration (hot-pod adapter waiting queues); "
                             f"lora-affinity {cur_w} → {result['lora_affinity_weight']}", 'info')

        return result

    def _epp_objective(self, test_result) -> float:
        """Objective for comparing EPP variants.

        LoRA runs optimize TTFT p99 — adapter swap-ins are a tail event
        (misrouted requests pay the CPU→GPU swap-in tax) and vanish from
        the p90. Non-LoRA runs keep the p90 objective.
        """
        if not test_result:
            return float('inf')
        if getattr(self.config, 'lora_enabled', False):
            return test_result.ttft_p99 or test_result.ttft_p90 or float('inf')
        return test_result.ttft_p90 or float('inf')

    def _build_variant_plugins(self, weights: Dict) -> Dict:
        """Build the plugin dict for an EPP variant.

        Core weights (prefix/kv/queue/active/slo) come from ``weights``;
        affinity/optional plugins carry forward from the Auto derivation
        via _merge_auto_plugins. LoRA-affinity overrides ride in the
        weights dict itself so the 3-tuple result shape is unchanged:

        - weights['lora_affinity_weight']  -> override the weight
        - weights['lora_affinity_enabled'] is False -> scorer disabled
        - neither key                      -> auto-derived value (default)
        """
        plugins = self._merge_auto_plugins({
            'prefix_cache': {'enabled': True, 'weight': weights['prefix_cache_weight']},
            'kv_cache': {'enabled': True, 'weight': weights['kv_cache_weight']},
            'queue': {'enabled': True, 'weight': weights['queue_weight']},
            'active_request': {'enabled': True, 'weight': weights.get('active_request_weight', 2)},
            'slo': {'enabled': weights.get('slo_enabled', False), 'weight': weights.get('slo_weight', 0)},
        })
        if weights.get('lora_affinity_enabled') is False:
            plugins.pop('lora_affinity', None)
        elif weights.get('lora_affinity_weight') is not None:
            plugins['lora_affinity'] = {
                'enabled': True,
                'weight': float(weights['lora_affinity_weight']),
            }
        return plugins

    def _run_epp_variant(self, prereq_mgr, arch: str, base_cfg, concurrency: int,
                         name: str, weights: Dict, base_epp: Dict = None,
                         baseline_ttft: float = None):
        """Deploy one EPP weight variant, benchmark it, save to DB.

        Returns the TestResult on success, None on failure/skip.
        """
        base_epp = base_epp or {}
        test_id = f"step11-epp-{arch}-{name}"
        lora_str = ''
        if weights.get('lora_affinity_enabled') is False:
            lora_str = ', lora-affinity=off'
        elif weights.get('lora_affinity_weight') is not None:
            lora_str = f", lora-affinity={weights['lora_affinity_weight']}"
        self.log(f"  Testing: {name} (cache={weights['prefix_cache_weight']}, "
                 f"kv={weights['kv_cache_weight']}, queue={weights['queue_weight']}{lora_str})", 'info')

        epp_cfg = {
            'preset': 'custom',
            'maxPrefixBlocksToMatch': base_epp.get('maxPrefixBlocksToMatch', 256),
            'lruCapacityPerServer': base_epp.get('lruCapacityPerServer', 31250),
            'nonCachedTokens': base_epp.get('nonCachedTokens', 16),
            'plugins': self._build_variant_plugins(weights),
        }

        success = prereq_mgr.update_epp_config(
            architecture=arch,
            epp_config=epp_cfg,
            log_callback=lambda msg: self.log(msg, 'info')
        )
        if not success:
            self.log(f"  ❌ Failed to update EPP config for {name}", 'error')
            return None

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
            lora_enabled=base_cfg.lora_enabled,
            lora_num_adapters=base_cfg.lora_num_adapters,
            lora_adapter_size_mb=base_cfg.lora_adapter_size_mb,
            lora_max_loras=getattr(base_cfg, 'lora_max_loras', None),
            lora_max_rank=getattr(base_cfg, 'lora_max_rank', None),
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
            local_disk_path=getattr(base_cfg, 'local_disk_path', None) or getattr(self.config, 'local_disk_path', None),
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

        if not (result and result.guidellm_success):
            self.log(f"  ❌ {name}: benchmark failed", 'error')
            return None

        obj = self._epp_objective(result)
        obj_label = 'p99' if getattr(self.config, 'lora_enabled', False) else 'p90'
        tput = result.throughput_p90 or 0
        if baseline_ttft:
            if obj > baseline_ttft * 1.5:
                self.log(f"  ⚠️  {name}: TTFT {obj:.0f}ms is {obj/baseline_ttft:.1f}x worse than "
                         f"baseline ({baseline_ttft:.0f}ms) — EPP weights degraded performance", 'warning')
            else:
                improvement = ((baseline_ttft - obj) / baseline_ttft * 100) if baseline_ttft else 0
                self.log(f"  ✅ {name}: TTFT {obj_label}={obj:.1f}ms ({improvement:+.1f}% vs baseline), "
                         f"Throughput p90={tput:.2f} req/s", 'success')
        else:
            self.log(f"  ✅ {name}: TTFT {obj_label}={obj:.1f}ms, Throughput p90={tput:.2f} req/s", 'success')

        self.all_test_results.append((epp_test_config, result))

        try:
            import json as _json
            tmgr = TemplateManager()
            cm_template = f'prereq/gaie-configmap-{arch}.yaml.j2'
            auto_w = getattr(self.config, 'epp_auto_weights', None) or {}
            from core.prereq_manager import is_epp_v011_plus
            cm_yaml = tmgr.render_template(cm_template, **{
                'namespace': self.config.namespace,
                'gaie_name': f'gaie-{arch}-epp',
                'config_file': f'{arch}-config.yaml',
                'epp_v011_plus': is_epp_v011_plus(getattr(self.config, 'scheduler_image', '') or ''),
                'prefix_cache_weight': weights['prefix_cache_weight'],
                'kv_cache_weight': weights['kv_cache_weight'],
                'queue_weight': weights['queue_weight'],
                'active_request_enabled': True,
                'active_request_weight': weights.get('active_request_weight', 2),
                'slo_enabled': weights.get('slo_enabled', False),
                'slo_weight': weights.get('slo_weight', 0),
                'session_aware_enabled': bool(auto_w.get('session_aware_enabled')),
                'session_aware_weight': auto_w.get('session_aware_weight', 2.0),
                'lora_affinity_enabled': (False if weights.get('lora_affinity_enabled') is False
                                          else bool(auto_w.get('lora_affinity_enabled'))),
                'lora_affinity_weight': (weights.get('lora_affinity_weight')
                                         if weights.get('lora_affinity_weight') is not None
                                         else auto_w.get('lora_affinity_weight', 3.0)),
                'max_prefix_blocks': base_epp.get('maxPrefixBlocksToMatch', 256),
                'lru_capacity': base_epp.get('lruCapacityPerServer', 31250),
                'non_cached_tokens': base_epp.get('nonCachedTokens', 16),
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
        return result

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

        # Best PD config from step7 DB results (single-test runs store their
        # one config under a single-* id — fall back to it)
        pd_row = _best_from_db('pd', 'step7-') or _best_from_db('pd', 'single-')
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

        # Best Aggregated config from step6 DB results (single-test fallback)
        agg_row = _best_from_db('aggregated', 'step6-') or _best_from_db('aggregated', 'single-')
        if agg_row:
            agg_tp = agg_row[1]
            agg_pp = None
            tc_raw = agg_row[8]
            if tc_raw:
                try:
                    agg_pp = _json.loads(tc_raw).get('pipeline_parallel_size')
                except Exception:
                    pass
            agg_cfg = self._create_aggregated_config(
                tp=agg_tp,
                num_gpus=self.config.total_gpus,
                isl=self.config.isl,
                osl=self.config.osl,
                test_id="step11-epp-aggregated",
                use_concurrency=True,
                pipeline_parallel_size=agg_pp or 1,
            )
            configs_to_test.append(('aggregated', agg_cfg, default_concurrency))
            pp_str = f"xPP={agg_pp} " if agg_pp and agg_pp > 1 else ""
            self.log(f"  Aggregated: {self.config.total_gpus // (agg_tp * (agg_pp or 1))}×TP{agg_tp}{pp_str}at c={default_concurrency}", 'info')

        # Best EP config from step7-ep DB results (single-test fallback)
        ep_row = _best_from_db('ep', 'step7-ep-') or _best_from_db('ep', 'single-')
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

        # The auto-derived weights (lora-affinity, demoted load scorers) are
        # stored on the TEST config by deploy_prereqs — the run config never
        # sees them, so variants would fall back to a slot-blind re-derivation
        # and silently drop the affinity scorers. Adopt the most recent
        # deployed test's weights before building any variant.
        if not getattr(self.config, 'epp_auto_weights', None):
            for cfg, _r in reversed(self.all_test_results):
                if getattr(cfg, 'epp_auto_weights', None):
                    self.config.epp_auto_weights = cfg.epp_auto_weights
                    self.log("  📋 Adopted auto EPP weights from the deployed test config "
                             "(lora-affinity and demotions carry into variants)", 'info')
                    break

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

            # Compute per-architecture pod count from DB rows (resume-safe)
            arch_pods = 1
            if arch == 'pd' and pd_row:
                arch_pods = pd_row[3] + pd_row[4]
            elif arch == 'aggregated' and agg_row:
                agg_tp_val = agg_row[1]
                arch_pods = self.config.total_gpus // agg_tp_val if agg_tp_val else 1
            elif arch == 'ep' and ep_row:
                arch_pods = ep_row[3] + ep_row[4]

            prom_data = self._get_best_result_prom(arch)
            if not prom_data:
                self.log(f"  ⚠️  Skipping {arch.upper()} EPP tuning — no Prometheus metrics available from Step 6/7", 'warning')
                self.log("     Check that Thanos/Prometheus is accessible and metrics are being scraped", 'warning')
                self.epp_benchmark_results[arch] = [('_skipped_no_metrics', {}, None)]
                continue

            # For ≤3 pods, smart weight derivation adds noise — use preset as-is
            if arch_pods <= 3:
                self.log(f"  ✅ {arch.upper()}: {arch_pods} pods — using preset weights (too few pods for smart derivation)", 'success')
                self.epp_benchmark_results[arch] = [('_skipped_few_pods', self._get_user_baseline_weights(), None)]
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

                # LoRA-affinity A/B: directly measure the adapter-locality
                # mechanism (RFE-2527). Same core weights, three lora-affinity
                # settings — derived weight, half weight, scorer disabled.
                auto_w = getattr(self.config, 'epp_auto_weights', None) or {}
                lora_ab_active = (bool(getattr(self.config, 'lora_enabled', False))
                                  and int(getattr(self.config, 'lora_num_adapters', 0) or 0) > 20
                                  and bool(auto_w.get('lora_affinity_enabled')))
                if lora_ab_active:
                    derived_lora_w = float(auto_w.get('lora_affinity_weight', 3.0) or 3.0)
                    self.log(f"  🔀 LoRA A/B: lora-affinity {derived_lora_w} vs "
                             f"{max(1.0, round(derived_lora_w / 2, 1))} vs off — measuring adapter-locality routing", 'decision')

                weights_unchanged = (
                    smart_weights['prefix_cache_weight'] == base_w['prefix_cache_weight'] and
                    smart_weights['kv_cache_weight'] == base_w['kv_cache_weight'] and
                    smart_weights['queue_weight'] == base_w['queue_weight'] and
                    smart_weights.get('active_request_weight', 2) == base_w.get('active_request_weight', 2)
                )
                if weights_unchanged and not lora_ab_active:
                    self.log(f"  ✅ Smart-derived weights match user preset — skipping EPP test for {arch}.", 'success')
                    self.epp_benchmark_results[arch] = [('_skipped_weights_match', base_w, None)]
                    continue
                self.log(f"  ↔ Weights differ from preset — running EPP test for {arch}", 'info')
                weight_combos = [('smart-derived', smart_weights)]
                if lora_ab_active:
                    derived_lora_w = float(auto_w.get('lora_affinity_weight', 3.0) or 3.0)
                    weight_combos.append(('lora-affinity-half',
                                          {**smart_weights, 'lora_affinity_weight': max(1.0, round(derived_lora_w / 2, 1))}))
                    weight_combos.append(('lora-affinity-off',
                                          {**smart_weights, 'lora_affinity_enabled': False}))
            else:
                self.log("  ⚠️  Smart weight derivation returned None — using fallback combos", 'warning')
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

                result = self._run_epp_variant(
                    prereq_mgr, arch, base_cfg, concurrency, name, weights,
                    base_epp=base, baseline_ttft=baseline_ttft,
                )
                if result:
                    arch_results.append((name, weights, result))

            # Smart EPP: try to refine from measured metrics after first test
            if smart_weights and arch_results and not self._should_stop():
                first_result = arch_results[0][2]
                self.log("\n  Attempting metrics-based refinement...", 'info')
                refined = self._refine_epp_from_metrics(first_result, arch=arch, base_weights=smart_weights)
                if refined and (refined['prefix_cache_weight'] != smart_weights['prefix_cache_weight'] or
                                refined['kv_cache_weight'] != smart_weights['kv_cache_weight'] or
                                refined['queue_weight'] != smart_weights['queue_weight'] or
                                refined.get('lora_affinity_weight') != smart_weights.get('lora_affinity_weight') or
                                refined.get('lora_affinity_enabled') != smart_weights.get('lora_affinity_enabled')):
                    self.log("  Weights changed — running validation test", 'info')
                    try:
                        self.orchestrator.deployment_manager.kubectl.run(
                            ['delete', 'lws', '-l', 'component=serveit-test',
                             '-n', self.config.namespace, '--ignore-not-found=true'], check=False)
                        time.sleep(5)
                    except Exception:
                        pass
                    rresult = self._run_epp_variant(
                        prereq_mgr, arch, base_cfg, concurrency, 'smart-refined', refined,
                        base_epp=base, baseline_ttft=None,
                    )
                    if rresult:
                        arch_results.append(('smart-refined', refined, rresult))
                else:
                    self.log("  Metrics confirm derived weights — no refinement needed", 'success')

            # A/B guardrail: if smart-derived is worse than Step 6/7 baseline, test balanced
            if (baseline_ttft and arch_results and not self._should_stop()):
                best_smart = min(self._epp_objective(r[2]) for r in arch_results)
                if best_smart > baseline_ttft * 1.05:
                    self.log(f"\n  ⚠️  Smart TTFT ({best_smart:.0f}ms) > baseline ({baseline_ttft:.0f}ms) — testing balanced fallback", 'warning')
                    try:
                        self.orchestrator.deployment_manager.kubectl.run(
                            ['delete', 'lws', '-l', 'component=serveit-test',
                             '-n', self.config.namespace, '--ignore-not-found=true'], check=False)
                        time.sleep(5)
                    except Exception:
                        pass
                    balanced_w = {'prefix_cache_weight': 2.0, 'kv_cache_weight': 2.0, 'queue_weight': 2.0, 'slo_enabled': False}
                    fb_result = self._run_epp_variant(
                        prereq_mgr, arch, base_cfg, concurrency, 'balanced-fallback', balanced_w,
                        base_epp=base, baseline_ttft=None,
                    )
                    if fb_result:
                        arch_results.append(('balanced-fallback', balanced_w, fb_result))

            self.epp_benchmark_results[arch] = arch_results

            valid_arch = [r for r in arch_results if r[2] is not None]
            if valid_arch:
                best_name = min(valid_arch, key=lambda x: self._epp_objective(x[2]))[0]
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
            best_name, best_weights, best_result = min(valid, key=lambda x: self._epp_objective(x[2]))

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
                    baseline_ttft = self._epp_objective(best_result)

            epp_ttft = self._epp_objective(best_result)
            if baseline_ttft and epp_ttft >= baseline_ttft:
                self.log(f"  ⏩ Keeping original preset for {arch} — smart-derived TTFT {epp_ttft:.0f}ms >= baseline {baseline_ttft:.0f}ms", 'info')
                continue

            lora_str = ''
            if best_weights.get('lora_affinity_enabled') is False:
                lora_str = ', lora-affinity=off'
            elif best_weights.get('lora_affinity_weight') is not None:
                lora_str = f", lora-affinity={best_weights['lora_affinity_weight']}"
            self.log(f"  Applying best EPP config for {arch}: {best_name} "
                     f"(cache={best_weights['prefix_cache_weight']}, kv={best_weights['kv_cache_weight']}, "
                     f"queue={best_weights['queue_weight']}{lora_str}) — TTFT {epp_ttft:.0f}ms vs baseline {baseline_ttft:.0f}ms", 'success')
            epp_config = {
                'preset': 'custom',
                'plugins': self._build_variant_plugins(best_weights),
            }

            # PERSIST the winning profile on the run config so all
            # subsequent tests (sweeps, calibrated load) use the tuned
            # weights. Without this, the next test's deploy_prereqs
            # re-derives auto weights from scratch and overwrites the
            # EPP tuning result.
            self.config.epp_preset = 'custom'
            self.config.epp_custom_enabled = True
            self.config.epp_config = epp_config
            self.config.epp_auto_weights = {
                'prefix_cache_weight': best_weights.get('prefix_cache_weight', 3.0),
                'kv_cache_weight': best_weights.get('kv_cache_weight', 0),
                'kv_cache_enabled': (best_weights.get('kv_cache_weight', 0) or 0) > 0,
                'queue_weight': best_weights.get('queue_weight', 2.0),
                'active_request_weight': best_weights.get('active_request_weight', 0),
                'active_request_enabled': (best_weights.get('active_request_weight', 0) or 0) > 0,
                'lora_affinity_enabled': bool(best_weights.get('lora_affinity_weight')),
                'lora_affinity_weight': best_weights.get('lora_affinity_weight', 0),
                'session_aware_enabled': True,
                'session_aware_weight': best_weights.get('session_aware_weight', 5.0),
            }
            self.log("  💾 Tuned EPP profile persisted — subsequent tests will use "
                     "it instead of re-deriving auto weights", 'success')

            from core import PrereqManager
            mgr = PrereqManager(
                namespace=self.config.namespace,
                kubectl_runner=self.orchestrator.deployment_manager.kubectl,
                scheduler_image=getattr(self.config, 'scheduler_image', None)
            )
            mgr.update_epp_config(arch, epp_config, log_callback=self.log)

        # Only update global config if a non-baseline winner was actually applied above
        # (the per-arch loop skips architectures where EPP didn't beat baseline)

