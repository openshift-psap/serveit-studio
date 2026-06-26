"""Steps 2-3: TP calibration — decode and prefill TP sweeps."""

from typing import Optional


from core.optimizer.config import OptimalTP

class TPCalibrationMixin:
    """Mixin providing TP calibration methods for RecipeOptimizer."""

    def _optimize_decode_tp(self):
        """
        Step 2: Test ALL valid TP values for decode workload.

        Tests decode-only workload (ISL=1, OSL=target) with every valid TP.
        Objective: lowest TTFT (when objective='ttft') or highest TPSG.
        """
        valid_tp = self._get_valid_tp_options(role='decode')
        self.log(f"Testing all {len(valid_tp)} valid TP values: {valid_tp}", 'info')
        self.log(f"Workload: ISL=1, OSL={self.config.osl} (decode-focused)", 'info')

        use_ttft = self.config.objective == 'ttft'
        best_tp = None
        best_tpsg = 0.0
        best_ttft = float('inf')
        best_throughput = None
        all_candidates = []

        for i, tp in enumerate(valid_tp):
            if self._should_stop():
                break

            test_id = f"step2-trial{i + 1}-decode-tp{tp}"
            self.log(f"  Test {i + 1}/{len(valid_tp)}: TP={tp}", 'info')

            # Check for completed test from previous run
            if test_id in self.completed_tests:
                row = self.completed_tests[test_id]
                result = self._make_test_result_from_db(row)
                self.log("    ⏩ Resuming from DB (already completed)", 'info')
            else:
                safe_c = self._estimate_safe_concurrency(tp)
                test_config = self._create_aggregated_config(
                    tp=tp,
                    num_gpus=tp,
                    isl=1,
                    osl=self.config.osl,
                    test_id=test_id,
                    use_concurrency=True,
                    concurrency_override=safe_c
                )
                # Calibration uses max_requests instead of duration to avoid
                # flooding — send a controlled number, get clean TPSG measurement
                test_config.stop_mode = 'max_requests'
                test_config.max_requests = safe_c * 10

                result = self.orchestrator.run_test(
                    test_config,
                    cleanup=True,
                    log_callback=lambda msg: self.log(msg, 'info'),
                    stop_check=self._should_stop
                )

                self.all_test_results.append((test_config, result))
                self._save_test_to_database(test_config, result)
                self._check_pod_errors(test_config, result)
                self._check_request_errors(test_config, result)

                if not result or not result.guidellm_success:
                    self.log("    ❌ Test failed - STOPPING optimization", 'error')
                    self.log(f"    🔍 Debug: kubectl get pods -n {self.config.namespace} -l test-id={test_id}", 'error')
                    self.log(f"    🧹 Cleanup: kubectl delete lws -n {self.config.namespace} -l test-id={test_id}", 'error')
                    raise RuntimeError(f"Test {test_id} failed - stopping optimization")

            # Calculate TPSG
            if result.throughput_p90 and result.throughput_p90 > 0:
                tpsg = (result.throughput_p90 * self.config.osl) / tp
            elif result.throughput_p50 and result.throughput_p50 > 0:
                tpsg = (result.throughput_p50 * self.config.osl) / tp
            else:
                self.log("    ❌ No throughput metric available", 'error')
                continue

            ttft = result.ttft_p90 if result.ttft_p90 else float('inf')
            all_candidates.append((tp, tpsg, ttft, result.throughput_p90))

            if use_ttft:
                self.log(f"    ✅ TTFT_p90: {ttft:.0f}ms, TPSG: {tpsg:.0f} tokens/s/GPU", 'success')
                if ttft < best_ttft:
                    best_ttft = ttft
                    best_tp = tp
                    best_tpsg = tpsg
                    best_throughput = result.throughput_p90
            else:
                self.log(f"    ✅ TPSG: {tpsg:.0f} tokens/s/GPU", 'success')
                if tpsg > best_tpsg:
                    best_tpsg = tpsg
                    best_tp = tp
                    best_ttft = ttft
                    best_throughput = result.throughput_p90

        # If TTFT-based selection found nothing (all TTFTs were inf, common for
        # decode-only ISL=1 workloads), fall back to highest TPSG
        if best_tp is None and all_candidates:
            self.log("  ⚠️  All TTFT values are inf (normal for ISL=1 decode tests), selecting by highest TPSG", 'warning')
            all_candidates.sort(key=lambda x: x[1], reverse=True)  # sort by TPSG desc
            best_tp, best_tpsg, best_ttft, best_throughput = all_candidates[0]

        if best_tp is None:
            raise RuntimeError("All decode TP tests failed - no valid results")

        # Store all TP results for multi-TP split generation
        self.decode_tp_results = [
            {'tp': tp, 'tpsg': tpsg, 'ttft_p90': ttft, 'throughput_p90': thr}
            for tp, tpsg, ttft, thr in all_candidates
        ]

        self.optimal_decode_tp = OptimalTP(
            tp=best_tp,
            tpsg=best_tpsg,
            ttft_p90=best_ttft,
            throughput_p90=best_throughput
        )

        criterion = "lowest TTFT" if use_ttft else "highest TPSG"
        self.log("", 'info')
        self.log(f"✅ Optimal Decode TP: {self.optimal_decode_tp.tp} (selected by {criterion})", 'success')
        self.log(f"   TTFT_p90: {best_ttft:.0f}ms, TPSG: {self.optimal_decode_tp.tpsg:.0f} tokens/s/GPU", 'info')
        self.log(f"   Tested all {len(valid_tp)} TP values", 'info')

    def _optimize_prefill_tp(self):
        """
        Step 3: Test ALL valid TP values for prefill workload.

        Tests prefill-only workload (ISL=target, OSL=1) with every valid TP.
        Objective: lowest TTFT (when objective='ttft') or highest TPSG.
        """
        valid_tp = self._get_valid_tp_options(role='prefill')
        self.log(f"Testing all {len(valid_tp)} valid TP values: {valid_tp}", 'info')
        self.log(f"Workload: ISL={self.config.isl}, OSL=1 (prefill-focused)", 'info')

        use_ttft = self.config.objective == 'ttft'
        best_tp = None
        best_tpsg = 0.0
        best_ttft = float('inf')
        best_throughput = None
        all_candidates = []

        for i, tp in enumerate(valid_tp):
            if self._should_stop():
                break

            test_id = f"step3-trial{i + 1}-prefill-tp{tp}"
            self.log(f"  Test {i + 1}/{len(valid_tp)}: TP={tp}", 'info')

            # Check for completed test from previous run
            if test_id in self.completed_tests:
                row = self.completed_tests[test_id]
                result = self._make_test_result_from_db(row)
                self.log("    ⏩ Resuming from DB (already completed)", 'info')
            else:
                safe_c = self._estimate_safe_concurrency(tp)
                test_config = self._create_aggregated_config(
                    tp=tp,
                    num_gpus=tp,
                    isl=self.config.isl,
                    osl=1,
                    test_id=test_id,
                    use_concurrency=True,
                    concurrency_override=safe_c
                )
                test_config.stop_mode = 'max_requests'
                test_config.max_requests = safe_c * 10

                result = self.orchestrator.run_test(
                    test_config,
                    cleanup=True,
                    log_callback=lambda msg: self.log(msg, 'info'),
                    stop_check=self._should_stop
                )

                self.all_test_results.append((test_config, result))
                self._save_test_to_database(test_config, result)
                self._check_pod_errors(test_config, result)
                self._check_request_errors(test_config, result)

                if not result or not result.guidellm_success:
                    self.log("    ❌ Test failed - STOPPING optimization", 'error')
                    self.log(f"    🔍 Debug: kubectl get pods -n {self.config.namespace} -l test-id={test_id}", 'error')
                    self.log(f"    🧹 Cleanup: kubectl delete lws -n {self.config.namespace} -l test-id={test_id}", 'error')
                    raise RuntimeError(f"Test {test_id} failed - stopping optimization")

            # Calculate TPSG
            if result.throughput_p90 and result.throughput_p90 > 0:
                tpsg = (result.throughput_p90 * self.config.isl) / tp
            elif result.throughput_p50 and result.throughput_p50 > 0:
                tpsg = (result.throughput_p50 * self.config.isl) / tp
            else:
                self.log("    ❌ No throughput metric available", 'error')
                continue

            ttft = result.ttft_p90 if result.ttft_p90 else float('inf')
            all_candidates.append((tp, tpsg, ttft, result.throughput_p90))

            if use_ttft:
                self.log(f"    ✅ TTFT_p90: {ttft:.0f}ms, TPSG: {tpsg:.0f} tokens/s/GPU", 'success')
                if ttft < best_ttft:
                    best_ttft = ttft
                    best_tp = tp
                    best_tpsg = tpsg
                    best_throughput = result.throughput_p90
            else:
                self.log(f"    ✅ TPSG: {tpsg:.0f} tokens/s/GPU", 'success')
                if tpsg > best_tpsg:
                    best_tpsg = tpsg
                    best_tp = tp
                    best_ttft = ttft
                    best_throughput = result.throughput_p90

        if best_tp is None and all_candidates:
            self.log("  ⚠️  All TTFT values are inf, selecting by highest TPSG", 'warning')
            all_candidates.sort(key=lambda x: x[1], reverse=True)
            best_tp, best_tpsg, best_ttft, best_throughput = all_candidates[0]

        if best_tp is None:
            raise RuntimeError("All prefill TP tests failed - no valid results")

        # Store all TP results for multi-TP split generation
        self.prefill_tp_results = [
            {'tp': tp, 'tpsg': tpsg, 'ttft_p90': ttft, 'throughput_p90': thr}
            for tp, tpsg, ttft, thr in all_candidates
        ]

        self.optimal_prefill_tp = OptimalTP(
            tp=best_tp,
            tpsg=best_tpsg,
            ttft_p90=best_ttft,
            throughput_p90=best_throughput
        )

        criterion = "lowest TTFT" if use_ttft else "highest TPSG"
        self.log("", 'info')
        self.log(f"✅ Optimal Prefill TP: {self.optimal_prefill_tp.tp} (selected by {criterion})", 'success')
        self.log(f"   TTFT_p90: {best_ttft:.0f}ms, TPSG: {self.optimal_prefill_tp.tpsg:.0f} tokens/s/GPU", 'info')
        self.log(f"   Tested all {len(valid_tp)} TP values", 'info')

