"""Steps 2-3: Combined TP calibration — decode and prefill in a single sweep."""



from core.optimizer.config import OptimalTP

# Calibration measurement windows (seconds). Calibration runs stop on
# duration, not a request count: wall time stays bounded no matter how
# slow the model is, every TP trial in the sweep gets the same measured
# window, and the completed-request count self-adjusts to real throughput
# instead of a latency-model guess (a 15ms/token estimate was off by ~5x
# on GLM-5.3 TP16, turning a 1-minute measurement target into 45 minutes
# and 3415 requests).
CALIBRATION_DURATION_DECODE = 180
CALIBRATION_DURATION_PREFILL = 90


def _calibration_pool_size(streams: int, isl: int, osl: int, duration: int) -> int:
    """Dataset rows needed for a duration-based calibration run.

    guidellm's json_file loader does not cycle the pool — when it runs out
    the benchmark simply ends early (requests_exhausted) — so size the pool
    for profile warmup (60s) plus the measurement window plus slack, at the
    estimated request rate. A pessimistic estimate just leaves unused rows;
    an optimistic one ends the run early, which still yields plenty of
    samples for p90 statistics.
    """
    prefill_t = isl * 0.0005           # ~0.5ms per input token
    decode_t = osl * 0.015             # ~15ms per output token
    req_time = max(0.1, prefill_t + decode_t)
    est_rps = streams / req_time
    return int((60 + duration + 30) * est_rps)


class TPCalibrationMixin:
    """Mixin providing TP calibration methods for RecipeOptimizer."""

    def _calibration_streams(self, tp: int, isl: int, osl: int, role: str) -> int:
        """Concurrency for a calibration run.

        Starts from the KV-safe estimate but clamps it to the target pod's
        vLLM admission (max_num_seqs x 1.25). Concurrency far above
        admission does not raise throughput — the GPU batch is already
        saturated — it only converts TTFT into queueing delay and inflates
        run duration (244 streams into a 64-slot decode pod measured
        TTFT p50 of 124s, nearly all queue wait).

        Mirrors the admission computation in _create_pd_config (including
        the calibration max_model_len cap) so the clamp matches what the
        deployed pod will actually run with.
        """
        safe_c = self._estimate_safe_concurrency(tp, isl=isl, osl=osl)

        saved_mml = None
        if self.config.objective == 'pd_only':
            cal_mml = (self.config.isl or 1500) + (self.config.osl or 425) + 1024
            if self.config.max_model_len and self.config.max_model_len > cal_mml:
                saved_mml = self.config.max_model_len
                self.config.max_model_len = cal_mml
        try:
            if role == 'aggregated':
                admission = self._compute_max_num_seqs(tp, role=role, num_pods=1)
            else:
                gmu = self._compute_gpu_mem_util(tp, log=False)
                if role == 'decode':
                    # Same NIXL reserve + floor as _create_pd_config
                    gmu = max(round(min(gmu, gmu - 0.05), 2), 0.80)
                admission = self._compute_max_num_seqs(
                    tp, role=role, gpu_mem_util_override=gmu, num_pods=1)
        finally:
            if saved_mml is not None:
                self.config.max_model_len = saved_mml

        if not admission:
            return safe_c
        streams = min(safe_c, max(8, int(admission * 1.25)))
        if streams < safe_c:
            self.log(f"   Calibration streams for TP={tp}: {streams} "
                     f"(KV-safe {safe_c} clamped to {role} admission {admission})", 'info')
        return streams

    @staticmethod
    def _apply_calibration_workload(config, streams: int, duration: int):
        """Set duration-based calibration workload on a test config."""
        config.num_users = streams
        config.request_rate = streams
        config.stop_mode = 'duration'
        config.test_duration = duration
        config.max_requests = None

    def _optimize_tp_combined(self):
        """
        Steps 2-3: Test ALL valid TP values for both decode and prefill workloads.

        For each TP, deploys the serving pod once and runs both workloads
        (decode: ISL=1, OSL=target; prefill: ISL=target, OSL=1) before
        cleaning up. This avoids redundant model loads — critical for large
        models (550B+) where loading takes 10+ minutes per deployment.
        """
        decode_tps = set(self._get_valid_tp_options(role='decode'))
        prefill_tps = set(self._get_valid_tp_options(role='prefill'))
        all_tps = sorted(decode_tps | prefill_tps)

        self.log(f"Testing {len(all_tps)} TP values: {all_tps}", 'info')
        self.log(f"  Decode-valid: {sorted(decode_tps)}, Prefill-valid: {sorted(prefill_tps)}", 'info')
        self.log(f"  Decode workload: ISL=1, OSL={self.config.osl}", 'info')
        self.log(f"  Prefill workload: ISL={self.config.isl}, OSL=1", 'info')

        use_ttft = self.config.objective == 'ttft'
        pd_only = self.config.objective == 'pd_only'

        decode_candidates = []
        prefill_candidates = []

        # Pre-compute stream counts and dataset pool size across all TPs.
        # Pool must be larger than KV cap to prevent full-cache hits from skewing results
        max_decode_reqs = 0
        max_prefill_reqs = 0
        max_decode_kvcap = 0
        max_prefill_kvcap = 0
        gpu_vram = getattr(self, '_gpu_vram_gb', 80.0)
        model_gb = self._estimate_model_size_gb()
        for tp in all_tps:
            avail = gpu_vram - model_gb / tp - 5.0
            if tp in decode_tps:
                c = self._calibration_streams(
                    tp, isl=1, osl=self.config.osl,
                    role='decode' if pd_only else 'aggregated')
                max_decode_reqs = max(max_decode_reqs, _calibration_pool_size(
                    c, 1, self.config.osl, CALIBRATION_DURATION_DECODE))
                kv_hpg = max(1, (self._model_config or {}).get('num_key_value_heads', 8) // tp)
                head_dim = (self._model_config or {}).get('head_dim', 256)
                n_layers = (self._model_config or {}).get('num_hidden_layers', 32)
                kv_seq = (2 * n_layers * kv_hpg * head_dim * (1 + self.config.osl) * 2) / (1024**3)
                max_decode_kvcap = max(max_decode_kvcap, int(avail / kv_seq) if kv_seq > 0 else 0)
            if tp in prefill_tps:
                c = self._calibration_streams(
                    tp, isl=self.config.isl, osl=1,
                    role='prefill' if pd_only else 'aggregated')
                max_prefill_reqs = max(max_prefill_reqs, _calibration_pool_size(
                    c, self.config.isl, 1, CALIBRATION_DURATION_PREFILL))
                kv_hpg = max(1, (self._model_config or {}).get('num_key_value_heads', 8) // tp)
                head_dim = (self._model_config or {}).get('head_dim', 256)
                n_layers = (self._model_config or {}).get('num_hidden_layers', 32)
                kv_seq = (2 * n_layers * kv_hpg * head_dim * (self.config.isl + 1) * 2) / (1024**3)
                max_prefill_kvcap = max(max_prefill_kvcap, int(avail / kv_seq) if kv_seq > 0 else 0)
        # Pool must exceed KV cap so prefix cache can't hold all unique prompts
        max_decode_reqs = max(max_decode_reqs, max_decode_kvcap * 3)
        max_prefill_reqs = max(max_prefill_reqs, max_prefill_kvcap * 3)

        # Pre-generate calibration datasets so guidellm doesn't regenerate per test
        decode_dataset = self._generate_calibration_dataset(
            isl=1, osl=self.config.osl, label='decode', pool_size=max_decode_reqs)
        prefill_dataset = self._generate_calibration_dataset(
            isl=self.config.isl, osl=1, label='prefill', pool_size=max_prefill_reqs)

        for i, tp in enumerate(all_tps):
            if self._should_stop():
                break

            run_decode = tp in decode_tps
            run_prefill = tp in prefill_tps

            decode_test_id = f"step2-trial{i + 1}-decode-tp{tp}"
            prefill_test_id = f"step3-trial{i + 1}-prefill-tp{tp}"

            decode_cached = decode_test_id in self.completed_tests
            prefill_cached = prefill_test_id in self.completed_tests

            self.log(f"\n  TP={tp} ({'decode' if run_decode else ''}{'+'if run_decode and run_prefill else ''}{'prefill' if run_prefill else ''})", 'info')

            # ── Decode test ─────────────────────────────────────────────
            decode_result = None
            decode_config = None
            deployed = False

            if run_decode:
                if decode_cached:
                    row = self.completed_tests[decode_test_id]
                    decode_result = self._make_test_result_from_db(row)
                    self.log("    ⏩ Decode: resuming from DB (already completed)", 'info')
                else:
                    cal_streams = self._calibration_streams(
                        tp, isl=1, osl=self.config.osl,
                        role='decode' if pd_only else 'aggregated')
                    try:
                        if pd_only:
                            # pd_only: use 1P+1D PD pod so no aggregated pods run.
                            # Cap max_model_len to ISL+OSL+margin so pods don't OOM.
                            from core.optimizer.config import FeasibleSplit, OptimalTP
                            _saved_mml = self.config.max_model_len
                            _cal_mml = (self.config.isl or 1500) + (self.config.osl or 425) + 1024
                            if _saved_mml and _saved_mml > _cal_mml:
                                self.config.max_model_len = _cal_mml
                            _tmp = OptimalTP(tp=tp, tpsg=100.0, ttft_p90=None)
                            _prev_ptp = getattr(self, 'optimal_prefill_tp', None)
                            _prev_dtp = getattr(self, 'optimal_decode_tp', None)
                            self.optimal_prefill_tp = _tmp
                            self.optimal_decode_tp  = _tmp
                            _split = FeasibleSplit(
                                prefill_pods=1, decode_pods=1,
                                prefill_tp=tp, decode_tp=tp,
                                prefill_gpus=tp, decode_gpus=tp,
                                total_gpus=tp * 2, prefill_pct=0.5,
                            )
                            decode_config = self._create_pd_config(_split)
                            decode_config.max_model_len = _cal_mml  # force cap after advanced overrides
                            decode_config.speculative_method = None
                            decode_config.speculative_num_tokens = None
                            decode_config.prefill_speculative_num_tokens = None
                            decode_config.cpu_offload_gb = None
                            decode_config.weight_cpu_offload_gb = None
                            self.config.max_model_len = _saved_mml  # restore
                            if _prev_ptp: self.optimal_prefill_tp = _prev_ptp
                            if _prev_dtp: self.optimal_decode_tp  = _prev_dtp
                            decode_config.test_id = decode_test_id
                            decode_config.isl = 1
                            decode_config.osl = self.config.osl
                        else:
                            decode_config = self._create_aggregated_config(
                                tp=tp, num_gpus=tp, isl=1, osl=self.config.osl,
                                test_id=decode_test_id,
                                use_concurrency=True, concurrency_override=cal_streams
                            )
                        # Disable speculative decoding and CPU offloading for all calibration configs
                        if decode_config is not None:
                            decode_config.speculative_method = None
                            decode_config.speculative_num_tokens = None
                            decode_config.prefill_speculative_num_tokens = None
                            decode_config.cpu_offload_gb = None
                            decode_config.weight_cpu_offload_gb = None
                    except (ValueError, TypeError) as e:
                        self.log(f"  ⏭️  Skipping TP={tp} decode: {e}", 'warning')
                        run_decode = False
                        decode_result = None
                        decode_config = None
                    if decode_config is not None:
                        self._apply_calibration_workload(
                            decode_config, cal_streams, CALIBRATION_DURATION_DECODE)
                        self.log(f"    Decode calibration workload: {cal_streams} streams, "
                                 f"{CALIBRATION_DURATION_DECODE}s window (duration-based)", 'info')
                        if decode_dataset:
                            decode_config.workload_mode = 'dataset'
                            decode_config.dataset_source = decode_dataset
                            decode_config.dataset_column = 'prompt'
                            decode_config.dataset_max_output = self.config.osl

                        # Keep deployment alive for prefill test
                        needs_prefill_after = run_prefill and not prefill_cached
                        decode_result = self.orchestrator.run_test(
                            decode_config,
                            cleanup=not needs_prefill_after,
                            log_callback=lambda msg: self.log(msg, 'info'),
                            stop_check=self._should_stop
                        )
                        deployed = needs_prefill_after and decode_result and decode_result.guidellm_success

                        self.all_test_results.append((decode_config, decode_result))
                        self._save_test_to_database(decode_config, decode_result)

                        if not decode_result or not decode_result.guidellm_success:
                            if self._is_memory_failure(decode_result):
                                self.log(f"    ⚠️  Decode TP={tp} OOM — skipping prefill too", 'warning')
                                continue
                            # Retry once after restarting infra (gateway/EPP cert rotation)
                            err = getattr(decode_result, 'error_message', '') or ''
                            self.log(f"    ⚠️  Decode TP={tp} failed ({err[:80]}) — restarting infra and retrying", 'warning')
                            decode_result = self.orchestrator.run_test(
                                decode_config,
                                cleanup=not needs_prefill_after,
                                log_callback=lambda msg: self.log(msg, 'info'),
                                stop_check=self._should_stop
                            )
                            deployed = needs_prefill_after and decode_result and decode_result.guidellm_success
                            self.all_test_results.append((decode_config, decode_result))
                            self._save_test_to_database(decode_config, decode_result)
                            if not decode_result or not decode_result.guidellm_success:
                                raise RuntimeError(f"Test {decode_test_id} failed after retry - stopping optimization")

                        self._check_pod_errors(decode_config, decode_result)
                        self._check_request_errors(decode_config, decode_result)

                # Accumulate decode candidate (skip if result is None due to KV budget skip)
                if decode_result is not None:
                    tpsg, ttft = self._extract_tpsg_ttft(decode_result, self.config.osl, tp)
                    if tpsg is not None:
                        self._log_candidate('Decode', tp, tpsg, ttft, use_ttft)
                        decode_candidates.append((tp, tpsg, ttft, decode_result.throughput_p90))

            # ── Prefill test ────────────────────────────────────────────
            if run_prefill:
                if self._should_stop():
                    if deployed and decode_config:
                        self.orchestrator.cleanup_deployment(decode_config,
                            log_callback=lambda msg: self.log(msg, 'info'))
                    break

                prefill_result = None
                if prefill_cached:
                    row = self.completed_tests[prefill_test_id]
                    prefill_result = self._make_test_result_from_db(row)
                    self.log("    ⏩ Prefill: resuming from DB (already completed)", 'info')
                else:
                    cal_streams = self._calibration_streams(
                        tp, isl=self.config.isl, osl=1,
                        role='prefill' if pd_only else 'aggregated')
                    try:
                        if pd_only:
                            from core.optimizer.config import FeasibleSplit, OptimalTP
                            _saved_mml = self.config.max_model_len
                            _cal_mml = (self.config.isl or 1500) + (self.config.osl or 425) + 1024
                            if _saved_mml and _saved_mml > _cal_mml:
                                self.config.max_model_len = _cal_mml
                            _tmp = OptimalTP(tp=tp, tpsg=100.0, ttft_p90=None)
                            _prev_ptp = getattr(self, 'optimal_prefill_tp', None)
                            _prev_dtp = getattr(self, 'optimal_decode_tp', None)
                            self.optimal_prefill_tp = _tmp
                            self.optimal_decode_tp  = _tmp
                            _split = FeasibleSplit(
                                prefill_pods=1, decode_pods=1,
                                prefill_tp=tp, decode_tp=tp,
                                prefill_gpus=tp, decode_gpus=tp,
                                total_gpus=tp * 2, prefill_pct=0.5,
                            )
                            prefill_config = self._create_pd_config(_split)
                            prefill_config.max_model_len = _cal_mml  # force cap after advanced overrides
                            prefill_config.speculative_method = None
                            prefill_config.speculative_num_tokens = None
                            prefill_config.prefill_speculative_num_tokens = None
                            prefill_config.cpu_offload_gb = None
                            prefill_config.weight_cpu_offload_gb = None
                            self.config.max_model_len = _saved_mml  # restore
                            if _prev_ptp: self.optimal_prefill_tp = _prev_ptp
                            if _prev_dtp: self.optimal_decode_tp  = _prev_dtp
                            prefill_config.test_id = prefill_test_id
                            prefill_config.isl = self.config.isl
                            prefill_config.osl = 1
                        else:
                            prefill_config = self._create_aggregated_config(
                                tp=tp, num_gpus=tp, isl=self.config.isl, osl=1,
                                test_id=prefill_test_id,
                                use_concurrency=True, concurrency_override=cal_streams
                            )
                        # Disable speculative decoding and CPU offloading for all calibration configs
                        if prefill_config is not None:
                            prefill_config.speculative_method = None
                            prefill_config.speculative_num_tokens = None
                            prefill_config.prefill_speculative_num_tokens = None
                            prefill_config.cpu_offload_gb = None
                            prefill_config.weight_cpu_offload_gb = None
                    except (ValueError, TypeError) as e:
                        self.log(f"  ⏭️  Skipping TP={tp} prefill: {e}", 'warning')
                        run_prefill = False
                        prefill_result = None
                        prefill_config = None
                    if prefill_config is not None:
                        self._apply_calibration_workload(
                            prefill_config, cal_streams, CALIBRATION_DURATION_PREFILL)
                        self.log(f"    Prefill calibration workload: {cal_streams} streams, "
                                 f"{CALIBRATION_DURATION_PREFILL}s window (duration-based)", 'info')
                        if prefill_dataset:
                            prefill_config.workload_mode = 'dataset'
                            prefill_config.dataset_source = prefill_dataset
                            prefill_config.dataset_column = 'prompt'
                            prefill_config.dataset_max_output = 1

                        prefill_result = self.orchestrator.run_test(
                            prefill_config,
                            cleanup=False,
                            skip_deploy=deployed,
                            skip_prereqs=deployed,
                            log_callback=lambda msg: self.log(msg, 'info'),
                            stop_check=self._should_stop
                        )

                        self.all_test_results.append((prefill_config, prefill_result))
                        self._save_test_to_database(prefill_config, prefill_result)

                        if not prefill_result or not prefill_result.guidellm_success:
                            if self._is_memory_failure(prefill_result):
                                self.log(f"    ⚠️  Prefill TP={tp} OOM — skipping", 'warning')
                            else:
                                err = getattr(prefill_result, 'error_message', '') or ''
                                self.log(f"    ⚠️  Prefill TP={tp} failed ({err[:80]}) — restarting infra and retrying", 'warning')
                                self._restart_infra_pods()
                                import time as _time; _time.sleep(15)
                                prefill_result = self.orchestrator.run_test(
                                    prefill_config,
                                    cleanup=False,
                                    skip_deploy=deployed,
                                    skip_prereqs=deployed,
                                    log_callback=lambda msg: self.log(msg, 'info'),
                                    stop_check=self._should_stop
                                )
                                self.all_test_results.append((prefill_config, prefill_result))
                                self._save_test_to_database(prefill_config, prefill_result)
                                if not prefill_result or not prefill_result.guidellm_success:
                                    if deployed and decode_config:
                                        self.orchestrator.cleanup_deployment(decode_config,
                                            log_callback=lambda msg: self.log(msg, 'info'))
                                    raise RuntimeError(f"Test {prefill_test_id} failed after retry - stopping optimization")
                        else:
                            self._check_pod_errors(prefill_config, prefill_result)
                            self._check_request_errors(prefill_config, prefill_result)

                # Accumulate prefill candidate
                if prefill_result and prefill_result.guidellm_success:
                    tpsg, ttft = self._extract_tpsg_ttft(prefill_result, self.config.isl, tp)
                    if tpsg is not None:
                        self._log_candidate('Prefill', tp, tpsg, ttft, use_ttft)
                        prefill_candidates.append((tp, tpsg, ttft, prefill_result.throughput_p90))

            # ── Cleanup deployment ──────────────────────────────────────
            if deployed and decode_config:
                self.orchestrator.cleanup_deployment(decode_config,
                    log_callback=lambda msg: self.log(msg, 'info'))

        # ── Select optimal TPs ──────────────────────────────────────────
        self.optimal_decode_tp = self._select_best_tp(decode_candidates, 'Decode', use_ttft)
        self.decode_tp_results = [
            {'tp': tp, 'tpsg': tpsg, 'ttft_p90': ttft, 'throughput_p90': thr}
            for tp, tpsg, ttft, thr in decode_candidates
        ]

        self.optimal_prefill_tp = self._select_best_tp(prefill_candidates, 'Prefill', use_ttft)
        self.prefill_tp_results = [
            {'tp': tp, 'tpsg': tpsg, 'ttft_p90': ttft, 'throughput_p90': thr}
            for tp, tpsg, ttft, thr in prefill_candidates
        ]

    # ── Helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _extract_tpsg_ttft(result, seq_len, tp):
        """Calculate TPSG and extract TTFT from a test result."""
        if result.throughput_p90 and result.throughput_p90 > 0:
            tpsg = (result.throughput_p90 * seq_len) / tp
        elif result.throughput_p50 and result.throughput_p50 > 0:
            tpsg = (result.throughput_p50 * seq_len) / tp
        else:
            return None, None
        ttft = result.ttft_p90 if result.ttft_p90 else float('inf')
        return tpsg, ttft

    def _log_candidate(self, role, tp, tpsg, ttft, use_ttft):
        if use_ttft:
            self.log(f"    ✅ {role} TP={tp}: TTFT_p90={ttft:.0f}ms, TPSG={tpsg:.0f}", 'success')
        else:
            self.log(f"    ✅ {role} TP={tp}: TPSG={tpsg:.0f} tokens/s/GPU", 'success')

    def _select_best_tp(self, candidates, role, use_ttft):
        """Select optimal TP from candidates list."""
        if not candidates:
            raise RuntimeError(f"All {role.lower()} TP tests failed - no valid results")

        if use_ttft:
            # Filter out inf TTFTs first
            real_ttft = [(tp, tpsg, ttft, thr) for tp, tpsg, ttft, thr in candidates if ttft < float('inf')]
            if real_ttft:
                best = min(real_ttft, key=lambda x: x[2])
            else:
                self.log(f"  ⚠️  All {role} TTFT values are inf (normal for ISL=1), selecting by highest TPSG", 'warning')
                best = max(candidates, key=lambda x: x[1])
        else:
            best = max(candidates, key=lambda x: x[1])

        tp, tpsg, ttft, throughput = best
        criterion = "lowest TTFT" if use_ttft else "highest TPSG"
        self.log("", 'info')
        self.log(f"✅ Optimal {role} TP: {tp} (selected by {criterion})", 'success')
        self.log(f"   TTFT_p90: {ttft:.0f}ms, TPSG: {tpsg:.0f} tokens/s/GPU", 'info')
        self.log(f"   Tested {len(candidates)} TP values", 'info')

        return OptimalTP(tp=tp, tpsg=tpsg, ttft_p90=ttft, throughput_p90=throughput)
