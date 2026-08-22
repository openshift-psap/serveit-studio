"""Step 13: Cache hit sweep — measure performance across prefix cache hit ratios."""

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

        Uses a fixed BASE seed (independent of hit_pct) so all levels share
        deterministic random content, but includes hit_pct in the file path
        so each level gets its own dataset with the appropriate prefix structure.
        Zeroes out ISL/OSL stdev for consistent prompt lengths across levels.

        For 0%: generates a dataset with hit_pct=1 (minimal prefix) to keep the
        same prompt structure as other levels, rather than using synthetic mode.
        """
        import hashlib
        saved = {
            'prefix_cache_hit_pct': self.config.prefix_cache_hit_pct,
            'prefix_cache_mode': self.config.prefix_cache_mode,
            'prefix_cache_groups': self.config.prefix_cache_groups,
            'prefix_cache_seed': self.config.prefix_cache_seed,
            'workload_mode': self.config.workload_mode,
            'dataset_source': getattr(self.config, 'dataset_source', None),
            'prefix_tokens': getattr(self.config, 'prefix_tokens', None),
            'prefix_count': getattr(self.config, 'prefix_count', None),
            'isl_stdev': self.config.isl_stdev,
            'osl_stdev': self.config.osl_stdev,
        }
        try:
            # Fixed base seed — model/ISL/OSL determine base content, hit_pct varies
            # the prefix structure. Same seed across architectures → reuse datasets.
            cache_mode = self.config.prefix_cache_mode or 'identical'
            groups_str = str(self.config.prefix_cache_groups or 5) if cache_mode == 'multi_group' else '0'
            seed_input = f"{self.config.model_name}:{self.config.isl}:{self.config.osl}:cache_sweep:{hit_pct}:{cache_mode}:{groups_str}"
            level_seed = int(hashlib.sha256(seed_input.encode()).hexdigest()[:8], 16)

            self.config.prefix_cache_seed = level_seed
            self.config.isl_stdev = 0
            self.config.osl_stdev = 0
            self.config.prefix_cache_hit_pct = hit_pct

            self._generate_prefix_cache_dataset()
            return getattr(self.config, 'dataset_source', None)
        finally:
            for k, v in saved.items():
                setattr(self.config, k, v)

    def _run_cache_sweep_for_arch(self, arch_label: str, concurrency: int,
                                   levels: List[int], create_config_fn,
                                   gpus: int, concurrency_tag: str = '',
                                   sweep_key: str = '') -> List[Dict]:
        """Run cache hit sweep for one architecture, return list of results."""
        if not hasattr(self, 'cache_sweep_results'):
            self.cache_sweep_results = {}
        if sweep_key and sweep_key not in self.cache_sweep_results:
            self.cache_sweep_results[sweep_key] = []
        results = self.cache_sweep_results[sweep_key] if sweep_key else []
        tag = f"-{concurrency_tag}" if concurrency_tag else ""
        import re as _re
        _clean = _re.sub(r'\s*\((AG|PD|EP|aggregated|pd|ep)\)\s*$', '', arch_label)
        safe_label = _re.sub(r'[^a-z0-9-]', '', _clean.lower().replace('×', 'x').replace(' ', '-').replace('+', '-'))
        safe_label = _re.sub(r'-+', '-', safe_label).strip('-')
        if len(f"step13-cache{tag}-{safe_label}-h100-prefill") > 58:
            safe_label = safe_label[:30]
        self.log(f"\n🗂️  Cache Sweep: {arch_label} ({len(levels)} levels, c={concurrency}{tag})", 'info')

        # Check which levels need testing vs already completed
        levels_to_run = []
        for hit_pct in levels:
            test_id = f"step13-cache{tag}-{safe_label}-h{hit_pct}"
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
                _vllm_tps = None
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
                                _ptps = (_pm.get('vllm_prompt_tokens_rate') or {}).get('avg', 0)
                                _gtps = (_pm.get('vllm_generation_tokens_rate') or {}).get('avg', 0)
                                if _ptps + _gtps > 0:
                                    _vllm_tps = round(_ptps + _gtps)
                    except Exception:
                        pass
                results.append({'hit_pct': hit_pct, 'actual_hit_rate': _actual_hr,
                    'cache_hits_rate': _hits_r, 'cache_queries_rate': _queries_r,
                    'throughput_mean': round(tput, 2),
                    'output_tps_mean': round(output_tps, 2),
                    'vllm_tps': _vllm_tps,
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
        deploy_test_id = f"step13-cache{tag}-{safe_label}-h{levels_to_run[0]}"

        for i, hit_pct in enumerate(levels_to_run):
            if self._should_stop():
                break

            is_first = (i == 0)
            test_id = f"step13-cache{tag}-{safe_label}-h{hit_pct}"
            dataset_path = self._generate_cache_dataset_for_level(hit_pct)

            config = create_config_fn()
            config.test_id = deploy_test_id
            config.num_users = concurrency
            config.request_rate = concurrency
            config.enable_prefix_caching = True

            # Use the user's EPP config (or Step 9 tuned weights) — don't force cache_optimized
            # which causes request pileup on pods with cached prefixes
            if is_first:
                epp = config.epp_config or self._build_epp_config() or {}
                try:
                    from core import PrereqManager
                    prereq = PrereqManager(
                        namespace=self.config.namespace,
                        kubectl_runner=self.orchestrator.deployment_manager.kubectl,
                    )
                    prereq.update_epp_config(
                        config.architecture, epp,
                        log_callback=lambda msg: self.log(msg, 'info')
                    )
                except Exception as e:
                    self.log(f"  ⚠️  Failed to update EPP for cache sweep: {e}", 'warning')

            if dataset_path:
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

            # Compute server total tok/s from DB (prometheus data is added during save)
            vllm_tps = None
            if self.db_manager and self.run_id:
                try:
                    import json as _j2
                    with self.db_manager.get_connection() as _conn2:
                        _row2 = _conn2.execute(
                            'SELECT metrics_json FROM test_configurations WHERE run_id=? AND config_name=?',
                            (self.run_id, test_id)).fetchone()
                        if _row2 and _row2[0]:
                            _m2 = _j2.loads(_row2[0])
                            _pm2 = _m2.get('prometheus_metrics', {})
                            _ptps = (_pm2.get('vllm_prompt_tokens_rate') or {}).get('avg', 0)
                            _gtps = (_pm2.get('vllm_generation_tokens_rate') or {}).get('avg', 0)
                            if _ptps + _gtps > 0:
                                vllm_tps = round(_ptps + _gtps)
                except Exception:
                    pass

            results.append({
                'hit_pct': hit_pct,
                'actual_hit_rate': actual_hit_rate,
                'cache_hits_rate': cache_hits_rate,
                'cache_queries_rate': cache_queries_rate,
                'throughput_mean': round(tput, 2),
                'output_tps_mean': round(output_tps, 2),
                'vllm_tps': vllm_tps,
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

        # --- Pull configs + calibrated concurrency from Step 11 concurrency sweep ---
        sweep_configs = []
        sweep_results = getattr(self, 'concurrency_sweep_results', {})

        if sweep_results:
            for sweep_key, points in sweep_results.items():
                if not points:
                    continue
                # Skip configs that started with 2+ consecutive discards (fundamentally broken)
                # Check both throughput and quality from DB
                _disc_ids = set()
                if self.db_manager:
                    try:
                        with self.db_manager.get_connection() as _conn:
                            _rows = _conn.execute("SELECT config_name FROM test_configurations WHERE run_id=? AND quality='discard'", (self.run_id,)).fetchall()
                            _disc_ids = {r[0] for r in _rows}
                    except Exception:
                        pass
                _start_discards = 0
                for p in points:
                    tid = p.get('test_id', '')
                    if p.get('throughput_mean', 0) <= 0 or tid in _disc_ids:
                        _start_discards += 1
                    else:
                        break
                if _start_discards >= 2:
                    self.log(f"  ⚠️  Skipping {sweep_key}: first {_start_discards} tests all discarded (not viable)", 'warning')
                    continue
                viable_points = [p for p in points if p.get('throughput_mean', 0) > 0]
                if not viable_points:
                    self.log(f"  ⚠️  Skipping {sweep_key}: no viable sweep results", 'warning')
                    continue
                best_point = max(viable_points, key=lambda p: p.get('throughput_mean', 0))
                cal_c = best_point['concurrency']
                first = points[0]
                config_label = first.get('config_label') or sweep_key
                gpus = first.get('gpus', self.config.total_gpus)

                if sweep_key.startswith('pd-') or sweep_key.startswith('ep-'):
                    arch = 'ep' if sweep_key.startswith('ep-') else 'pd'
                    matching = None
                    pool = self.ep_results if arch == 'ep' else (self.pareto_results or [])
                    for split, result in pool:
                        sk = f"{arch}-{split.prefill_pods}p{split.decode_pods}d-tp{split.prefill_tp}-{split.decode_tp}"
                        if sk == sweep_key or f"{arch}-{split.prefill_pods}p{split.decode_pods}d-ptp{split.prefill_tp}-dtp{split.decode_tp}" == sweep_key:
                            matching = (split, result)
                            break
                    if matching:
                        sweep_configs.append(('pd' if arch == 'pd' else 'ep', matching[0], cal_c, gpus, config_label))
                elif sweep_key.startswith('agg-'):
                    import re
                    m = re.search(r'tp(\d+)', sweep_key)
                    tp = int(m.group(1)) if m else self.config.total_gpus
                    sweep_configs.append(('agg', tp, cal_c, gpus, config_label))

            self.log(f"Cache sweep: {len(sweep_configs)} configs from concurrency sweep", 'info')
            for sc in sweep_configs:
                self.log(f"  {sc[4]} at c={sc[2]}", 'info')
        else:
            self.log("⚠️  No concurrency sweep results — using user concurrency for all configs", 'warning')
            default_concurrency = user_concurrency
            if getattr(self, 'aggregated_tp', None) and getattr(self, 'aggregated_result', None):
                sweep_configs.append(('agg', self.aggregated_tp, default_concurrency, self.config.total_gpus, f"Aggregated TP{self.aggregated_tp}"))

        # --- Run cache sweep using configs + calibrated concurrency from Step 11 ---
        if self.config.cache_sweep_enabled and sweep_configs:
            self.log(f"\n{'='*60}", 'info')
            self.log("Cache Sweep (using calibrated concurrency from Step 11)", 'decision')
            self.log(f"{'='*60}", 'info')

            for sc in sweep_configs:
                if self._should_stop():
                    break
                if sc[0] in ('pd', 'ep'):
                    split = sc[1]
                    cal_c = sc[2]
                    total_gpus = sc[3]
                    config_label = sc[4]
                    pd_label = 'EP' if sc[0] == 'ep' else 'PD'
                    current_split = split
                    sweep_key = f"{pd_label.lower()}-{split.prefill_pods}p{split.decode_pods}d"
                    sweep = self._run_cache_sweep_for_arch(
                        f"{pd_label}-{config_label}", cal_c, levels,
                        lambda: self._create_pd_config(current_split) if pd_label == 'PD' else self._create_ep_config(split=current_split),
                        total_gpus, sweep_key=sweep_key
                    )
                    for r in sweep:
                        r['config_label'] = config_label
                else:
                    tp = sc[1]
                    cal_c = sc[2]
                    total_gpus_agg = self.config.total_gpus
                    config_label = sc[4]
                    current_tp = tp
                    sweep_key = f"aggregated-tp{tp}"
                    sweep = self._run_cache_sweep_for_arch(
                        f"Aggregated-{config_label}", cal_c, levels,
                        lambda: self._create_aggregated_config(
                            tp=current_tp, num_gpus=total_gpus_agg,
                            isl=self.config.isl, osl=self.config.osl,
                            test_id='_placeholder_', use_concurrency=True
                        ),
                        total_gpus_agg, sweep_key=sweep_key
                    )
                    for r in sweep:
                        r['config_label'] = config_label
                    sweep_key = f"aggregated-tp{tp}"
                    self.cache_sweep_results[sweep_key] = sweep

        # --- Optional second sweep at user-defined concurrency (if different from calibrated) ---
        calibrated_concurrency = getattr(self, 'achievable_concurrency', None)
        if calibrated_concurrency:
            calibrated_concurrency = int(calibrated_concurrency)
        if self.config.cache_sweep_use_calibrated and calibrated_concurrency and user_concurrency != calibrated_concurrency and not self._should_stop():
            self.log(f"\n{'='*60}", 'info')
            self.log(f"Cache Sweep at user-defined concurrency ({user_concurrency})", 'decision')
            self.log(f"{'='*60}", 'info')

            for sc in sweep_configs:
                if self._should_stop():
                    break
                if sc[0] in ('pd', 'ep'):
                    split = sc[1]
                    config_label = sc[4]
                    total_gpus = sc[3]
                    pd_label = 'EP' if sc[0] == 'ep' else 'PD'
                    current_split = split
                    sweep_key = f"{pd_label.lower()}-{split.prefill_pods}p{split.decode_pods}d-user"
                    sweep = self._run_cache_sweep_for_arch(
                        f"{pd_label}-{config_label}", user_concurrency, levels,
                        lambda: self._create_pd_config(current_split) if pd_label == 'PD' else self._create_ep_config(split=current_split),
                        total_gpus, concurrency_tag='user', sweep_key=sweep_key
                    )
                    for r in sweep:
                        r['config_label'] = config_label
                else:
                    tp = sc[1]
                    total_gpus_agg = self.config.total_gpus
                    config_label = sc[4]
                    current_tp = tp
                    sweep_key = f"aggregated-tp{tp}-user"
                    sweep = self._run_cache_sweep_for_arch(
                        f"Aggregated-{config_label}", user_concurrency, levels,
                        lambda: self._create_aggregated_config(
                            tp=current_tp, num_gpus=total_gpus_agg,
                            isl=self.config.isl, osl=self.config.osl,
                            test_id='_placeholder_', use_concurrency=True
                        ),
                        total_gpus_agg, concurrency_tag='user', sweep_key=sweep_key
                    )
                    for r in sweep:
                        r['config_label'] = config_label

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
            self.log("\n📊 Cache Hit Sweep Summary:", 'decision')
            for arch_key, sweep in self.cache_sweep_results.items():
                if not sweep:
                    continue
                first = sweep[0]
                last = sweep[-1]
                self.log(f"  {arch_key}: h={first['hit_pct']}%→{last['hit_pct']}% | "
                         f"TTFT {first['ttft_p90']:.0f}→{last['ttft_p90']:.0f}ms | "
                         f"Throughput {first['throughput_mean']:.1f}→{last['throughput_mean']:.1f} req/s", 'info')
