"""Step 13: Cache hit sweep — measure performance across prefix cache hit ratios."""

import copy
from typing import Dict, List, Optional


class CacheSweepMixin:
    """Mixin providing cache hit sweep methods for RecipeOptimizer."""

    def _get_cache_sweep_levels(self) -> List[int]:
        levels = self.config.cache_sweep_levels
        if levels:
            return sorted(set(max(0, min(100, l)) for l in levels))

        step_pct = getattr(self.config, 'cache_sweep_step_pct', 10)
        if step_pct and int(step_pct) > 0:
            step = int(step_pct)
            return list(range(0, 101, step))

        return [0, 10, 30, 50, 70, 100]

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
                if hasattr(self, 'random_dataset_path') and self.random_dataset_path:
                    return self.random_dataset_path
                return self._generate_random_dataset()

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

            # Override EPP to cache_optimized for cache sweep — prefix-cache-scorer
            # must have high weight for requests to route to pods with cached prefixes
            if is_first:
                epp = config.epp_config or self._build_epp_config() or {}
                cache_epp = dict(epp)
                cache_epp['preset'] = 'cache_optimized'
                try:
                    from core import PrereqManager
                    prereq = PrereqManager(
                        namespace=self.config.namespace,
                        kubectl_runner=self.orchestrator.deployment_manager.kubectl,
                    )
                    prereq.update_epp_config(
                        config.architecture, cache_epp,
                        log_callback=lambda msg: self.log(msg, 'info')
                    )
                except Exception as e:
                    self.log(f"  ⚠️  Failed to update EPP for cache sweep: {e}", 'warning')

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
        calibrated_concurrency = getattr(self, 'achievable_concurrency', None)
        if calibrated_concurrency:
            calibrated_concurrency = int(calibrated_concurrency)

        # Use calibrated concurrency as default when available
        default_concurrency = calibrated_concurrency or user_concurrency
        if calibrated_concurrency and calibrated_concurrency != user_concurrency:
            self.log(f"Using calibrated concurrency: {calibrated_concurrency} (from Step 11, user requested {user_concurrency})", 'info')
        else:
            self.log(f"Using concurrency: {default_concurrency}", 'info')

        # --- Select configs using same recommendation logic as concurrency sweep ---
        def _tput_of(result):
            return result.throughput_mean or result.throughput_p90 or 0

        def _score(result):
            ttft = result.ttft_p90 if result.ttft_p90 else 1e9
            tput = _tput_of(result) or 0.001
            return ttft / tput

        all_candidates = []
        if hasattr(self, 'pareto_results') and self.pareto_results:
            for split, result in self.pareto_results:
                if result.ttft_p90 and _tput_of(result) > 0:
                    all_candidates.append(('pd', split, result))
        if hasattr(self, 'aggregated_search_results') and self.aggregated_search_results:
            for tp, result in self.aggregated_search_results:
                if result.ttft_p90 and _tput_of(result) > 0:
                    all_candidates.append(('agg', tp, result))
        elif getattr(self, 'aggregated_tp', None) and getattr(self, 'aggregated_result', None):
            if self.aggregated_result.ttft_p90 and _tput_of(self.aggregated_result) > 0:
                all_candidates.append(('agg', self.aggregated_tp, self.aggregated_result))

        selected = []
        seen_keys = set()
        def _config_key(c):
            if c[0] == 'pd':
                s = c[1]
                return ('pd', s.prefill_pods, s.prefill_tp, s.decode_pods, s.decode_tp)
            return ('agg', c[1])
        def _add_unique(candidate):
            key = _config_key(candidate)
            if key not in seen_keys:
                seen_keys.add(key)
                selected.append(candidate)

        cache_sweep_all = getattr(self.config, 'cache_sweep_all_configs', False)
        cache_sweep_max = getattr(self.config, 'cache_sweep_max_configs', None)

        if all_candidates:
            if cache_sweep_all:
                # All 4 recommendation configs first
                _add_unique(min(all_candidates, key=lambda x: _score(x[2])))
                _add_unique(min(all_candidates, key=lambda x: x[2].ttft_p90 or 1e9))
                _add_unique(max(all_candidates, key=lambda x: _tput_of(x[2])))
                def _gpus(c):
                    if c[0] == 'pd':
                        return c[1].prefill_pods * c[1].prefill_tp + c[1].decode_pods * c[1].decode_tp
                    else:
                        return c[1] * (self.config.total_gpus // c[1]) if c[1] else self.config.total_gpus
                _add_unique(max(all_candidates, key=lambda x: _tput_of(x[2]) / max(_gpus(x), 1)))
                # Fill remaining slots by score
                limit = int(cache_sweep_max) if cache_sweep_max else len(all_candidates)
                for c in sorted(all_candidates, key=lambda x: _score(x[2])):
                    if len(selected) >= limit:
                        break
                    _add_unique(c)
            else:
                # Best PD and best aggregated by balanced score
                pd_candidates = [c for c in all_candidates if c[0] == 'pd']
                agg_candidates = [c for c in all_candidates if c[0] == 'agg']
                if pd_candidates:
                    _add_unique(min(pd_candidates, key=lambda x: _score(x[2])))
                if agg_candidates:
                    _add_unique(min(agg_candidates, key=lambda x: _score(x[2])))

        self.log(f"Cache sweep: {len(selected)} configs selected", 'info')

        # --- Sweep at default concurrency (calibrated if available, otherwise user-defined) ---
        if self.config.cache_sweep_enabled:
            self.log(f"\n{'='*60}", 'info')
            self.log(f"Cache Sweep at concurrency {default_concurrency}" +
                     (f" (calibrated)" if calibrated_concurrency else ""), 'decision')
            self.log(f"{'='*60}", 'info')

            for c in selected:
                if self._should_stop():
                    break
                if c[0] == 'pd':
                    split = c[1]
                    pd_label = 'EP' if getattr(split, 'enable_expert_parallel', False) else 'PD'
                    config_label = f"{split.prefill_pods}P×TP{split.prefill_tp} + {split.decode_pods}D×TP{split.decode_tp}"
                    total_gpus = split.prefill_pods * split.prefill_tp + split.decode_pods * split.decode_tp
                    current_split = split
                    sweep = self._run_cache_sweep_for_arch(
                        f"{pd_label}-{config_label}", default_concurrency, levels,
                        lambda: self._create_pd_config(current_split) if pd_label == 'PD' else self._create_ep_config(split=current_split),
                        total_gpus
                    )
                    for r in sweep:
                        r['config_label'] = config_label
                    sweep_key = f"{pd_label.lower()}-{split.prefill_pods}p{split.decode_pods}d"
                    self.cache_sweep_results[sweep_key] = sweep
                else:
                    tp = c[1]
                    total_gpus_agg = self.config.total_gpus
                    agg_replicas = total_gpus_agg // tp if tp else total_gpus_agg
                    config_label = f"{agg_replicas}×TP{tp}"
                    current_tp = tp
                    sweep = self._run_cache_sweep_for_arch(
                        f"Aggregated-{config_label}", default_concurrency, levels,
                        lambda: self._create_aggregated_config(
                            tp=current_tp, num_gpus=total_gpus_agg,
                            isl=self.config.isl, osl=self.config.osl,
                            test_id='_placeholder_', use_concurrency=True
                        ),
                        total_gpus_agg
                    )
                    for r in sweep:
                        r['config_label'] = config_label
                    sweep_key = f"aggregated-tp{tp}"
                    self.cache_sweep_results[sweep_key] = sweep

        # --- Optional second sweep at user-defined concurrency (if different from calibrated) ---
        if self.config.cache_sweep_use_calibrated and calibrated_concurrency and user_concurrency != calibrated_concurrency and not self._should_stop():
            self.log(f"\n{'='*60}", 'info')
            self.log(f"Cache Sweep at user-defined concurrency ({user_concurrency})", 'decision')
            self.log(f"{'='*60}", 'info')

            for c in selected:
                if self._should_stop():
                    break
                if c[0] == 'pd':
                    split = c[1]
                    pd_label = 'EP' if getattr(split, 'enable_expert_parallel', False) else 'PD'
                    config_label = f"{split.prefill_pods}P×TP{split.prefill_tp} + {split.decode_pods}D×TP{split.decode_tp}"
                    total_gpus = split.prefill_pods * split.prefill_tp + split.decode_pods * split.decode_tp
                    current_split = split
                    sweep = self._run_cache_sweep_for_arch(
                        f"{pd_label}-{config_label}", user_concurrency, levels,
                        lambda: self._create_pd_config(current_split) if pd_label == 'PD' else self._create_ep_config(split=current_split),
                        total_gpus, concurrency_tag='user'
                    )
                    for r in sweep:
                        r['config_label'] = config_label
                    sweep_key = f"{pd_label.lower()}-{split.prefill_pods}p{split.decode_pods}d-user"
                    self.cache_sweep_results[sweep_key] = sweep
                else:
                    tp = c[1]
                    total_gpus_agg = self.config.total_gpus
                    agg_replicas = total_gpus_agg // tp if tp else total_gpus_agg
                    config_label = f"{agg_replicas}×TP{tp}"
                    current_tp = tp
                    sweep = self._run_cache_sweep_for_arch(
                        f"Aggregated-{config_label}", user_concurrency, levels,
                        lambda: self._create_aggregated_config(
                            tp=current_tp, num_gpus=total_gpus_agg,
                            isl=self.config.isl, osl=self.config.osl,
                            test_id='_placeholder_', use_concurrency=True
                        ),
                        total_gpus_agg, concurrency_tag='user'
                    )
                    for r in sweep:
                        r['config_label'] = config_label
                    self.cache_sweep_results[f"aggregated-tp{tp}-user"] = sweep

        # --- Restore user's EPP preset ---
        try:
            user_preset = (self.config.epp_config or {}).get('preset', 'balanced')
            if user_preset != 'cache_optimized':
                from core import PrereqManager
                prereq = PrereqManager(
                    namespace=self.config.namespace,
                    kubectl_runner=self.orchestrator.deployment_manager.kubectl,
                )
                user_epp = dict(self.config.epp_config or {})
                user_epp['preset'] = user_preset
                for arch in ('aggregated', 'pd', 'ep'):
                    prereq.update_epp_config(
                        arch, user_epp,
                        log_callback=lambda msg: self.log(msg, 'info')
                    )
                self.log(f"  🔄 Restored EPP preset: {user_preset}", 'info')
        except Exception as e:
            self.log(f"  ⚠️  Failed to restore EPP preset: {e}", 'warning')

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
