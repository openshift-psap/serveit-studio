"""Steps 10-11: Latency-bounded throughput search and calibrated load."""



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
        prefill_tpsg = self.optimal_prefill_tp.tpsg if self.optimal_prefill_tp else 5000
        service_time = (self.config.isl / prefill_tpsg) + (self.config.osl / decode_tpsg)
        response_time = concurrency / throughput_mean
        queue_time = max(0, response_time - service_time)
        utilization = service_time / response_time if response_time > 0 else 1

        # Calibrated concurrency = throughput × target_latency
        # Use 1s as the target latency floor — enough headroom for real workloads
        # but not so aggressive that it drops to unrealistically low concurrency
        target_response = max(service_time * 3, 1.0)
        cal_concurrency = max(1, int(throughput_mean * target_response))
        # Floor at 50% of user concurrency — never recommend less than half
        cal_concurrency = max(cal_concurrency, int(concurrency * 0.5))
        # Cap at 120% of user concurrency
        cal_concurrency = min(cal_concurrency, int(concurrency * 1.2))

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

        Supports three modes via config.concurrency_sweep_levels:
        - None: auto-generate ~6 levels up to 1.5× calibrated
        - Single int N: generate N levels centered on calibrated,
          step = ceil(calibrated * 0.2 / 10) * 10, overflow shifts above
        - List of ints: use those exact levels (calibrated always included)
        """
        custom = getattr(self.config, 'concurrency_sweep_levels', None)
        count = getattr(self.config, 'concurrency_sweep_count', None)
        step_pct = getattr(self.config, 'concurrency_sweep_step_pct', 20)

        if custom is not None:
            # List of explicit levels
            if isinstance(custom, list) and len(custom) > 1:
                levels = sorted(set(int(l) for l in custom if l > 0))
                if calibrated and calibrated not in levels:
                    levels.append(calibrated)
                    levels.sort()
                return levels

            # Single number N = requested count of levels
            n = int(custom[0]) if isinstance(custom, list) else int(custom)
            if n < 1:
                n = 6
            step = max(1, round(calibrated * step_pct / 100))

            below_count = (n - 1) // 2
            above_count = n - 1 - below_count

            # How many actually fit below (minimum level = step)
            max_below = max(0, (calibrated - step) // step)
            actual_below = min(below_count, max_below)
            # Shift overflow to above
            actual_above = above_count + (below_count - actual_below)

            levels = []
            for i in range(actual_below, 0, -1):
                levels.append(calibrated - i * step)
            levels.append(calibrated)
            for i in range(1, actual_above + 1):
                levels.append(calibrated + i * step)

            return [l for l in levels if l > 0]

        # Count-based generation (from UI count + step% inputs)
        # Step is percentage of user-requested concurrency, not calibrated
        if count and int(count) > 0:
            n = int(count)
            user_concurrency = int(self.config.qps)
            step = max(1, round(user_concurrency * step_pct / 100))

            def _round(v):
                if step >= 20:
                    return int(round(v / 10) * 10)
                elif step >= 5:
                    return int(round(v / 5) * 5)
                return v

            below_count = (n - 1) // 2
            above_count = n - 1 - below_count

            max_below = max(0, (calibrated - step) // step)
            actual_below = min(below_count, max_below)
            actual_above = above_count + (below_count - actual_below)

            levels = []
            for i in range(actual_below, 0, -1):
                levels.append(_round(calibrated - i * step))
            levels.append(calibrated)
            for i in range(1, actual_above + 1):
                levels.append(_round(calibrated + i * step))

            return [l for l in levels if l > 0]

        # Default: ~6 levels up to 1.5× calibrated
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

    def _run_sweep_for_arch(self, arch_label, calibrated, levels, create_config_fn, gpus, config_label=None):
        """Run concurrency sweep for one architecture, return list of results.

        Deploys pods once, runs all concurrency levels with pods still up,
        then cleans up at the end. Only guidellm is re-run per level.
        Results are saved incrementally to self.concurrency_sweep_results.
        """
        arch_key = arch_label.lower()
        if arch_key not in self.concurrency_sweep_results:
            self.concurrency_sweep_results[arch_key] = []
        results = self.concurrency_sweep_results[arch_key]
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
                self._append_sweep_result(results, result, concurrency, calibrated, gpus, test_id, config_label)
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

            self._append_sweep_result(results, result, concurrency, calibrated, gpus, test_id, config_label)

        # Cleanup after all levels are done
        if deploy_test_id:
            self.log(f"  🧹 Cleaning up {arch_label} sweep deployment...", 'info')
            cleanup_config = create_config_fn()
            cleanup_config.test_id = deploy_test_id
            self.orchestrator.cleanup_deployment(cleanup_config, log_callback=lambda msg: self.log(msg, 'info'))

        return results

    def _append_sweep_result(self, results, result, concurrency, calibrated, gpus, test_id, config_label=None):
        """Helper to build a sweep result dict."""
        tput = result.throughput_mean or result.throughput_p90 or 0
        output_tps = result.output_tps_mean or 0
        if output_tps == 0 and tput > 0:
            osl = getattr(self.config, 'osl', 100)
            output_tps = tput * osl
        ttft = result.ttft_p90 or 0
        interactivity = (output_tps / concurrency) if output_tps > 0 and concurrency > 0 else 0
        throughput_per_gpu = (output_tps / gpus) if gpus > 0 and output_tps > 0 else 0

        results.append({
            'concurrency': concurrency,
            'is_calibrated': concurrency == calibrated,
            'throughput_mean': round(tput, 2),
            'output_tps_mean': round(output_tps, 2),
            'interactivity': round(interactivity, 2),
            'throughput_per_gpu': round(throughput_per_gpu, 2),
            'ttft_p50': round(result.ttft_p50 or 0, 1),
            'ttft_p90': round(ttft, 1),
            'ttft_p95': round(result.ttft_p95 or 0, 1),
            'ttft_p99': round(result.ttft_p99 or 0, 1),
            'itl_p90': round(result.itl_p90 or 0, 1),
            'itl_p95': round(result.itl_p95 or 0, 1),
            'itl_p99': round(result.itl_p99 or 0, 1),
            'gpus': gpus,
            'test_id': test_id,
            'config_label': config_label,
            'cache_hit_pct': getattr(result, 'cache_hit_pct', None),
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
        sweep_on = self.config.inferencex_sweep_enabled or getattr(self.config, 'concurrency_sweep_count', None)
        all_configs = getattr(self.config, 'concurrency_sweep_all_configs', False)
        max_configs = getattr(self.config, 'concurrency_sweep_max_configs', None)

        def _tput_of(result):
            return result.throughput_mean or result.throughput_p90 or 0

        def _score(result):
            ttft = result.ttft_p90 if result.ttft_p90 else 1e9
            tput = _tput_of(result) or 0.001
            return ttft / tput

        # --- Build unified config list: recommendation configs first, then by score ---
        # Collect all candidates: ('pd', split, result), ('agg', tp, result), or ('ep', split, result)
        all_candidates = []
        if self.pareto_results:
            for split, result in self.pareto_results:
                if result.ttft_p90 and _tput_of(result) > 0:
                    all_candidates.append(('pd', split, result))
        if hasattr(self, 'aggregated_search_results') and self.aggregated_search_results:
            for tp, result in self.aggregated_search_results:
                if result.ttft_p90 and _tput_of(result) > 0:
                    all_candidates.append(('agg', tp, result))
        elif self.aggregated_tp and self.aggregated_result:
            if self.aggregated_result.ttft_p90 and _tput_of(self.aggregated_result) > 0:
                all_candidates.append(('agg', self.aggregated_tp, self.aggregated_result))
        if hasattr(self, 'ep_results') and self.ep_results:
            for split, result in self.ep_results:
                if result.ttft_p90 and _tput_of(result) > 0:
                    all_candidates.append(('ep', split, result))

        # Pick the 4 recommendation configs (deduplicated by config identity)
        selected = []
        seen_keys = set()

        def _config_key(c):
            if c[0] in ('pd', 'ep'):
                s = c[1]
                return (c[0], s.prefill_pods, s.prefill_tp, s.decode_pods, s.decode_tp)
            return ('agg', c[1])

        def _add_unique(candidate):
            key = _config_key(candidate)
            if key not in seen_keys:
                seen_keys.add(key)
                selected.append(candidate)

        if all_candidates:
            if all_configs:
                # All 4 recommendation configs first
                _add_unique(min(all_candidates, key=lambda x: _score(x[2])))
                _add_unique(min(all_candidates, key=lambda x: x[2].ttft_p90 or 1e9))
                _add_unique(max(all_candidates, key=lambda x: _tput_of(x[2])))
                def _gpus(c):
                    if c[0] in ('pd', 'ep'):
                        return c[1].prefill_pods * c[1].prefill_tp + c[1].decode_pods * c[1].decode_tp
                    else:
                        return c[1] * (self.config.total_gpus // c[1]) if c[1] else self.config.total_gpus
                _add_unique(max(all_candidates, key=lambda x: _tput_of(x[2]) / max(_gpus(x), 1)))
                # Fill remaining slots by score
                limit = int(max_configs) if max_configs else len(all_candidates)
                for c in sorted(all_candidates, key=lambda x: _score(x[2])):
                    if len(selected) >= limit:
                        break
                    _add_unique(c)
            else:
                # Best PD and best aggregated by balanced score
                pd_candidates = [c for c in all_candidates if c[0] == 'pd']
                agg_candidates = [c for c in all_candidates if c[0] == 'agg']
                ep_candidates = [c for c in all_candidates if c[0] == 'ep']
                if pd_candidates:
                    _add_unique(min(pd_candidates, key=lambda x: _score(x[2])))
                if ep_candidates:
                    _add_unique(min(ep_candidates, key=lambda x: _score(x[2])))
                if agg_candidates:
                    _add_unique(min(agg_candidates, key=lambda x: _score(x[2])))

        self.log(f"Concurrency sweep: {len(selected)} configs selected", 'info')
        for c in selected:
            if c[0] in ('pd', 'ep'):
                arch_label = c[0].upper()
                self.log(f"  {c[1].prefill_pods}P×TP{c[1].prefill_tp} + {c[1].decode_pods}D×TP{c[1].decode_tp} ({arch_label})", 'info')
            else:
                replicas = self.config.total_gpus // c[1] if c[1] else self.config.total_gpus
                self.log(f"  {replicas}×TP{c[1]} (Aggregated)", 'info')

        # --- Sweep PD configs ---
        pd_configs = [(c[1], c[2]) for c in selected if c[0] == 'pd']
        cal_results = []
        for cfg_idx, (split, pd_result) in enumerate(pd_configs):
            if self._should_stop():
                break
            pd_tput_mean = _tput_of(pd_result)
            label = f"{split.prefill_pods}P×TP{split.prefill_tp} + {split.decode_pods}D×TP{split.decode_tp}"
            pd_calibrated = self._compute_calibrated_concurrency(pd_tput_mean, original_concurrency, f'PD ({label})')
            if sweep_on:
                pd_levels = self._generate_sweep_levels(pd_calibrated)
            else:
                pd_levels = [pd_calibrated]

            total_gpus_pd = (split.prefill_pods * split.prefill_tp +
                             split.decode_pods * split.decode_tp)

            sweep_key = f"pd-{split.prefill_pods}p{split.decode_pods}d-tp{split.prefill_tp}-{split.decode_tp}"
            current_split = split
            pd_sweep = self._run_sweep_for_arch(
                sweep_key, pd_calibrated, pd_levels,
                lambda: self._create_pd_config(current_split),
                total_gpus_pd, config_label=label
            )

            cr = [r for r in pd_sweep if r['is_calibrated']]
            if cr:
                cal_results.extend(cr)
                if cfg_idx == 0:
                    matching = [r for _, r in self.all_test_results if hasattr(_, 'test_id') and _.test_id == cr[0]['test_id']]
                    if matching:
                        self.calibrated_pd_result = matching[0]

        if self._should_stop():
            return

        # --- Sweep Aggregated configs ---
        agg_configs = [(c[1], c[2]) for c in selected if c[0] == 'agg']
        cal_agg_results = []

        if not agg_configs:
            self.log("⚠️  No aggregated baseline — skipping aggregated sweep", 'warning')
        else:
            total_gpus_agg = self.aggregated_gpus or self.config.total_gpus
            cal_agg_results = []
            for cfg_idx, (agg_tp, agg_result) in enumerate(agg_configs):
                if self._should_stop():
                    break
                agg_tput_mean = _tput_of(agg_result)
                agg_replicas = total_gpus_agg // agg_tp if agg_tp else total_gpus_agg
                label = f"{agg_replicas}×TP{agg_tp}"
                agg_calibrated = self._compute_calibrated_concurrency(agg_tput_mean, original_concurrency, f'Aggregated ({label})')
                if sweep_on:
                    agg_levels = self._generate_sweep_levels(agg_calibrated)
                else:
                    agg_levels = [agg_calibrated]

                sweep_key = f"aggregated-tp{agg_tp}"
                current_tp = agg_tp
                agg_sweep = self._run_sweep_for_arch(
                    sweep_key, agg_calibrated, agg_levels,
                    lambda: self._create_aggregated_config(
                        tp=current_tp, num_gpus=total_gpus_agg,
                        isl=self.config.isl, osl=self.config.osl,
                        test_id='_placeholder_', use_concurrency=True
                    ),
                    total_gpus_agg, config_label=label
                )

                cr = [r for r in agg_sweep if r['is_calibrated']]
                if cr:
                    cal_agg_results.extend(cr)
                    if cfg_idx == 0:
                        matching = [r for _, r in self.all_test_results if hasattr(_, 'test_id') and _.test_id == cr[0]['test_id']]
                        if matching:
                            self.calibrated_agg_result = matching[0]

            # --- Summary comparison at calibrated point ---
            if cal_results and cal_agg_results:
                self.log("", 'info')
                self.log("📊 Calibrated Load Comparison:", 'decision')
                pd_c = cal_results[0]
                agg_c = cal_agg_results[0]
                self.log(f"  PD  (c={pd_c['concurrency']}): TTFT={pd_c['ttft_p90']:.0f}ms, {pd_c['throughput_per_gpu']:.0f} tok/s/gpu", 'info')
                self.log(f"  Agg (c={agg_c['concurrency']}): TTFT={agg_c['ttft_p90']:.0f}ms, {agg_c['throughput_per_gpu']:.0f} tok/s/gpu", 'info')

        # --- Sweep EP configs ---
        ep_configs = [(c[1], c[2]) for c in selected if c[0] == 'ep']
        cal_ep_results = []
        for cfg_idx, (split, ep_result) in enumerate(ep_configs):
            if self._should_stop():
                break
            ep_tput_mean = _tput_of(ep_result)
            label = f"{split.prefill_pods}P×TP{split.prefill_tp} + {split.decode_pods}D×TP{split.decode_tp} (EP)"
            ep_calibrated = self._compute_calibrated_concurrency(ep_tput_mean, original_concurrency, f'EP ({label})')
            if sweep_on:
                ep_levels = self._generate_sweep_levels(ep_calibrated)
            else:
                ep_levels = [ep_calibrated]

            total_gpus_ep = (split.prefill_pods * split.prefill_tp +
                             split.decode_pods * split.decode_tp)

            sweep_key = f"ep-{split.prefill_pods}p{split.decode_pods}d-ptp{split.prefill_tp}-dtp{split.decode_tp}"
            current_split = split
            ep_sweep = self._run_sweep_for_arch(
                sweep_key, ep_calibrated, ep_levels,
                lambda: self._create_ep_config(current_split),
                total_gpus_ep, config_label=label
            )

            cr = [r for r in ep_sweep if r['is_calibrated']]
            if cr:
                cal_ep_results.extend(cr)

        # Compare with original (overloaded) result
        if pd_configs and cal_results:
            orig_result = pd_configs[0][1]
            orig_ttft = orig_result.ttft_p90 or orig_result.ttft_p50 or 0
            orig_tput = orig_result.throughput_mean or orig_result.throughput_p90 or 0
            if orig_ttft > 0:
                cal_pd_ttft = cal_results[0]['ttft_p90']
                ttft_improvement = ((orig_ttft - cal_pd_ttft) / orig_ttft) * 100
                self.log("", 'info')
                self.log("📉 Impact of load reduction on best PD config:", 'info')
                self.log(f"  TTFT:       {orig_ttft:.1f}ms → {cal_pd_ttft:.1f}ms "
                        f"({ttft_improvement:+.1f}%)", 'info')
                self.log(f"  Throughput: {orig_tput:.2f} → {cal_results[0]['throughput_mean']:.2f} req/s", 'info')

        # --- EPP-tuned calibrated load tests (if EPP tuning improved over baseline) ---
        use_epp_tuned = getattr(self.config, 'concurrency_sweep_use_epp_tuned', False)
        if use_epp_tuned and getattr(self, 'epp_benchmark_results', None) and not self._should_stop():
            # Check if any EPP result actually improved over baseline
            has_improvement = False
            for arch, results in self.epp_benchmark_results.items():
                valid_epp = [r for r in results if r[2] is not None]
                if not valid_epp:
                    continue
                best_epp_ttft = min((r[2].ttft_p90 for r in valid_epp if r[2].ttft_p90), default=float('inf'))
                baseline_ttft = None
                if arch == 'aggregated' and self.aggregated_result:
                    baseline_ttft = self.aggregated_result.ttft_p90
                elif arch == 'pd' and self.pareto_results:
                    baseline_ttft = min((r[1].ttft_p90 for s, r in self.pareto_results if r.ttft_p90), default=None)
                elif arch == 'ep' and getattr(self, 'best_ep_result', None):
                    baseline_ttft = self.best_ep_result.ttft_p90
                if baseline_ttft and best_epp_ttft < baseline_ttft:
                    has_improvement = True
                    break

            if not has_improvement:
                self.log("", 'info')
                self.log("⏩ Skipping EPP-tuned sweep — EPP weights did not improve over baseline", 'info')
            else:
                self.log("", 'info')
                self.log("📊 Re-testing with EPP-tuned weights at calibrated load (EPP improved over baseline)...", 'info')

                for arch, results in self.epp_benchmark_results.items():
                    valid_epp = [r for r in results if r[2] is not None]
                    if not valid_epp:
                        continue
                    best_name, best_weights, best_epp_result = min(
                        valid_epp, key=lambda x: x[2].ttft_p90 or float('inf')
                    )

                    if arch == 'pd' and pd_configs:
                        best_split = pd_configs[0][0]
                        epp_test_id = f"step10-epp-{best_split.prefill_pods}p{best_split.decode_pods}d-ptp{best_split.prefill_tp}-dtp{best_split.decode_tp}"
                        if epp_test_id in self.completed_tests:
                            self.log("  ⏩ EPP PD test: resuming from DB", 'info')
                            continue
                        epp_config = self._create_pd_config(best_split)
                        epp_config.test_id = epp_test_id
                        epp_config.num_users = int(cal_results[0]['concurrency']) if cal_results else int(self.config.qps)
                        epp_config.request_rate = epp_config.num_users
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
                            self.log("  ⏩ EPP Aggregated test: resuming from DB", 'info')
                            continue
                        epp_config = self._create_aggregated_config(
                            tp=self.aggregated_tp,
                            num_gpus=self.aggregated_gpus or self.config.total_gpus,
                            isl=self.config.isl,
                            osl=self.config.osl,
                            test_id=epp_test_id,
                            use_concurrency=True
                        )
                        cal_conc = cal_agg_results[0]['concurrency'] if cal_agg_results else int(self.config.qps)
                        epp_config.num_users = int(cal_conc)
                        epp_config.request_rate = int(cal_conc)
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

