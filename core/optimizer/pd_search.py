"""Steps 4-7: PD split search — feasible splits, pareto, validation."""

import os
import time
from typing import List, Tuple, Optional


from core.optimizer.config import FeasibleSplit

class PDSearchMixin:
    """Mixin providing PD search methods for RecipeOptimizer."""

    def _select_tp_pairs(self):
        """
        Select (prefill_tp, decode_tp) pairs to test in Step 7.

        Builds the cross-product of the top-N prefill TPs and top-N decode TPs,
        then deduplicates.  N is controlled by config.tp_pair_top_n (1=fast, 2=thorough).
        """
        use_ttft = self.config.objective == 'ttft'
        top_n = self.config.tp_pair_top_n

        # Rank prefill TPs: by TTFT (lower=better) for ttft objective, by TPSG otherwise
        if use_ttft:
            prefill_ranked = sorted(self.prefill_tp_results, key=lambda r: r['ttft_p90'] or float('inf'))
        else:
            prefill_ranked = sorted(self.prefill_tp_results, key=lambda r: r['tpsg'], reverse=True)
        # Rank decode TPs: always by TPSG (decode throughput efficiency)
        decode_ranked = sorted(self.decode_tp_results, key=lambda r: r['tpsg'], reverse=True)

        top_prefill = [r['tp'] for r in prefill_ranked[:top_n]]
        top_decode = [r['tp'] for r in decode_ranked[:top_n]]

        prefill_metric = "TTFT" if use_ttft else "TPSG"
        self.log(f"  Top-{top_n} prefill TPs (by {prefill_metric}): {top_prefill}", 'info')
        self.log(f"  Top-{top_n} decode TPs (by TPSG): {top_decode}", 'info')

        # Cross-product of top-N × top-N, deduplicated, primary pair first.
        # NIXL KV transfer constraint: when prefill_tp >= num_kv_heads (KV cache is
        # replicated across prefill TP workers) AND prefill_tp > decode_tp, the
        # handshake fails with AssertionError in _validate_remote_agent_handshake.
        num_kv_heads = (self._model_config or {}).get('num_key_value_heads', 0)
        seen = set()
        skipped = []
        self._selected_tp_pairs = []

        # Primary pair: best prefill × best decode (always first)
        primary = (top_prefill[0], top_decode[0])

        allow_asymmetric = getattr(self.config, 'allow_asymmetric_tp', False)
        all_pairs = [primary] + [(ptp, dtp) for ptp in top_prefill for dtp in top_decode if (ptp, dtp) != primary]
        for ptp, dtp in all_pairs:
            if (ptp, dtp) in seen:
                continue
            seen.add((ptp, dtp))
            if ptp > dtp and not allow_asymmetric:
                skipped.append((ptp, dtp))
                continue
            self._selected_tp_pairs.append((ptp, dtp))

        if skipped:
            skipped_str = ', '.join(f'(PTP={p}, DTP={d})' for p, d in skipped)
            self.log(f"  ⚠️  Skipped {len(skipped)} pairs: prefill TP > decode TP "
                     f"(NIXL KV transfer crashes with asymmetric TP — vllm#43523)", 'warning')
            self.log(f"     Affected: [{skipped_str}]", 'warning')
            self.log(f"     Enable 'Allow Prefill TP > Decode TP' in Test Config to override", 'warning')

        # Fall back to symmetric if everything was filtered
        if not self._selected_tp_pairs:
            for tp in top_prefill:
                self._selected_tp_pairs.append((tp, tp))

        for ptp, dtp in self._selected_tp_pairs:
            label = f"TP={ptp}" if ptp == dtp else f"Prefill TP={ptp}, Decode TP={dtp}"
            tag = " (primary)" if (ptp, dtp) == primary else ""
            self.log(f"  ✅ {label}{tag}", 'success')

    def _usable_gpus_for_tp(self, tp: int) -> int:
        """Count GPUs on nodes that can actually host pods with this TP."""
        if not self.cluster_resources:
            return self.config.total_gpus
        gpu_nodes = [n for n in self.cluster_resources.nodes if n.gpus > 0]
        if not gpu_nodes:
            return self.config.total_gpus
        usable = sum(n.gpus for n in gpu_nodes if n.gpus >= tp)
        return min(usable, self.config.total_gpus)

    def _generate_splits_for_tp_pair(self, prefill_tp: int, decode_tp: int) -> List[FeasibleSplit]:
        """Generate all valid splits for a (prefill_tp, decode_tp) pair."""
        usable_gpus = self._usable_gpus_for_tp(max(prefill_tp, decode_tp))
        if usable_gpus < prefill_tp + decode_tp:
            return []
        splits = []
        for prefill_gpus in range(prefill_tp, usable_gpus, prefill_tp):
            decode_gpus = usable_gpus - prefill_gpus
            if decode_gpus >= decode_tp and decode_gpus % decode_tp == 0:
                prefill_pct = (prefill_gpus / usable_gpus) * 100
                splits.append(FeasibleSplit(
                    prefill_pods=prefill_gpus // prefill_tp,
                    decode_pods=decode_gpus // decode_tp,
                    prefill_tp=prefill_tp,
                    decode_tp=decode_tp,
                    prefill_gpus=prefill_gpus,
                    decode_gpus=decode_gpus,
                    total_gpus=usable_gpus,
                    prefill_pct=prefill_pct
                ))
        return splits

    def _smart_pd_search(self, tp_pairs: List[tuple]) -> List[FeasibleSplit]:
        """Calculate mathematically optimal P/D splits from calibration data.

        For each TP pair, uses measured per-pod throughput from Steps 2-3 to
        compute the balanced prefill/decode ratio, then returns ~3 candidate
        splits around that optimum.
        """
        import math

        prefill_by_tp = {r['tp']: r for r in self.prefill_tp_results}
        decode_by_tp = {r['tp']: r for r in self.decode_tp_results}

        smart_splits = []

        for ptp, dtp in tp_pairs:
            prefill_thr = prefill_by_tp.get(ptp, {}).get('throughput_p90', 0)
            decode_thr = decode_by_tp.get(dtp, {}).get('throughput_p90', 0)

            if prefill_thr <= 0 or decode_thr <= 0:
                self.log(f"  ⚠️  Skipping PTP={ptp}/DTP={dtp}: missing throughput data", 'warning')
                continue

            all_valid = self._generate_splits_for_tp_pair(ptp, dtp)
            if not all_valid:
                continue

            usable_gpus = self._usable_gpus_for_tp(max(ptp, dtp))
            r = decode_thr / prefill_thr
            d_ideal = usable_gpus / (r * ptp + dtp)

            candidates_d = sorted({
                max(1, math.floor(d_ideal) - 1),
                max(1, math.floor(d_ideal)),
                max(1, math.ceil(d_ideal)),
                math.ceil(d_ideal) + 1,
            })

            self.log(f"  Smart search PTP={ptp}/DTP={dtp}:", 'info')
            self.log(f"    Prefill: {prefill_thr:.2f} req/s/pod, Decode: {decode_thr:.2f} req/s/pod", 'info')
            self.log(f"    Balanced ratio P:D = {r:.2f}:1, ideal decode pods = {d_ideal:.1f}", 'info')

            valid_by_decode = {s.decode_pods: s for s in all_valid}
            selected = []
            for d in candidates_d:
                if d in valid_by_decode:
                    selected.append(valid_by_decode[d])

            # Always include edge splits (max prefill, max decode)
            # These are often optimal and Smart Search may skip them
            min_decode = min(valid_by_decode.keys())
            max_decode = max(valid_by_decode.keys())
            for edge_d in [min_decode, max_decode]:
                if edge_d in valid_by_decode and valid_by_decode[edge_d] not in selected:
                    selected.append(valid_by_decode[edge_d])

            if len(selected) < 2 and all_valid:
                by_distance = sorted(all_valid, key=lambda s: abs(s.decode_pods - d_ideal))
                for s in by_distance:
                    if s not in selected:
                        selected.append(s)
                    if len(selected) >= 3:
                        break

            for s in selected:
                self.log(f"    -> {s.prefill_pods}P + {s.decode_pods}D "
                         f"({s.prefill_pct:.1f}% prefill)", 'info')

            smart_splits.extend(selected)

        seen = set()
        unique = []
        for s in smart_splits:
            key = (s.prefill_pods, s.decode_pods, s.prefill_tp, s.decode_tp)
            if key not in seen:
                seen.add(key)
                unique.append(s)

        unique.sort(key=lambda s: (s.prefill_tp, s.decode_tp, s.prefill_pct))

        exhaustive_count = sum(len(self._generate_splits_for_tp_pair(p, d)) for p, d in tp_pairs)
        self.log(f"  Smart PD search: {len(unique)} candidates (vs {exhaustive_count} exhaustive)", 'success')
        return unique

    def _calculate_feasible_splits(self):
        """
        Steps 4-5: Calculate ideal P/D ratio and select splits to test.

        Uses optimal TPs from Steps 2-3 to calculate resource requirements,
        enumerate all valid splits, then select those nearest the ideal ratio.
        Supports asymmetric TP — prefill and decode can use different TP values.
        """
        # Step 4: Cluster capacity analysis
        # config.qps is concurrency (simultaneous in-flight requests), NOT requests/sec.
        # GPU cost per request (GPU-seconds): cost = tokens / TPSG
        prefill_tpsg = self.optimal_prefill_tp.tpsg
        decode_tpsg = self.optimal_decode_tp.tpsg

        if prefill_tpsg <= 0 or decode_tpsg <= 0:
            self.log("❌ Cannot calculate GPU splits: TPSG values are zero or negative", 'error')
            self.log(f"   Prefill TPSG: {prefill_tpsg}, Decode TPSG: {decode_tpsg}", 'error')
            self.log("   This indicates benchmark failures. Check gateway configuration and pod health.", 'error')
            raise ValueError(f"Invalid TPSG values: prefill={prefill_tpsg}, decode={decode_tpsg}")

        prefill_cost = self.config.isl / prefill_tpsg
        decode_cost = self.config.osl / decode_tpsg
        total_cost = prefill_cost + decode_cost

        max_throughput_pct = (prefill_cost / total_cost) * 100

        total_gpus = self.config.total_gpus
        sustainable_qps = total_gpus / total_cost / self.config.headroom
        concurrency = self.config.qps

        self.log("Step 4: Cluster Capacity Analysis", 'info')
        self.log(f"  Concurrency (simultaneous requests): {concurrency:.0f}", 'info')
        self.log(f"  GPU cost per request:", 'info')
        self.log(f"    Prefill: {self.config.isl} ISL ÷ {prefill_tpsg:.0f} TPSG = {prefill_cost:.2f} GPU-sec", 'info')
        self.log(f"    Decode:  {self.config.osl} OSL ÷ {decode_tpsg:.0f} TPSG = {decode_cost:.2f} GPU-sec", 'info')
        self.log(f"    Total:   {total_cost:.2f} GPU-sec/request", 'info')
        self.log(f"  Max-throughput prefill ratio: {max_throughput_pct:.1f}%", 'info')

        if self.config.objective == 'ttft':
            prefill_tp = self.optimal_prefill_tp.tp
            decode_tp = self.optimal_decode_tp.tp
            ideal_decode_gpus = min(int(concurrency) * decode_tp,
                                    total_gpus - prefill_tp)
            self.ideal_prefill_pct = ((total_gpus - ideal_decode_gpus) / total_gpus) * 100
            ideal_decode_pods = ideal_decode_gpus // decode_tp
            self.log(f"  Latency-optimal prefill ratio: {self.ideal_prefill_pct:.1f}%"
                     f" (targeting {ideal_decode_pods} decode pods for {int(concurrency)} users)", 'info')
        else:
            self.ideal_prefill_pct = max_throughput_pct

        self.log("", 'info')

        self.log(f"Step 5: Sustainable Throughput (with {self.config.headroom}x headroom)", 'info')
        self.log(f"  Available: {total_gpus} GPUs", 'info')
        self.log(f"  Sustainable QPS: {total_gpus} ÷ {total_cost:.2f} ÷ {self.config.headroom} = {sustainable_qps:.2f} req/s", 'info')

        sustainable_concurrency = max(1, int(total_gpus / (total_cost * self.config.headroom)))

        self._gpu_sizing = {
            'concurrency': concurrency,
            'isl': self.config.isl,
            'osl': self.config.osl,
            'prefill_tpsg': round(prefill_tpsg, 1),
            'decode_tpsg': round(decode_tpsg, 1),
            'prefill_cost': round(prefill_cost, 2),
            'decode_cost': round(decode_cost, 2),
            'total_cost': round(total_cost, 2),
            'headroom': self.config.headroom,
            'max_throughput_pct': round(max_throughput_pct, 1),
            'ideal_prefill_pct': round(self.ideal_prefill_pct, 1),
            'sustainable_throughput_rps': round(sustainable_qps, 2),
            'sustainable_concurrency': sustainable_concurrency,
            'total_gpus': total_gpus,
        }

        # Sustainable concurrency: max concurrent users before overload.
        # Each request uses total_cost GPU-seconds, so max concurrent = GPUs / (cost × headroom)
        sustainable_concurrency = max(1, int(total_gpus / (total_cost * self.config.headroom)))
        implied_throughput = sustainable_qps  # cluster max in req/s

        if concurrency > sustainable_concurrency:
            self.sustainable_throughput_rps = sustainable_qps
            self.achievable_concurrency = sustainable_concurrency
            self.log(f"  Requested concurrency: {concurrency:.0f} users", 'info')
            self.log(f"  Sustainable: {sustainable_concurrency} users ({sustainable_qps:.2f} req/s)", 'info')
            self.log(f"  ⚠️  Load exceeds capacity ({concurrency:.0f} > {sustainable_concurrency} users)", 'warning')
            if self.config.use_achievable_qps:
                self.effective_concurrency = sustainable_concurrency
                self.log(f"  ✅ Scaling down to {sustainable_concurrency} concurrent users for Steps 7-8", 'success')
            else:
                self.effective_concurrency = int(self.config.qps)
                self.log(f"  ℹ️  Using original concurrency ({concurrency:.0f}) for Steps 7-8 — expect overload", 'info')
                if self.config.latency_constraint_enabled:
                    self.log(f"  ℹ️  Step 10 will find max throughput under latency SLA", 'info')
                else:
                    self.log(f"  ℹ️  Step 11 will re-test at sustainable load ({sustainable_concurrency} users)", 'info')
        else:
            self.effective_concurrency = int(self.config.qps)
            self.log(f"  ✅ Cluster can handle the load ({concurrency:.0f} users, capacity: {sustainable_concurrency} users)", 'success')

        self.log("", 'info')

        # Enumerate valid splits
        tp_pairs_to_test = getattr(self, '_selected_tp_pairs', None)
        if tp_pairs_to_test is None:
            tp_pairs_to_test = [(self.optimal_prefill_tp.tp, self.optimal_decode_tp.tp)]

        self.log("Feasible P/D Splits:", 'info')
        for ptp, dtp in tp_pairs_to_test:
            if ptp == dtp:
                self.log(f"  Testing: TP={ptp} (symmetric)", 'info')
            else:
                self.log(f"  Testing: Prefill TP={ptp}, Decode TP={dtp} (asymmetric)", 'info')

        # Generate splits for all selected TP pairs
        all_valid_splits = []
        for ptp, dtp in tp_pairs_to_test:
            tp_splits = self._generate_splits_for_tp_pair(ptp, dtp)
            all_valid_splits.extend(tp_splits)
            label = f"TP={ptp}" if ptp == dtp else f"PTP={ptp}/DTP={dtp}"
            self.log(f"  {label}: {len(tp_splits)} valid splits", 'info')

        self.log(f"  Total valid splits: {len(all_valid_splits)}", 'info')

        # Select splits to test based on search mode
        import re
        resumed_step7 = {name: row for name, row in self.completed_tests.items() if name.startswith('step7-')}

        if self.config.pd_search_mode == 'smart':
            self.log(f"\n  Search mode: Smart (calculated ~3 splits per TP pair)", 'info')
            planned = self._smart_pd_search(tp_pairs_to_test)

            if resumed_step7:
                self.log(f"  Resuming: found {len(resumed_step7)} completed step7 tests", 'info')
                planned_ids = {f"step7-{s.prefill_pods}p{s.decode_pods}d-ptp{s.prefill_tp}-dtp{s.decode_tp}" for s in planned}
                self.feasible_splits = list(planned)
                for name, row in resumed_step7.items():
                    if name not in planned_ids:
                        m = re.match(r'step7-(\d+)p(\d+)d-ptp(\d+)-dtp(\d+)', name)
                        if m:
                            pp, dp, ptp_v, dtp_v = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
                            self.feasible_splits.append(FeasibleSplit(
                                prefill_pods=pp, decode_pods=dp,
                                prefill_tp=ptp_v, decode_tp=dtp_v,
                                prefill_gpus=pp * ptp_v, decode_gpus=dp * dtp_v,
                                total_gpus=pp * ptp_v + dp * dtp_v,
                                prefill_pct=(pp * ptp_v / (pp * ptp_v + dp * dtp_v)) * 100
                            ))
            else:
                self.feasible_splits = planned

            self.feasible_splits.sort(key=lambda s: (s.prefill_tp, s.decode_tp, s.prefill_pct))
        else:
            # Exhaustive mode: test all valid splits (original behavior)
            self.log(f"\n  Search mode: Exhaustive (all valid splits)", 'info')
            max_splits = self.config.max_pd_splits

            if resumed_step7:
                self.log(f"  Resuming: found {len(resumed_step7)} completed step7 tests", 'info')

                split_by_id = {}
                for s in all_valid_splits:
                    tid = f"step7-{s.prefill_pods}p{s.decode_pods}d-ptp{s.prefill_tp}-dtp{s.decode_tp}"
                    split_by_id[tid] = s

                completed_split_ids = set()
                self.feasible_splits = []
                for name in sorted(resumed_step7.keys()):
                    if name in split_by_id:
                        self.feasible_splits.append(split_by_id[name])
                        completed_split_ids.add(name)
                    else:
                        m = re.match(r'step7-(\d+)p(\d+)d-ptp(\d+)-dtp(\d+)', name)
                        if m:
                            pp, dp, ptp_v, dtp_v = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
                            self.feasible_splits.append(FeasibleSplit(
                                prefill_pods=pp, decode_pods=dp,
                                prefill_tp=ptp_v, decode_tp=dtp_v,
                                prefill_gpus=pp * ptp_v, decode_gpus=dp * dtp_v,
                                total_gpus=pp * ptp_v + dp * dtp_v,
                                prefill_pct=(pp * ptp_v / (pp * ptp_v + dp * dtp_v)) * 100
                            ))
                            completed_split_ids.add(name)

                candidates = [s for s in all_valid_splits
                              if f"step7-{s.prefill_pods}p{s.decode_pods}d-ptp{s.prefill_tp}-dtp{s.decode_tp}" not in completed_split_ids]
                if max_splits > 0:
                    remaining_slots = max_splits - len(self.feasible_splits)
                    if remaining_slots > 0:
                        candidates.sort(key=lambda s: abs(s.prefill_pct - self.ideal_prefill_pct))
                        self.feasible_splits.extend(candidates[:remaining_slots])
                else:
                    self.feasible_splits.extend(candidates)

                self.feasible_splits.sort(key=lambda s: (s.prefill_tp, s.decode_tp, s.prefill_pct))
            elif max_splits <= 0 or len(all_valid_splits) <= max_splits:
                self.feasible_splits = all_valid_splits
                self.feasible_splits.sort(key=lambda s: (s.prefill_tp, s.decode_tp, s.prefill_pct))
            else:
                by_pair = {}
                for s in all_valid_splits:
                    key = (s.prefill_tp, s.decode_tp)
                    by_pair.setdefault(key, []).append(s)

                selected = []
                for key, splits in by_pair.items():
                    best = min(splits, key=lambda s: abs(s.prefill_pct - self.ideal_prefill_pct))
                    selected.append(best)

                selected_set = set(id(s) for s in selected)
                remaining = [s for s in all_valid_splits if id(s) not in selected_set]
                remaining.sort(key=lambda s: abs(s.prefill_pct - self.ideal_prefill_pct))
                slots_left = max_splits - len(selected)
                if slots_left > 0:
                    selected.extend(remaining[:slots_left])

                self.feasible_splits = selected
                self.feasible_splits.sort(key=lambda s: (s.prefill_tp, s.decode_tp, s.prefill_pct))

        for split in self.feasible_splits:
            self.log(f"  ✓ {split.prefill_pods}P×TP{split.prefill_tp} + {split.decode_pods}D×TP{split.decode_tp} "
                    f"= {self.config.total_gpus} GPUs ({split.prefill_pct:.1f}% prefill)", 'info')

        self.log(f"\n  Splits to test: {len(self.feasible_splits)}", 'success')

    def _search_aggregated_configs(self):
        """
        Step 6: Search for the best aggregated configuration.

        Tests all valid TP values with the full ISL+OSL workload using
        all available GPUs. This finds the actual best aggregated config
        before PD/EP testing, so the architecture comparison in Step 8
        requires no additional tests.
        """
        valid_tp = self._get_valid_tp_options()
        total_gpus = self.config.total_gpus

        self.log(f"Testing aggregated at full workload: ISL={self.config.isl}, OSL={self.config.osl}", 'info')
        self.log(f"TP values: {valid_tp}, GPUs: {total_gpus}", 'info')

        for i, tp in enumerate(valid_tp):
            if self._should_stop():
                break

            replicas = total_gpus // tp
            if replicas < 1:
                continue
            actual_gpus = tp * replicas

            test_id = f"step6-agg-tp{tp}-{replicas}r"
            self.log(f"  Test {i + 1}/{len(valid_tp)}: TP={tp}, {replicas} replicas ({actual_gpus} GPUs)", 'info')

            if test_id in self.completed_tests:
                row = self.completed_tests[test_id]
                result = self._make_test_result_from_db(row)
                self.log("    ⏩ Resuming from DB (already completed)", 'info')
            else:
                test_config = self._create_aggregated_config(
                    tp=tp,
                    num_gpus=actual_gpus,
                    isl=self.config.isl,
                    osl=self.config.osl,
                    test_id=test_id,
                    use_concurrency=True
                )

                result = self.orchestrator.run_test(
                    test_config,
                    cleanup=True,
                    log_callback=lambda msg: self.log(msg, 'info'),
                    stop_check=self._should_stop
                )

                self.all_test_results.append((test_config, result))
                self._save_test_to_database(test_config, result)

                if not result or not result.guidellm_success:
                    self.log("    ❌ Test failed - STOPPING optimization", 'error')
                    self.log(f"    🔍 Debug: kubectl get pods -n {self.config.namespace} -l test-id={test_id}", 'error')
                    raise RuntimeError(f"Test {test_id} failed - stopping optimization")

            ttft = result.ttft_p90 or result.ttft_p50 or 1000000.0
            throughput = result.throughput_p90 or result.throughput_p50 or 0.0

            self.log(f"    ✅ TTFT p90: {ttft:.1f}ms, Throughput p90: {throughput:.2f} req/s", 'success')
            self.aggregated_search_results.append((tp, result))

        if not self.aggregated_search_results:
            self.log("❌ No aggregated test results!", 'error')
            return

        # Select best based on optimization objective
        if self.config.objective == 'throughput':
            best_tp, best_result = max(
                self.aggregated_search_results,
                key=lambda x: x[1].throughput_p90 if x[1].throughput_p90 else 0.0
            )
            criterion = "highest throughput"
        else:
            best_tp, best_result = min(
                self.aggregated_search_results,
                key=lambda x: x[1].ttft_p90 if x[1].ttft_p90 else 1000000.0
            )
            criterion = "lowest TTFT"

        self.aggregated_result = best_result
        self.aggregated_tp = best_tp
        self.aggregated_gpus = total_gpus

        best_ttft = best_result.ttft_p90 or best_result.ttft_p50 or 0
        best_tput = best_result.throughput_p90 or best_result.throughput_p50 or 0

        self.log("", 'info')
        self.log(f"✅ Best Aggregated: TP={best_tp}, {total_gpus // best_tp} replicas "
                 f"(selected by {criterion})", 'success')
        self.log(f"   TTFT p90: {best_ttft:.1f}ms, Throughput p90: {best_tput:.2f} req/s", 'info')

    def _optimize_pd_splits(self):
        """
        Step 7: Exhaustively test all selected P/D splits.

        Tests each feasible split and identifies the Pareto front
        (configurations where no other is better in both TTFT and throughput).
        """
        if not self.feasible_splits:
            self.log("❌ No feasible splits to test!", 'error')
            return

        self.log(f"Testing all {len(self.feasible_splits)} P/D split configurations...", 'info')
        isl_s = f"ISL={self.config.isl}" + (f"(σ={self.config.isl_stdev})" if self.config.isl_stdev else "")
        osl_s = f"OSL={self.config.osl}" + (f"(σ={self.config.osl_stdev})" if self.config.osl_stdev else "")
        turns_s = f", Turns={self.config.turns}" if self.config.turns > 1 else ""
        rate_label = f"Concurrency={int(self.config.qps)}" if self.config.rate_type == 'concurrent' else f"Rate={int(self.config.qps)} req/s ({self.config.rate_type})"
        self.log(f"Workload: {isl_s}, {osl_s}, {rate_label}{turns_s}", 'info')

        for i, split in enumerate(self.feasible_splits):
            if self._should_stop():
                break

            test_id = f"step7-{split.prefill_pods}p{split.decode_pods}d-ptp{split.prefill_tp}-dtp{split.decode_tp}"
            self.log(f"  Test {i + 1}/{len(self.feasible_splits)}: "
                    f"{split.prefill_pods}P×TP{split.prefill_tp} + "
                    f"{split.decode_pods}D×TP{split.decode_tp} ({split.prefill_pct:.0f}% prefill)", 'info')

            # Check for completed test from previous run
            if test_id in self.completed_tests:
                row = self.completed_tests[test_id]
                result = self._make_test_result_from_db(row)
                self.log("    ⏩ Resuming from DB (already completed)", 'info')
            else:
                test_config = self._create_pd_config(split)

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

            ttft = result.ttft_p90 if result.ttft_p90 else result.ttft_p50 if result.ttft_p50 else 1000000.0
            throughput = result.throughput_p90 if result.throughput_p90 else result.throughput_p50 if result.throughput_p50 else 0.0

            self.log(f"    ✅ TTFT p90: {ttft:.1f}ms, Throughput p90: {throughput:.2f} req/s", 'success')

            self.pareto_results.append((split, result))

        # Find Pareto front from results
        pareto_front = self._find_pareto_front()

        self.log("", 'info')
        self.log(f"✅ Found {len(pareto_front)} Pareto optimal configurations:", 'success')
        for i, (split, result) in enumerate(pareto_front, 1):
            ttft = result.ttft_p90 or result.ttft_p50 or 0
            throughput = result.throughput_p90 or result.throughput_p50 or 0
            self.log(f"  {i}. {split.prefill_pods}P×TP{split.prefill_tp} + {split.decode_pods}D×TP{split.decode_tp}: "
                    f"TTFT={ttft:.1f}ms, Throughput={throughput:.2f} req/s", 'info')

    def _validate_pd_vs_aggregated(self):
        """
        Step 8: Compare best PD config against best Aggregated from Step 6.

        No new tests — uses the best aggregated result already found in Step 6
        and the best PD result from Step 7.
        """
        if not self.pareto_results:
            self.log("⚠️  No PD results to compare — skipping Step 8", 'warning')
            return

        if not self.aggregated_result:
            self.log("⚠️  No aggregated results to compare — skipping Step 8", 'warning')
            return

        # Best PD by TTFT
        best_split, best_pd_result = min(
            self.pareto_results,
            key=lambda x: x[1].ttft_p90 if x[1].ttft_p90 else 1000000.0
        )

        best_pd_ttft = best_pd_result.ttft_p90 or best_pd_result.ttft_p50 or 0
        best_pd_tput = best_pd_result.throughput_p90 or best_pd_result.throughput_p50 or 0

        agg_ttft = self.aggregated_result.ttft_p90 or self.aggregated_result.ttft_p50 or 1000000.0
        agg_tput = self.aggregated_result.throughput_p90 or self.aggregated_result.throughput_p50 or 0.0

        self.log(f"Best PD: {best_split.prefill_pods}P×TP{best_split.prefill_tp} + "
                f"{best_split.decode_pods}D×TP{best_split.decode_tp}", 'info')
        self.log(f"  TTFT p90: {best_pd_ttft:.1f}ms, Throughput p90: {best_pd_tput:.2f} req/s", 'info')
        self.log(f"Best Aggregated: TP={self.aggregated_tp}, "
                f"{self.aggregated_gpus // self.aggregated_tp} replicas", 'info')
        self.log(f"  TTFT p90: {agg_ttft:.1f}ms, Throughput p90: {agg_tput:.2f} req/s", 'info')
        self.log("", 'info')

        # Compare
        ttft_diff = best_pd_ttft - agg_ttft
        ttft_pct = (ttft_diff / agg_ttft * 100) if agg_ttft > 0 else 0
        tput_diff = best_pd_tput - agg_tput
        tput_pct = (tput_diff / agg_tput * 100) if agg_tput > 0 else 0

        self.log("📊 PD vs Aggregated Comparison:", 'decision')
        self.log(f"  TTFT p90:       PD={best_pd_ttft:.1f}ms vs Agg={agg_ttft:.1f}ms "
                f"({'PD wins by' if ttft_diff < 0 else 'Agg wins by'} {abs(ttft_pct):.1f}%)", 'info')
        self.log(f"  Throughput p90:  PD={best_pd_tput:.2f} vs Agg={agg_tput:.2f} req/s "
                f"({'PD wins by' if tput_diff > 0 else 'Agg wins by'} {abs(tput_pct):.1f}%)", 'info')

        if agg_ttft < best_pd_ttft and agg_tput >= best_pd_tput:
            self.log("", 'info')
            self.log("⚡ AGGREGATED IS BETTER — lower TTFT and equal/higher throughput", 'decision')
        elif agg_ttft < best_pd_ttft:
            self.log("", 'info')
            self.log("⚡ AGGREGATED HAS BETTER TTFT but lower throughput — check the report for trade-offs", 'decision')
        else:
            self.log("", 'info')
            self.log("✅ PD CONFIRMED — PD has equal or better TTFT than Aggregated", 'decision')

