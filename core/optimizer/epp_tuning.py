"""Step 9: EPP tuning — routing architecture search."""

from typing import Dict, Optional, Tuple


from core.config_generator import TestConfig
from core.optimizer.config import FeasibleSplit
from core.template_manager import TemplateManager


def _has_prefix_reuse(cfg) -> bool:
    """True when the workload has real prefix sharing (synthetic prefix
    buckets), even without a configured cache hit percentage."""
    if float(getattr(cfg, "prefix_tokens", 0) or 0) > 0:
        return True
    return any(float(w.get("prefix_tokens", 0) or 0) > 0 for w in (getattr(cfg, "parallel_workloads", None) or []))


class EPPTuningMixin:
    """Mixin providing EPP tuning methods for RecipeOptimizer."""

    def _get_best_result_prom(self, arch: str) -> Optional[Dict]:
        """Get Prometheus metrics dict from the best Step 6/7 result from the database."""
        import json as _json

        if not self.db_manager or not self.run_id:
            return None

        step_prefix = "step6-" if arch == "aggregated" else ("step7-ep-" if arch == "ep" else "step7-")
        # Single test runs store their one config under a single-* test id —
        # fall back to it when the recipe steps produced no rows
        prefixes = [step_prefix, "single-"]
        try:
            with self.db_manager.get_connection() as conn:
                row = None
                for prefix in prefixes:
                    row = conn.execute(
                        """
                        SELECT metrics_json FROM test_configurations
                        WHERE run_id = ? AND architecture = ? AND status = 'completed'
                          AND config_name LIKE ? AND ttft_p90 IS NOT NULL AND ttft_p90 > 0
                        ORDER BY ttft_p90 ASC LIMIT 1
                    """,
                        (self.run_id, arch, f"{prefix}%"),
                    ).fetchone()
                    if row and row[0]:
                        break
                if row and row[0]:
                    full = _json.loads(row[0])
                    prom = full.get("prometheus_metrics", {})
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
            return val.get("avg")
        return val if isinstance(val, (int, float)) else None

    def _extract_cache_hit_rate(self, arch: str) -> Optional[float]:
        """Extract actual prefix cache hit rate from Prometheus metrics."""
        prom = self._get_best_result_prom(arch)
        if not prom:
            return None
        hits = self._prom_avg(prom, "vllm_prefix_cache_hits_rate")
        queries = self._prom_avg(prom, "vllm_prefix_cache_queries_rate")
        if hits is not None and queries and queries > 0:
            rate = (hits / queries) * 100
            config_desc = (
                f"TP={self.aggregated_tp}"
                if arch == "aggregated"
                else ("best EP config" if arch == "ep" else "best PD split")
            )
            self.log(f"    Measured {arch} cache hit rate: {rate:.1f}% (from {config_desc})", "info")
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

        kv_pct = self._prom_avg(prom, "vllm_kv_cache_pct")
        requests_waiting = self._prom_avg(prom, "vllm_requests_waiting")
        requests_running = self._prom_avg(prom, "vllm_requests_running")
        queue_time = self._prom_avg(prom, "vllm_queue_time_rate")
        prefill_time = self._prom_avg(prom, "vllm_prefill_time_rate")
        decode_time = self._prom_avg(prom, "vllm_decode_time_rate")

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
            self.log(f"    Measured {arch}: KV cache={kv_pressure:.4f}, queue pressure={queue_pressure:.4f}", "info")

        return kv_pressure, queue_pressure

    def _get_user_baseline_weights(self) -> Dict:
        """Get the user's selected EPP preset weights as the starting point."""
        presets = {
            "balanced": {
                "prefix_cache_weight": 3.0,
                "kv_cache_weight": 2.0,
                "queue_weight": 2.0,
                "active_request_weight": 2.0,
            },
            "cache_optimized": {
                "prefix_cache_weight": 5.0,
                "kv_cache_weight": 1.0,
                "queue_weight": 2.0,
                "active_request_weight": 1.0,
            },
            "queue_balanced": {
                "prefix_cache_weight": 1.0,
                "kv_cache_weight": 1.0,
                "queue_weight": 3.0,
                "active_request_weight": 3.0,
            },
            "latency_aware": {
                "prefix_cache_weight": 3.0,
                "kv_cache_weight": 2.0,
                "queue_weight": 2.0,
                "active_request_weight": 2.0,
            },
        }
        preset = getattr(self.config, "epp_preset", "balanced") or "balanced"
        if preset == "auto":
            # Auto: use the workload-derived weights (computed at prereq time
            # and stored on the config by _derive_auto_epp_weights).
            # IMPORTANT: check the _enabled flags — the weight values for
            # disabled scorers are placeholders (1.0), NOT real weights.
            # Returning the placeholder causes the smart tuner to "enable"
            # the scorer via adjustment, deploying configs the baseline
            # didn't have.
            auto = getattr(self.config, "epp_auto_weights", None)
            if auto:
                return {
                    "prefix_cache_weight": auto.get("prefix_cache_weight", 3.0),
                    "kv_cache_weight": (auto.get("kv_cache_weight", 2.0) if auto.get("kv_cache_enabled") else 0),
                    "queue_weight": auto.get("queue_weight", 2.0),
                    "active_request_weight": (
                        auto.get("active_request_weight", 2.0) if auto.get("active_request_enabled") else 0
                    ),
                }
            # Fallback if auto weights weren't pre-computed (e.g. resume from
            # older config) — derive now
            from core.prereq_manager import PrereqManager

            pm = PrereqManager.__new__(PrereqManager)
            derived = pm._derive_auto_epp_weights(self.config)
            return {
                "prefix_cache_weight": derived["prefix_cache_weight"],
                "kv_cache_weight": derived["kv_cache_weight"],
                "queue_weight": derived["queue_weight"],
                "active_request_weight": derived["active_request_weight"],
            }
        if preset == "custom" and self.config.epp_config:
            plugins = self.config.epp_config.get("plugins", {})
            # Absent scorers are NOT configured — return 0, not a placeholder
            # default. A placeholder would seed the weighted archetypes with
            # scorers the user deliberately left out of the custom config
            # (same bug class the auto branch's comment warns about).
            return {
                "prefix_cache_weight": float(plugins.get("prefix_cache", {}).get("weight", 0) or 0),
                "kv_cache_weight": float(plugins.get("kv_cache", {}).get("weight", 0) or 0),
                "queue_weight": float(plugins.get("queue", {}).get("weight", 0) or 0),
                "active_request_weight": float(plugins.get("active_request", {}).get("weight", 0) or 0),
            }
        return presets.get(preset, presets["balanced"])

    def _compute_smart_epp_weights(self, num_pods: int = 1, arch: str = "aggregated") -> Optional[Dict]:
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
            if getattr(self.config, "objective", "") == "single_test":
                # Single test mode skips calibration (Steps 2-3) — no TPSG
                # data is expected. max_seqs falls back to 256.
                self.log("  ℹ️  No TPSG calibration data (single test mode) — using max_seqs fallback", "info")
            else:
                self.log(
                    "🚨 BUG: TPSG data missing — Steps 2-3 should always produce this. "
                    f"prefill_tpsg={prefill_tpsg}, decode_tpsg={decode_tpsg}",
                    "error",
                )

        tp = self.optimal_decode_tp.tp if self.optimal_decode_tp else 1
        max_seqs = self._compute_max_num_seqs(tp) or 256

        # Get user's baseline weights as starting point
        base_w = self._get_user_baseline_weights()
        w_prefix = base_w["prefix_cache_weight"]
        w_kv = base_w["kv_cache_weight"]
        w_queue = base_w["queue_weight"]
        w_active = base_w["active_request_weight"]
        preset_name = getattr(self.config, "epp_preset", "balanced") or "balanced"

        self.log(f"  Smart EPP Weight Refinement ({arch}, {num_pods} pods):", "info")
        self.log(f"    Starting from user preset: {preset_name} ({w_prefix}:{w_kv}:{w_queue}:{w_active})", "info")

        # Measure actual metrics
        measured_hit_rate = self._extract_cache_hit_rate(arch)
        cache_pct = measured_hit_rate if measured_hit_rate is not None else (self.config.prefix_cache_hit_pct or 0)
        cache_source = "measured" if measured_hit_rate is not None else "configured"
        kv_pressure, queue_pressure = self._extract_kv_and_queue_metrics(arch)

        prom = self._get_best_result_prom(arch)
        active_running = self._prom_avg(prom, "vllm_requests_running") if prom else None
        active_load = min(active_running / max(max_seqs, 1), 1.0) if active_running and active_running > 0 else 0

        # Compute adjustment signals (-1 to +1 range)
        # Prefix: high cache hit rate with diverse prompts → increase
        cache_mode = getattr(self.config, "prefix_cache_mode", "identical") or "identical"
        if cache_mode == "identical":
            cache_routing_value = 0.1  # routing doesn't help for identical
        elif cache_mode == "shared_prefix":
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
        w_kv = max(0.1, min(5, w_kv + adj_kv)) if w_kv > 0 else 0
        w_queue = max(0.1, min(5, w_queue + adj_queue))
        w_active = max(0.1, min(5, w_active + adj_active)) if w_active > 0 else 0

        has_sla = getattr(self.config, "latency_constraint_enabled", False)

        self.log(
            f"    Cache hit: {cache_pct:.0f}% ({cache_source}), mode={cache_mode}, routing_value={cache_routing_value:.1f} → prefix adj={adj_prefix:+d}",
            "info",
        )
        self.log(f"    KV pressure: {kv_pressure:.4f} → kv adj={adj_kv:+d}", "info")
        self.log(f"    Queue pressure: {queue_pressure:.4f} → queue adj={adj_queue:+d}", "info")
        self.log(f"    Active load: {active_load:.4f} → active adj={adj_active:+d}", "info")
        self.log(f"    → Weights: prefix={w_prefix}, kv={w_kv}, queue={w_queue}, active={w_active}", "success")

        return {
            "prefix_cache_weight": float(w_prefix),
            "kv_cache_weight": float(w_kv),
            "queue_weight": float(w_queue),
            "active_request_weight": float(w_active),
            "slo_enabled": has_sla,
            "slo_weight": 3.0 if has_sla else 0,
        }

    def _refine_epp_from_metrics(
        self, test_result, arch: str = "aggregated", base_weights: Dict = None
    ) -> Optional[Dict]:
        """Refine EPP weights from the smart-derived test's own Prometheus metrics.

        After running with smart-derived weights, reads the test's actual metrics
        and applies a second round of ±1 adjustments. Uses the smart-derived
        weights as the starting point (not the user preset).
        """
        if not test_result:
            return None

        import json

        test_id = getattr(test_result, "test_id", None)
        prom = None
        if self.db_manager and self.run_id and test_id:
            try:
                with self.db_manager.get_connection() as conn:
                    row = conn.execute(
                        "SELECT metrics_json, test_config_json FROM test_configurations WHERE config_name = ? AND run_id = ?",
                        (test_id, self.run_id),
                    ).fetchone()
                    if row and row[0]:
                        full = json.loads(row[0])
                        prom = full.get("prometheus_metrics", {})
            except Exception:
                pass
        if not prom:
            return None

        max_seqs = self._compute_max_num_seqs(self.optimal_decode_tp.tp if self.optimal_decode_tp else 1) or 256

        hits = self._prom_avg(prom, "vllm_prefix_cache_hits_rate")
        queries = self._prom_avg(prom, "vllm_prefix_cache_queries_rate")
        actual_hit_pct = (hits / queries * 100) if hits is not None and queries and queries > 0 else 0
        kv_pressure = self._prom_avg(prom, "vllm_kv_cache_pct") or 0
        requests_waiting = self._prom_avg(prom, "vllm_requests_waiting") or 0
        requests_running = self._prom_avg(prom, "vllm_requests_running") or 0
        total_reqs = requests_running + requests_waiting
        queue_pressure = (requests_waiting / total_reqs) if total_reqs > 0 else 0
        active_load = requests_running / max(max_seqs, 1)

        if actual_hit_pct > 0:
            self.log(f"    Measured cache hit rate: {actual_hit_pct:.1f}%", "info")
        self.log(
            f"    Measured KV cache: {kv_pressure:.4f}, queue pressure: {queue_pressure:.4f}, active load: {active_load:.4f}",
            "info",
        )

        # Get pod count for damping
        num_pods = 1
        if arch == "pd" and self.pareto_results:
            best_split = min(self.pareto_results, key=lambda x: x[1].ttft_p90 if x[1].ttft_p90 else 1e9)[0]
            num_pods = best_split.prefill_pods + best_split.decode_pods
        elif arch == "aggregated" and self.aggregated_tp:
            num_pods = self.config.total_gpus // (self.aggregated_tp * (self.aggregated_pp or 1))

        # Compute adjustment signals from the test's own metrics
        cache_mode = getattr(self.config, "prefix_cache_mode", "identical") or "identical"
        cache_routing_value = 0.1 if cache_mode == "identical" else (0.3 if cache_mode == "shared_prefix" else 0.7)
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
        w_prefix = max(0.1, min(5, bw["prefix_cache_weight"] + adj_prefix))
        w_kv = max(0.1, min(5, bw["kv_cache_weight"] + adj_kv)) if bw["kv_cache_weight"] > 0 else 0
        w_queue = max(0.1, min(5, bw["queue_weight"] + adj_queue))
        w_active = (
            max(0.1, min(5, bw.get("active_request_weight", 2) + adj_active))
            if bw.get("active_request_weight", 2) > 0
            else 0
        )

        self.log(f"    → Refined weights: prefix={w_prefix}, kv={w_kv}, queue={w_queue}, active={w_active}", "success")

        result = {
            "prefix_cache_weight": float(w_prefix),
            "kv_cache_weight": float(w_kv),
            "queue_weight": float(w_queue),
            "active_request_weight": float(w_active),
            "slo_enabled": False,
        }

        # LoRA-affinity feedback: read the test's own adapter-routing
        # telemetry (vllm_lora_adapter_info) and adjust the weight by the
        # regime the measurements actually show:
        #   churn high            -> capacity problem, weight can't help
        #   full residency + low
        #   affinity              -> signal not biting, raise the weight
        #   high affinity + hot-pod
        #   waiting queues        -> over-concentration, lower the weight
        if getattr(self.config, "lora_enabled", False):
            lora_info = prom.get("vllm_lora_adapter_info") or {}
            aff = lora_info.get("adapter_pod_affinity") or {}
            lpods = lora_info.get("pods") or {}
            if aff and lpods:
                total = len(aff)
                conc = sum(1 for v in aff.values() if len(v) == 1)
                affinity_pct = 100.0 * conc / max(total, 1)
                churn_vals = [p.get("residency_set_changes", 0) for p in lpods.values()]
                avg_churn = sum(churn_vals) / max(len(churn_vals), 1)
                waiting = sum(len(p.get("waiting_adapters_last", []) or []) for p in lpods.values())
                self.log(
                    f"    LoRA routing: affinity={affinity_pct:.0f}% single-pod, "
                    f"churn={avg_churn:.0f}/pod, waiting-queue={waiting}",
                    "info",
                )
                auto_w = getattr(self.config, "epp_auto_weights", None) or {}
                cur_w = float(auto_w.get("lora_affinity_weight", 3.0) or 3.0)
                if avg_churn > 20:
                    self.log(
                        "    → adapter churn high — residency is thrashing (capacity); "
                        "lora-affinity weight cannot fix this, keeping",
                        "warning",
                    )
                elif affinity_pct < 50 and cur_w < 5:
                    result["lora_affinity_weight"] = min(5.0, cur_w + 1.0)
                    self.log(
                        f"    → full residency but affinity low — signal not biting; "
                        f"lora-affinity {cur_w} → {result['lora_affinity_weight']}",
                        "info",
                    )
                elif affinity_pct > 90 and waiting > max(2, total // 4):
                    result["lora_affinity_weight"] = max(1.0, cur_w - 1.0)
                    self.log(
                        f"    → over-concentration (hot-pod adapter waiting queues); "
                        f"lora-affinity {cur_w} → {result['lora_affinity_weight']}",
                        "info",
                    )

        return result

    def _epp_objective(self, test_result) -> float:
        """Objective for comparing EPP variants.

        Latency-constrained runs optimize the constrained percentile so the
        search targets what the SLO measures. LoRA runs optimize TTFT p99 —
        adapter swap-ins are a tail event (misrouted requests pay the
        CPU→GPU swap-in tax) and vanish from the p90. Other runs keep p90.

        Guidellm's TTFT measurement is broken for some EPP configs (count=0,
        all zeros — the EPP's response handler batches chunks in a way that
        hides the first-token boundary). When that happens, fall back to the
        Prometheus TTFT so the search ranks on real latency instead of a
        phantom 0.0ms winner.
        """
        if not test_result:
            return float("inf")

        if getattr(self.config, "lora_enabled", False):
            ttft = test_result.ttft_p99 or test_result.ttft_p90
            if not ttft or ttft <= 0:
                ttft = self._prom_ttft_fallback(test_result)
            return ttft or float("inf")
        if getattr(self.config, "latency_constraint_enabled", False):
            pct = getattr(self.config, "latency_constraint_percentile", "p90") or "p90"
            ttft = getattr(test_result, f"ttft_{pct}", None) or test_result.ttft_p90
            if not ttft or ttft <= 0:
                ttft = self._prom_ttft_fallback(test_result)
            return ttft or float("inf")
        ttft = test_result.ttft_p90
        if not ttft or ttft <= 0:
            ttft = self._prom_ttft_fallback(test_result)
        return ttft or float("inf")

    def _epp_tokens_per_second(self, test_result) -> Optional[float]:
        """Total token throughput (prompt + generation).

        Prefers the window-exact guidellm value (total tokens over the
        benchmark's own elapsed window) — computed in-hand, so it works
        even before the DB row lands. Falls back to Prometheus prompt +
        generation rate averages (diluted by collection-window idle —
        only roughly comparable) when guidellm reported nothing.
        """
        in_hand = getattr(test_result, "tokens_per_second", None)
        if in_hand and in_hand > 0:
            return in_hand
        import json as _json

        test_id = getattr(test_result, "test_id", None)
        if not test_id or not self.db_manager or not self.run_id:
            return None
        try:
            with self.db_manager.get_connection() as conn:
                row = conn.execute(
                    "SELECT metrics_json FROM test_configurations WHERE config_name = ? AND run_id = ?",
                    (test_id, self.run_id),
                ).fetchone()
            if not row or not row[0]:
                return None
            _mj = _json.loads(row[0])
            _stored = _mj.get("tokens_per_second")
            if _stored and _stored > 0:
                return _stored
            prom = _mj.get("prometheus_metrics", {})
            prompt = self._prom_avg(prom, "vllm_prompt_tokens_rate") or 0
            gen = self._prom_avg(prom, "vllm_generation_tokens_rate") or 0
            total = prompt + gen
            return total if total > 0 else None
        except Exception:
            return None

    def _prom_ttft_fallback(self, test_result) -> Optional[float]:
        """Read the Prometheus TTFT p90 (seconds) from a variant's own metrics.

        Returns milliseconds or None. Used when guidellm's TTFT is broken
        (count=0) so the search still ranks on real latency.
        """
        import json as _json

        test_id = getattr(test_result, "test_id", None)
        if not test_id or not self.db_manager or not self.run_id:
            return None
        try:
            with self.db_manager.get_connection() as conn:
                row = conn.execute(
                    "SELECT metrics_json FROM test_configurations WHERE config_name = ? AND run_id = ?",
                    (test_id, self.run_id),
                ).fetchone()
            if not row or not row[0]:
                return None
            prom = _json.loads(row[0]).get("prometheus_metrics", {})
            val = prom.get("vllm_ttft_p90")
            if isinstance(val, dict):
                val = val.get("avg")
            if val and val > 0:
                return val * 1000.0  # seconds -> milliseconds
        except Exception:
            pass
        return None

    def _epp_screen_duration(self, base_cfg) -> int:
        """Short-run duration for screening/ablation/weight phases.

        Ranking variants among themselves only needs a stable TTFT
        distribution under constant load; the winners get re-validated at
        full duration in the final phase.
        """
        full = int(getattr(base_cfg, "test_duration", 300) or 300)
        screen = int(getattr(self.config, "epp_screen_seconds", 0) or 0)
        if screen <= 0:
            screen = max(60, min(150, full // 2))
        return max(30, min(screen, full))

    def _epp_max_evals(self) -> int:
        return int(getattr(self.config, "epp_max_evals", 0) or 0) or 12

    def _run_epp_spec(
        self,
        prereq_mgr,
        arch: str,
        base_cfg,
        concurrency: int,
        spec,
        base_epp: Dict = None,
        baseline_ttft: float = None,
        screen: bool = False,
    ):
        """Deploy one EPP spec variant, benchmark it, save to DB.

        Spec-driven: the configmap is rendered from the structured spec
        (picker, filter params, producers, scorer weights) rather than the
        flat legacy weight dict. Returns the TestResult on success.
        """
        from core.optimizer.epp_search import EPPSpec

        if isinstance(spec, dict):
            spec = EPPSpec.from_dict(spec)
        base_epp = base_epp or {}
        merged_base = {**base_epp, **{k: v for k, v in spec.base_epp.items() if v is not None}}
        spec = spec.copy(base_epp=merged_base).resolve()[0]

        test_id = f"step11-epp-{arch}-{spec.name}"
        duration = (
            self._epp_screen_duration(base_cfg) if screen else int(getattr(base_cfg, "test_duration", 300) or 300)
        )
        self.log(f"  Testing: {spec.name} [{spec.phase}] — {spec.describe()}", "info")
        if spec.rationale:
            self.log(f"    why: {spec.rationale}", "info")

        epp_cfg = spec.to_epp_config()

        success = prereq_mgr.update_epp_config(
            architecture=arch, epp_config=epp_cfg, log_callback=lambda msg: self.log(msg, "info")
        )
        if not success:
            self.log(f"  ❌ Failed to update EPP config for {spec.name}", "error")
            return None

        epp_test_config = TestConfig(
            test_id=test_id,
            architecture=base_cfg.architecture,
            model_name=base_cfg.model_name,
            namespace=base_cfg.namespace,
            isl=base_cfg.isl,
            osl=base_cfg.osl,
            num_users=concurrency,
            parallel_workloads=getattr(base_cfg, "parallel_workloads", None),
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
            test_duration=duration,
            workload_mode=base_cfg.workload_mode,
            dataset_source=base_cfg.dataset_source,
            block_size=base_cfg.block_size,
            prefix_tokens=getattr(base_cfg, "prefix_tokens", None),
            prefix_count=getattr(base_cfg, "prefix_count", None),
            isl_min=getattr(base_cfg, "isl_min", None),
            isl_max=getattr(base_cfg, "isl_max", None),
            osl_min=getattr(base_cfg, "osl_min", None),
            osl_max=getattr(base_cfg, "osl_max", None),
            network_type=base_cfg.network_type,
            nccl_ib_hca=base_cfg.nccl_ib_hca,
            rdma_device_resources=getattr(base_cfg, "rdma_device_resources", None),
            rdma_nics_per_node=base_cfg.rdma_nics_per_node,
            memory_request=base_cfg.memory_request,
            memory_limit=base_cfg.memory_limit,
            cpu_request=base_cfg.cpu_request,
            cpu_limit=base_cfg.cpu_limit,
            lora_enabled=base_cfg.lora_enabled,
            lora_num_adapters=base_cfg.lora_num_adapters,
            lora_benchmark_mode=getattr(base_cfg, "lora_benchmark_mode", "round_robin"),
            lora_adapter_size_mb=base_cfg.lora_adapter_size_mb,
            lora_max_loras=getattr(base_cfg, "lora_max_loras", None),
            lora_max_rank=getattr(base_cfg, "lora_max_rank", None),
            max_num_seqs=base_cfg.max_num_seqs,
            prefill_max_num_seqs=base_cfg.prefill_max_num_seqs,
            decode_max_num_seqs=base_cfg.decode_max_num_seqs,
            max_num_batched_tokens=base_cfg.max_num_batched_tokens,
            prefill_gpu_memory_utilization=base_cfg.prefill_gpu_memory_utilization,
            decode_gpu_memory_utilization=base_cfg.decode_gpu_memory_utilization,
            selected_nodes=base_cfg.selected_nodes,
            prefill_decode_ratio=base_cfg.prefill_decode_ratio,
            epp_config=epp_cfg,
            selected_dra_classes=getattr(base_cfg, "selected_dra_classes", None)
            or getattr(self.config, "selected_dra_classes", []),
            dra_gpu_resource_key=getattr(base_cfg, "dra_gpu_resource_key", None)
            or getattr(self.config, "dra_gpu_resource_key", None),
            gateway_class=getattr(base_cfg, "gateway_class", None) or getattr(self.config, "gateway_class", "istio"),
            per_node_storage=getattr(base_cfg, "per_node_storage", None)
            or getattr(self.config, "per_node_storage", False),
            node_nfs_pvcs=getattr(base_cfg, "node_nfs_pvcs", None) or getattr(self.config, "node_nfs_pvcs", []),
            storage_class=getattr(base_cfg, "storage_class", None) or getattr(self.config, "storage_class", None),
            pvc_size=getattr(base_cfg, "pvc_size", None) or getattr(self.config, "pvc_size", None),
            local_disk_path=getattr(base_cfg, "local_disk_path", None) or getattr(self.config, "local_disk_path", None),
            rdma_network_annotation=getattr(base_cfg, "rdma_network_annotation", None)
            or getattr(self.config, "rdma_network_annotation", None),
            exclusive_pf=getattr(base_cfg, "exclusive_pf", False) or getattr(self.config, "exclusive_pf", False),
            scheduler_image=getattr(base_cfg, "scheduler_image", None) or getattr(self.config, "scheduler_image", None),
        )

        # Only skip deployment if model pods are actually running — after a
        # resume (previous EPP tuning cleaned up), the pods are gone and the
        # variant gets 503 on the smoke test.
        _pods_running = False
        try:
            _lbl = f"llm-d.ai/test-id={base_cfg.test_id}"
            _r = self.orchestrator.deployment_manager.kubectl.run(
                [
                    "get",
                    "pods",
                    "-n",
                    self.orchestrator.namespace,
                    "-l",
                    _lbl,
                    "-o",
                    'jsonpath={.items[?(@.status.phase=="Running")].metadata.name}',
                ],
                check=False,
                timeout=10,
            )
            _pods_running = bool(_r.returncode == 0 and _r.stdout.strip())
        except Exception:
            pass
        if not _pods_running:
            self.log(f"  📦 No running pods for {base_cfg.test_id} — deploying for EPP variant", "info")

        result = self.orchestrator.run_test(
            epp_test_config,
            cleanup=False,
            skip_deploy=_pods_running,
            log_callback=lambda msg: self.log(msg, "info"),
            stop_check=self._should_stop,
        )

        if not (result and result.guidellm_success):
            self.log(f"  ❌ {spec.name}: benchmark failed", "error")
            return None

        obj = self._epp_objective(result)
        obj_label = "p99" if getattr(self.config, "lora_enabled", False) else "p90"
        tput = result.throughput_p90 or 0
        tps = self._epp_tokens_per_second(result) or 0
        if baseline_ttft:
            if obj > baseline_ttft * 1.5:
                self.log(
                    f"  ⚠️  {spec.name}: TTFT {obj:.0f}ms is {obj / baseline_ttft:.1f}x worse than "
                    f"baseline ({baseline_ttft:.0f}ms) — routing degraded performance",
                    "warning",
                )
            else:
                improvement = ((baseline_ttft - obj) / baseline_ttft * 100) if baseline_ttft else 0
                self.log(
                    f"  ✅ {spec.name}: TTFT {obj_label}={obj:.1f}ms ({improvement:+.1f}% vs baseline), "
                    f"{tps:.0f} tok/s, {tput:.2f} req/s, duration={duration}s",
                    "success",
                )
        else:
            self.log(
                f"  ✅ {spec.name}: TTFT {obj_label}={obj:.1f}ms, {tps:.0f} tok/s, {tput:.2f} req/s, duration={duration}s",
                "success",
            )

        self.all_test_results.append((epp_test_config, result))

        # Archive the exact configmap this variant ran (rendered through the
        # same shared context builder update_epp_config uses).
        try:
            import json as _json
            from core import PrereqManager

            tmgr = TemplateManager()
            ctx = PrereqManager.build_epp_template_context(
                arch,
                epp_cfg,
                scheduler_image=getattr(self.config, "scheduler_image", None),
                namespace=self.config.namespace,
            )
            if ctx:
                cm_yaml = tmgr.render_template(f"prereq/gaie-configmap-{arch}.yaml.j2", **ctx)
                manifests = {"epp-configmap": cm_yaml}
                lws_manifests = tmgr.render_config(base_cfg)
                for mk, mv in lws_manifests.items():
                    if "service" not in mk:
                        manifests[mk] = mv
                epp_test_config._epp_manifests = _json.dumps(manifests)
        except Exception:
            epp_test_config._epp_manifests = None
        self._save_epp_test_to_database(epp_test_config, result)
        return result

    def _epp_best_entry(self, entries):
        """Pick the best (name, weights, result) entry.

        Full-duration validations (phase='final') supersede the screening
        number of the same variant — screening runs are short and only
        meant for ranking.
        """
        finals = [e for e in entries if (e[1] or {}).get("phase") == "final"]
        pool = finals or entries
        return min(pool, key=lambda x: self._epp_objective(x[2]))

    def _epp_search_context(self, arch: str, arch_pods: int, base_cfg=None):
        """Assemble the workload facts the search generators reason over."""
        from core.optimizer.epp_search import SearchContext

        cfg = self.config
        isl = int(getattr(cfg, "isl", 1000) or 1000)
        osl = int(getattr(cfg, "osl", 100) or 100)
        if isl > 4 * max(osl, 1):
            bound = "prefill"
        elif osl > 4 * max(isl, 1):
            bound = "decode"
        else:
            bound = "balanced"
        base_epp = self._build_epp_config() or {}
        user_epp = getattr(cfg, "epp_config", None) or {}
        for k in ("maxPrefixBlocksToMatch", "lruCapacityPerServer", "nonCachedTokens"):
            if user_epp.get(k) is not None:
                base_epp[k] = user_epp[k]
        prefill_pods = None
        if arch in ("pd", "ep") and base_cfg is not None:
            prefill_pods = int(getattr(base_cfg, "prefill_replicas", 0) or 0) or None
        peak = self._measure_peak_prefill_throughput(arch, arch_pods, prefill_pods=prefill_pods)
        if user_epp.get("peakPrefillThroughput"):
            peak = float(user_epp["peakPrefillThroughput"])
        smart = dict(self._compute_smart_epp_weights(num_pods=arch_pods, arch=arch) or {})
        # Fold the Auto derivation's binary enable decisions and affinity
        # weights into the smart weights the archetype builders read from
        # (lora-affinity weight, session-aware, no-hit-lru, token-load).
        auto = dict(getattr(cfg, "epp_auto_weights", None) or {})
        for key in ("lora_affinity_weight", "session_aware_weight", "no_hit_lru_weight", "token_load_weight"):
            if auto.get(key) and not smart.get(key):
                smart[key] = auto[key]
        session_aware = bool(auto.get("session_aware_enabled")) or int(getattr(cfg, "turns", 1) or 1) > 1
        # A forced custom config is the control arm and parameter source
        from core.optimizer.epp_search import spec_from_custom_config

        user_spec = None
        aff_params = {}
        if (getattr(cfg, "epp_preset", "") or "") == "custom" and user_epp:
            user_spec = spec_from_custom_config(user_epp, arch)
            for k in ("affinityThreshold", "maxTTFTPenaltyMs", "explorationProbability"):
                if user_epp.get(k) is not None:
                    aff_params[k] = user_epp[k]
        return SearchContext(
            arch=arch,
            num_pods=arch_pods,
            workload_bound=bound,
            cache_mode=getattr(cfg, "prefix_cache_mode", "identical") or "identical",
            cache_hit_pct=(
                float(getattr(cfg, "prefix_cache_hit_pct", 0) or 0) or (50.0 if _has_prefix_reuse(cfg) else 0.0)
            ),
            session_aware=session_aware,
            lora_enabled=bool(getattr(cfg, "lora_enabled", False)),
            slo_enabled=bool(getattr(cfg, "latency_constraint_enabled", False)),
            smart_weights=smart,
            user_weights=self._get_user_baseline_weights(),
            user_spec=user_spec,
            affinity_filter_params=aff_params,
            peak_prefill_throughput=peak,
            base_epp=base_epp,
        ), peak

    def _measure_peak_prefill_throughput(
        self, arch: str, num_pods: int, prefill_pods: Optional[int] = None
    ) -> Optional[float]:
        """Derive per-pod peak prefill throughput — never a static value.

        The prefix-cache-affinity-filter estimates a sticky endpoint's TTFT
        as inFlightTokens / peakPrefillThroughput, so the parameter must
        reflect this deployment's hardware/model. Priority chain:

        1. benchmark Prometheus: per-pod prompt token rate divided by the
           prefill duty cycle (the token rate the pod sustains *while
           prefilling*)
        2. TP calibration TPSG (tokens/s/GPU, available from step 2/3
           before any arch search) × deployment TP × headroom
        3. None — omit the parameter and let the EPP's built-in default
           apply (15928, calibrated for Qwen3-32B/H100/TP2)

        The prompt-rate Prometheus query excludes decode pods, so for
        pd/ep the divisor is the prefill pod count, not the total.
        """
        # 1. benchmark-derived
        prom = self._get_best_result_prom(arch)
        if prom:
            prompt_rate = self._prom_avg(prom, "vllm_prompt_tokens_rate")
            prefill_time = self._prom_avg(prom, "vllm_prefill_time_rate")
            decode_time = self._prom_avg(prom, "vllm_decode_time_rate")
            div = prefill_pods or num_pods
            if prompt_rate and prompt_rate > 0 and div > 0:
                per_pod = prompt_rate / div
                duty = 0.3
                if prefill_time and decode_time is not None and (prefill_time + decode_time) > 0:
                    duty = max(prefill_time / (prefill_time + decode_time), 0.1)
                est = max(1000.0, min(100000.0, per_pod / duty))
                self.log(
                    f"    peakPrefillThroughput ≈ {est:.0f} tok/s/pod "
                    f"(prompt {per_pod:.0f} tok/s/pod ÷ prefill duty {duty:.0%})",
                    "info",
                )
                return est

        # 2. TP calibration TPSG × deployment TP
        tp_obj = getattr(self, "optimal_prefill_tp", None)
        dep_tp = None
        if arch == "aggregated" and getattr(self, "aggregated_tp", None):
            dep_tp = self.aggregated_tp
        elif tp_obj and tp_obj.tp:
            dep_tp = tp_obj.tp
        if tp_obj and getattr(tp_obj, "tpsg", None) and dep_tp:
            # calibration runs below unloaded peak — small headroom
            est = max(1000.0, min(100000.0, tp_obj.tpsg * dep_tp * 1.15))
            self.log(
                f"    peakPrefillThroughput ≈ {est:.0f} tok/s/pod "
                f"(TP calibration {tp_obj.tpsg:.0f} tok/s/GPU × TP{dep_tp} × 1.15 headroom)",
                "info",
            )
            return est

        self.log(
            "    ⚠️ peakPrefillThroughput not measurable (no benchmark metrics, "
            "no TP calibration) — the EPP built-in default applies "
            "(15928 tok/s, calibrated for other hardware)",
            "warning",
        )
        return None

    def _epp_filter_diagnosis(self, test_result) -> Optional[Dict]:
        """Read the affinity-filter decision telemetry from a variant's own run.

        The EPP counts every filter decision by outcome (sticky /
        no_match / load_override); the mix says whether the filter is
        healthy, starved, or fighting overload:

        - sticky high, load_override low → working as designed
        - no_match high → nothing to be sticky to (threshold too high or
          no prefix reuse) → loosen
        - load_override high → sticky set saturates, the TTFT gate keeps
          breaking → widen the sticky set (or peakPrefillThroughput is
          underestimated)
        """
        import json as _json

        if not test_result or not self.db_manager or not self.run_id:
            return None
        test_id = getattr(test_result, "test_id", None)
        if not test_id:
            return None
        try:
            with self.db_manager.get_connection() as conn:
                row = conn.execute(
                    "SELECT metrics_json FROM test_configurations WHERE config_name = ? AND run_id = ?",
                    (test_id, self.run_id),
                ).fetchone()
            if not row or not row[0]:
                return None
            full = _json.loads(row[0])
            return (full.get("prometheus_metrics") or {}).get("epp_affinity_decisions")
        except Exception:
            return None

    def _ordered_filter_threshold_moves(self, winner: "object", diag: Optional[Dict]):
        """Order the affinity-threshold moves by the measured outcome mix."""
        from core.optimizer.epp_search import _affinity_filter_neighbors

        moves = _affinity_filter_neighbors(winner)
        thr_moves = [m for m in moves if m.name.startswith("w-thr-")]
        other = [m for m in moves if not m.name.startswith("w-thr-")]
        if not thr_moves or not diag or not diag.get("shares"):
            return thr_moves + other

        shares = diag["shares"]
        no_match = shares.get("no_match", 0)
        load_override = shares.get("load_override", 0)
        sticky = shares.get("sticky", 0)
        if no_match + load_override > 0.5:
            # starved or fighting overload → loosen first (0.6 before 0.9)
            self.log(
                f"    filter telemetry: sticky={sticky:.0%}, no_match={no_match:.0%}, "
                f"load_override={load_override:.0%} → loosening threshold first",
                "info",
            )
            thr_moves.sort(key=lambda m: 0 if "0p6" in m.name else 1)
        else:
            # healthy stickiness → try tightening for more cache hits
            self.log(
                f"    filter telemetry: sticky={sticky:.0%}, no_match={no_match:.0%}, "
                f"load_override={load_override:.0%} → tightening threshold first",
                "info",
            )
            thr_moves.sort(key=lambda m: 0 if "0p9" in m.name else 1)
        return thr_moves + other

    def _calibrated_token_load_params(self, test_result) -> Dict:
        """Calibrate token-load-scorer's queueThresholdTokens from the run.

        The scorer saturates (score → 0) at queueThresholdTokens and is
        near-flat well below it; the 4 Mi default dwarfs typical in-flight
        token scales (~50K/pod), compressing scores into a sliver of [0,1]
        where they are invisible next to other scorers. Setting the
        threshold to ~2× the observed peak keeps the score range
        meaningful.
        """
        import json as _json

        if not test_result or not self.db_manager or not self.run_id:
            return {}
        test_id = getattr(test_result, "test_id", None)
        if not test_id:
            return {}
        try:
            with self.db_manager.get_connection() as conn:
                row = conn.execute(
                    "SELECT metrics_json FROM test_configurations WHERE config_name = ? AND run_id = ?",
                    (test_id, self.run_id),
                ).fetchone()
            if not row or not row[0]:
                return {}
            prom = _json.loads(row[0]).get("prometheus_metrics") or {}
        except Exception:
            return {}
        peak = prom.get("epp_inflight_tokens_max")
        if isinstance(peak, dict):
            peak = peak.get("max") or peak.get("avg")
        if not peak or peak <= 0:
            return {}
        threshold = int(max(262144, min(16777216, float(peak) * 2)))
        self.log(
            f"    token-load calibration: observed peak {float(peak):.0f} in-flight tokens/pod "
            f"→ queueThresholdTokens {threshold} (2× peak, clamped [256K, 16Mi])",
            "info",
        )
        return {"queueThresholdTokens": threshold}

    def _run_epp_search(self, prereq_mgr, arch: str, base_cfg, concurrency: int, arch_pods: int):
        """Four-phase EPP routing search for one architecture.

        Phase 1 (screen): benchmark the routing archetypes — the llm-d
        optimized-baseline composition (hard affinity filter + single load
        scorer), the weighted multi-scorer stack with each picker, and the
        user's preset as control — on short runs.

        Phase 2 (ablate): add/remove workload-relevant scorers on the
        screening winner (session affinity, no-hit-lru, kv-cache, LoRA
        affinity A/B) — still short runs.

        Phase 3 (weight): coordinate-descent moves on the winner's highest
        impact dimensions (or filter threshold/load weight for the filter
        composition), informed by the measured metrics refinement.

        Phase 4 (final): re-validate the best variant at full duration so
        the comparison against the step 6/7 baseline is apples-to-apples.

        Returns the (name, weights, result) tuples for
        epp_benchmark_results[arch].
        """
        from core.optimizer import epp_search as es

        ctx, peak = self._epp_search_context(arch, arch_pods, base_cfg=base_cfg)
        base_epp = ctx.base_epp
        baseline_ttft = None
        if arch == "aggregated" and self.aggregated_result:
            baseline_ttft = self.aggregated_result.ttft_p90
        elif arch == "pd" and self.pareto_results:
            best_pd = min(self.pareto_results, key=lambda x: x[1].ttft_p99 or x[1].ttft_p90 or 1e9)
            baseline_ttft = best_pd[1].ttft_p90
        elif arch == "ep" and getattr(self, "best_ep_result", None):
            baseline_ttft = self.best_ep_result.ttft_p90

        max_evals = self._epp_max_evals()
        evals = 0
        arch_results = []
        seen_hashes = set()

        def _budget_left():
            # reserve the last eval for the full-duration validation
            return evals < max_evals - 1 and not self._should_stop()

        def _run(spec, screen=True):
            nonlocal evals
            resolved = spec.resolve()[0]
            h = resolved.hash_key()
            if h in seen_hashes:
                return None
            seen_hashes.add(h)
            evals += 1
            result = self._run_epp_spec(
                prereq_mgr,
                arch,
                base_cfg,
                concurrency,
                resolved,
                base_epp=base_epp,
                baseline_ttft=(baseline_ttft if not screen else None),
                screen=screen,
            )
            if result:
                arch_results.append((resolved.name, resolved.legacy_weights(), result))
            return result

        def _best_so_far():
            valid = [r for r in arch_results if r[2] is not None]
            if not valid:
                return None
            return self._epp_best_entry(valid)

        # Full-duration validation supersedes the short screening number of
        # the same variant: screening ranks candidates, the final run is the
        # one comparable against the step 6/7 baseline.
        # Implemented in _epp_best_entry (prefer phase='final' entries).

        # -- Phase 1: archetype screening --------------------------------
        p1 = es.phase1_specs(ctx)
        self.log(
            f"  Phase 1/4 — archetype screening ({len(p1)} candidates, "
            f"{self._epp_screen_duration(base_cfg)}s runs): "
            f"{', '.join(s.name for s in p1)}",
            "decision",
        )
        for spec in p1:
            if not _budget_left():
                break
            _run(spec)

        winner_entry = _best_so_far()
        if not winner_entry:
            self.log("  ⚠️  No screening variant produced a result — aborting EPP search", "warning")
            return arch_results
        winner_spec = es.EPPSpec.from_dict(winner_entry[1]["spec"])

        # -- Phase 2: workload-driven ablation ----------------------------
        ablations = es.ablation_candidates(winner_spec, ctx)[:2]
        if ablations and _budget_left():
            self.log(f"  Phase 2/4 — ablation on '{winner_spec.name}' ({len(ablations)} candidates)", "decision")
            for spec in ablations:
                if not _budget_left():
                    break
                _run(spec)

        winner_entry = _best_so_far() or winner_entry
        winner_spec = es.EPPSpec.from_dict(winner_entry[1]["spec"])

        # -- Phase 3: weight / parameter moves -----------------------------
        moves = es.weight_neighbors(winner_spec)
        if winner_spec.affinity_filter:
            # Filter compositions reason from the filter's own telemetry
            # (decision outcome mix), not from scorer-weight signals.
            diag = self._epp_filter_diagnosis(winner_entry[2])
            moves = self._ordered_filter_threshold_moves(winner_spec, diag)
            tl_params = self._calibrated_token_load_params(winner_entry[2])
            if tl_params and "token_load" in winner_spec.scorers:
                calibrated = winner_spec.copy(
                    name="w-tl-calibrated",
                    phase="weight",
                    rationale="queueThresholdTokens calibrated from the measured in-flight token peak (2× headroom)",
                    scorer_params={**winner_spec.scorer_params, "token_load": tl_params},
                ).resolve()[0]
                if calibrated.hash_key() != winner_spec.hash_key():
                    moves.insert(0, calibrated)
        refined = self._refine_epp_from_metrics(winner_entry[2], arch=arch, base_weights=None)
        if refined and not winner_spec.affinity_filter:
            scorer_view = {}
            for key, wkey in (
                ("prefix_cache", "prefix_cache_weight"),
                ("kv_cache", "kv_cache_weight"),
                ("queue", "queue_weight"),
                ("active_request", "active_request_weight"),
            ):
                w = float(refined.get(wkey) or 0)
                if w > 0:
                    scorer_view[key] = w
            if refined.get("lora_affinity_weight") is not None and ctx.lora_enabled:
                scorer_view["lora_affinity"] = float(refined["lora_affinity_weight"])
            elif refined.get("lora_affinity_enabled") is False:
                scorer_view.pop("lora_affinity", None)
            if scorer_view and scorer_view != winner_spec.scorers:
                moves.insert(
                    0,
                    winner_spec.copy(
                        name="w-refined",
                        phase="weight",
                        rationale="second-round ±1 refinement from the measured metrics of the current winner",
                        scorers=scorer_view,
                    ).resolve()[0],
                )
        if moves and _budget_left():
            moves = moves[:2]
            self.log(f"  Phase 3/4 — weight/parameter moves (testing {', '.join(m.name for m in moves)})", "decision")
            for spec in moves:
                if not _budget_left():
                    break
                _run(spec)

        # -- Phase 4: full-duration validation -----------------------------
        winner_entry = _best_so_far() or winner_entry
        winner_spec = es.EPPSpec.from_dict(winner_entry[1]["spec"])
        if winner_entry[2] is not None and winner_spec.phase != "final":
            if evals < max_evals and not self._should_stop():
                self.log(f"  Phase 4/4 — full-duration validation of '{winner_spec.name}'", "decision")
                final = es.final_spec(winner_spec)
                final_result = self._run_epp_spec(
                    prereq_mgr,
                    arch,
                    base_cfg,
                    concurrency,
                    final,
                    base_epp=base_epp,
                    baseline_ttft=baseline_ttft,
                    screen=False,
                )
                if final_result:
                    arch_results.append((final.name, final.legacy_weights(), final_result))
            else:
                self.log(
                    "  ⏩ Eval budget exhausted — keeping short-run result as the winner (not full-duration validated)",
                    "warning",
                )

        valid = [r for r in arch_results if r[2] is not None]
        if valid:
            best_name, best_w, _ = self._epp_best_entry(valid)
            self.log(
                f"  Best {arch}: {best_name} ({len(valid)} variants tested, "
                f"{evals} evals) — {es.EPPSpec.from_dict(best_w['spec']).describe()}",
                "success",
            )
        return arch_results

    def _benchmark_epp_strategies(self):
        """Step 9: EPP Tuning — smart weight derivation + validation.

        Derives EPP weights mathematically from calibration data (1 test),
        then optionally refines from measured metrics (1 more test on OpenShift).
        Falls back to preset sweep if calibration data is unavailable.
        """
        if not self.config.epp_benchmark:
            return

        self.log("\n" + "=" * 80, "info")
        self.log("STEP 9: EPP Tuning (Routing Architecture Search)", "decision")
        self.log("=" * 80, "info")
        self.log(
            f"  Search: 4 phases (archetype screening → ablation → weight moves →"
            f" full-duration validation), budget {self._epp_max_evals()} evals/arch",
            "info",
        )

        # Collect best configs per architecture from DB (resume-safe)
        configs_to_test = []
        default_concurrency = int(self.config.qps)
        if hasattr(self, "effective_concurrency") and self.effective_concurrency:
            default_concurrency = self.effective_concurrency

        import json as _json

        def _best_from_db(arch, step_prefix):
            """Find best config for an architecture from DB test results."""
            if not self.db_manager or not self.run_id:
                return None
            try:
                with self.db_manager.get_connection() as conn:
                    rows = conn.execute(
                        """
                        SELECT config_name, tensor_parallelism, decode_tp, prefill_pods, decode_pods,
                               ttft_p90, ttft_p99, throughput_p90, test_config_json
                        FROM test_configurations
                        WHERE run_id = ? AND architecture = ? AND status = 'completed'
                          AND config_name LIKE ? AND ttft_p90 IS NOT NULL AND ttft_p90 > 0
                        ORDER BY ttft_p90 ASC
                        LIMIT 1
                    """,
                        (self.run_id, arch, f"{step_prefix}%"),
                    ).fetchall()
                return rows[0] if rows else None
            except Exception:
                return None

        # Best PD config from step7 DB results (single-test runs store their
        # one config under a single-* id — fall back to it)
        pd_row = _best_from_db("pd", "step7-") or _best_from_db("pd", "single-")
        if pd_row:
            tc_raw = pd_row[8]
            tc = _json.loads(tc_raw) if tc_raw else {}
            split = FeasibleSplit(
                prefill_pods=tc.get("prefill_replicas", pd_row[3]),
                decode_pods=tc.get("decode_replicas", pd_row[4]),
                prefill_tp=tc.get("prefill_tp", pd_row[1]),
                decode_tp=tc.get("decode_tp", pd_row[2] or pd_row[1]),
                prefill_gpus=tc.get("prefill_replicas", pd_row[3]) * tc.get("prefill_tp", pd_row[1]),
                decode_gpus=tc.get("decode_replicas", pd_row[4]) * tc.get("decode_tp", pd_row[2] or pd_row[1]),
                total_gpus=self.config.total_gpus,
                prefill_pct=0,
            )
            pd_cfg = self._create_pd_config(split)
            configs_to_test.append(("pd", pd_cfg, default_concurrency))
            self.log(
                f"  PD: {split.prefill_pods}P×TP{split.prefill_tp} + {split.decode_pods}D×TP{split.decode_tp} at c={default_concurrency}",
                "info",
            )

        # Best Aggregated config from step6 DB results (single-test fallback)
        agg_row = _best_from_db("aggregated", "step6-") or _best_from_db("aggregated", "single-")
        if agg_row:
            agg_tp = agg_row[1]
            agg_pp = None
            agg_num_gpus = self.config.total_gpus
            tc_raw = agg_row[8]
            if tc_raw:
                try:
                    _tc = _json.loads(tc_raw)
                    agg_pp = _tc.get("pipeline_parallel_size")
                    # reuse the tested deployment's shape — redeploying the
                    # search at max_gpus re-loads models nobody benchmarked
                    _reps = _tc.get("replicas")
                    if _reps and agg_tp:
                        agg_num_gpus = int(_reps) * int(agg_tp) * int(agg_pp or 1)
                except Exception:
                    pass
            agg_cfg = self._create_aggregated_config(
                tp=agg_tp,
                num_gpus=agg_num_gpus,
                isl=self.config.isl,
                osl=self.config.osl,
                test_id="step11-epp-aggregated",
                use_concurrency=True,
                pipeline_parallel_size=agg_pp or 1,
            )
            configs_to_test.append(("aggregated", agg_cfg, default_concurrency))
            pp_str = f"xPP={agg_pp} " if agg_pp and agg_pp > 1 else ""
            self.log(
                f"  Aggregated: {self.config.total_gpus // (agg_tp * (agg_pp or 1))}×TP{agg_tp}{pp_str}at c={default_concurrency}",
                "info",
            )

        # Best EP config from step7-ep DB results (single-test fallback)
        ep_row = _best_from_db("ep", "step7-ep-") or _best_from_db("ep", "single-")
        if ep_row:
            tc_raw = ep_row[8]
            tc = _json.loads(tc_raw) if tc_raw else {}
            ep_split = FeasibleSplit(
                prefill_pods=tc.get("prefill_replicas", ep_row[3]),
                decode_pods=tc.get("decode_replicas", ep_row[4]),
                prefill_tp=tc.get("prefill_tp", ep_row[1]),
                decode_tp=tc.get("decode_tp", ep_row[2] or ep_row[1]),
                prefill_gpus=tc.get("prefill_replicas", ep_row[3]) * tc.get("prefill_tp", ep_row[1]),
                decode_gpus=tc.get("decode_replicas", ep_row[4]) * tc.get("decode_tp", ep_row[2] or ep_row[1]),
                total_gpus=self.config.total_gpus,
                prefill_pct=0,
            )
            ep_cfg = self._create_ep_config(ep_split)
            configs_to_test.append(("ep", ep_cfg, default_concurrency))
            self.log(
                f"  EP: {ep_split.prefill_pods}P+{ep_split.decode_pods}D "
                f"PTP={ep_split.prefill_tp} DTP={ep_split.decode_tp} at c={default_concurrency}",
                "info",
            )

        # Skip single-pod configs — EPP routing is meaningless with 1 backend
        filtered = []
        for arch, cfg, conc in configs_to_test:
            total_pods = 1
            if arch == "aggregated" and agg_row:
                agg_tp = agg_row[1]
                total_pods = self.config.total_gpus // agg_tp if agg_tp else 1
            elif arch == "pd" and pd_row:
                total_pods = pd_row[3] + pd_row[4]
            elif arch == "ep" and ep_row:
                total_pods = ep_row[3] + ep_row[4]
            if total_pods <= 1:
                self.log(f"  ⏩ Skipping {arch.upper()} EPP tuning — single pod, nothing to route", "info")
                if not self.epp_benchmark_results:
                    self.epp_benchmark_results = {}
                self.epp_benchmark_results[arch] = [("_skipped_single_pod", {}, None)]
                continue
            filtered.append((arch, cfg, conc))
        configs_to_test = filtered

        if not configs_to_test:
            self.log("⚠️  No multi-pod configs for EPP tuning — skipping", "warning")
            return

        if not self.epp_benchmark_results:
            self.epp_benchmark_results = {}

        # The auto-derived weights (lora-affinity, demoted load scorers) are
        # stored on the TEST config by deploy_prereqs — the run config never
        # sees them, so variants would fall back to a slot-blind re-derivation
        # and silently drop the affinity scorers. Adopt the most recent
        # deployed test's weights before building any variant.
        if not getattr(self.config, "epp_auto_weights", None):
            for cfg, _r in reversed(self.all_test_results):
                if getattr(cfg, "epp_auto_weights", None):
                    self.config.epp_auto_weights = cfg.epp_auto_weights
                    self.log(
                        "  📋 Adopted auto EPP weights from the deployed test config "
                        "(lora-affinity and demotions carry into variants)",
                        "info",
                    )
                    break

        from core import PrereqManager

        prereq_mgr = PrereqManager(
            namespace=self.config.namespace,
            kubectl_runner=self.orchestrator.deployment_manager.kubectl,
            scheduler_image=getattr(self.config, "scheduler_image", None),
        )

        for arch_idx, (arch, base_cfg, concurrency) in enumerate(configs_to_test):
            if self._should_stop():
                break

            if arch in self.epp_benchmark_results and self.epp_benchmark_results[arch]:
                # The EPP search is only complete when the full-duration
                # validation (phase='final') ran. If the run was stopped
                # mid-search, the partial results are restored but the
                # search should CONTINUE from where it left off, not skip.
                has_final = any((entry[1] or {}).get("phase") == "final" for entry in self.epp_benchmark_results[arch])
                if has_final:
                    self.log(
                        f"\n  --- EPP Tuning: {arch.upper()} — already completed (resumed from DB), skipping ---",
                        "info",
                    )
                    continue
                else:
                    self.log(
                        f"\n  --- EPP Tuning: {arch.upper()} — search was interrupted mid-way "
                        f"({len(self.epp_benchmark_results[arch])} variants done, no final validation). "
                        f"Continuing search... ---",
                        "info",
                    )

            self.log(f"\n  --- EPP Tuning: {arch.upper()} (c={concurrency}) ---", "decision")

            # Compute per-architecture pod count from DB rows (resume-safe)
            arch_pods = 1
            if arch == "pd" and pd_row:
                arch_pods = pd_row[3] + pd_row[4]
            elif arch == "aggregated" and agg_row:
                agg_tp_val = agg_row[1]
                arch_pods = self.config.total_gpus // agg_tp_val if agg_tp_val else 1
                if agg_cfg is not None:
                    arch_pods = max(1, (agg_cfg.replicas or arch_pods))
            elif arch == "ep" and ep_row:
                arch_pods = ep_row[3] + ep_row[4]

            prom_data = self._get_best_result_prom(arch)
            if not prom_data:
                self.log(
                    f"  ⚠️  Skipping {arch.upper()} EPP tuning — no Prometheus metrics available from Step 6/7",
                    "warning",
                )
                self.log("     Check that Thanos/Prometheus is accessible and metrics are being scraped", "warning")
                self.epp_benchmark_results[arch] = [("_skipped_no_metrics", {}, None)]
                continue

            # For ≤3 pods, smart weight derivation adds noise — use preset as-is
            if arch_pods <= 3:
                self.log(
                    f"  ✅ {arch.upper()}: {arch_pods} pods — using preset weights (too few pods for smart derivation)",
                    "success",
                )
                self.epp_benchmark_results[arch] = [("_skipped_few_pods", self._get_user_baseline_weights(), None)]
                continue

            self.epp_benchmark_results[arch] = self._run_epp_search(prereq_mgr, arch, base_cfg, concurrency, arch_pods)

            # Clean up the reused deployment after all variants for this arch
            self.log("  🧹 Cleaning up deployment after EPP tuning variants", "info")
            try:
                self.orchestrator.deployment_manager.delete_deployment(
                    base_cfg.test_id, base_cfg.architecture, log_callback=lambda msg: self.log(msg, "info")
                )
                self.orchestrator.deployment_manager.wait_for_pods_terminated(
                    base_cfg.test_id, timeout=300, log_callback=lambda msg: self.log(msg, "info")
                )
            except Exception:
                pass

    def _epp_tuned_config_for(self, arch: str, best_weights: Dict) -> Dict:
        """Rebuild the tuned epp_config for later steps (step 10 re-tests).

        Prefers the stored spec (picker, filter params, producers) and falls
        back to reconstructing a weighted spec from a legacy flat weight
        dict (old DB rows).
        """
        from core.optimizer.epp_search import EPPSpec

        spec_dict = (best_weights or {}).get("spec")
        if spec_dict:
            spec = EPPSpec.from_dict(spec_dict)
            if spec.arch != arch:
                spec = spec.copy(arch=arch)
            return spec.to_epp_config()
        scorers = {}
        for key, wkey in (
            ("prefix_cache", "prefix_cache_weight"),
            ("kv_cache", "kv_cache_weight"),
            ("queue", "queue_weight"),
            ("active_request", "active_request_weight"),
            ("token_load", "token_load_weight"),
            ("lora_affinity", "lora_affinity_weight"),
        ):
            w = float((best_weights or {}).get(wkey) or 0)
            if w > 0:
                scorers[key] = w
        return EPPSpec(name="tuned", arch=arch, phase="final", scorers=scorers).to_epp_config()

    def _apply_best_epp_config(self):
        """After EPP tuning, deploy the best-performing EPP config for subsequent steps.
        Only applies if the best EPP result beats the Step 6/7 baseline."""
        if not self.epp_benchmark_results:
            return
        from core.optimizer.epp_search import EPPSpec

        for arch, results in self.epp_benchmark_results.items():
            valid = [r for r in results if r[2] is not None]
            if not valid:
                continue
            best_name, best_weights, best_result = self._epp_best_entry(valid)

            # Compare against Step 6/7 baseline — only apply if EPP result is better
            baseline_ttft = None
            if arch == "aggregated" and self.aggregated_result:
                baseline_ttft = self.aggregated_result.ttft_p90
            elif arch == "pd" and self.pareto_results:
                best_pd = min(self.pareto_results, key=lambda x: x[1].ttft_p90 if x[1].ttft_p90 else 1e9)
                baseline_ttft = best_pd[1].ttft_p90
            elif arch == "ep" and getattr(self, "best_ep_result", None):
                baseline_ttft = self.best_ep_result.ttft_p90
            if not baseline_ttft:
                prom = self._get_best_result_prom(arch)
                if not prom:
                    baseline_ttft = self._epp_objective(best_result)

            epp_ttft = self._epp_objective(best_result)
            if baseline_ttft and epp_ttft >= baseline_ttft:
                self.log(
                    f"  ⏩ Keeping original preset for {arch} — tuned TTFT {epp_ttft:.0f}ms >= baseline {baseline_ttft:.0f}ms",
                    "info",
                )
                continue

            spec_dict = best_weights.get("spec")
            if spec_dict:
                spec = EPPSpec.from_dict(spec_dict)
                describe = spec.describe()
                epp_config = spec.to_epp_config()
            else:
                # Legacy weight-dict entry (restored from an old DB row):
                # reconstruct a weighted-max spec from the flat weights.
                describe = (
                    f"cache={best_weights.get('prefix_cache_weight')}, "
                    f"kv={best_weights.get('kv_cache_weight')}, "
                    f"queue={best_weights.get('queue_weight')}"
                )
                scorers = {}
                for key, wkey in (
                    ("prefix_cache", "prefix_cache_weight"),
                    ("kv_cache", "kv_cache_weight"),
                    ("queue", "queue_weight"),
                    ("active_request", "active_request_weight"),
                    ("token_load", "token_load_weight"),
                    ("lora_affinity", "lora_affinity_weight"),
                ):
                    w = float(best_weights.get(wkey) or 0)
                    if w > 0:
                        scorers[key] = w
                spec = EPPSpec(name=best_name, arch=arch, phase="final", scorers=scorers)
                epp_config = spec.to_epp_config()

            self.log(
                f"  Applying best EPP config for {arch}: {best_name} "
                f"({describe}) — TTFT {epp_ttft:.0f}ms vs baseline {baseline_ttft:.0f}ms",
                "success",
            )

            # PERSIST the winning profile on the run config so all
            # subsequent tests (sweeps, calibrated load) use the tuned
            # config. Without this, the next test's deploy_prereqs
            # re-derives auto weights from scratch and overwrites the
            # EPP tuning result. User can disable via epp_apply_tuned.
            if getattr(self.config, "epp_apply_tuned", getattr(self.config, "concurrency_sweep_use_epp_tuned", True)):
                self.config.epp_preset = "custom"
                self.config.epp_custom_enabled = True
                self.config.epp_config = epp_config
                auto_w = dict(getattr(self.config, "epp_auto_weights", None) or {})
                auto_w.update(
                    {
                        "prefix_cache_weight": best_weights.get("prefix_cache_weight", 3.0) or 0,
                        "kv_cache_weight": best_weights.get("kv_cache_weight", 0) or 0,
                        "kv_cache_enabled": (best_weights.get("kv_cache_weight", 0) or 0) > 0,
                        "queue_weight": best_weights.get("queue_weight", 2.0) or 0,
                        "active_request_weight": best_weights.get("active_request_weight", 0) or 0,
                        "active_request_enabled": (best_weights.get("active_request_weight", 0) or 0) > 0,
                        "lora_affinity_enabled": bool(best_weights.get("lora_affinity_weight")),
                        "lora_affinity_weight": best_weights.get("lora_affinity_weight", 0) or 0,
                    }
                )
                if spec_dict:
                    auto_w["spec"] = spec_dict
                    if spec.affinity_filter:
                        auto_w["prefix_cache_affinity_enabled"] = True
                        auto_w["peak_prefill_throughput"] = spec.affinity_filter.get("peakPrefillThroughput")
                self.config.epp_auto_weights = auto_w
                self.log(
                    "  💾 Tuned EPP profile persisted — subsequent tests will use "
                    "it instead of re-deriving auto weights",
                    "success",
                )
            else:
                self.log(
                    "  ⏩ Tuned profile NOT persisted (disabled by user) — "
                    "subsequent tests will re-derive auto weights",
                    "info",
                )

            from core import PrereqManager

            mgr = PrereqManager(
                namespace=self.config.namespace,
                kubectl_runner=self.orchestrator.deployment_manager.kubectl,
                scheduler_image=getattr(self.config, "scheduler_image", None),
            )
            mgr.update_epp_config(arch, epp_config, log_callback=self.log)
