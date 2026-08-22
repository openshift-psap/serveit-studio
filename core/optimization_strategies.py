"""
Optimization strategies for different goals.

Each strategy implements Steps 4-10 of the recipe-based optimization,
tailored to its specific optimization goal:
- TTFTStrategy: Response Time Priority (Aggregated search + PD splits)
- ThroughputStrategy: Throughput Priority (Aggregated search + EP configs)
- BalancedStrategy: Balanced Performance (Aggregated search + PD + EP)
- AggregatedOnlyStrategy: Test only aggregated (standard) configurations
- PDOnlyStrategy: Test only Prefill/Decode disaggregation splits
- EPOnlyStrategy: Test only Expert Parallelism configurations

Step 6 (Aggregated Configuration Search) runs before architecture-specific
testing, so that Step 8 comparisons require no additional tests.
"""

import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .recipe_optimizer import RecipeOptimizer

logger = logging.getLogger(__name__)


class OptimizationStrategy(ABC):
    """Base class for optimization strategies.

    Each strategy receives a reference to the RecipeOptimizer and
    orchestrates Steps 4-9 for its specific optimization goal.
    """

    def __init__(self, optimizer: 'RecipeOptimizer'):
        self.opt = optimizer

    @abstractmethod
    def execute(self):
        """Execute Steps 4-9 for this optimization goal."""
        pass

    def _run_epp_tuning_if_enabled(self):
        """Step 9: EPP Tuning — runs before Step 10 so latency search uses optimal EPP weights."""
        if not getattr(self.opt.config, 'epp_custom_enabled', True):
            return
        if self.opt.config.epp_benchmark and not self.opt._should_stop():
            self.opt._benchmark_epp_strategies()
            self.opt._apply_best_epp_config()

    def _run_speculative_if_enabled(self):
        """Step 12: Speculative decoding comparison."""
        if self.opt._should_run_speculative() and not self.opt._should_stop():
            self.opt._run_speculative_comparison()

    def _run_cache_sweep_if_enabled(self):
        """Step 13: Cache hit sweep."""
        if (self.opt.config.cache_sweep_enabled or self.opt.config.cache_sweep_use_calibrated) and not self.opt._should_stop():
            self.opt.log("STEP 13: Cache Hit Sweep", 'decision')
            self.opt.log("-" * 80, 'info')
            self.opt._run_cache_hit_sweep()
            self.opt.log("", 'info')


class TTFTStrategy(OptimizationStrategy):
    """Response Time Priority: Aggregated search + PD disaggregation.

    Steps 4-5: Calculate feasible P/D splits from TP calibration data
    Step 6: Search for best aggregated configuration
    Step 7: Test all feasible PD splits, find Pareto front
    Step 8: Compare PD vs Aggregated (no new tests)
    Step 9: EPP Tuning (conditional)
    Step 10: Latency-bounded throughput maximization (conditional)
    Step 11: Re-test at calibrated load if overloaded
    """

    def execute(self):
        # Select TP pairs to test (supports asymmetric prefill/decode TP)
        self.opt._select_tp_pairs()

        # Steps 4-5: Calculate feasible splits
        self.opt.log("STEPS 4-5: Resource Sizing & Feasible Splits", 'decision')
        self.opt.log("-" * 80, 'info')
        self.opt._calculate_feasible_splits()
        self.opt.log("", 'info')
        if self.opt._should_stop():
            return

        # Step 6: Aggregated configuration search
        self.opt.log("STEP 6: Aggregated Configuration Search", 'decision')
        self.opt.log("-" * 80, 'info')
        self.opt._search_aggregated_configs()
        self.opt.log("", 'info')
        if self.opt._should_stop():
            return

        # Step 7: Find optimal P/D split
        self.opt.log("STEP 7: P/D Split Optimization (Pareto Front)", 'decision')
        self.opt.log("-" * 80, 'info')
        self.opt._optimize_pd_splits()
        self.opt.log("", 'info')
        if self.opt._should_stop():
            return

        # Step 8: Compare PD vs Aggregated (no new tests)
        self.opt.log("STEP 8: PD vs Aggregated Comparison", 'decision')
        self.opt.log("-" * 80, 'info')
        self.opt._validate_pd_vs_aggregated()
        self.opt.log("", 'info')
        if self.opt._should_stop():
            return

        # Step 9: EPP Tuning (before latency search so it uses optimal EPP)
        self._run_epp_tuning_if_enabled()

        # Step 10: Latency-bounded throughput maximization (only if enabled)
        if self.opt._should_run_latency_bounded_search():
            self.opt._run_latency_bounded_search()
            self.opt.log("", 'info')

        # Recalculate achievable concurrency from actual Step 7 throughput
        self.opt._recalculate_achievable_concurrency()

        # Step 11: Calibrated load / Concurrency sweep (user-controlled)
        if self.opt.config.calibrated_load_enabled and len(self.opt.pareto_results) > 0:
            self.opt._recalculate_achievable_concurrency()
            self.opt.log("STEP 11: Calibrated Load Validation", 'decision')
            self.opt.log("-" * 80, 'info')
            self.opt._validate_at_calibrated_load()
            self.opt.log("", 'info')

        # Step 12: Speculative decoding comparison (conditional)
        self._run_speculative_if_enabled()

        # Step 13: Cache hit sweep (user-controlled)
        self._run_cache_sweep_if_enabled()


class ThroughputStrategy(OptimizationStrategy):
    """Throughput Priority: Aggregated search + EP (Expert Parallelism).

    Steps 4-5: Enumerate EP configurations (TP x replicas)
    Step 6: Search for best aggregated configuration
    Step 7: Test EP configs at full workload, find best by throughput
    Step 8: Compare EP vs Aggregated (no new tests)
    Step 9: EPP Tuning (conditional)
    Step 10: Latency-bounded throughput maximization (conditional)
    Step 11: Re-test at calibrated load if overloaded
    """

    def execute(self):
        # Steps 4-5: Calculate EP configurations
        self.opt.log("STEPS 4-5: EP Configuration Space", 'decision')
        self.opt.log("-" * 80, 'info')
        self._calculate_ep_configs()
        self.opt.log("", 'info')
        if self.opt._should_stop():
            return

        # Step 6: Aggregated configuration search
        self.opt.log("STEP 6: Aggregated Configuration Search", 'decision')
        self.opt.log("-" * 80, 'info')
        self.opt._search_aggregated_configs()
        self.opt.log("", 'info')
        if self.opt._should_stop():
            return

        # Step 7: Test EP configurations
        self.opt.log("STEP 7: EP Configuration Testing (Throughput Optimization)", 'decision')
        self.opt.log("-" * 80, 'info')
        self._test_ep_configs()
        self.opt.log("", 'info')
        if self.opt._should_stop():
            return

        # Step 8: Compare EP vs Aggregated (no new tests)
        self.opt.log("STEP 8: EP vs Aggregated Comparison", 'decision')
        self.opt.log("-" * 80, 'info')
        self._validate_ep_vs_aggregated()
        self.opt.log("", 'info')
        if self.opt._should_stop():
            return

        # Step 9: EPP Tuning (before latency search so it uses optimal EPP)
        self._run_epp_tuning_if_enabled()

        # Step 10: Latency-bounded throughput maximization (only if enabled)
        if self.opt._should_run_latency_bounded_search():
            self.opt._run_latency_bounded_search()
            self.opt.log("", 'info')

        # Step 11: Calibrated load / Concurrency sweep (user-controlled)
        if self.opt.config.calibrated_load_enabled and len(self.opt.ep_results) > 0:
            self.opt._recalculate_achievable_concurrency()
            self.opt.log("STEP 11: Calibrated Load Validation", 'decision')
            self.opt.log("-" * 80, 'info')
            self._validate_ep_at_calibrated_load()
            self.opt.log("", 'info')

        # Step 12: Speculative decoding comparison (conditional)
        self._run_speculative_if_enabled()

        # Step 13: Cache hit sweep (user-controlled)
        self._run_cache_sweep_if_enabled()

    def _calculate_ep_configs(self):
        """Steps 4-5: Calculate EP configuration space using PD-style splits.

        EP uses the same prefill/decode split structure as PD, with asymmetric
        TP support. Each split has separate prefill_tp, decode_tp, and pod counts.
        """
        from .optimizer.config import FeasibleSplit

        total_gpus = self.opt.config.total_gpus
        valid_tp = self.opt._get_valid_tp_options()
        # Add multi-node TP options for EP (e.g., TP16 across 2 nodes)
        if self.opt.cluster_resources and self.opt.cluster_resources.gpu_node_count >= 2:
            multi_tp = self.opt.cluster_resources.get_multi_node_tp_options()
            for tp in multi_tp:
                if tp <= total_gpus and tp not in valid_tp:
                    valid_tp.append(tp)
            valid_tp.sort()

        prefill_tpsg = self.opt.optimal_prefill_tp.tpsg if self.opt.optimal_prefill_tp else 0
        decode_tpsg = self.opt.optimal_decode_tp.tpsg if self.opt.optimal_decode_tp else 0
        if prefill_tpsg > 0 and decode_tpsg > 0:
            raw_prefill_cost = self.opt.config.isl / prefill_tpsg
            decode_cost = self.opt.config.osl / decode_tpsg
            cache_hit_pct = getattr(self.opt.config, 'prefix_cache_hit_pct', 0) or 0
            if cache_hit_pct > 0:
                prefill_cost = raw_prefill_cost * (1.0 - cache_hit_pct / 100.0)
            else:
                prefill_cost = raw_prefill_cost
            total_cost = prefill_cost + decode_cost
            sustainable_qps = total_gpus / total_cost / self.opt.config.headroom
            sustainable_concurrency = max(1, int(total_gpus / (total_cost * self.opt.config.headroom)))
            concurrency = int(self.opt.config.qps)

            self.opt.log("Step 4: Cluster Capacity Analysis (EP)", 'info')
            self.opt.log(f"  Concurrency (simultaneous requests): {concurrency}", 'info')
            self.opt.log("  GPU cost per request:", 'info')
            self.opt.log(f"    Prefill: {self.opt.config.isl} ISL ÷ {prefill_tpsg:.0f} TPSG = {raw_prefill_cost:.2f} GPU-sec", 'info')
            if cache_hit_pct > 0:
                self.opt.log(f"    Prefill (cache-adjusted): {raw_prefill_cost:.2f} × {1.0 - cache_hit_pct/100:.0%} active = {prefill_cost:.2f} GPU-sec ({cache_hit_pct}% cache hit)", 'info')
            self.opt.log(f"    Decode:  {self.opt.config.osl} OSL ÷ {decode_tpsg:.0f} TPSG = {decode_cost:.2f} GPU-sec", 'info')
            self.opt.log(f"    Total:   {total_cost:.2f} GPU-sec/request", 'info')
            self.opt.log("", 'info')

            self.opt.log("Step 5: Sustainable Throughput & EP Configurations", 'info')
            self.opt.log(f"  Available: {total_gpus} GPUs", 'info')
            self.opt.log(f"  Sustainable: {sustainable_concurrency} users ({sustainable_qps:.2f} req/s)", 'info')

            if concurrency > sustainable_concurrency:
                self.opt.sustainable_throughput_rps = sustainable_qps
                self.opt.achievable_concurrency = sustainable_concurrency
                self.opt.log(f"  ⚠️  Load exceeds capacity ({concurrency} > {sustainable_concurrency} users)", 'warning')
                if self.opt.config.use_achievable_qps:
                    self.opt.effective_concurrency = sustainable_concurrency
                    self.opt.log(f"  ✅ Scaling down to {sustainable_concurrency} concurrent users for benchmarks", 'success')
                else:
                    self.opt.effective_concurrency = concurrency
                    self.opt.log(f"  ℹ️  Using original concurrency ({concurrency}) — expect overload", 'info')
            else:
                self.opt.effective_concurrency = concurrency
                self.opt.log(f"  ✅ Cluster can handle the load ({concurrency} users, capacity: {sustainable_concurrency} users)", 'success')
        self.opt.log("", 'info')

        self.opt.log("EP Configurations (PD split):", 'info')
        self.opt.log(f"  Valid TP values: {valid_tp}", 'info')
        self.opt.log(f"  Total GPUs: {total_gpus}", 'info')

        ep_configs = []
        seen = set()
        num_experts = self.opt._num_experts or 0
        allow_asymmetric = getattr(self.opt.config, 'allow_asymmetric_tp', False)
        for prefill_tp in valid_tp:
            for decode_tp in valid_tp:
                if prefill_tp > decode_tp and not allow_asymmetric:
                    continue
                max_prefill_pods = total_gpus // prefill_tp
                for prefill_pods in range(1, max_prefill_pods + 1):
                    prefill_gpus = prefill_tp * prefill_pods
                    remaining = total_gpus - prefill_gpus
                    if remaining < decode_tp:
                        continue
                    decode_pods = remaining // decode_tp
                    if decode_pods < 1:
                        continue
                    decode_gpus = decode_pods * decode_tp
                    # EPLB requires num_experts % ep_ranks == 0 for both roles
                    prefill_ep = prefill_tp * prefill_pods
                    decode_ep = decode_tp * decode_pods
                    if num_experts > 0 and (num_experts % decode_ep != 0 or num_experts % prefill_ep != 0):
                        continue
                    total_used = prefill_gpus + decode_gpus
                    if total_used < total_gpus:
                        continue
                    key = (prefill_tp, decode_tp, prefill_pods, decode_pods)
                    if key in seen:
                        continue
                    seen.add(key)
                    split = FeasibleSplit(
                        prefill_pods=prefill_pods,
                        decode_pods=decode_pods,
                        prefill_tp=prefill_tp,
                        decode_tp=decode_tp,
                        prefill_gpus=prefill_gpus,
                        decode_gpus=decode_gpus,
                        total_gpus=total_used,
                        prefill_pct=(prefill_gpus / total_used) * 100,
                    )
                    ep_configs.append(split)
                    self.opt.log(f"  ✓ PTP={prefill_tp} DTP={decode_tp}: "
                                 f"{prefill_pods}P+{decode_pods}D = {total_used} GPUs "
                                 f"(prefill_EP={prefill_ep}, decode_EP={decode_ep})", 'info')

        self.opt.ep_configs = ep_configs
        self.opt.log(f"\n  EP configs to test: {len(ep_configs)}", 'success')

    def _test_ep_configs(self):
        """Step 7: Test all EP configurations at full workload.

        Tests each EP config (PD-style split) and finds the best by throughput.
        """
        if not self.opt.ep_configs:
            self.opt.log("❌ No EP configs to test!", 'error')
            return

        self.opt.log(f"Testing all {len(self.opt.ep_configs)} EP configurations...", 'info')
        self.opt.log(f"Workload: ISL={self.opt.config.isl}, OSL={self.opt.config.osl}, "
                     f"Concurrency={int(self.opt.effective_concurrency)}", 'info')

        for i, split in enumerate(self.opt.ep_configs):
            if self.opt._should_stop():
                break

            test_id = f"step7-ep-{split.prefill_pods}p{split.decode_pods}d-ptp{split.prefill_tp}-dtp{split.decode_tp}"
            self.opt.log(f"  Test {i + 1}/{len(self.opt.ep_configs)}: "
                         f"PTP={split.prefill_tp} DTP={split.decode_tp}, "
                         f"{split.prefill_pods}P+{split.decode_pods}D ({split.total_gpus} GPUs)", 'info')

            if test_id in self.opt.completed_tests:
                row = self.opt.completed_tests[test_id]
                result = self.opt._make_test_result_from_db(row)
                self.opt.log("    ⏩ Resuming from DB (already completed)", 'info')
            else:
                test_config = self.opt._create_ep_config(split)

                result = self.opt.orchestrator.run_test(
                    test_config,
                    cleanup=True,
                    log_callback=lambda msg: self.opt.log(msg, 'info'),
                    stop_check=self.opt._should_stop
                )

                self.opt.all_test_results.append((test_config, result))
                self.opt._save_test_to_database(test_config, result)
                self.opt._check_pod_errors(test_config, result)
                self.opt._check_request_errors(test_config, result)

                if not result or not result.guidellm_success:
                    self.opt.log(f"    ⚠️  EP test {test_id} failed — skipping this config", 'warning')
                    continue

            ttft = result.ttft_p90 or result.ttft_p50 or 1000000.0
            throughput = result.throughput_mean or result.throughput_p90 or 0.0

            self.opt.log(f"    ✅ TTFT p90: {ttft:.1f}ms, Throughput mean: {throughput:.2f} req/s", 'success')
            self.opt.ep_results.append((split, result))

        if self.opt.ep_results:
            best_split, best_result = max(
                self.opt.ep_results,
                key=lambda x: x[1].throughput_p90 if x[1].throughput_p90 else 0.0
            )
            self.opt.best_ep_config = best_split
            self.opt.best_ep_result = best_result

            best_ttft = best_result.ttft_p90 or best_result.ttft_p50 or 0
            best_tput = best_result.throughput_mean or best_result.throughput_p90 or 0

            self.opt.log("", 'info')
            self.opt.log("✅ Best EP Configuration (by throughput):", 'success')
            self.opt.log(f"  PTP={best_split.prefill_tp} DTP={best_split.decode_tp}, "
                         f"{best_split.prefill_pods}P+{best_split.decode_pods}D ({best_split.total_gpus} GPUs)", 'info')
            self.opt.log(f"  TTFT p90: {best_ttft:.1f}ms, Throughput mean: {best_tput:.2f} req/s", 'info')

    def _validate_ep_vs_aggregated(self):
        """Step 8: Compare best EP config against best Aggregated from Step 6.

        No new tests — uses the best aggregated result already found in Step 6.
        """
        if not self.opt.best_ep_result or not self.opt.best_ep_config:
            self.opt.log("⚠️  No EP results to compare — skipping Step 8", 'warning')
            return

        if not self.opt.aggregated_result:
            self.opt.log("⚠️  No aggregated results to compare — skipping Step 8", 'warning')
            return

        best_cfg = self.opt.best_ep_config
        best_ep_ttft = self.opt.best_ep_result.ttft_p90 or self.opt.best_ep_result.ttft_p50 or 0
        best_ep_tput = self.opt.best_ep_result.throughput_mean or self.opt.best_ep_result.throughput_p90 or 0

        agg_ttft = self.opt.aggregated_result.ttft_p90 or self.opt.aggregated_result.ttft_p50 or 1000000.0
        agg_tput = self.opt.aggregated_result.throughput_mean or self.opt.aggregated_result.throughput_p90 or 0.0

        self.opt.log(f"Best EP: PTP={best_cfg.prefill_tp} DTP={best_cfg.decode_tp}, "
                     f"{best_cfg.prefill_pods}P+{best_cfg.decode_pods}D ({best_cfg.total_gpus} GPUs)", 'info')
        self.opt.log(f"  TTFT p90: {best_ep_ttft:.1f}ms, Throughput mean: {best_ep_tput:.2f} req/s", 'info')
        self.opt.log(f"Best Aggregated: TP={self.opt.aggregated_tp}, "
                     f"{self.opt.aggregated_gpus // self.opt.aggregated_tp} replicas", 'info')
        self.opt.log(f"  TTFT p90: {agg_ttft:.1f}ms, Throughput mean: {agg_tput:.2f} req/s", 'info')
        self.opt.log("", 'info')

        # Compare
        ttft_diff = best_ep_ttft - agg_ttft
        ttft_pct = (ttft_diff / agg_ttft * 100) if agg_ttft > 0 else 0
        tput_diff = best_ep_tput - agg_tput
        tput_pct = (tput_diff / agg_tput * 100) if agg_tput > 0 else 0

        self.opt.log("📊 EP vs Aggregated Comparison:", 'decision')
        self.opt.log(f"  TTFT p90:       EP={best_ep_ttft:.1f}ms vs Agg={agg_ttft:.1f}ms "
                     f"({'EP wins by' if ttft_diff < 0 else 'Agg wins by'} {abs(ttft_pct):.1f}%)", 'info')
        self.opt.log(f"  Throughput mean:  EP={best_ep_tput:.2f} vs Agg={agg_tput:.2f} req/s "
                     f"({'EP wins by' if tput_diff > 0 else 'Agg wins by'} {abs(tput_pct):.1f}%)", 'info')

        if agg_tput > best_ep_tput and agg_ttft <= best_ep_ttft:
            self.opt.log("", 'info')
            self.opt.log("⚡ AGGREGATED IS BETTER — higher throughput and equal/lower TTFT", 'decision')
        elif agg_tput > best_ep_tput:
            self.opt.log("", 'info')
            self.opt.log("⚡ AGGREGATED HAS BETTER THROUGHPUT but higher TTFT — check trade-offs", 'decision')
        else:
            self.opt.log("", 'info')
            self.opt.log("✅ EP CONFIRMED — EP has equal or better throughput than Aggregated", 'decision')

    def _should_run_step10(self) -> bool:
        """Check if Step 11 should run for EP."""
        return (
            self.opt.achievable_concurrency is not None
            and not self.opt.config.use_achievable_qps
            and not self.opt.config.latency_constraint_enabled
            and len(self.opt.ep_results) > 0
        )

    def _validate_ep_at_calibrated_load(self):
        """Step 11: Re-test best EP and Aggregated at calibrated load.

        Steps 7-8 ran at the original QPS which may overload the cluster.
        This step re-runs at a sustainable rate for realistic numbers.
        """
        calibrated_concurrency = self.opt.achievable_concurrency
        best_cfg = self.opt.best_ep_config
        best_ep_result = self.opt.best_ep_result

        overloaded_ttft = best_ep_result.ttft_p90 or best_ep_result.ttft_p50 or 0
        overloaded_tput = best_ep_result.throughput_mean or best_ep_result.throughput_p90 or 0

        self.opt.log(f"Re-testing best EP config at calibrated load ({calibrated_concurrency:.0f} users "
                     f"vs original {self.opt.config.qps:.0f} users)", 'info')
        self.opt.log(f"Best EP: PTP={best_cfg.prefill_tp} DTP={best_cfg.decode_tp}, "
                     f"{best_cfg.prefill_pods}P+{best_cfg.decode_pods}D", 'info')
        self.opt.log(f"  Step 7 results (overloaded): TTFT={overloaded_ttft:.1f}ms, "
                     f"Throughput={overloaded_tput:.2f} req/s", 'info')
        self.opt.log("", 'info')

        test_id = f"step9-ep-{best_cfg.prefill_pods}p{best_cfg.decode_pods}d-ptp{best_cfg.prefill_tp}-dtp{best_cfg.decode_tp}"

        if test_id in self.opt.completed_tests:
            row = self.opt.completed_tests[test_id]
            ep_result = self.opt._make_test_result_from_db(row)
            self.opt.log("  ⏩ EP test: resuming from DB (already completed)", 'info')
        else:
            ep_config = self.opt._create_ep_config(split=best_cfg)
            ep_config.test_id = test_id
            ep_config.num_users = int(calibrated_concurrency)
            ep_config.request_rate = int(calibrated_concurrency)

            ep_result = self.opt.orchestrator.run_test(
                ep_config,
                cleanup=True,
                log_callback=lambda msg: self.opt.log(msg, 'info')
            )

            self.opt.all_test_results.append((ep_config, ep_result))
            self.opt._save_test_to_database(ep_config, ep_result)
            self.opt._check_pod_errors(ep_config, ep_result)
            self.opt._check_request_errors(ep_config, ep_result)

            if not ep_result or not ep_result.guidellm_success:
                self.opt.log("❌ EP calibrated load test failed", 'error')
                return

        cal_ep_ttft = ep_result.ttft_p90 or ep_result.ttft_p50 or 0
        cal_ep_tput = ep_result.throughput_mean or ep_result.throughput_p90 or 0
        self.opt.calibrated_ep_result = ep_result

        self.opt.log(f"  ✅ EP at calibrated load: TTFT={cal_ep_ttft:.1f}ms, "
                     f"Throughput={cal_ep_tput:.2f} req/s", 'success')

        # --- Test Aggregated at calibrated load ---
        if not self.opt.aggregated_tp:
            self.opt.log("⚠️  No aggregated baseline — skipping aggregated re-test", 'warning')
            return
        agg_tp = self.opt.aggregated_tp
        total_gpus = self.opt.aggregated_gpus
        agg_test_id = f"step9-aggregated-tp{agg_tp}"
        # Backwards compat: check old ID format with total_gpus embedded
        if agg_test_id not in self.opt.completed_tests:
            old_id = f"step9-aggregated-{total_gpus}gpu-tp{agg_tp}"
            if old_id in self.opt.completed_tests:
                agg_test_id = old_id

        if agg_test_id in self.opt.completed_tests:
            row = self.opt.completed_tests[agg_test_id]
            agg_result = self.opt._make_test_result_from_db(row)
            self.opt.log("  ⏩ Aggregated test: resuming from DB (already completed)", 'info')
        else:
            agg_config = self.opt._create_aggregated_config(
                tp=agg_tp,
                num_gpus=total_gpus,
                isl=self.opt.config.isl,
                osl=self.opt.config.osl,
                test_id=agg_test_id,
                use_concurrency=True
            )
            agg_config.num_users = int(calibrated_concurrency)
            agg_config.request_rate = int(calibrated_concurrency)

            agg_result = self.opt.orchestrator.run_test(
                agg_config,
                cleanup=True,
                log_callback=lambda msg: self.opt.log(msg, 'info')
            )

            self.opt.all_test_results.append((agg_config, agg_result))
            self.opt._save_test_to_database(agg_config, agg_result)
            self.opt._check_pod_errors(agg_config, agg_result)
            self.opt._check_request_errors(agg_config, agg_result)

            if not agg_result or not agg_result.guidellm_success:
                self.opt.log("❌ Aggregated calibrated load test failed", 'error')
                self.opt.log("   EP calibrated results stand", 'warning')
                return

        cal_agg_ttft = agg_result.ttft_p90 or agg_result.ttft_p50 or 0
        cal_agg_tput = agg_result.throughput_mean or agg_result.throughput_p90 or 0
        self.opt.calibrated_agg_result = agg_result

        self.opt.log(f"  ✅ Aggregated at calibrated load: TTFT={cal_agg_ttft:.1f}ms, "
                     f"Throughput={cal_agg_tput:.2f} req/s", 'success')
        self.opt.log("", 'info')

        # --- Compare ---
        self.opt.log(f"📊 Calibrated Load Results ({int(calibrated_concurrency)} users):", 'decision')

        ttft_diff = cal_ep_ttft - cal_agg_ttft
        ttft_pct = (ttft_diff / cal_agg_ttft * 100) if cal_agg_ttft > 0 else 0
        tput_diff = cal_ep_tput - cal_agg_tput
        tput_pct = (tput_diff / cal_agg_tput * 100) if cal_agg_tput > 0 else 0

        self.opt.log(f"  TTFT p90:       EP={cal_ep_ttft:.1f}ms vs Agg={cal_agg_ttft:.1f}ms "
                     f"({'EP wins by' if ttft_diff < 0 else 'Agg wins by'} {abs(ttft_pct):.1f}%)", 'info')
        self.opt.log(f"  Throughput mean:  EP={cal_ep_tput:.2f} vs Agg={cal_agg_tput:.2f} req/s "
                     f"({'EP wins by' if tput_diff > 0 else 'Agg wins by'} {abs(tput_pct):.1f}%)", 'info')
        self.opt.log("", 'info')

        # Compare with overloaded results
        if overloaded_ttft > 0:
            ttft_improvement = ((overloaded_ttft - cal_ep_ttft) / overloaded_ttft) * 100
            self.opt.log("📉 Impact of load reduction on best EP config:", 'info')
            self.opt.log(f"  TTFT:       {overloaded_ttft:.1f}ms → {cal_ep_ttft:.1f}ms "
                         f"({ttft_improvement:+.1f}%)", 'info')
            self.opt.log(f"  Throughput: {overloaded_tput:.2f} → {cal_ep_tput:.2f} req/s", 'info')


class BalancedStrategy(OptimizationStrategy):
    """Full Coverage: Tests ALL architectures — Standard, PD, EP, PD-EP.

    Runs aggregated search first, then PD and EP optimization paths
    (including multi-node TEP/DP+EP when available), followed by a
    comprehensive comparison using stored results.
    """

    def execute(self):
        # === Planning: Steps 4-5 ===
        self.opt.log("=" * 80, 'info')
        self.opt.log("BALANCED OPTIMIZATION — Planning", 'success')
        self.opt.log("=" * 80, 'info')
        self.opt.log("", 'info')

        # Select TP pairs for PD (supports asymmetric prefill/decode TP)
        self.opt._select_tp_pairs()

        # Steps 4-5 for PD: Calculate feasible splits
        self.opt.log("STEPS 4-5: PD Resource Sizing & Feasible Splits", 'decision')
        self.opt.log("-" * 80, 'info')
        self.opt._calculate_feasible_splits()
        self.opt.log("", 'info')
        if self.opt._should_stop():
            return

        ep_strategy = ThroughputStrategy(self.opt)

        # Steps 4-5 for EP: Calculate EP configs
        self.opt.log("STEPS 4-5: EP Configuration Space", 'decision')
        self.opt.log("-" * 80, 'info')
        ep_strategy._calculate_ep_configs()
        self.opt.log("", 'info')
        if self.opt._should_stop():
            return

        # === Step 6: Aggregated Search (shared baseline) ===
        self.opt.log("=" * 80, 'info')
        self.opt.log("BALANCED OPTIMIZATION — Aggregated Search", 'success')
        self.opt.log("=" * 80, 'info')
        self.opt.log("", 'info')

        self.opt.log("STEP 6: Aggregated Configuration Search", 'decision')
        self.opt.log("-" * 80, 'info')
        self.opt._search_aggregated_configs()
        self.opt.log("", 'info')
        if self.opt._should_stop():
            return

        # === Step 7: Architecture-Specific Testing ===
        self.opt.log("=" * 80, 'info')
        self.opt.log("BALANCED OPTIMIZATION — Architecture Testing", 'success')
        self.opt.log("=" * 80, 'info')
        self.opt.log("", 'info')

        # Step 7a for PD: Test P/D splits
        self.opt.log("STEP 7a: P/D Split Optimization (Pareto Front)", 'decision')
        self.opt.log("-" * 80, 'info')
        self.opt._optimize_pd_splits()
        self.opt.log("", 'info')
        if self.opt._should_stop():
            return

        # Step 7b for EP: Test EP configs
        self.opt.log("STEP 7b: EP Configuration Testing", 'decision')
        self.opt.log("-" * 80, 'info')
        ep_strategy._test_ep_configs()
        self.opt.log("", 'info')
        if self.opt._should_stop():
            return

        # === Step 8: Three-way Comparison (no new tests) ===
        self.opt.log("=" * 80, 'info')
        self.opt.log("BALANCED OPTIMIZATION — Architecture Comparison", 'success')
        self.opt.log("=" * 80, 'info')
        self.opt.log("", 'info')

        self.opt.log("STEP 8: PD vs EP vs Aggregated Comparison", 'decision')
        self.opt.log("-" * 80, 'info')
        self._validate_three_way()
        self.opt.log("", 'info')
        if self.opt._should_stop():
            return

        # Step 9: EPP Tuning (before latency search so it uses optimal EPP)
        self._run_epp_tuning_if_enabled()

        # Step 10: Latency-bounded throughput maximization (only if enabled)
        if self.opt._should_run_latency_bounded_search():
            self.opt._run_latency_bounded_search()
            self.opt.log("", 'info')

        # Step 11: Calibrated load / Concurrency sweep (user-controlled)
        if self.opt.config.calibrated_load_enabled and (len(self.opt.pareto_results) > 0 or len(self.opt.ep_results) > 0):
            self.opt._recalculate_achievable_concurrency()
            self.opt.log("STEP 11: Calibrated Load Validation & Concurrency Sweep", 'decision')
            self.opt.log("-" * 80, 'info')
            self.opt._validate_at_calibrated_load()
            self.opt.log("", 'info')

        # Step 12: Speculative decoding comparison (conditional)
        self._run_speculative_if_enabled()

        # Step 13: Cache hit sweep (user-controlled)
        self._run_cache_sweep_if_enabled()

    def _validate_three_way(self):
        """Step 8: Three-way comparison — best PD vs best EP vs Aggregated.

        No new tests — uses the best aggregated result from Step 6,
        the best PD from Step 7a, and the best EP from Step 7b.
        """
        has_pd = len(self.opt.pareto_results) > 0
        has_ep = self.opt.best_ep_result is not None
        has_agg = self.opt.aggregated_result is not None

        if not has_pd and not has_ep:
            self.opt.log("⚠️  No PD or EP results — skipping Step 8", 'warning')
            return

        if not has_agg:
            self.opt.log("⚠️  No aggregated results — skipping Step 8", 'warning')
            return

        # Get best PD config
        best_pd_split = None
        if has_pd:
            best_pd_split, best_pd_result = min(
                self.opt.pareto_results,
                key=lambda x: x[1].ttft_p90 if x[1].ttft_p90 else 1000000.0
            )
            pd_ttft = best_pd_result.ttft_p90 or best_pd_result.ttft_p50 or 0
            pd_tput = best_pd_result.throughput_mean or best_pd_result.throughput_p90 or 0
            self.opt.log(f"Best PD: {best_pd_split.prefill_pods}P×TP{best_pd_split.prefill_tp} + "
                         f"{best_pd_split.decode_pods}D×TP{best_pd_split.decode_tp}", 'info')
            self.opt.log(f"  TTFT p90: {pd_ttft:.1f}ms, Throughput mean: {pd_tput:.2f} req/s", 'info')

        # Get best EP config
        if has_ep:
            ep_cfg = self.opt.best_ep_config
            ep_ttft = self.opt.best_ep_result.ttft_p90 or self.opt.best_ep_result.ttft_p50 or 0
            ep_tput = self.opt.best_ep_result.throughput_mean or self.opt.best_ep_result.throughput_p90 or 0
            self.opt.log(f"Best EP: PTP={ep_cfg.prefill_tp} DTP={ep_cfg.decode_tp}, "
                         f"{ep_cfg.prefill_pods}P+{ep_cfg.decode_pods}D ({ep_cfg.total_gpus} GPUs)", 'info')
            self.opt.log(f"  TTFT p90: {ep_ttft:.1f}ms, Throughput mean: {ep_tput:.2f} req/s", 'info')

        agg_ttft = self.opt.aggregated_result.ttft_p90 or self.opt.aggregated_result.ttft_p50 or 1000000.0
        agg_tput = self.opt.aggregated_result.throughput_mean or self.opt.aggregated_result.throughput_p90 or 0.0
        self.opt.log(f"Best Aggregated: TP={self.opt.aggregated_tp}, "
                     f"{self.opt.aggregated_gpus // self.opt.aggregated_tp} replicas", 'info')
        self.opt.log(f"  TTFT p90: {agg_ttft:.1f}ms, Throughput mean: {agg_tput:.2f} req/s", 'info')
        self.opt.log("", 'info')

        # Three-way comparison
        self.opt.log("📊 Three-Way Architecture Comparison:", 'decision')
        self.opt.log(f"  {'Architecture':<25} {'TTFT p90':>12} {'Throughput mean':>16}", 'info')
        self.opt.log(f"  {'-'*25} {'-'*12} {'-'*16}", 'info')
        self.opt.log(f"  {'Aggregated':<25} {agg_ttft:>10.1f}ms {agg_tput:>14.2f} req/s", 'info')

        if has_pd:
            pd_label = (f"PD ({best_pd_split.prefill_pods}P+"
                        f"{best_pd_split.decode_pods}D)")
            self.opt.log(f"  {pd_label:<25} {pd_ttft:>10.1f}ms {pd_tput:>14.2f} req/s", 'info')

        if has_ep:
            ep_label = f"EP ({ep_cfg.prefill_pods}P+{ep_cfg.decode_pods}D)"
            self.opt.log(f"  {ep_label:<25} {ep_ttft:>10.1f}ms {ep_tput:>14.2f} req/s", 'info')

        # Determine winners
        self.opt.log("", 'info')
        candidates = [('Aggregated', agg_ttft, agg_tput)]
        if has_pd:
            candidates.append(('PD', pd_ttft, pd_tput))
        if has_ep:
            candidates.append(('EP', ep_ttft, ep_tput))

        ttft_winner = min(candidates, key=lambda x: x[1])
        tput_winner = max(candidates, key=lambda x: x[2])

        self.opt.log(f"  🏆 Lowest TTFT: {ttft_winner[0]} ({ttft_winner[1]:.1f}ms)", 'success')
        self.opt.log(f"  🏆 Highest Throughput: {tput_winner[0]} ({tput_winner[2]:.2f} req/s)", 'success')

    def _should_run_step10(self) -> bool:
        """Check if Step 10 should run for balanced mode."""
        return (
            self.opt.achievable_concurrency is not None
            and not self.opt.config.use_achievable_qps
            and not self.opt.config.latency_constraint_enabled
            and (len(self.opt.pareto_results) > 0 or len(self.opt.ep_results) > 0)
        )

    def _validate_all_at_calibrated_load(self):
        """Step 10: Re-test all architectures at calibrated load."""
        calibrated_concurrency = self.opt.achievable_concurrency

        self.opt.log(f"Re-testing all architectures at calibrated load ({calibrated_concurrency:.0f} users)", 'info')
        self.opt.log("", 'info')

        # --- PD at calibrated load ---
        if self.opt.pareto_results:
            best_split, best_pd_result = min(
                self.opt.pareto_results,
                key=lambda x: x[1].ttft_p90 if x[1].ttft_p90 else 1000000.0
            )
            test_id = (f"step9-{best_split.prefill_pods}p{best_split.decode_pods}d"
                       f"-ptp{best_split.prefill_tp}-dtp{best_split.decode_tp}")

            if test_id in self.opt.completed_tests:
                row = self.opt.completed_tests[test_id]
                pd_result = self.opt._make_test_result_from_db(row)
                self.opt.log("  ⏩ PD test: resuming from DB", 'info')
            else:
                pd_config = self.opt._create_pd_config(best_split)
                pd_config.test_id = test_id
                pd_config.num_users = int(calibrated_concurrency)
                pd_config.request_rate = int(calibrated_concurrency)

                pd_result = self.opt.orchestrator.run_test(
                    pd_config, cleanup=True,
                    log_callback=lambda msg: self.opt.log(msg, 'info')
                )
                self.opt.all_test_results.append((pd_config, pd_result))
                self.opt._save_test_to_database(pd_config, pd_result)
                self.opt._check_pod_errors(pd_config, pd_result)
                self.opt._check_request_errors(pd_config, pd_result)

                if not pd_result or not pd_result.guidellm_success:
                    self.opt.log("❌ PD calibrated load test failed", 'error')
                    pd_result = None

            if pd_result:
                self.opt.calibrated_pd_result = pd_result
                cal_ttft = pd_result.ttft_p90 or pd_result.ttft_p50 or 0
                cal_tput = pd_result.throughput_mean or pd_result.throughput_p90 or 0
                self.opt.log(f"  ✅ PD at calibrated load: TTFT={cal_ttft:.1f}ms, "
                             f"Throughput={cal_tput:.2f} req/s", 'success')

        # --- EP at calibrated load ---
        if self.opt.best_ep_config:
            ep_cfg = self.opt.best_ep_config
            test_id = f"step9-ep-{ep_cfg.prefill_pods}p{ep_cfg.decode_pods}d-ptp{ep_cfg.prefill_tp}-dtp{ep_cfg.decode_tp}"

            if test_id in self.opt.completed_tests:
                row = self.opt.completed_tests[test_id]
                ep_result = self.opt._make_test_result_from_db(row)
                self.opt.log("  ⏩ EP test: resuming from DB", 'info')
            else:
                ep_config = self.opt._create_ep_config(split=ep_cfg)
                ep_config.test_id = test_id
                ep_config.num_users = int(calibrated_concurrency)
                ep_config.request_rate = int(calibrated_concurrency)

                ep_result = self.opt.orchestrator.run_test(
                    ep_config, cleanup=True,
                    log_callback=lambda msg: self.opt.log(msg, 'info')
                )
                self.opt.all_test_results.append((ep_config, ep_result))
                self.opt._save_test_to_database(ep_config, ep_result)
                self.opt._check_pod_errors(ep_config, ep_result)
                self.opt._check_request_errors(ep_config, ep_result)

                if not ep_result or not ep_result.guidellm_success:
                    self.opt.log("❌ EP calibrated load test failed", 'error')
                    ep_result = None

            if ep_result:
                self.opt.calibrated_ep_result = ep_result
                cal_ttft = ep_result.ttft_p90 or ep_result.ttft_p50 or 0
                cal_tput = ep_result.throughput_mean or ep_result.throughput_p90 or 0
                self.opt.log(f"  ✅ EP at calibrated load: TTFT={cal_ttft:.1f}ms, "
                             f"Throughput={cal_tput:.2f} req/s", 'success')

        # --- Aggregated at calibrated load ---
        if self.opt.aggregated_tp:
            agg_tp = self.opt.aggregated_tp
            total_gpus = self.opt.aggregated_gpus
            agg_test_id = f"step9-aggregated-tp{agg_tp}"
            # Backwards compat: check old ID format with total_gpus embedded
            if agg_test_id not in self.opt.completed_tests:
                old_id = f"step9-aggregated-{total_gpus}gpu-tp{agg_tp}"
                if old_id in self.opt.completed_tests:
                    agg_test_id = old_id

            if agg_test_id in self.opt.completed_tests:
                row = self.opt.completed_tests[agg_test_id]
                agg_result = self.opt._make_test_result_from_db(row)
                self.opt.log("  ⏩ Aggregated test: resuming from DB", 'info')
            else:
                agg_config = self.opt._create_aggregated_config(
                    tp=agg_tp, num_gpus=total_gpus,
                    isl=self.opt.config.isl, osl=self.opt.config.osl,
                    test_id=agg_test_id, use_concurrency=True
                )
                agg_config.num_users = int(calibrated_concurrency)
                agg_config.request_rate = int(calibrated_concurrency)

                agg_result = self.opt.orchestrator.run_test(
                    agg_config, cleanup=True,
                    log_callback=lambda msg: self.opt.log(msg, 'info')
                )
                self.opt.all_test_results.append((agg_config, agg_result))
                self.opt._save_test_to_database(agg_config, agg_result)
                self.opt._check_pod_errors(agg_config, agg_result)
                self.opt._check_request_errors(agg_config, agg_result)

                if not agg_result or not agg_result.guidellm_success:
                    self.opt.log("❌ Aggregated calibrated load test failed", 'error')
                    agg_result = None

            if agg_result:
                self.opt.calibrated_agg_result = agg_result
                cal_ttft = agg_result.ttft_p90 or agg_result.ttft_p50 or 0
                cal_tput = agg_result.throughput_mean or agg_result.throughput_p90 or 0
                self.opt.log(f"  ✅ Aggregated at calibrated load: TTFT={cal_ttft:.1f}ms, "
                             f"Throughput={cal_tput:.2f} req/s", 'success')

        self.opt.log("", 'info')

        # Summary table
        self.opt.log(f"📊 Calibrated Load Results ({int(calibrated_concurrency)} users):", 'decision')
        self.opt.log(f"  {'Architecture':<25} {'TTFT p90':>12} {'Throughput mean':>16}", 'info')
        self.opt.log(f"  {'-'*25} {'-'*12} {'-'*16}", 'info')

        if self.opt.calibrated_agg_result:
            t = self.opt.calibrated_agg_result.ttft_p90 or 0
            p = self.opt.calibrated_agg_result.throughput_mean or self.opt.calibrated_agg_result.throughput_p90 or 0
            self.opt.log(f"  {'Aggregated':<25} {t:>10.1f}ms {p:>14.2f} req/s", 'info')
        if self.opt.calibrated_pd_result:
            t = self.opt.calibrated_pd_result.ttft_p90 or 0
            p = self.opt.calibrated_pd_result.throughput_mean or self.opt.calibrated_pd_result.throughput_p90 or 0
            self.opt.log(f"  {'PD (best)':<25} {t:>10.1f}ms {p:>14.2f} req/s", 'info')
        if self.opt.calibrated_ep_result:
            t = self.opt.calibrated_ep_result.ttft_p90 or 0
            p = self.opt.calibrated_ep_result.throughput_mean or self.opt.calibrated_ep_result.throughput_p90 or 0
            self.opt.log(f"  {'EP (best)':<25} {t:>10.1f}ms {p:>14.2f} req/s", 'info')


class SingleTestStrategy(OptimizationStrategy):
    """Single Test: Run one user-specified configuration — no sweeps, no optimization.

    Builds a TestConfig from the user's exact architecture/TP/pods, deploys,
    benchmarks, and records the result. Supports aggregated, PD, and EP.
    """

    def execute(self):
        self.opt.log("SINGLE TEST: Running user-specified configuration", 'decision')
        self.opt.log("-" * 80, 'info')

        cfg = self.opt.config
        arch = cfg.single_test_architecture or 'aggregated'
        self.opt.log(f"Architecture: {arch}", 'info')

        if arch == 'aggregated':
            tp = cfg.single_test_tp or 1
            replicas = cfg.single_test_replicas or 1
            num_gpus = tp * replicas
            self.opt.log(f"TP={tp}, {replicas} replicas ({num_gpus} GPUs)", 'info')

            test_config = self.opt._create_aggregated_config(
                tp=tp,
                num_gpus=num_gpus,
                isl=cfg.isl,
                osl=cfg.osl,
                test_id=f"single-agg-tp{tp}-{replicas}r",
                use_concurrency=True,
            )

        elif arch == 'pd':
            from core.optimizer.config import FeasibleSplit
            prefill_tp = cfg.single_test_prefill_tp or cfg.single_test_tp or 4
            decode_tp = cfg.single_test_decode_tp or cfg.single_test_tp or 8
            prefill_pods = cfg.single_test_prefill_pods or 1
            decode_pods = cfg.single_test_decode_pods or 1
            total_gpus = (prefill_tp * prefill_pods) + (decode_tp * decode_pods)

            self.opt.log(f"Prefill: {prefill_pods} pods × TP{prefill_tp}, "
                         f"Decode: {decode_pods} pods × TP{decode_tp} "
                         f"({total_gpus} GPUs)", 'info')

            split = FeasibleSplit(
                prefill_pods=prefill_pods,
                decode_pods=decode_pods,
                prefill_tp=prefill_tp,
                decode_tp=decode_tp,
                prefill_gpus=prefill_tp * prefill_pods,
                decode_gpus=decode_tp * decode_pods,
                total_gpus=total_gpus,
                prefill_pct=prefill_pods / (prefill_pods + decode_pods),
            )
            test_config = self.opt._create_pd_config(split)
            test_config.test_id = f"single-pd-{prefill_pods}p{decode_pods}d-ptp{prefill_tp}-dtp{decode_tp}"

        elif arch == 'ep':
            tp = cfg.single_test_tp or 1
            replicas = cfg.single_test_replicas or 1
            num_gpus = tp * replicas
            self.opt.log(f"TP={tp}, {replicas} replicas ({num_gpus} GPUs)", 'info')

            test_config = self.opt._create_ep_config(
                tp=tp,
                num_gpus=num_gpus,
                isl=cfg.isl,
                osl=cfg.osl,
                test_id=f"single-ep-tp{tp}-{replicas}r",
            )

        else:
            self.opt.log(f"❌ Unknown architecture: {arch}", 'error')
            return

        self.opt.log("", 'info')
        sweep_after = cfg.calibrated_load_enabled or getattr(cfg, 'concurrency_sweep_count', None) or getattr(cfg, 'concurrency_sweep_levels', None)

        # Check if already completed (resume case)
        if test_config.test_id in self.opt.completed_tests:
            self.opt.log(f"  ⏩ {test_config.test_id}: already completed — resuming from DB", 'info')
            row = self.opt.completed_tests[test_config.test_id]
            result = self.opt._make_test_result_from_db(row)
        else:
            result = self.opt.orchestrator.run_test(
                test_config,
                cleanup=not sweep_after,
                log_callback=lambda msg: self.opt.log(msg, 'info'),
                stop_check=self.opt._should_stop,
            )
            self.opt._save_test_to_database(test_config, result)

        self.opt.all_test_results.append((test_config, result))

        if result and result.guidellm_success:
            ttft = result.ttft_p90 or result.ttft_p50 or 0
            throughput = result.throughput_mean or result.throughput_p90 or 0
            self.opt.log(f"✅ TTFT p90: {ttft:.1f}ms, Throughput mean: {throughput:.2f} req/s", 'success')

            # Run calibrated load / concurrency sweep if enabled
            if cfg.calibrated_load_enabled and result:
                self.opt.log("", 'info')
                self.opt.log("STEP 11: Calibrated Load / Concurrency Sweep", 'decision')
                self.opt.log("-" * 80, 'info')
                if arch == 'aggregated':
                    self.opt.aggregated_tp = tp
                    self.opt.aggregated_gpus = num_gpus
                    self.opt.aggregated_result = result
                elif arch == 'pd':
                    self.opt.pareto_results = [(split, result)]
                elif arch == 'ep':
                    self.opt.aggregated_tp = tp
                    self.opt.aggregated_gpus = num_gpus
                    self.opt.aggregated_result = result
                self.opt._validate_at_calibrated_load()
        else:
            self.opt.log("❌ Test failed", 'error')


class AggregatedOnlyStrategy(OptimizationStrategy):
    """Aggregated Only: Skip architecture comparison, test only standard aggregated configs.

    Step 6: Search for best aggregated configuration across TP values
    Step 9: Latency-bounded throughput maximization (conditional)
    """

    def execute(self):
        self.opt.log("STEP 6: Aggregated Configuration Search", 'decision')
        self.opt.log("-" * 80, 'info')
        self.opt._search_aggregated_configs()
        self.opt.log("", 'info')
        if self.opt._should_stop():
            return

        self._run_epp_tuning_if_enabled()

        if self.opt._should_run_latency_bounded_search():
            self.opt._run_latency_bounded_search()
            self.opt.log("", 'info')

        self._run_speculative_if_enabled()
        self._run_cache_sweep_if_enabled()


class PDOnlyStrategy(OptimizationStrategy):
    """PD Only: Test only Prefill/Decode disaggregation splits.

    Steps 4-5: Calculate feasible P/D splits from TP calibration data
    Step 7: Test all feasible PD splits, find Pareto front
    Step 9: Latency-bounded throughput maximization (conditional)
    """

    def execute(self):
        self.opt._select_tp_pairs()

        self.opt.log("STEPS 4-5: Resource Sizing & Feasible Splits", 'decision')
        self.opt.log("-" * 80, 'info')
        self.opt._calculate_feasible_splits()
        self.opt.log("", 'info')
        if self.opt._should_stop():
            return

        self.opt.log("STEP 7: P/D Split Optimization (Pareto Front)", 'decision')
        self.opt.log("-" * 80, 'info')
        self.opt._optimize_pd_splits()
        self.opt.log("", 'info')
        if self.opt._should_stop():
            return

        self._run_epp_tuning_if_enabled()

        if self.opt._should_run_latency_bounded_search():
            self.opt._run_latency_bounded_search()
            self.opt.log("", 'info')

        self._run_speculative_if_enabled()
        self._run_cache_sweep_if_enabled()


class EPOnlyStrategy(OptimizationStrategy):
    """EP Only: Test only Expert Parallelism configurations.

    Steps 4-5: Enumerate EP configurations (TP x replicas)
    Step 7: Test EP configs at full workload, find best by throughput
    Step 9: Latency-bounded throughput maximization (conditional)
    """

    def execute(self):
        ep_strategy = ThroughputStrategy(self.opt)

        self.opt.log("STEPS 4-5: EP Configuration Space", 'decision')
        self.opt.log("-" * 80, 'info')
        ep_strategy._calculate_ep_configs()
        self.opt.log("", 'info')
        if self.opt._should_stop():
            return

        self.opt.log("STEP 7: EP Configuration Testing (Throughput Optimization)", 'decision')
        self.opt.log("-" * 80, 'info')
        ep_strategy._test_ep_configs()
        self.opt.log("", 'info')
        if self.opt._should_stop():
            return

        self._run_epp_tuning_if_enabled()

        if self.opt._should_run_latency_bounded_search():
            self.opt._run_latency_bounded_search()
            self.opt.log("", 'info')

        self._run_speculative_if_enabled()
        self._run_cache_sweep_if_enabled()
