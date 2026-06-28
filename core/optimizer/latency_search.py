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
            self._check_request_errors(cfg, result)

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

    def _compute_calibrated_concurrency(self, throughput_mean, concurrency, arch_label):
        """Compute sustainable concurrency from measured Step 7 data.

        Uses Little's Law: at the tested concurrency, each user produces
        throughput_mean/concurrency req/s. The service time without queuing
        is estimated from the decode TPSG. Sustainable concurrency is where
        queue time stays reasonable (~2x service time).
        """
        if not throughput_mean or throughput_mean <= 0:
            return self.achievable_concurrency or max(1, int(self.config.total_gpus / 1.3))

        decode_tpsg = self.optimal_decode_tp.tpsg if self.optimal_decode_tp else 500
        service_time = (self.config.isl / (self.optimal_prefill_tp.tpsg if self.optimal_prefill_tp else 5000)) + (self.config.osl / decode_tpsg)
        response_time = concurrency / throughput_mean
        queue_time = max(0, response_time - service_time)
        utilization = service_time / response_time if response_time > 0 else 1

        # Target: 3x service time as total response time (2x queuing headroom)
        target_response = service_time * 3
        # Scale concurrency proportionally: new_c = old_c × (target_response / actual_response)
        cal_concurrency = max(1, int(concurrency * target_response / response_time))
        # Cap at measured throughput (can't exceed cluster capacity)
        cal_concurrency = min(cal_concurrency, int(throughput_mean * target_response))

        self.log(f"  📊 {arch_label} Load Analysis:", 'info')
        self.log(f"    Measured: {throughput_mean:.1f} req/s at c={concurrency:.0f}", 'info')
        self.log(f"    Response time: {concurrency:.0f} / {throughput_mean:.1f} = {response_time:.2f}s", 'info')
        self.log(f"    Service time (no queue): {service_time*1000:.0f}ms", 'info')
        self.log(f"    Queue time: {queue_time*1000:.0f}ms ({queue_time/response_time*100:.0f}% of response)", 'info')
        self.log(f"    Utilization: {utilization*100:.0f}%", 'info')
        self.log(f"    → Calibrated concurrency: {cal_concurrency} users (target {target_response*1000:.0f}ms response)", 'info')

        # Store for report display
        if not hasattr(self, '_calibration_analysis'):
            self._calibration_analysis = {}
        self._calibration_analysis[arch_label.lower()] = {
            'throughput_mean': round(throughput_mean, 2),
            'concurrency_tested': int(concurrency),
            'response_time_s': round(response_time, 3),
            'service_time_ms': round(service_time * 1000, 1),
            'queue_time_ms': round(queue_time * 1000, 1),
            'queue_pct': round(queue_time / response_time * 100 if response_time > 0 else 0, 1),
            'utilization_pct': round(utilization * 100, 1),
            'calibrated_concurrency': cal_concurrency,
        }

        return cal_concurrency

    def _generate_sweep_levels(self, calibrated):
        """Generate concurrency levels for the InferenceX sweep.

        Produces ~6 rounded levels from low to 1.5× calibrated,
        always including the exact calibrated value.
        """
        sweep_max = max(int(calibrated * 1.5), calibrated + 5)

        def round_to(n, base=5):
            return max(base, base * round(n / base))

        step = max(5, round_to(sweep_max // 6))
        levels = set()
        v = step
        while v <= sweep_max:
            levels.add(round_to(v))
            v += step
        levels.add(calibrated)
        levels.add(round_to(sweep_max))
        return sorted(l for l in levels if l > 0)

    def _run_sweep_for_arch(self, arch_label, calibrated, levels, create_config_fn, gpus):
        """Run concurrency sweep for one architecture, return list of results.

        Deploys pods once, runs all concurrency levels with pods still up,
        then cleans up at the end. Only guidellm is re-run per level.
        """
        results = []
        self.log(f"\n📊 InferenceX Sweep: {arch_label} ({len(levels)} levels: {levels})", 'info')
        self.log(f"   Calibrated load: {calibrated} users", 'info')

        # Check which levels need testing vs already completed
        levels_to_run = []
        for concurrency in levels:
            test_id = f"step11-sweep-{arch_label.lower()}-c{concurrency}"
            if test_id in self.completed_tests:
                row = self.completed_tests[test_id]
                result = self._make_test_result_from_db(row)
                self.log(f"  ⏩ c={concurrency}: resuming from DB", 'info')
                self._append_sweep_result(results, result, concurrency, calibrated, gpus, test_id)
            else:
                levels_to_run.append(concurrency)

        if not levels_to_run:
            return results

        # All levels share the same pods — use first level's test_id for deployment
        deploy_test_id = f"step11-sweep-{arch_label.lower()}-c{levels_to_run[0]}"

        for i, concurrency in enumerate(levels_to_run):
            if self._should_stop():
                break

            test_id = f"step11-sweep-{arch_label.lower()}-c{concurrency}"
            config = create_config_fn()
            config.test_id = deploy_test_id
            config.num_users = concurrency
            config.request_rate = concurrency

            is_first = (i == 0)

            result = self.orchestrator.run_test(
                config,
                cleanup=False,
                skip_deploy=not is_first,
                log_callback=lambda msg: self.log(msg, 'info'),
                stop_check=self._should_stop
            )

            # Save with the actual test_id
            config.test_id = test_id

            self.all_test_results.append((config, result))
            self._save_test_to_database(config, result)
            self._check_pod_errors(config, result)
            self._check_request_errors(config, result)

            if not result or not result.guidellm_success:
                self.log(f"  ❌ c={concurrency}: test failed, skipping", 'warning')
                continue

            self._append_sweep_result(results, result, concurrency, calibrated, gpus, test_id)

        # Cleanup after all levels are done
        if deployed_test_id:
            self.log(f"  🧹 Cleaning up {arch_label} sweep deployment...", 'info')
            cleanup_config = create_config_fn()
            cleanup_config.test_id = deploy_test_id
            self.orchestrator.cleanup_deployment(cleanup_config, log_callback=lambda msg: self.log(msg, 'info'))

        return results

    def _append_sweep_result(self, results, result, concurrency, calibrated, gpus, test_id):
        """Helper to build a sweep result dict."""
        tput = result.throughput_mean or result.throughput_p90 or 0
        output_tps = result.output_tps_mean or 0
        ttft = result.ttft_p90 or 0
        interactivity = output_tps if output_tps > 0 else 0
        throughput_per_gpu = (output_tps * concurrency / gpus) if gpus > 0 and output_tps > 0 else 0

        results.append({
            'concurrency': concurrency,
            'is_calibrated': concurrency == calibrated,
            'throughput_mean': round(tput, 2),
            'output_tps_mean': round(output_tps, 2),
            'interactivity': round(interactivity, 2),
            'throughput_per_gpu': round(throughput_per_gpu, 2),
            'ttft_p90': round(ttft, 1),
            'ttft_p50': round(result.ttft_p50 or 0, 1),
            'itl_p90': round(result.itl_p90 or 0, 1),
            'gpus': gpus,
            'test_id': test_id,
        })

        self.log(f"  ✅ c={concurrency}: TTFT={ttft:.0f}ms, "
                f"tput={tput:.1f} req/s, "
                f"interactivity={interactivity:.1f} tok/s/user, "
                f"throughput/GPU={throughput_per_gpu:.0f} tok/s/gpu"
                f"{' ← calibrated' if concurrency == calibrated else ''}", 'info')

        # Save sweep progress incrementally so the report updates live
        self._save_sweep_progress()

    def _validate_at_calibrated_load(self):
        """
        Step 11: Concurrency sweep for InferenceX charts.

        Computes calibrated concurrency from measured Step 7 throughput using
        Little's Law, then runs a sweep from low to 1.5× calibrated for both
        best PD and Aggregated configs.
        """
        if not hasattr(self, 'concurrency_sweep_results'):
            self.concurrency_sweep_results = {}

        original_concurrency = int(self.config.qps)

        # Find best PD config by TTFT from Step 7
        best_split, best_pd_result = min(
            self.pareto_results,
            key=lambda x: x[1].ttft_p90 if x[1].ttft_p90 else 1000000.0
        )

        overloaded_ttft = best_pd_result.ttft_p90 or best_pd_result.ttft_p50 or 0
        overloaded_tput = best_pd_result.throughput_p90 or best_pd_result.throughput_p50 or 0
        pd_tput_mean = best_pd_result.throughput_mean or best_pd_result.throughput_p50 or 0

        pd_calibrated = self._compute_calibrated_concurrency(pd_tput_mean, original_concurrency, 'PD')
        if self.config.inferencex_sweep_enabled:
            pd_levels = self._generate_sweep_levels(pd_calibrated)
        else:
            pd_levels = [pd_calibrated]

        total_gpus_pd = (best_split.prefill_pods * best_split.prefill_tp +
                         best_split.decode_pods * best_split.decode_tp)

        # --- PD sweep ---
        pd_sweep = self._run_sweep_for_arch(
            'PD', pd_calibrated, pd_levels,
            lambda: self._create_pd_config(best_split),
            total_gpus_pd
        )
        self.concurrency_sweep_results['pd'] = pd_sweep

        # Store calibrated result for backwards compat
        cal_results = [r for r in pd_sweep if r['is_calibrated']]
        if cal_results:
            cal_test = cal_results[0]
            matching = [r for _, r in self.all_test_results if hasattr(_, 'test_id') and _.test_id == cal_test['test_id']]
            if matching:
                self.calibrated_pd_result = matching[0]

        if self._should_stop():
            return

        # --- Aggregated sweep ---
        if not self.aggregated_tp:
            self.log("⚠️  No aggregated baseline — skipping aggregated sweep", 'warning')
            return

        agg_tp = self.aggregated_tp
        total_gpus_agg = self.aggregated_gpus
        agg_tput_mean = (self.aggregated_result.throughput_mean or self.aggregated_result.throughput_p50 or 0) if self.aggregated_result else 0
        agg_calibrated = self._compute_calibrated_concurrency(agg_tput_mean, original_concurrency, 'Aggregated')
        if self.config.inferencex_sweep_enabled:
            agg_levels = self._generate_sweep_levels(agg_calibrated)
        else:
            agg_levels = [agg_calibrated]

        agg_sweep = self._run_sweep_for_arch(
            'Aggregated', agg_calibrated, agg_levels,
            lambda: self._create_aggregated_config(
                tp=agg_tp, num_gpus=total_gpus_agg,
                isl=self.config.isl, osl=self.config.osl,
                test_id='_placeholder_', use_concurrency=True
            ),
            total_gpus_agg
        )
        self.concurrency_sweep_results['aggregated'] = agg_sweep

        cal_agg = [r for r in agg_sweep if r['is_calibrated']]
        if cal_agg:
            matching = [r for _, r in self.all_test_results if hasattr(_, 'test_id') and _.test_id == cal_agg[0]['test_id']]
            if matching:
                self.calibrated_agg_result = matching[0]

        # --- Summary comparison at calibrated point ---
        if cal_results and cal_agg:
            self.log("", 'info')
            self.log(f"📊 Calibrated Load Comparison:", 'decision')
            pd_c = cal_results[0]
            agg_c = cal_agg[0]
            self.log(f"  PD  (c={pd_c['concurrency']}): TTFT={pd_c['ttft_p90']:.0f}ms, {pd_c['throughput_per_gpu']:.0f} tok/s/gpu", 'info')
            self.log(f"  Agg (c={agg_c['concurrency']}): TTFT={agg_c['ttft_p90']:.0f}ms, {agg_c['throughput_per_gpu']:.0f} tok/s/gpu", 'info')

        # Compare with overloaded
        if overloaded_ttft > 0 and cal_results:
            cal_pd_ttft = cal_results[0]['ttft_p90']
            ttft_improvement = ((overloaded_ttft - cal_pd_ttft) / overloaded_ttft) * 100
            self.log("", 'info')
            self.log("📉 Impact of load reduction on best PD config:", 'info')
            self.log(f"  TTFT:       {overloaded_ttft:.1f}ms → {cal_pd_ttft:.1f}ms "
                    f"({ttft_improvement:+.1f}%)", 'info')
            self.log(f"  Throughput: {overloaded_tput:.2f} → {cal_results[0]['throughput_mean']:.2f} req/s", 'info')

        # --- EPP-tuned calibrated load tests (if EPP tuning ran) ---
        if getattr(self, 'epp_benchmark_results', None) and not self._should_stop():
            self.log("", 'info')
            self.log("📊 Re-testing with EPP-tuned weights at calibrated load...", 'info')

            for arch, results in self.epp_benchmark_results.items():
                if not results:
                    continue
                best_name, best_weights, best_epp_result = min(
                    results, key=lambda x: x[2].ttft_p90 or float('inf')
                )

                if arch == 'pd' and best_split:
                    epp_test_id = f"step10-epp-{best_split.prefill_pods}p{best_split.decode_pods}d-ptp{best_split.prefill_tp}-dtp{best_split.decode_tp}"
                    if epp_test_id in self.completed_tests:
                        self.log(f"  ⏩ EPP PD test: resuming from DB", 'info')
                        continue
                    epp_config = self._create_pd_config(best_split)
                    epp_config.test_id = epp_test_id
                    epp_config.num_users = int(calibrated_concurrency)
                    epp_config.request_rate = int(calibrated_concurrency)
                    epp_config.epp_config = {
                        'preset': 'custom',
                        'plugins': {
                            'prefix_cache': {'enabled': True, 'weight': best_weights['prefix_cache_weight']},
                            'kv_cache': {'enabled': True, 'weight': best_weights['kv_cache_weight']},
                            'queue': {'enabled': True, 'weight': best_weights['queue_weight']},
                        }
                    }
                elif arch == 'aggregated' and self.aggregated_tp:
                    epp_test_id = f"step10-epp-aggregated-tp{self.aggregated_tp}"
                    if epp_test_id in self.completed_tests:
                        self.log(f"  ⏩ EPP Aggregated test: resuming from DB", 'info')
                        continue
                    epp_config = self._create_aggregated_config(
                        tp=self.aggregated_tp,
                        num_gpus=self.aggregated_gpus,
                        isl=self.config.isl,
                        osl=self.config.osl,
                        test_id=epp_test_id,
                        use_concurrency=True
                    )
                    epp_config.num_users = int(calibrated_concurrency)
                    epp_config.request_rate = int(calibrated_concurrency)
                    epp_config.epp_config = {
                        'preset': 'custom',
                        'plugins': {
                            'prefix_cache': {'enabled': True, 'weight': best_weights['prefix_cache_weight']},
                            'kv_cache': {'enabled': True, 'weight': best_weights['kv_cache_weight']},
                            'queue': {'enabled': True, 'weight': best_weights['queue_weight']},
                        }
                    }
                else:
                    continue

                epp_result = self.orchestrator.run_test(
                    epp_config,
                    cleanup=True,
                    log_callback=lambda msg: self.log(msg, 'info'),
                    stop_check=self._should_stop
                )
                self.all_test_results.append((epp_config, epp_result))
                self._save_test_to_database(epp_config, epp_result)

                if epp_result and epp_result.guidellm_success:
                    epp_ttft = epp_result.ttft_p90 or 0
                    epp_tput = epp_result.throughput_p90 or 0
                    self.log(f"  ✅ EPP {arch} at calibrated load: TTFT={epp_ttft:.1f}ms, "
                            f"Throughput={epp_tput:.2f} req/s", 'success')
                else:
                    self.log(f"  ❌ EPP {arch} calibrated load test failed", 'error')

                if self._should_stop():
                    break

