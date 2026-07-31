"""Steps 2-3: Combined TP calibration — decode and prefill in a single sweep."""

from typing import Optional


from core.optimizer.config import OptimalTP

class TPCalibrationMixin:
    """Mixin providing TP calibration methods for RecipeOptimizer."""

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

        # Pre-compute max requests and KV cap across all TPs to size calibration datasets
        # Pool must be larger than KV cap to prevent full-cache hits from skewing results
        max_decode_reqs = 0
        max_decode_kv = 0
        max_prefill_reqs = 0
        max_prefill_kv = 0
        for tp in all_tps:
            if tp in decode_tps:
                c = self._estimate_safe_concurrency(tp, isl=1, osl=self.config.osl)
                max_decode_reqs = max(max_decode_reqs, _calibration_max_requests(c, 1 + self.config.osl, tp))
                max_decode_kv = max(max_decode_kv, c)
            if tp in prefill_tps:
                c = self._estimate_safe_concurrency(tp, isl=self.config.isl, osl=1)
                max_prefill_reqs = max(max_prefill_reqs, _calibration_max_requests(c, self.config.isl + 1, tp))
                max_prefill_kv = max(max_prefill_kv, c)
        # Ensure pool is at least 3x KV cap so cached entries get evicted before reuse
        max_decode_reqs = max(max_decode_reqs, max_decode_kv * 3)
        max_prefill_reqs = max(max_prefill_reqs, max_prefill_kv * 3)

        # Pre-generate calibration datasets so guidellm doesn't regenerate per test
        decode_dataset = self._generate_calibration_dataset(
            isl=1, osl=self.config.osl, label='decode', pool_size=max_decode_reqs)
        prefill_dataset = self._generate_calibration_dataset(
            isl=self.config.isl, osl=1, label='prefill', pool_size=max_prefill_reqs)

        use_ttft = self.config.objective == 'ttft'

        decode_candidates = []
        prefill_candidates = []

        model_size_b = getattr(self, '_model_size_b', 8)
        tokens_per_gpu = 500_000 if model_size_b < 100 else (250_000 if model_size_b < 200 else 100_000)

        def _calibration_max_requests(safe_c, seq_len, tp):
            """Calculate max requests from a per-GPU token budget."""
            budget = tokens_per_gpu * tp
            reqs = max(1, budget // seq_len)
            reqs = max(reqs, safe_c * 3)  # at least 3 full batches
            reqs = min(reqs, safe_c * 120)  # never more than 120 batches
            return reqs

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
                    safe_c = self._estimate_safe_concurrency(tp, isl=1, osl=self.config.osl)
                    decode_config = self._create_aggregated_config(
                        tp=tp, num_gpus=tp, isl=1, osl=self.config.osl,
                        test_id=decode_test_id,
                        use_concurrency=True, concurrency_override=safe_c
                    )
                    decode_config.stop_mode = 'max_requests'
                    decode_config.max_requests = _calibration_max_requests(safe_c, 1 + self.config.osl, tp)
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
                        self.log(f"    ❌ Decode TP={tp} failed (non-memory) — stopping", 'error')
                        raise RuntimeError(f"Test {decode_test_id} failed - stopping optimization")

                    self._check_pod_errors(decode_config, decode_result)
                    self._check_request_errors(decode_config, decode_result)

                # Accumulate decode candidate
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
                    safe_c = self._estimate_safe_concurrency(tp, isl=self.config.isl, osl=1)
                    prefill_config = self._create_aggregated_config(
                        tp=tp, num_gpus=tp, isl=self.config.isl, osl=1,
                        test_id=prefill_test_id,
                        use_concurrency=True, concurrency_override=safe_c
                    )
                    prefill_config.stop_mode = 'max_requests'
                    prefill_config.max_requests = _calibration_max_requests(safe_c, self.config.isl + 1, tp)
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
                            self.log(f"    ❌ Prefill TP={tp} failed (non-memory) — stopping", 'error')
                            if deployed and decode_config:
                                self.orchestrator.cleanup_deployment(decode_config,
                                    log_callback=lambda msg: self.log(msg, 'info'))
                            raise RuntimeError(f"Test {prefill_test_id} failed - stopping optimization")
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
