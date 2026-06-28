"""Step 13: Cache hit sweep — measure performance across prefix cache hit ratios."""

import copy
from typing import Dict, List, Optional


class CacheSweepMixin:
    """Mixin providing cache hit sweep methods for RecipeOptimizer."""

    def _get_cache_sweep_levels(self) -> List[int]:
        levels = self.config.cache_sweep_levels
        if not levels:
            levels = [0, 10, 30, 50, 70, 100]
        return sorted(set(max(0, min(100, l)) for l in levels))

    def _generate_cache_dataset_for_level(self, hit_pct: int) -> Optional[str]:
        """Generate a prefix cache dataset for a specific hit percentage.

        Temporarily overrides config fields, generates the dataset via the
        existing _generate_prefix_cache_dataset(), then restores originals.
        Returns the dataset file path, or None on failure.
        """
        saved = {
            'prefix_cache_hit_pct': self.config.prefix_cache_hit_pct,
            'prefix_cache_mode': self.config.prefix_cache_mode,
            'prefix_cache_groups': self.config.prefix_cache_groups,
            'prefix_cache_seed': self.config.prefix_cache_seed,
            'workload_mode': self.config.workload_mode,
            'dataset_source': getattr(self.config, 'dataset_source', None),
        }
        try:
            self.config.prefix_cache_hit_pct = hit_pct
            self.config.prefix_cache_mode = self.config.cache_sweep_mode or 'identical'
            self.config.prefix_cache_groups = self.config.cache_sweep_groups or 5
            self.config.prefix_cache_seed = None

            if hit_pct == 0:
                self.config.workload_mode = 'synthetic'
                return None

            self._generate_prefix_cache_dataset()
            return getattr(self.config, 'dataset_source', None)
        finally:
            for k, v in saved.items():
                setattr(self.config, k, v)

    def _run_cache_sweep_for_arch(self, arch_label: str, concurrency: int,
                                   levels: List[int], create_config_fn,
                                   gpus: int, concurrency_tag: str = '') -> List[Dict]:
        """Run cache hit sweep for one architecture, return list of results."""
        results = []
        tag = f"-{concurrency_tag}" if concurrency_tag else ""
        self.log(f"\n🗂️  Cache Sweep: {arch_label} ({len(levels)} levels, c={concurrency}{tag})", 'info')

        # Check which levels need testing vs already completed
        levels_to_run = []
        for hit_pct in levels:
            test_id = f"step13-cache{tag}-{arch_label.lower()}-h{hit_pct}"
            if test_id in self.completed_tests:
                row = self.completed_tests[test_id]
                result = self._make_test_result_from_db(row)
                self.log(f"  ⏩ h={hit_pct}%: resuming from DB", 'info')
                tput = result.throughput_mean or result.throughput_p90 or 0
                output_tps = result.output_tps_mean or 0
                ttft = result.ttft_p90 or 0
                # Extract actual hit rate from DB for resumed tests
                _actual_hr = None
                _hits_r = None
                _queries_r = None
                if self.db_manager and self.run_id:
                    try:
                        import json as _json
                        with self.db_manager.get_connection() as _conn:
                            _row = _conn.execute(
                                'SELECT metrics_json FROM test_configurations WHERE run_id=? AND config_name=?',
                                (self.run_id, test_id)).fetchone()
                            if _row and _row[0]:
                                _mj = _json.loads(_row[0])
                                _pm = _mj.get('prometheus_metrics', {})
                                _h = _pm.get('vllm_prefix_cache_hits_rate', {}).get('avg', 0)
                                _q = _pm.get('vllm_prefix_cache_queries_rate', {}).get('avg', 0)
                                _actual_hr = round(_h / _q * 100, 1) if _q > 0 else 0.0
                                _hits_r = round(_h, 1)
                                _queries_r = round(_q, 1)
                    except Exception:
                        pass
                results.append({'hit_pct': hit_pct, 'actual_hit_rate': _actual_hr,
                    'cache_hits_rate': _hits_r, 'cache_queries_rate': _queries_r,
                    'throughput_mean': round(tput, 2),
                    'output_tps_mean': round(output_tps, 2),
                    'ttft_p50': round(result.ttft_p50 or 0, 1),
                    'ttft_p90': round(ttft, 1),
                    'ttft_p95': round(result.ttft_p95 or 0, 1),
                    'ttft_p99': round(result.ttft_p99 or 0, 1),
                    'itl_p90': round(result.itl_p90 or 0, 1),
                    'itl_p95': round(result.itl_p95 or 0, 1),
                    'itl_p99': round(result.itl_p99 or 0, 1),
                    'gpus': gpus, 'concurrency': concurrency, 'test_id': test_id})
            else:
                levels_to_run.append(hit_pct)

        if not levels_to_run:
            return results

        # All levels share the same pods — use first level's test_id for deployment
        deploy_test_id = f"step13-cache{tag}-{arch_label.lower()}-h{levels_to_run[0]}"

        for i, hit_pct in enumerate(levels_to_run):
            if self._should_stop():
                break

            test_id = f"step13-cache{tag}-{arch_label.lower()}-h{hit_pct}"
            dataset_path = self._generate_cache_dataset_for_level(hit_pct)

            config = create_config_fn()
            config.test_id = deploy_test_id
            config.num_users = concurrency
            config.request_rate = concurrency
            config.enable_prefix_caching = True

            if dataset_path and hit_pct > 0:
                config.workload_mode = 'dataset'
                config.dataset_source = dataset_path
                config.dataset_column = 'prompt'
                config.dataset_max_output = self.config.osl
            else:
                config.workload_mode = 'synthetic'

            is_first = (i == 0)

            result = self.orchestrator.run_test(
                config,
                cleanup=False,
                skip_deploy=not is_first,
                log_callback=lambda msg: self.log(msg, 'info'),
                stop_check=self._should_stop
            )

            # Save with the actual test_id (not the deploy_test_id)
            config.test_id = test_id

            self.all_test_results.append((config, result))
            self._save_test_to_database(config, result)
            self._check_pod_errors(config, result)
            self._check_request_errors(config, result)

            if not result or not result.guidellm_success:
                self.log(f"  ❌ h={hit_pct}%: test failed, skipping", 'warning')
                continue

            tput = result.throughput_mean or result.throughput_p90 or 0
            output_tps = result.output_tps_mean or 0
            ttft = result.ttft_p90 or 0

            # Extract actual prefix cache hit rate from Prometheus metrics
            actual_hit_rate = None
            cache_hits_rate = None
            cache_queries_rate = None
            if self.db_manager and self.run_id:
                try:
                    import json as _json
                    with self.db_manager.get_connection() as conn:
                        row = conn.execute(
                            'SELECT metrics_json FROM test_configurations WHERE run_id=? AND config_name=?',
                            (self.run_id, test_id)).fetchone()
                        if row and row[0]:
                            mj = _json.loads(row[0])
                            pm = mj.get('prometheus_metrics', {})
                            hits = pm.get('vllm_prefix_cache_hits_rate', {}).get('avg', 0)
                            queries = pm.get('vllm_prefix_cache_queries_rate', {}).get('avg', 0)
                            if queries > 0:
                                actual_hit_rate = round(hits / queries * 100, 1)
                            else:
                                actual_hit_rate = 0.0
                            cache_hits_rate = round(hits, 1)
                            cache_queries_rate = round(queries, 1)
                except Exception:
                    pass

            results.append({
                'hit_pct': hit_pct,
                'actual_hit_rate': actual_hit_rate,
                'cache_hits_rate': cache_hits_rate,
                'cache_queries_rate': cache_queries_rate,
                'throughput_mean': round(tput, 2),
                'output_tps_mean': round(output_tps, 2),
                'ttft_p50': round(result.ttft_p50 or 0, 1),
                'ttft_p90': round(ttft, 1),
                'ttft_p95': round(result.ttft_p95 or 0, 1),
                'ttft_p99': round(result.ttft_p99 or 0, 1),
                'itl_p90': round(result.itl_p90 or 0, 1),
                'itl_p95': round(result.itl_p95 or 0, 1),
                'itl_p99': round(result.itl_p99 or 0, 1),
                'gpus': gpus,
                'concurrency': concurrency,
                'test_id': test_id,
            })

            self.log(f"  ✅ h={hit_pct}%: TTFT={ttft:.0f}ms, "
                     f"tput={tput:.1f} req/s, "
                     f"output={output_tps:.1f} tok/s", 'info')

            self._save_sweep_progress()

        # Cleanup after all levels are done
        if deploy_test_id:
            self.log(f"  🧹 Cleaning up {arch_label} cache sweep deployment...", 'info')
            cleanup_config = create_config_fn()
            cleanup_config.test_id = deploy_test_id
            self.orchestrator.cleanup_deployment(cleanup_config, log_callback=lambda msg: self.log(msg, 'info'))

        return results

    def _run_cache_hit_sweep(self):
        """Step 13: Cache hit sweep.

        Tests the best PD/EP and Aggregated configs at multiple prefix cache
        hit ratios. Optionally runs at both user-defined and calibrated
        concurrency levels.
        """
        if not hasattr(self, 'cache_sweep_results'):
            self.cache_sweep_results = {}

        levels = self._get_cache_sweep_levels()
        mode = self.config.cache_sweep_mode or 'identical'

        self.log(f"Cache hit levels: {levels}", 'info')
        self.log(f"Cache mode: {mode}", 'info')

        user_concurrency = int(self.config.qps)
        calibrated_concurrency = None
        if self.config.cache_sweep_use_calibrated:
            calibrated_concurrency = getattr(self, 'achievable_concurrency', None)
            if calibrated_concurrency:
                calibrated_concurrency = int(calibrated_concurrency)
                self.log(f"Calibrated concurrency: {calibrated_concurrency} (from Step 11)", 'info')
            else:
                self.log("⚠️  No calibrated concurrency available (Step 11 didn't run). "
                         "Falling back to user-defined concurrency.", 'warning')

        # --- Find best configs ---
        best_split = None
        best_pd_result = None
        total_gpus_pd = 0
        if hasattr(self, 'pareto_results') and self.pareto_results:
            def _pd_score(x):
                ttft = x[1].ttft_p90 if x[1].ttft_p90 else 1e9
                tput = x[1].throughput_mean or x[1].throughput_p90 or 0.001
                return ttft / tput
            best_split, best_pd_result = min(self.pareto_results, key=_pd_score)
            total_gpus_pd = (best_split.prefill_pods * best_split.prefill_tp +
                             best_split.decode_pods * best_split.decode_tp)
        elif hasattr(self, 'best_ep_config') and self.best_ep_config:
            best_split = self.best_ep_config
            best_pd_result = self.best_ep_result
            total_gpus_pd = (best_split.prefill_pods * best_split.prefill_tp +
                             best_split.decode_pods * best_split.decode_tp)

        agg_tp = getattr(self, 'aggregated_tp', None)
        agg_result = getattr(self, 'aggregated_result', None)
        total_gpus_agg = getattr(self, 'aggregated_gpus', 0) or self.config.total_gpus

        # --- Sweep at user-defined concurrency ---
        if self.config.cache_sweep_enabled:
            self.log(f"\n{'='*60}", 'info')
            self.log(f"Cache Sweep at user-defined concurrency ({user_concurrency})", 'decision')
            self.log(f"{'='*60}", 'info')

            if best_split:
                pd_label = 'EP' if getattr(best_split, 'enable_expert_parallel', False) else 'PD'
                pd_config_label = f"{best_split.prefill_pods}P×TP{best_split.prefill_tp} + {best_split.decode_pods}D×TP{best_split.decode_tp}"
                sweep = self._run_cache_sweep_for_arch(
                    pd_label, user_concurrency, levels,
                    lambda: self._create_pd_config(best_split) if pd_label == 'PD' else self._create_ep_config(split=best_split),
                    total_gpus_pd
                )
                for r in sweep:
                    r['config_label'] = pd_config_label
                self.cache_sweep_results[pd_label.lower()] = sweep

            if agg_tp and agg_result and not self._should_stop():
                agg_replicas = total_gpus_agg // agg_tp if agg_tp else total_gpus_agg
                agg_config_label = f"{agg_replicas}×TP{agg_tp}"
                sweep = self._run_cache_sweep_for_arch(
                    'Aggregated', user_concurrency, levels,
                    lambda: self._create_aggregated_config(
                        tp=agg_tp, num_gpus=total_gpus_agg,
                        isl=self.config.isl, osl=self.config.osl,
                        test_id='_placeholder_', use_concurrency=True
                    ),
                    total_gpus_agg
                )
                for r in sweep:
                    r['config_label'] = agg_config_label
                self.cache_sweep_results['aggregated'] = sweep

        # --- Sweep at calibrated concurrency ---
        if self.config.cache_sweep_use_calibrated and calibrated_concurrency and not self._should_stop():
            self.log(f"\n{'='*60}", 'info')
            self.log(f"Cache Sweep at calibrated concurrency ({calibrated_concurrency})", 'decision')
            self.log(f"{'='*60}", 'info')

            if best_split:
                pd_label = 'EP' if getattr(best_split, 'enable_expert_parallel', False) else 'PD'
                pd_config_label = f"{best_split.prefill_pods}P×TP{best_split.prefill_tp} + {best_split.decode_pods}D×TP{best_split.decode_tp}"
                sweep = self._run_cache_sweep_for_arch(
                    pd_label, calibrated_concurrency, levels,
                    lambda: self._create_pd_config(best_split) if pd_label == 'PD' else self._create_ep_config(split=best_split),
                    total_gpus_pd, concurrency_tag='cal'
                )
                for r in sweep:
                    r['config_label'] = pd_config_label
                self.cache_sweep_results[f'{pd_label.lower()}_calibrated'] = sweep

            if agg_tp and agg_result and not self._should_stop():
                agg_replicas = total_gpus_agg // agg_tp if agg_tp else total_gpus_agg
                agg_config_label = f"{agg_replicas}×TP{agg_tp}"
                sweep = self._run_cache_sweep_for_arch(
                    'Aggregated', calibrated_concurrency, levels,
                    lambda: self._create_aggregated_config(
                        tp=agg_tp, num_gpus=total_gpus_agg,
                        isl=self.config.isl, osl=self.config.osl,
                        test_id='_placeholder_', use_concurrency=True
                    ),
                    total_gpus_agg, concurrency_tag='cal'
                )
                for r in sweep:
                    r['config_label'] = agg_config_label
                self.cache_sweep_results['aggregated_calibrated'] = sweep

        # --- Summary ---
        if self.cache_sweep_results:
            self.log(f"\n📊 Cache Hit Sweep Summary:", 'decision')
            for arch_key, sweep in self.cache_sweep_results.items():
                if not sweep:
                    continue
                first = sweep[0]
                last = sweep[-1]
                self.log(f"  {arch_key}: h={first['hit_pct']}%→{last['hit_pct']}% | "
                         f"TTFT {first['ttft_p90']:.0f}→{last['ttft_p90']:.0f}ms | "
                         f"Throughput {first['throughput_mean']:.1f}→{last['throughput_mean']:.1f} req/s", 'info')
