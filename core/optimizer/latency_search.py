"""Steps 10-11: Latency-bounded throughput search and calibrated load."""

import time
from typing import Dict, Optional


class LatencySearchMixin:
    """Mixin providing latency search methods for RecipeOptimizer."""

    def _should_run_latency_bounded_search(self) -> bool:
        """Check if Step 10 (latency-bounded throughput maximization) should run."""
        return self.config.latency_constraint_enabled

    def _run_latency_bounded_search(self):
        """
        Step 10: Find maximum throughput under a user-defined latency SLA.

        Uses exponential search + bisection over concurrency levels for both
        the best PD and best aggregated configurations.  The starting
        concurrency comes from the calibrated (sustainable) QPS calculation.
        """
        from core.user_defined_tuning import (
            LatencyBinarySearch, LatencyConstraintConfig
        )

        self.log("=" * 60, 'info')
        self.log("Step 10: Latency-Bounded Throughput Maximization", 'info')
        self.log("=" * 60, 'info')

        constraint = LatencyConstraintConfig(
            target_ms=float(self.config.latency_constraint_ms),
            percentile=self.config.latency_constraint_percentile,
        )

        default_c = self.achievable_concurrency if self.achievable_concurrency else int(self.config.qps)
        self.log(f"🎯 Target: TTFT {constraint.percentile.upper()} "
                 f"≤ {constraint.target_ms}ms", 'info')
        self.log(f"   Default starting concurrency: {default_c} concurrent users", 'info')
        self.log("", 'info')

        self.latency_search_results = {}

        def _estimate_starting_c(result, target_ms, percentile):
            """Estimate starting concurrency from a prior benchmark result.

            Uses P90 throughput (most stable measure of actual system capacity)
            scaled by the ratio of target latency to observed latency at the
            target percentile.
            """
            latency_field = f'ttft_{percentile}'
            observed_latency = getattr(result, latency_field, None)
            observed_tput = result.throughput_p90
            if not observed_latency or observed_latency <= 0 or not observed_tput or observed_tput <= 0:
                return None
            ceiling = observed_tput * (target_ms / observed_latency)
            estimated = max(1, int(ceiling * 0.6))
            return estimated

        def run_test_fn(cfg):
            result = self.orchestrator.run_test(
                cfg,
                cleanup=True,
                log_callback=lambda msg: self.log(msg, 'info'),
                stop_check=self._should_stop
            )
            self.all_test_results.append((cfg, result))
            return result

        def save_test_fn(cfg, result):
            self._save_test_to_database(cfg, result)
            self._check_pod_errors(cfg, result)

        # --- Search PD ---
        best_split = None
        if self.pareto_results:
            best_split, best_pd_result = min(
                self.pareto_results,
                key=lambda x: x[1].ttft_p90 if x[1].ttft_p90 else 1e9
            )
            self.log(f"📊 PD config: {best_split.prefill_pods}P×TP{best_split.prefill_tp} + "
                     f"{best_split.decode_pods}D×TP{best_split.decode_tp}", 'info')

            pd_starting_c = _estimate_starting_c(
                best_pd_result,
                constraint.target_ms, constraint.percentile
            )
            step7_latency = getattr(best_pd_result, f'ttft_{constraint.percentile}', None)
            step7_tput = best_pd_result.throughput_p90
            if pd_starting_c is not None:
                self.log(f"   Step 7 measured: throughput P90={step7_tput:.1f} req/s, "
                         f"TTFT {constraint.percentile.upper()}={step7_latency:.0f}ms "
                         f"→ estimated start c={pd_starting_c}", 'info')
            else:
                pd_starting_c = default_c
                self.log(f"   No latency data for {constraint.percentile.upper()}, "
                         f"using default c={default_c}", 'info')

            def create_pd_config(concurrency, test_id):
                cfg = self._create_pd_config(best_split)
                cfg.test_id = test_id
                cfg.num_users = concurrency
                cfg.request_rate = concurrency
                cfg.test_duration = max(180, self.config.test_duration)
                return cfg

            pd_search = LatencyBinarySearch(
                constraint=constraint,
                run_test_fn=run_test_fn,
                create_config_fn=create_pd_config,
                log_fn=self.log,
                stop_check_fn=self._should_stop,
                save_test_fn=save_test_fn,
                completed_tests=self.completed_tests,
                make_result_from_db_fn=self._make_test_result_from_db,
                starting_concurrency=pd_starting_c,
                architecture='pd',
                db_manager=self.db_manager,
                run_id=self.run_id,
            )
            pd_result = pd_search.search()
            if pd_result:
                self.latency_search_results['pd'] = pd_result
            self.log("", 'info')

            if self._should_stop():
                return

        # --- Search Aggregated configs ---
        # Test the primary aggregated TP (selected by objective in Step 6)
        # AND the best-throughput TP if different, since Step 10 maximizes throughput under SLA
        agg_configs_to_test = []
        if self.aggregated_tp:
            agg_configs_to_test.append((self.aggregated_tp, self.aggregated_gpus, f"aggregated-tp{self.aggregated_tp}"))

            if self.aggregated_search_results:
                best_tput_tp, _ = max(
                    self.aggregated_search_results,
                    key=lambda x: x[1].throughput_p90 if x[1].throughput_p90 else 0.0
                )
                if best_tput_tp != self.aggregated_tp:
                    tput_gpus = self.config.total_gpus
                    replicas = tput_gpus // best_tput_tp
                    actual_gpus = best_tput_tp * replicas
                    agg_configs_to_test.append((best_tput_tp, actual_gpus, f"aggregated-tp{best_tput_tp}"))
                    self.log(f"  Also testing best-throughput aggregated TP={best_tput_tp}", 'info')

        for agg_tp, agg_gpus, agg_arch in agg_configs_to_test:
            if self._should_stop():
                break

            self.log(f"📊 Aggregated config: TP={agg_tp}, "
                     f"{agg_gpus} GPUs", 'info')

            # Estimate starting concurrency from Step 6 aggregated result
            agg_starting_c = default_c
            agg_step6_result = None
            for tp_val, res in (self.aggregated_search_results or []):
                if tp_val == agg_tp:
                    agg_step6_result = res
                    break
            if agg_step6_result:
                est = _estimate_starting_c(
                    agg_step6_result,
                    constraint.target_ms, constraint.percentile
                )
                if est is not None:
                    step6_latency = getattr(agg_step6_result, f'ttft_{constraint.percentile}', None)
                    step6_tput = agg_step6_result.throughput_p90
                    self.log(f"   Step 6 measured: throughput P90={step6_tput:.1f} req/s, "
                             f"TTFT {constraint.percentile.upper()}={step6_latency:.0f}ms "
                             f"→ estimated start c={est}", 'info')
                    agg_starting_c = est

            def create_agg_config(concurrency, test_id, _tp=agg_tp, _gpus=agg_gpus):
                cfg = self._create_aggregated_config(
                    tp=_tp,
                    num_gpus=_gpus,
                    isl=self.config.isl,
                    osl=self.config.osl,
                    test_id=test_id,
                    use_concurrency=True
                )
                cfg.num_users = concurrency
                cfg.request_rate = concurrency
                cfg.test_duration = max(180, self.config.test_duration)
                return cfg

            agg_search = LatencyBinarySearch(
                constraint=constraint,
                run_test_fn=run_test_fn,
                create_config_fn=create_agg_config,
                log_fn=self.log,
                stop_check_fn=self._should_stop,
                save_test_fn=save_test_fn,
                completed_tests=self.completed_tests,
                make_result_from_db_fn=self._make_test_result_from_db,
                starting_concurrency=agg_starting_c,
                architecture=agg_arch,
                db_manager=self.db_manager,
                run_id=self.run_id,
            )
            agg_result = agg_search.search()
            if agg_result:
                self.latency_search_results[agg_arch] = agg_result
            self.log("", 'info')

        # --- Summary ---
        if self.latency_search_results:
            self.log("📊 Latency Search Summary:", 'decision')
            for arch, res in self.latency_search_results.items():
                self.log(f"  {arch.upper()}: c={res.optimal_concurrency}, "
                         f"throughput={res.achieved_throughput:.2f} req/s, "
                         f"TTFT {res.target_percentile.upper()}="
                         f"{res.achieved_latency_ms:.1f}ms "
                         f"({res.n_trials} trials)", 'success')

            # Pick overall winner by throughput
            best_arch = max(self.latency_search_results,
                           key=lambda k: self.latency_search_results[k].achieved_throughput)
            best = self.latency_search_results[best_arch]
            self.log(f"  🏆 Winner: {best_arch.upper()} with "
                     f"{best.achieved_throughput:.2f} req/s", 'decision')

            # Store as latency_bounded_result for compatibility with _build_results
            from core.user_defined_tuning import LatencyBoundedResult
            self.latency_bounded_result = LatencyBoundedResult(
                optimal_concurrency=best.optimal_concurrency,
                achieved_throughput=best.achieved_throughput,
                achieved_latency_ms=best.achieved_latency_ms,
                target_latency_ms=best.target_latency_ms,
                target_percentile=best.target_percentile,
                n_trials=sum(r.n_trials for r in self.latency_search_results.values()),
                best_config_source=best_arch,
            )

    def _should_run_step10(self) -> bool:
        """Check if Step 11 (calibrated load validation) should run.

        Step 11 runs when:
        1. The concurrency implies load beyond sustainable QPS
        2. The user did NOT enable 'use_achievable_qps'
        3. We have PD results from Step 7
        4. Step 10 (latency-bounded search) did NOT run — it already
           explores concurrency levels including calibrated load
        """
        return (
            self.achievable_concurrency is not None
            and not self.config.use_achievable_qps
            and not self.config.latency_constraint_enabled
            and len(self.pareto_results) > 0
        )

    def _validate_at_calibrated_load(self):
        """
        Step 11: Re-test best PD and Aggregated at sustainable concurrency.

        Steps 7-8 ran at the user's original concurrency which overloads the cluster.
        This step re-runs the best config at a sustainable level to show
        realistic latency and throughput numbers.
        """
        calibrated_concurrency = self.achievable_concurrency

        # Find best PD config by TTFT from Step 7
        best_split, best_pd_result = min(
            self.pareto_results,
            key=lambda x: x[1].ttft_p90 if x[1].ttft_p90 else 1000000.0
        )

        overloaded_ttft = best_pd_result.ttft_p90 or best_pd_result.ttft_p50 or 0
        overloaded_tput = best_pd_result.throughput_p90 or best_pd_result.throughput_p50 or 0

        self.log(f"Re-testing best PD config at calibrated load ({calibrated_concurrency:.0f} users "
                f"vs original {self.config.qps:.0f} users)", 'info')
        self.log(f"Best PD: {best_split.prefill_pods}P×TP{best_split.prefill_tp} + "
                f"{best_split.decode_pods}D×TP{best_split.decode_tp}", 'info')
        self.log(f"  Step 7 results (overloaded): TTFT={overloaded_ttft:.1f}ms, "
                f"Throughput={overloaded_tput:.2f} req/s", 'info')
        self.log("", 'info')

        # --- Test best PD at calibrated load ---
        test_id = (f"step10-{best_split.prefill_pods}p{best_split.decode_pods}d"
                  f"-ptp{best_split.prefill_tp}-dtp{best_split.decode_tp}")

        if test_id in self.completed_tests:
            row = self.completed_tests[test_id]
            pd_result = self._make_test_result_from_db(row)
            self.log("  ⏩ PD test: resuming from DB (already completed)", 'info')
        else:
            # Create a PD config identical to the best split but with calibrated load
            pd_config = self._create_pd_config(best_split)
            pd_config.test_id = test_id
            pd_config.num_users = int(calibrated_concurrency)
            pd_config.request_rate = int(calibrated_concurrency)

            pd_result = self.orchestrator.run_test(
                pd_config,
                cleanup=True,
                log_callback=lambda msg: self.log(msg, 'info'),
                stop_check=self._should_stop
            )

            self.all_test_results.append((pd_config, pd_result))
            self._save_test_to_database(pd_config, pd_result)
            self._check_pod_errors(pd_config, pd_result)

            if not pd_result or not pd_result.guidellm_success:
                self.log("❌ PD calibrated load test failed", 'error')
                return

        cal_pd_ttft = pd_result.ttft_p90 or pd_result.ttft_p50 or 0
        cal_pd_tput = pd_result.throughput_p90 or pd_result.throughput_p50 or 0
        self.calibrated_pd_result = pd_result

        self.log(f"  ✅ PD at calibrated load: TTFT={cal_pd_ttft:.1f}ms, "
                f"Throughput={cal_pd_tput:.2f} req/s", 'success')

        if self._should_stop():
            self.log("🛑 Optimization stopped — skipping aggregated calibration test", 'warning')
            return

        # --- Test Aggregated at calibrated load ---
        if not self.aggregated_tp:
            self.log("⚠️  No aggregated baseline — skipping aggregated re-test", 'warning')
            return
        agg_tp = self.aggregated_tp
        total_gpus = self.aggregated_gpus
        agg_test_id = f"step10-aggregated-tp{agg_tp}"
        # Backwards compat: check old ID format with total_gpus embedded
        if agg_test_id not in self.completed_tests:
            old_id = f"step10-aggregated-{total_gpus}gpu-tp{agg_tp}"
            if old_id in self.completed_tests:
                agg_test_id = old_id

        if agg_test_id in self.completed_tests:
            row = self.completed_tests[agg_test_id]
            agg_result = self._make_test_result_from_db(row)
            self.log("  ⏩ Aggregated test: resuming from DB (already completed)", 'info')
        else:
            agg_config = self._create_aggregated_config(
                tp=agg_tp,
                num_gpus=total_gpus,
                isl=self.config.isl,
                osl=self.config.osl,
                test_id=agg_test_id,
                use_concurrency=True
            )
            # Override with calibrated load
            agg_config.num_users = int(calibrated_concurrency)
            agg_config.request_rate = int(calibrated_concurrency)

            agg_result = self.orchestrator.run_test(
                agg_config,
                cleanup=True,
                log_callback=lambda msg: self.log(msg, 'info'),
                stop_check=self._should_stop
            )

            self.all_test_results.append((agg_config, agg_result))
            self._save_test_to_database(agg_config, agg_result)
            self._check_pod_errors(agg_config, agg_result)

            if not agg_result or not agg_result.guidellm_success:
                self.log("❌ Aggregated calibrated load test failed", 'error')
                self.log("   PD calibrated results stand", 'warning')
                return

        cal_agg_ttft = agg_result.ttft_p90 or agg_result.ttft_p50 or 0
        cal_agg_tput = agg_result.throughput_p90 or agg_result.throughput_p50 or 0
        self.calibrated_agg_result = agg_result

        self.log(f"  ✅ Aggregated at calibrated load: TTFT={cal_agg_ttft:.1f}ms, "
                f"Throughput={cal_agg_tput:.2f} req/s", 'success')
        self.log("", 'info')

        # --- Compare ---
        self.log(f"📊 Calibrated Load Results ({int(calibrated_concurrency)} users):", 'decision')

        ttft_diff = cal_pd_ttft - cal_agg_ttft
        ttft_pct = (ttft_diff / cal_agg_ttft * 100) if cal_agg_ttft > 0 else 0
        tput_diff = cal_pd_tput - cal_agg_tput
        tput_pct = (tput_diff / cal_agg_tput * 100) if cal_agg_tput > 0 else 0

        self.log(f"  TTFT p90:       PD={cal_pd_ttft:.1f}ms vs Agg={cal_agg_ttft:.1f}ms "
                f"({'PD wins by' if ttft_diff < 0 else 'Agg wins by'} {abs(ttft_pct):.1f}%)", 'info')
        self.log(f"  Throughput p90:  PD={cal_pd_tput:.2f} vs Agg={cal_agg_tput:.2f} req/s "
                f"({'PD wins by' if tput_diff > 0 else 'Agg wins by'} {abs(tput_pct):.1f}%)", 'info')
        self.log("", 'info')

        # Compare with overloaded results
        if overloaded_ttft > 0:
            ttft_improvement = ((overloaded_ttft - cal_pd_ttft) / overloaded_ttft) * 100
            self.log("📉 Impact of load reduction on best PD config:", 'info')
            self.log(f"  TTFT:       {overloaded_ttft:.1f}ms → {cal_pd_ttft:.1f}ms "
                    f"({ttft_improvement:+.1f}%)", 'info')
            self.log(f"  Throughput: {overloaded_tput:.2f} → {cal_pd_tput:.2f} req/s", 'info')

