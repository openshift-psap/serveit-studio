"""
Report analysis engine.

Contains all business logic for analyzing optimization results:
Pareto frontier calculation, summary statistics, deployment
recommendations, chart data preparation.
"""

import logging
from typing import List, Dict, Any

from core.report_data import TestResult, ParetoPoint

logger = logging.getLogger(__name__)


class ReportAnalyzer:
    """Analyze optimization test results."""

    def calculate_pareto_frontier(self,
                                  results: List[TestResult],
                                  metric: str = 'ttft_p99',
                                  throughput_metric: str = 'throughput_p90') -> List[ParetoPoint]:
        """
        Calculate Pareto frontier for optimization results.

        A configuration is on the Pareto frontier if no other configuration
        is strictly better in all objectives (lower latency, higher throughput, lower cost).
        """
        successful = [r for r in results if r.is_successful]

        if not successful:
            logger.warning("No successful test results for Pareto analysis")
            return []

        candidates = []
        for config in successful:
            ttft = getattr(config, metric)
            throughput = getattr(config, throughput_metric)
            cost = config.total_gpus

            if ttft is None or throughput is None or cost == 0:
                continue

            efficiency = throughput / cost
            candidates.append(ParetoPoint(
                config=config,
                ttft=ttft,
                throughput=throughput,
                cost=cost,
                efficiency=efficiency
            ))

        if not candidates:
            return []

        pareto_frontier = []
        for i, point in enumerate(candidates):
            dominated = False
            for j, other in enumerate(candidates):
                if i == j:
                    continue
                better_ttft = other.ttft <= point.ttft
                better_throughput = other.throughput >= point.throughput
                better_cost = other.cost <= point.cost
                strictly_better = (
                    (other.ttft < point.ttft) or
                    (other.throughput > point.throughput) or
                    (other.cost < point.cost)
                )
                if better_ttft and better_throughput and better_cost and strictly_better:
                    dominated = True
                    break
            if not dominated:
                pareto_frontier.append(point)

        pareto_frontier.sort(key=lambda p: p.cost)
        return pareto_frontier

    def get_summary_statistics(self, results: List[TestResult]) -> Dict[str, Any]:
        """Calculate summary statistics across all tests."""
        successful = [r for r in results if r.is_successful]
        broken = [r for r in results if r.status == 'completed' and not r.is_successful]

        if not successful:
            return {
                'total_tests': len(results),
                'successful_tests': 0,
                'failed_tests': len([r for r in results if r.status == 'failed']),
                'broken_tests': len(broken),
                'error': 'No successful tests'
            }

        by_arch = {}
        for arch in ['aggregated', 'pd', 'ep']:
            arch_results = [r for r in successful if r.architecture == arch]
            if arch_results:
                by_arch[arch] = {
                    'count': len(arch_results),
                    'avg_ttft_p90': sum(r.ttft_p90 for r in arch_results) / len(arch_results),
                    'avg_throughput_p90': sum(r.throughput_p90 for r in arch_results) / len(arch_results),
                    'avg_throughput_mean': sum((r.throughput_mean or r.throughput_p90) for r in arch_results) / len(arch_results),
                    'avg_gpus': sum(r.total_gpus for r in arch_results) / len(arch_results),
                    'best_ttft': min(r.ttft_p90 for r in arch_results),
                    'best_throughput': max((r.throughput_mean or r.throughput_p90) for r in arch_results),
                }

        best_ttft_config = min(successful, key=lambda r: r.ttft_p90)
        best_throughput_config = max(successful, key=lambda r: r.throughput_p90)
        best_efficiency_config = max(successful, key=lambda r: r.throughput_p90 / r.total_gpus)

        return {
            'total_tests': len(results),
            'successful_tests': len(successful),
            'failed_tests': len([r for r in results if r.status == 'failed']),
            'broken_tests': len(broken),
            'by_architecture': by_arch,
            'best_configs': {
                'lowest_latency': {
                    'name': best_ttft_config.display_label,
                    'ttft_p90': best_ttft_config.ttft_p90,
                    'ttft_p95': best_ttft_config.ttft_p95,
                    'ttft_p99': best_ttft_config.ttft_p99,
                    'throughput_p90': best_ttft_config.throughput_p90,
                    'gpus': best_ttft_config.total_gpus,
                },
                'highest_throughput': {
                    'name': best_throughput_config.display_label,
                    'ttft_p90': best_throughput_config.ttft_p90,
                    'throughput_p90': best_throughput_config.throughput_p90,
                    'throughput_p95': best_throughput_config.throughput_p95,
                    'throughput_p99': best_throughput_config.throughput_p99,
                    'gpus': best_throughput_config.total_gpus,
                },
                'most_efficient': {
                    'name': best_efficiency_config.display_label,
                    'ttft_p90': best_efficiency_config.ttft_p90,
                    'throughput_p90': best_efficiency_config.throughput_p90,
                    'gpus': best_efficiency_config.total_gpus,
                    'efficiency': best_efficiency_config.throughput_p90 / best_efficiency_config.total_gpus,
                }
            }
        }

    def build_recommendation(self, run_id, results, conn):
        """
        Build deployment recommendation from test results.

        Analyzes step2 (decode TP sweep), step3 (prefill TP sweep), and step7
        (PD split tests) to produce a concrete deployment recommendation.
        """
        run_row = conn.execute(
            'SELECT model, isl, osl, num_users, goal, constraint_notes, created_at, completed_at, isl_stdev, osl_stdev FROM optimization_runs WHERE id = ?',
            (run_id,)
        ).fetchone()
        if not run_row:
            return None

        run_meta = dict(run_row)
        isl = run_meta['isl']
        osl = run_meta['osl']

        run_config = {}
        try:
            rc_row = conn.execute('SELECT config_json FROM optimization_runs WHERE id = ?', (run_id,)).fetchone()
            if rc_row and rc_row['config_json']:
                import json as _jrc
                run_config = _jrc.loads(rc_row['config_json'])
        except Exception:
            pass

        goal = run_meta.get('goal')
        if not goal:
            has_step7 = any(r.config_name.startswith('step7') for r in results)
            if has_step7:
                goal = 'ttft'
            else:
                goal = 'ttft'

        goal_info = {
            'ttft': {
                'name': 'Response Time Priority',
                'description': ('This optimization focused on minimizing Time-to-First-Token '
                                '(TTFT) — how quickly the model starts responding. It compared '
                                'Aggregated inference against Prefill/Decode (PD) disaggregation. '
                                'PD separates the work into specialized prefill and decode pods '
                                'so that new requests don\'t wait behind ongoing generation.'),
            },
            'throughput': {
                'name': 'Throughput Priority',
                'description': ('This optimization focused on maximizing requests per second. '
                                'It compared Aggregated inference against Expert Parallelism '
                                '(EP) — a pool of independent pods with expert-level prefill '
                                'load balancing (EPLB). EP varies tensor parallelism to find '
                                'the optimal balance between per-pod efficiency and replica count.'),
            },
            'balanced': {
                'name': 'Balanced Performance',
                'description': ('This optimization compared all three architectures: '
                                'Aggregated (baseline), Prefill/Decode disaggregation (PD), '
                                'and Expert Parallelism (EP). It finds the best architecture '
                                'for your specific model and workload across both latency '
                                'and throughput.'),
            },
            'aggregated_only': {
                'name': 'Aggregated Only',
                'description': ('This optimization tested only the standard Aggregated '
                                'architecture — all GPUs share the same workload with no '
                                'architecture comparison. It searched across TP values to '
                                'find the best aggregated configuration for your workload.'),
            },
            'pd_only': {
                'name': 'Prefill/Decode Only',
                'description': ('This optimization tested only Prefill/Decode (PD) '
                                'disaggregation — separate GPU groups handle input processing '
                                'and text generation. It searched across P/D splits to find '
                                'the optimal ratio for your workload.'),
            },
            'ep_only': {
                'name': 'Expert Parallelism Only',
                'description': ('This optimization tested only Expert Parallelism (EP) '
                                '— a pool of independent pods with expert-level load balancing. '
                                'It searched across TP values and replica counts to find '
                                'the best EP configuration for your workload.'),
            },
        }

        # Categorize tests by step
        step2_tests = []
        step3_tests = []
        step7_pd_tests = []   # step7-{p}p{d}d-... (PD splits)
        step7_ep_tests = []   # step7-ep-tp{tp}-{r}r (EP configs)
        step6_agg_tests = []
        step8_tests = []
        step9_tests = []

        for r in results:
            if not r.is_successful:
                continue
            if r.config_name.startswith('step2'):
                step2_tests.append(r)
            elif r.config_name.startswith('step3'):
                step3_tests.append(r)
            elif r.config_name.startswith('step7-ep-'):
                step7_ep_tests.append(r)
            elif r.config_name.startswith('step7'):
                step7_pd_tests.append(r)
            elif r.config_name.startswith('step6-agg'):
                step6_agg_tests.append(r)
            elif r.config_name.startswith('step8'):
                step8_tests.append(r)
            elif r.config_name.startswith('step9'):
                step9_tests.append(r)

        # Backward compatibility: combine PD tests for existing code
        step7_tests = step7_pd_tests

        # --- Optimal Decode TP (from step 2) ---
        decode_tp_results = []
        for r in step2_tests:
            tput = r.throughput_p90 or r.throughput_p50 or 0
            if tput > 0:
                tpsg = (tput * osl) / r.tensor_parallelism
                decode_tp_results.append({
                    'tp': r.tensor_parallelism,
                    'tpsg': round(tpsg, 1),
                    'itl_p90': round(r.itl_p90, 2) if r.itl_p90 else None,
                    'ttft_p90': round(r.ttft_p90, 2) if r.ttft_p90 else None,
                })

        optimal_decode = None
        if decode_tp_results:
            if goal == 'ttft':
                # Response Time Priority: select by lowest TTFT
                candidates = [x for x in decode_tp_results if x['ttft_p90'] is not None]
                if candidates:
                    optimal_decode = min(candidates, key=lambda x: x['ttft_p90'])
            if not optimal_decode:
                optimal_decode = max(decode_tp_results, key=lambda x: x['tpsg'])

        # --- Optimal Prefill TP (from step 3) ---
        prefill_tp_results = []
        for r in step3_tests:
            tput = r.throughput_p90 or r.throughput_p50 or 0
            if tput > 0:
                tpsg = (tput * isl) / r.tensor_parallelism
                prefill_tp_results.append({
                    'tp': r.tensor_parallelism,
                    'tpsg': round(tpsg, 1),
                    'ttft_p90': round(r.ttft_p90, 2) if r.ttft_p90 else None,
                })

        optimal_prefill = None
        if prefill_tp_results:
            if goal == 'ttft':
                candidates = [x for x in prefill_tp_results if x['ttft_p90'] is not None]
                if candidates:
                    optimal_prefill = min(candidates, key=lambda x: x['ttft_p90'])
            if not optimal_prefill:
                optimal_prefill = max(prefill_tp_results, key=lambda x: x['tpsg'])

        # --- Helper to extract full percentile data ---
        def _percentiles(r):
            return {
                'ttft': {
                    'p50': round(r.ttft_p50, 1) if r.ttft_p50 else None,
                    'p90': round(r.ttft_p90, 1) if r.ttft_p90 else None,
                    'p95': round(r.ttft_p95, 1) if r.ttft_p95 else None,
                    'p99': round(r.ttft_p99, 1) if r.ttft_p99 else None,
                },
                'itl': {
                    'p50': round(r.itl_p50, 2) if r.itl_p50 else None,
                    'p90': round(r.itl_p90, 2) if r.itl_p90 else None,
                    'p95': round(r.itl_p95, 2) if r.itl_p95 else None,
                    'p99': round(r.itl_p99, 2) if r.itl_p99 else None,
                },
                'throughput': {
                    'p50': round(r.throughput_p50, 2) if r.throughput_p50 else None,
                    'p90': round(r.throughput_p90, 2) if r.throughput_p90 else None,
                    'p95': round(r.throughput_p95, 2) if r.throughput_p95 else None,
                    'p99': round(r.throughput_p99, 2) if r.throughput_p99 else None,
                },
            }

        def _config_dict(r, include_eff=False):
            c = None
            if r.test_config_json:
                try:
                    import json as _jc
                    c = _jc.loads(r.test_config_json).get('num_users')
                except Exception:
                    pass
            if c is None and r.metrics_json:
                try:
                    import json as _jc
                    mj = _jc.loads(r.metrics_json)
                    c = int(mj.get('concurrency_mean') or mj.get('concurrency_p50') or 0) or None
                except Exception:
                    pass
            test_settings = {}
            if r.test_config_json:
                try:
                    import json as _jtc
                    test_settings = _jtc.loads(r.test_config_json)
                except Exception:
                    pass
            d = {
                'config_name': r.display_label,
                'test_id': r.config_name,
                'prefill_pods': r.prefill_pods,
                'decode_pods': r.decode_pods,
                'tp': r.tensor_parallelism,
                'prefill_tp': test_settings.get('prefill_tp') or r.tensor_parallelism,
                'decode_tp': test_settings.get('decode_tp') or r.tensor_parallelism,
                'ttft_p50': round(r.ttft_p50, 1) if r.ttft_p50 else None,
                'ttft_p90': round(r.ttft_p90, 1),
                'ttft_p95': round(r.ttft_p95, 1) if r.ttft_p95 else None,
                'ttft_p99': round(r.ttft_p99, 1) if r.ttft_p99 else None,
                'itl_p90': round(r.itl_p90, 2) if r.itl_p90 else None,
                'throughput_mean': round(r.throughput_mean, 2) if r.throughput_mean else None,
                'throughput_p50': round(r.throughput_p50, 2) if r.throughput_p50 else None,
                'throughput_p90': round(r.throughput_p90, 2),
                'throughput_p95': round(r.throughput_p95, 2) if r.throughput_p95 else None,
                'throughput_p99': round(r.throughput_p99, 2) if r.throughput_p99 else None,
                'gpus': r.total_gpus,
                'ratio': f"{r.prefill_pods}:{r.decode_pods}",
                'percentiles': _percentiles(r),
                'concurrency': c,
                'epp_config': test_settings.get('epp_config'),
                'test_settings': {
                    'isl': test_settings.get('isl'),
                    'osl': test_settings.get('osl'),
                    'isl_stdev': test_settings.get('isl_stdev') or run_config.get('isl_stdev'),
                    'osl_stdev': test_settings.get('osl_stdev') or run_config.get('osl_stdev'),
                    'num_users': test_settings.get('num_users'),
                    'turns': test_settings.get('turns'),
                    'rate_type': test_settings.get('request_type') or run_config.get('rate_type'),
                    'test_duration': test_settings.get('test_duration'),
                    'stop_mode': test_settings.get('stop_mode'),
                    'max_requests': test_settings.get('max_requests'),
                    'workload_mode': test_settings.get('workload_mode') or run_config.get('workload_mode'),
                    'dataset_source': test_settings.get('dataset_source') or run_config.get('dataset_source'),
                    'dataset_column': test_settings.get('dataset_column') or run_config.get('dataset_column'),
                    'dataset_max_output': test_settings.get('dataset_max_output') or run_config.get('dataset_max_output'),
                    'prefix_cache_hit_pct': run_config.get('prefix_cache_hit_pct', 0),
                    'prefix_cache_mode': run_config.get('prefix_cache_mode'),
                    'prefix_cache_groups': run_config.get('prefix_cache_groups'),
                    'advanced_vllm': run_config.get('advanced_vllm'),
                    'tp_pair_top_n': run_config.get('tp_pair_top_n'),
                    'pd_search_mode': run_config.get('pd_search_mode'),
                    'use_achievable_qps': run_config.get('use_achievable_qps'),
                    'latency_constraint_enabled': run_config.get('latency_constraint_enabled'),
                    'latency_constraint_ms': run_config.get('latency_constraint_ms'),
                    'latency_constraint_percentile': run_config.get('latency_constraint_percentile'),
                    'epp_preset': run_config.get('epp_preset'),
                    'epp_benchmark': run_config.get('epp_benchmark'),
                    'epp_custom_enabled': run_config.get('epp_custom_enabled'),
                    'advanced_vllm_custom_enabled': run_config.get('advanced_vllm_custom_enabled'),
                },
            }
            if include_eff:
                d['efficiency'] = round(r.throughput_p90 / r.total_gpus, 3)
            return d

        # --- Best PD configuration (from step 7) ---
        best_pd_ttft = None
        best_pd_throughput = None

        if step7_tests:
            by_ttft = min(step7_tests, key=lambda r: r.ttft_p99 or r.ttft_p90 or 1e9)
            best_pd_ttft = _config_dict(by_ttft)

            by_tput = max(step7_tests, key=lambda r: r.throughput_mean or r.throughput_p90 or 0)
            best_pd_throughput = _config_dict(by_tput)

        # --- Best EP configuration (from step7-ep tests) ---
        best_ep_throughput = None
        best_ep_ttft = None
        ep_all_configs = []

        if step7_ep_tests:
            import re as _re
            for r in step7_ep_tests:
                m = _re.match(r'step7-ep-tp(\d+)-(\d+)r', r.config_name)
                ep_tp = int(m.group(1)) if m else r.tensor_parallelism
                ep_replicas = int(m.group(2)) if m else (r.total_gpus // r.tensor_parallelism)
                ep_entry = {
                    'config_name': r.display_label,
                    'test_id': r.config_name,
                    'tp': ep_tp,
                    'replicas': ep_replicas,
                    'ttft_p90': round(r.ttft_p90, 1) if r.ttft_p90 else None,
                    'throughput_p90': round(r.throughput_p90, 2) if r.throughput_p90 else None,
                    'gpus': r.total_gpus,
                    'percentiles': _percentiles(r),
                }
                ep_all_configs.append(ep_entry)

            # Best by throughput
            by_tput = max(step7_ep_tests, key=lambda r: r.throughput_p90 or 0)
            m = _re.match(r'step7-ep-tp(\d+)-(\d+)r', by_tput.config_name)
            best_ep_throughput = {
                'config_name': by_tput.display_label,
                'test_id': by_tput.config_name,
                'tp': int(m.group(1)) if m else by_tput.tensor_parallelism,
                'replicas': int(m.group(2)) if m else (by_tput.total_gpus // by_tput.tensor_parallelism),
                'ttft_p90': round(by_tput.ttft_p90, 1) if by_tput.ttft_p90 else None,
                'throughput_p90': round(by_tput.throughput_p90, 2) if by_tput.throughput_p90 else None,
                'gpus': by_tput.total_gpus,
                'percentiles': _percentiles(by_tput),
            }

            # Best by TTFT
            by_ttft = min(step7_ep_tests, key=lambda r: r.ttft_p90 if r.ttft_p90 else 1000000.0)
            m = _re.match(r'step7-ep-tp(\d+)-(\d+)r', by_ttft.config_name)
            best_ep_ttft = {
                'config_name': by_ttft.display_label,
                'test_id': by_ttft.config_name,
                'tp': int(m.group(1)) if m else by_ttft.tensor_parallelism,
                'replicas': int(m.group(2)) if m else (by_ttft.total_gpus // by_ttft.tensor_parallelism),
                'ttft_p90': round(by_ttft.ttft_p90, 1) if by_ttft.ttft_p90 else None,
                'throughput_p90': round(by_ttft.throughput_p90, 2) if by_ttft.throughput_p90 else None,
                'gpus': by_ttft.total_gpus,
                'percentiles': _percentiles(by_ttft),
            }

        # --- Aggregated baseline (from step 6 search, or legacy step 8) ---
        aggregated_baseline = None
        agg_tests = step6_agg_tests if step6_agg_tests else step8_tests
        if agg_tests:
            agg = min(agg_tests, key=lambda r: r.ttft_p90 if r.ttft_p90 else 1000000.0)
            aggregated_baseline = _config_dict(agg)
            aggregated_baseline['replicas'] = agg.total_gpus // agg.tensor_parallelism

        # --- Best config per percentile per architecture ---
        best_by_percentile = {}
        for pctl in ('p90', 'p95', 'p99'):
            ttft_field = f'ttft_{pctl}'
            tput_field = f'throughput_{pctl}'
            pctl_data = {}
            # Best aggregated at this percentile
            if agg_tests:
                valid_agg = [r for r in agg_tests if getattr(r, ttft_field, None)]
                if valid_agg:
                    best_agg = min(valid_agg, key=lambda r: getattr(r, ttft_field))
                    agg_c = None
                    if best_agg.test_config_json:
                        try:
                            import json as _jj
                            agg_c = _jj.loads(best_agg.test_config_json).get('num_users')
                        except Exception:
                            pass
                    agg_manifest_types = []
                    if best_agg.manifests_yaml:
                        try:
                            import json as _jm
                            agg_manifest_types = list(_jm.loads(best_agg.manifests_yaml).keys())
                        except Exception:
                            pass
                    pctl_data['aggregated'] = {
                        'config_name': best_agg.display_label,
                        'test_id': best_agg.config_name,
                        'ttft': round(getattr(best_agg, ttft_field), 1),
                        'throughput_mean': round(best_agg.throughput_mean, 2) if best_agg.throughput_mean else None,
                        'throughput': round(getattr(best_agg, tput_field, 0) or 0, 2),
                        'gpus': best_agg.total_gpus,
                        'tp': best_agg.tensor_parallelism,
                        'concurrency': agg_c,
                        'manifest_types': agg_manifest_types,
                    }
            # Best PD at this percentile
            if step7_tests:
                valid_pd = [r for r in step7_tests if getattr(r, ttft_field, None)]
                if valid_pd:
                    best_pd = min(valid_pd, key=lambda r: getattr(r, ttft_field))
                    pd_c = None
                    if best_pd.test_config_json:
                        try:
                            import json as _jj
                            pd_c = _jj.loads(best_pd.test_config_json).get('num_users')
                        except Exception:
                            pass
                    pd_manifest_types = []
                    if best_pd.manifests_yaml:
                        try:
                            import json as _jm2
                            pd_manifest_types = list(_jm2.loads(best_pd.manifests_yaml).keys())
                        except Exception:
                            pass
                    pctl_data['pd'] = {
                        'config_name': best_pd.display_label,
                        'test_id': best_pd.config_name,
                        'ttft': round(getattr(best_pd, ttft_field), 1),
                        'throughput_mean': round(best_pd.throughput_mean, 2) if best_pd.throughput_mean else None,
                        'throughput': round(getattr(best_pd, tput_field, 0) or 0, 2),
                        'gpus': best_pd.total_gpus,
                        'prefill_pods': best_pd.prefill_pods,
                        'decode_pods': best_pd.decode_pods,
                        'concurrency': pd_c,
                        'manifest_types': pd_manifest_types,
                    }
            if pctl_data:
                best_by_percentile[pctl] = pctl_data

        # --- Build recommendation for each goal ---
        recommendations = {}

        pd_is_better_ttft = True
        if best_pd_ttft and aggregated_baseline:
            agg_p99 = aggregated_baseline.get('ttft_p99') or aggregated_baseline['ttft_p90']
            pd_p99 = best_pd_ttft.get('ttft_p99') or best_pd_ttft['ttft_p90']
            if agg_p99 < pd_p99:
                pd_is_better_ttft = False

        if best_pd_ttft:
            if pd_is_better_ttft or not aggregated_baseline:
                recommendations['response_time'] = {
                    'goal': 'Response Time (minimize TTFT)',
                    'config': best_pd_ttft,
                    'deploy': f"{best_pd_ttft['prefill_pods']} Prefill + {best_pd_ttft['decode_pods']} Decode pods, TP={best_pd_ttft['tp']}",
                    'metric_value': f"{best_pd_ttft['ttft_p90']} ms",
                    'metric_name': 'TTFT P90',
                    'architecture': 'PD',
                }
            else:
                recommendations['response_time'] = {
                    'goal': 'Response Time (minimize TTFT)',
                    'config': aggregated_baseline,
                    'deploy': f"{aggregated_baseline['replicas']} Aggregated pods, TP={aggregated_baseline['tp']}",
                    'metric_value': f"{aggregated_baseline['ttft_p90']} ms",
                    'metric_name': 'TTFT P90',
                    'architecture': 'Aggregated',
                }
        elif aggregated_baseline:
            recommendations['response_time'] = {
                'goal': 'Response Time (minimize TTFT)',
                'config': aggregated_baseline,
                'deploy': f"{aggregated_baseline['replicas']} Aggregated pods, TP={aggregated_baseline['tp']}",
                'metric_value': f"{aggregated_baseline['ttft_p90']} ms",
                'metric_name': 'TTFT P90',
                'architecture': 'Aggregated',
            }

        # Throughput recommendation: consider PD, EP, and Aggregated (select by mean throughput)
        throughput_candidates = []
        if best_pd_throughput:
            tput_mean = best_pd_throughput.get('throughput_mean') or best_pd_throughput['throughput_p90']
            throughput_candidates.append(('PD', tput_mean, {
                'goal': 'Throughput (maximize req/s)',
                'config': best_pd_throughput,
                'deploy': f"{best_pd_throughput['prefill_pods']} Prefill + {best_pd_throughput['decode_pods']} Decode pods, TP={best_pd_throughput['tp']}",
                'metric_value': f"{tput_mean} req/s",
                'metric_name': 'Throughput Mean',
                'architecture': 'PD',
            }))
        if best_ep_throughput:
            tput_mean = best_ep_throughput.get('throughput_mean') or best_ep_throughput['throughput_p90']
            throughput_candidates.append(('EP', tput_mean, {
                'goal': 'Throughput (maximize req/s)',
                'config': best_ep_throughput,
                'deploy': f"{best_ep_throughput['replicas']} EP pods × TP{best_ep_throughput['tp']} ({best_ep_throughput['gpus']} GPUs)",
                'metric_value': f"{tput_mean} req/s",
                'metric_name': 'Throughput Mean',
                'architecture': 'EP',
            }))
        if aggregated_baseline:
            tput_mean = aggregated_baseline.get('throughput_mean') or aggregated_baseline['throughput_p90']
            throughput_candidates.append(('Aggregated', tput_mean, {
                'goal': 'Throughput (maximize req/s)',
                'config': aggregated_baseline,
                'deploy': f"{aggregated_baseline['replicas']} Aggregated pods, TP={aggregated_baseline['tp']}",
                'metric_value': f"{tput_mean} req/s",
                'metric_name': 'Throughput Mean',
                'architecture': 'Aggregated',
            }))

        if throughput_candidates:
            _, _, best_rec = max(throughput_candidates, key=lambda x: x[1] or 0)
            recommendations['throughput'] = best_rec

        # --- PD vs Aggregated comparison data ---
        pd_vs_agg = None
        if best_pd_ttft and aggregated_baseline:
            pd_ttft = best_pd_ttft['ttft_p90']
            agg_ttft = aggregated_baseline['ttft_p90']
            pd_tput = best_pd_ttft['throughput_p90']
            agg_tput = aggregated_baseline['throughput_p90']
            pd_p99 = best_pd_ttft.get('ttft_p99') or pd_ttft
            agg_p99 = aggregated_baseline.get('ttft_p99') or agg_ttft
            pd_vs_agg = {
                'pd': best_pd_ttft,
                'aggregated': aggregated_baseline,
                'ttft_winner': 'PD' if pd_ttft <= agg_ttft else 'Aggregated',
                'ttft_diff_pct': round(abs(pd_ttft - agg_ttft) / max(agg_ttft, 0.01) * 100, 1),
                'throughput_winner': 'PD' if pd_tput >= agg_tput else 'Aggregated',
                'throughput_diff_pct': round(abs(pd_tput - agg_tput) / max(agg_tput, 0.01) * 100, 1),
                'ttft_p99_winner': 'PD' if pd_p99 <= agg_p99 else 'Aggregated',
                'ttft_p99_diff_pct': round(abs(pd_p99 - agg_p99) / max(agg_p99, 0.01) * 100, 1),
            }

        # --- EP vs Aggregated comparison data ---
        ep_vs_agg = None
        if best_ep_throughput and aggregated_baseline:
            ep_ttft = best_ep_throughput['ttft_p90'] or 0
            agg_ttft = aggregated_baseline['ttft_p90'] or 0
            ep_tput = best_ep_throughput['throughput_p90'] or 0
            agg_tput = aggregated_baseline['throughput_p90'] or 0
            ep_vs_agg = {
                'ep': best_ep_throughput,
                'aggregated': aggregated_baseline,
                'ttft_winner': 'EP' if ep_ttft <= agg_ttft else 'Aggregated',
                'ttft_diff_pct': round(abs(ep_ttft - agg_ttft) / max(agg_ttft, 0.01) * 100, 1),
                'throughput_winner': 'EP' if ep_tput >= agg_tput else 'Aggregated',
                'throughput_diff_pct': round(abs(ep_tput - agg_tput) / max(agg_tput, 0.01) * 100, 1),
            }

        # --- Constraint notes (from DB) ---
        # Asymmetric TP / NIXL constraints only apply to PD architecture.
        # Suppress them when the primary recommendation is Aggregated or EP.
        constraint_notes = []
        primary_key = 'response_time' if goal in ('ttft', 'pd_only') else 'throughput'
        primary_arch = (recommendations.get(primary_key, {}).get('architecture') or '').upper()
        if primary_arch == 'PD':
            raw_notes = run_meta.get('constraint_notes')
            if raw_notes:
                import json
                try:
                    constraint_notes = json.loads(raw_notes)
                except (json.JSONDecodeError, TypeError):
                    pass

        # Calculate total testing duration
        total_duration_str = None
        created_at = run_meta.get('created_at')
        completed_at = run_meta.get('completed_at')
        if created_at and completed_at:
            from datetime import datetime
            try:
                t0 = datetime.fromisoformat(created_at)
                t1 = datetime.fromisoformat(completed_at)
                delta = t1 - t0
                total_secs = int(delta.total_seconds())
                hours, remainder = divmod(total_secs, 3600)
                minutes, secs = divmod(remainder, 60)
                if hours > 0:
                    total_duration_str = f"{hours}h {minutes}m"
                else:
                    total_duration_str = f"{minutes}m {secs}s"
            except (ValueError, TypeError):
                pass

        return {
            'model': run_meta['model'],
            'workload': {
                'isl': isl, 'osl': osl, 'users': run_meta['num_users'],
                'isl_stdev': run_meta.get('isl_stdev'), 'osl_stdev': run_meta.get('osl_stdev'),
                'turns': run_meta.get('turns', 1),
            },
            'total_duration': total_duration_str,
            'goal': goal,
            'goal_info': goal_info.get(goal, goal_info['ttft']),
            'optimal_decode_tp': optimal_decode,
            'optimal_prefill_tp': optimal_prefill,
            'decode_tp_all': sorted(decode_tp_results, key=lambda x: x['tp']),
            'prefill_tp_all': sorted(prefill_tp_results, key=lambda x: x['tp']),
            'best_pd': {
                'for_response_time': best_pd_ttft,
                'for_throughput': best_pd_throughput,
            },
            'best_ep': {
                'for_throughput': best_ep_throughput,
                'for_response_time': best_ep_ttft,
            },
            'ep_all_configs': ep_all_configs,
            'aggregated_baseline': aggregated_baseline,
            'pd_vs_agg': pd_vs_agg,
            'ep_vs_agg': ep_vs_agg,
            'recommendations': recommendations,
            'best_by_percentile': best_by_percentile,
            'constraint_notes': constraint_notes,
            'pd_tests_count': len(step7_pd_tests),
            'ep_tests_count': len(step7_ep_tests),
            'total_tests': len([r for r in results if r.is_successful]),
        }

    def build_chart_data(self, results, pareto, stats,
                         calibration_results=None):
        """Build chart data dicts for the frontend (no Plotly dependency)."""
        successful = [r for r in results if r.is_successful]
        arch_colors = {
            'aggregated': '#1f77b4',
            'pd': '#ff7f0e',
            'ep': '#2ca02c',
        }

        charts = {}

        # --- TP Calibration Chart (Step 2 Decode / Step 3 Prefill) ---
        pareto_data = {'traces': [], 'pareto_table': []}
        if calibration_results:
            role_config = {
                'step2': ('Decode', '#3b82f6'),
                'step3': ('Prefill', '#f59e0b'),
            }
            for prefix, (label, color) in role_config.items():
                pts = sorted(
                    [r for r in calibration_results
                     if r.config_name.startswith(prefix)],
                    key=lambda r: r.total_gpus
                )
                if not pts:
                    continue
                hover = [
                    f"{r.display_label}<br>"
                    f"TP: {r.tensor_parallelism}<br>"
                    f"TTFT: {r.ttft_p90:.1f}ms<br>"
                    f"Throughput: {r.throughput_p90:.2f} req/s"
                    + (f"<br>ITL: {r.itl_p90:.2f}ms" if r.itl_p90 else "")
                    for r in pts
                ]
                pareto_data['traces'].append({
                    'x': [r.total_gpus for r in pts],
                    'y': [r.ttft_p90 for r in pts],
                    'text': hover,
                    'name': f'{label} TTFT',
                    'color': color,
                    'yaxis': 'y',
                })
                itl_vals = [r.itl_p90 for r in pts if r.itl_p90]
                if itl_vals:
                    itl_color = '#ef4444' if prefix == 'step2' else '#f97316'
                    pareto_data['traces'].append({
                        'x': [r.total_gpus for r in pts if r.itl_p90],
                        'y': itl_vals,
                        'text': [f"ITL P90: {v:.2f}ms" for v in itl_vals],
                        'name': f'{label} ITL',
                        'color': itl_color,
                        'yaxis': 'y2',
                    })

        # Pareto table still uses real test results
        for p in pareto:
            pareto_data['pareto_table'].append({
                'config_name': p.config.display_label,
                'test_id': p.config.config_name,
                'architecture': (p.config.architecture or 'unknown').upper(),
                'ttft_p50': round(p.config.ttft_p50, 2) if p.config.ttft_p50 else None,
                'ttft_p90': round(p.ttft, 2),
                'ttft_p95': round(p.config.ttft_p95, 2) if p.config.ttft_p95 else None,
                'ttft_p99': round(p.config.ttft_p99, 2) if p.config.ttft_p99 else None,
                'throughput_p50': round(p.config.throughput_p50, 2) if p.config.throughput_p50 else None,
                'throughput_p90': round(p.throughput, 2),
                'throughput_p95': round(p.config.throughput_p95, 2) if p.config.throughput_p95 else None,
                'throughput_p99': round(p.config.throughput_p99, 2) if p.config.throughput_p99 else None,
                'itl_p50': round(p.config.itl_p50, 2) if p.config.itl_p50 else None,
                'itl_p90': round(p.config.itl_p90, 2) if p.config.itl_p90 else None,
                'itl_p95': round(p.config.itl_p95, 2) if p.config.itl_p95 else None,
                'itl_p99': round(p.config.itl_p99, 2) if p.config.itl_p99 else None,
                'gpus': p.cost,
                'efficiency': round(p.efficiency, 3),
            })
        charts['pareto'] = pareto_data

        # --- Throughput vs Latency scatter ---
        scatter_data = {'traces': []}
        for arch, color in arch_colors.items():
            arch_res = [r for r in successful if r.architecture == arch]
            if not arch_res:
                continue
            scatter_data['traces'].append({
                'x': [r.ttft_p90 for r in arch_res],
                'y': [(r.throughput_mean or r.throughput_p90) for r in arch_res],
                'sizes': [r.total_gpus * 3 for r in arch_res],
                'text': [
                    f"{r.display_label}<br>"
                    f"TTFT: {r.ttft_p90:.1f}ms<br>"
                    f"Throughput Mean: {(r.throughput_mean or r.throughput_p90):.2f} req/s<br>"
                    f"GPUs: {r.total_gpus}"
                    for r in arch_res
                ],
                'test_ids': [r.config_name for r in arch_res],
                'name': arch.upper(),
                'color': color,
            })
        charts['scatter'] = scatter_data

        # --- Efficiency bar chart ---
        eff_data = {'configs': [], 'values': [], 'colors': []}
        if successful:
            with_eff = sorted(
                [(r.display_label, (r.throughput_mean or r.throughput_p90) / r.total_gpus, r.architecture) for r in successful],
                key=lambda x: x[1], reverse=True
            )[:15]
            eff_data['configs'] = [label for label, _, _ in with_eff]
            eff_data['values'] = [round(eff, 3) for _, eff, _ in with_eff]
            eff_data['colors'] = [arch_colors.get(arch, '#999') for _, _, arch in with_eff]
        charts['efficiency'] = eff_data

        # --- Architecture comparison ---
        arch_comp = {'architectures': [], 'avg_ttft': [], 'avg_throughput': [],
                     'avg_gpus': [], 'best_ttft': [], 'colors': []}
        if 'by_architecture' in stats and stats['by_architecture']:
            for arch, data in stats['by_architecture'].items():
                arch_comp['architectures'].append(arch.upper())
                arch_comp['avg_ttft'].append(round(data['avg_ttft_p90'], 2))
                arch_comp['avg_throughput'].append(round(data.get('avg_throughput_mean', data['avg_throughput_p90']), 2))
                arch_comp['avg_gpus'].append(round(data['avg_gpus'], 1))
                arch_comp['best_ttft'].append(round(data['best_ttft'], 2))
                arch_comp['colors'].append(arch_colors.get(arch, '#999'))
        charts['architecture'] = arch_comp

        # --- vLLM Engine Metrics ---
        charts['vllm'] = self._build_vllm_metrics(successful)

        return charts

    def _build_vllm_metrics(self, successful):
        """Extract vLLM Prometheus metrics from each test for charting."""
        import json

        configs = []
        ttft = {'p50': [], 'p90': [], 'p95': [], 'p99': []}
        itl = {'p50': [], 'p90': [], 'p95': [], 'p99': []}
        e2e = {'p50': [], 'p90': [], 'p95': [], 'p99': []}
        token_rates = {'prompt': [], 'generation': []}
        request_state = {'running': [], 'waiting': [], 'kv_cache': []}
        time_breakdown = {'prefill': [], 'decode': [], 'queue': [], 'preemptions': [], 'waiting': []}
        network = {'pod_tx': [], 'pod_rx': [], 'ib_rx': []}

        for r in successful:
            if not r.metrics_json:
                continue
            try:
                metrics = json.loads(r.metrics_json)
                prom = metrics.get('prometheus_metrics', {})
            except (json.JSONDecodeError, TypeError):
                continue

            if not prom or not any(
                k.startswith('vllm_') and prom.get(k) for k in prom
            ):
                continue

            configs.append(r.display_label)

            def get_avg(key, multiplier=1):
                val = prom.get(key)
                if val and isinstance(val, dict) and val.get('avg') is not None:
                    return round(val['avg'] * multiplier, 2)
                return 0

            # TTFT percentiles (seconds → ms)
            for p in ['p50', 'p90', 'p95', 'p99']:
                ttft[p].append(get_avg(f'vllm_ttft_{p}', 1000))

            # ITL percentiles (seconds → ms)
            for p in ['p50', 'p90', 'p95', 'p99']:
                itl[p].append(get_avg(f'vllm_itl_{p}', 1000))

            # E2E latency (seconds)
            for p in ['p50', 'p90', 'p95', 'p99']:
                e2e[p].append(get_avg(f'vllm_e2e_{p}'))

            # Token rates (tokens/sec)
            token_rates['prompt'].append(get_avg('vllm_prompt_tokens_rate'))
            token_rates['generation'].append(get_avg('vllm_generation_tokens_rate'))

            # Request state
            request_state['running'].append(get_avg('vllm_requests_running'))
            request_state['waiting'].append(get_avg('vllm_requests_waiting'))
            request_state['kv_cache'].append(
                round(get_avg('vllm_kv_cache_pct') * 100, 1)
            )

            # Time breakdown (rate values)
            time_breakdown['prefill'].append(get_avg('vllm_prefill_time_rate'))
            time_breakdown['decode'].append(get_avg('vllm_decode_time_rate'))
            time_breakdown['queue'].append(get_avg('vllm_queue_time_rate'))
            time_breakdown['preemptions'].append(get_avg('vllm_preemptions_rate'))
            time_breakdown['waiting'].append(get_avg('vllm_requests_waiting'))

            # Network throughput (bytes/s → MB/s)
            network['pod_tx'].append(
                round(get_avg('pod_network_tx_rate') / 1_000_000, 2)
            )
            network['pod_rx'].append(
                round(get_avg('pod_network_rx_rate') / 1_000_000, 2)
            )
            # InfiniBand RX (bytes/s → GB/s), only if value looks
            # like a rate (< 100 GB/s) rather than a cumulative counter
            ib_rx_raw = get_avg('ib_rx_rate')
            ib_rx_gbps = round(ib_rx_raw / 1_000_000_000, 2) if ib_rx_raw else 0
            network['ib_rx'].append(ib_rx_gbps if ib_rx_gbps < 100 else 0)

        if not configs:
            return None

        return {
            'configs': configs,
            'ttft': ttft,
            'itl': itl,
            'e2e': e2e,
            'token_rates': token_rates,
            'request_state': request_state,
            'time_breakdown': time_breakdown,
            'network': network,
        }

    def _build_calibrated_qps_data(self, step10_results, recommendation,
                                    calibrated_qps_value=None, concurrency=None,
                                    total_gpus_available=None,
                                    gpu_sizing=None):
        """Build Step 11 calibrated QPS comparison data for the report.

        Step 11 re-tests the best configs at a sustainable QPS when the cluster
        was overloaded. Handles PD, EP, and Aggregated results.
        """
        if not step10_results:
            return None

        step10_pd = None
        step10_ep = None
        step10_agg = None
        for r in step10_results:
            if r.config_name.startswith('step10-ep-') and step10_ep is None:
                step10_ep = r
            elif r.architecture == 'pd' and step10_pd is None:
                step10_pd = r
            elif r.config_name.startswith('step10-aggregated') and step10_agg is None:
                step10_agg = r

        if not step10_pd and not step10_ep:
            return None

        data = {}

        if calibrated_qps_value is not None:
            data['requested_rps'] = round(calibrated_qps_value, 2)
        if concurrency is not None:
            data['concurrency'] = concurrency
        if total_gpus_available is not None:
            data['total_gpus_available'] = total_gpus_available
        if gpu_sizing is not None:
            data['gpu_sizing'] = gpu_sizing

        # Get overloaded results from recommendation for comparison
        overloaded_pd = recommendation.get('best_pd', {}).get('for_response_time') if recommendation else None
        overloaded_agg = recommendation.get('aggregated_baseline') if recommendation else None
        overloaded_ep = recommendation.get('best_ep', {}).get('for_throughput') if recommendation else None

        def _step9_entry(r):
            return {
                'config_name': r.display_label,
                'test_id': r.config_name,
                'ttft_p50': round(r.ttft_p50, 1) if r.ttft_p50 else None,
                'ttft_p90': round(r.ttft_p90, 1) if r.ttft_p90 else None,
                'ttft_p95': round(r.ttft_p95, 1) if r.ttft_p95 else None,
                'ttft_p99': round(r.ttft_p99, 1) if r.ttft_p99 else None,
                'throughput_p50': round(r.throughput_p50, 2) if r.throughput_p50 else None,
                'throughput_p90': round(r.throughput_p90, 2) if r.throughput_p90 else None,
                'throughput_p95': round(r.throughput_p95, 2) if r.throughput_p95 else None,
                'throughput_p99': round(r.throughput_p99, 2) if r.throughput_p99 else None,
                'itl_p50': round(r.itl_p50, 2) if r.itl_p50 else None,
                'itl_p90': round(r.itl_p90, 2) if r.itl_p90 else None,
                'itl_p95': round(r.itl_p95, 2) if r.itl_p95 else None,
                'itl_p99': round(r.itl_p99, 2) if r.itl_p99 else None,
                'gpus': r.total_gpus,
            }

        if step10_pd:
            data['pd'] = _step9_entry(step10_pd)

        if step10_ep:
            data['ep'] = _step9_entry(step10_ep)

        if step10_agg:
            data['aggregated'] = _step9_entry(step10_agg)

        # Add overloaded comparison if available
        if overloaded_pd and step10_pd:
            data['overloaded_pd'] = {
                'ttft_p90': overloaded_pd.get('ttft_p90'),
                'throughput_p90': overloaded_pd.get('throughput_p90'),
            }
            if data['pd']['ttft_p90'] and overloaded_pd.get('ttft_p90'):
                old = overloaded_pd['ttft_p90']
                new = data['pd']['ttft_p90']
                data['ttft_improvement_pct'] = round((old - new) / old * 100, 1)

        if overloaded_ep and step10_ep:
            data['overloaded_ep'] = {
                'ttft_p90': overloaded_ep.get('ttft_p90'),
                'throughput_p90': overloaded_ep.get('throughput_p90'),
            }

        if overloaded_agg and step10_agg:
            data['overloaded_agg'] = {
                'ttft_p90': overloaded_agg.get('ttft_p90'),
                'throughput_p90': overloaded_agg.get('throughput_p90'),
            }

        # Winner comparison at calibrated QPS — pick the primary comparison
        # For TTFT goal: PD vs Agg. For throughput goal: EP vs Agg. For balanced: all.
        primary = step10_pd or step10_ep
        primary_key = 'pd' if step10_pd else 'ep'

        if step10_agg and primary and data[primary_key]['ttft_p90'] and data['aggregated']['ttft_p90']:
            p_ttft = data[primary_key]['ttft_p90']
            agg_ttft = data['aggregated']['ttft_p90']
            p_tput = data[primary_key]['throughput_p90'] or 0
            agg_tput = data['aggregated']['throughput_p90'] or 0
            arch_label = 'PD' if primary_key == 'pd' else 'EP'
            data['ttft_winner'] = arch_label if p_ttft <= agg_ttft else 'Aggregated'
            data['ttft_diff_pct'] = round(abs(p_ttft - agg_ttft) / max(agg_ttft, 0.01) * 100, 1)
            data['throughput_winner'] = arch_label if p_tput >= agg_tput else 'Aggregated'
            data['throughput_diff_pct'] = round(abs(p_tput - agg_tput) / max(agg_tput, 0.01) * 100, 1)

        return data

    def build_all_results_table(self, results):
        """Build the all-results table data for the frontend."""
        import json
        successful = [r for r in results if r.is_successful]
        all_results = []
        for r in sorted(successful, key=lambda r: r.ttft_p90):
            # Parse manifest types available for download
            manifest_types = []
            if r.manifests_yaml:
                try:
                    manifests = json.loads(r.manifests_yaml)
                    manifest_types = list(manifests.keys())
                except (json.JSONDecodeError, TypeError):
                    pass

            test_config = None
            if r.test_config_json:
                try:
                    test_config = json.loads(r.test_config_json)
                except (json.JSONDecodeError, TypeError):
                    pass

            all_results.append({
                'config_name': r.display_label,
                'test_id': r.config_name,
                'architecture': (r.architecture or 'unknown').upper(),
                'ttft_p50': round(r.ttft_p50, 2) if r.ttft_p50 else None,
                'ttft_p90': round(r.ttft_p90, 2),
                'ttft_p95': round(r.ttft_p95, 2) if r.ttft_p95 else None,
                'ttft_p99': round(r.ttft_p99, 2) if r.ttft_p99 else None,
                'itl_p90': round(r.itl_p90, 2) if r.itl_p90 else None,
                'throughput_p50': round(r.throughput_p50, 2) if r.throughput_p50 else None,
                'throughput_mean': round(r.throughput_mean, 2) if r.throughput_mean else None,
                'throughput_p90': round(r.throughput_p90, 2),
                'throughput_p95': round(r.throughput_p95, 2) if r.throughput_p95 else None,
                'throughput_p99': round(r.throughput_p99, 2) if r.throughput_p99 else None,
                'gpus': r.total_gpus,
                'efficiency': round(r.throughput_p90 / r.total_gpus, 3),
                'prefill_pods': r.prefill_pods,
                'decode_pods': r.decode_pods,
                'tp': r.tensor_parallelism,
                'prefill_tp': r.prefill_tp or r.tensor_parallelism,
                'decode_tp': r.decode_tp or r.tensor_parallelism,
                'manifest_types': manifest_types,
                'test_config': test_config,
            })
        return all_results

    def build_full_report_data(self, run_id, loader):
        """
        Build all data needed for the /api/runs/<id>/charts endpoint.

        Returns a dict with charts, summary, all_results, and recommendation.
        """
        results = loader.get_all_test_results(run_id)
        if not results:
            return None

        # Step 2/3 are TP calibration trials, not real workload tests.
        # Step 10 re-tests best config at calibrated QPS — shown separately.
        # Exclude both from main charts/tables/pareto.
        test_results = [
            r for r in results
            if not r.config_name.startswith(('step2', 'step3', 'step9', 'step10'))
        ]

        # Step 2/3 calibration results for the TP sweep Pareto chart
        calibration_results = [
            r for r in results
            if r.config_name.startswith(('step2', 'step3')) and r.is_successful
        ]

        # Step 10 calibrated QPS results (separate section)
        step10_results = [
            r for r in results
            if r.config_name.startswith('step10') and r.is_successful
        ]

        pareto = self.calculate_pareto_frontier(test_results)
        stats = self.get_summary_statistics(test_results)
        charts = self.build_chart_data(
            test_results, pareto, stats, calibration_results
        )
        all_results = self.build_all_results_table(test_results)
        recommendation = self.build_recommendation(run_id, results, loader.conn)

        # Load calibrated concurrency and capacity info from optimal_config JSON
        calibrated_qps_value = None
        total_gpus_available = None
        gpu_sizing = None
        concurrency = recommendation.get('workload', {}).get('users') if recommendation else None
        try:
            row = loader.conn.execute(
                'SELECT optimal_config, max_gpus FROM optimization_runs WHERE id = ?', (run_id,)
            ).fetchone()
            if row:
                if row['optimal_config']:
                    import json as _json
                    opt = _json.loads(row['optimal_config'])
                    # Prefer calibrated_concurrency (concurrent users), fall back to
                    # calibrated_qps (req/s, from older runs) for backwards compat
                    calibrated_qps_value = opt.get('calibrated_concurrency') or opt.get('calibrated_qps')
                    total_gpus_available = opt.get('total_gpus_available')
                    gpu_sizing = opt.get('gpu_sizing')
                    if concurrency is None:
                        concurrency = opt.get('concurrency') or opt.get('original_qps')

                # Fallback: recalculate from TPSG and capacity
                if calibrated_qps_value is None and recommendation:
                    max_gpus = row['max_gpus']
                    if max_gpus:
                        total_gpus_available = total_gpus_available or max_gpus
                        headroom = 1.3
                        calibrated_qps_value = max(1, int(max_gpus / headroom))
        except Exception:
            pass

        # Build Step 10 calibrated QPS comparison data
        calibrated_qps_data = self._build_calibrated_qps_data(
            step10_results, recommendation,
            calibrated_qps_value=calibrated_qps_value,
            concurrency=concurrency,
            total_gpus_available=total_gpus_available,
            gpu_sizing=gpu_sizing
        )

        # Load latency search trials (Step 9) if any
        latency_search_data = None
        try:
            trials = loader.conn.execute('''
                SELECT * FROM latency_search_trials
                WHERE run_id = ?
                ORDER BY architecture, trial_number ASC
            ''', (run_id,)).fetchall()
            if trials:
                by_arch = {}
                for t in trials:
                    t = dict(t)
                    arch = t['architecture']
                    by_arch.setdefault(arch, []).append(t)

                # Look up the deployment config and manifests for each trial
                arch_configs = {}
                manifest_lookup = {}
                for arch in by_arch:
                    first_test_id = by_arch[arch][0].get('test_id', '')
                    cfg_row = loader.conn.execute('''
                        SELECT config_name, architecture, prefill_pods, decode_pods,
                               tensor_parallelism
                        FROM test_configurations
                        WHERE run_id = ? AND config_name = ?
                    ''', (run_id, first_test_id)).fetchone()
                    if cfg_row:
                        r = dict(cfg_row)
                        if r['architecture'] == 'pd':
                            arch_configs[arch] = f"{r['prefill_pods']}P + {r['decode_pods']}D ×TP{r['tensor_parallelism']}"
                        else:
                            total = r['prefill_pods'] + r['decode_pods']
                            arch_configs[arch] = f"{total}×TP{r['tensor_parallelism']}"

                # Load manifest types and throughput_mean for each step9 test
                for trial_list in by_arch.values():
                    for trial in trial_list:
                        tid = trial.get('test_id', '')
                        if tid:
                            m_row = loader.conn.execute(
                                'SELECT manifests_yaml, metrics_json FROM test_configurations WHERE run_id=? AND config_name=?',
                                (run_id, tid)
                            ).fetchone()
                            if m_row:
                                if m_row['manifests_yaml']:
                                    try:
                                        import json as _json
                                        trial['manifest_types'] = list(_json.loads(m_row['manifests_yaml']).keys())
                                    except Exception:
                                        pass
                                if m_row['metrics_json']:
                                    try:
                                        import json as _json3
                                        mj = _json3.loads(m_row['metrics_json'])
                                        trial['throughput_mean'] = round(mj.get('throughput_mean', 0), 2) if mj.get('throughput_mean') else None
                                    except Exception:
                                        pass

                all_trials = []
                for arch_trials in by_arch.values():
                    all_trials.extend(arch_trials)
                latency_search_data = {
                    'trials': all_trials,
                    'by_architecture': by_arch,
                    'arch_configs': arch_configs,
                }
        except Exception:
            pass

        run_config = None
        try:
            import json as _json
            row = loader.conn.execute(
                'SELECT config_json FROM optimization_runs WHERE id = ?', (run_id,)
            ).fetchone()
            if row and row[0]:
                run_config = _json.loads(row[0])
                run_config.pop('hf_token', None)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f"Failed to load run config: {e}")

        # Step 11: EPP tuning results (grouped by architecture)
        epp_tuning_data = None
        epp_results = [r for r in results if r.config_name.startswith('step11-epp-') and r.is_successful]
        if epp_results:
            import json as _json2
            by_arch = {}
            for r in sorted(epp_results, key=lambda x: x.ttft_p90 or float('inf')):
                full_name = r.config_name.replace('step11-epp-', '')
                # Parse arch from name: "pd-cache-heavy" → arch="pd", name="cache-heavy"
                parts = full_name.split('-', 1)
                arch = parts[0] if len(parts) > 1 else 'unknown'
                name = parts[1] if len(parts) > 1 else full_name
                manifest_types = []
                if r.manifests_yaml:
                    try:
                        manifest_types = list(_json2.loads(r.manifests_yaml).keys())
                    except (_json2.JSONDecodeError, TypeError):
                        pass
                weights = {}
                if r.test_config_json:
                    try:
                        tc = _json2.loads(r.test_config_json)
                        epp = tc.get('epp_config') or {}
                        plugins = epp.get('plugins') or {}
                        weights = {
                            'prefix_cache': plugins.get('prefix_cache', {}).get('weight', '?'),
                            'kv_cache': plugins.get('kv_cache', {}).get('weight', '?'),
                            'queue': plugins.get('queue', {}).get('weight', '?'),
                            'active_request': plugins.get('active_request', {}).get('weight', 0),
                        }
                    except (_json2.JSONDecodeError, TypeError):
                        pass
                entry = {
                    'name': name,
                    'test_id': r.config_name,
                    'config_name': r.display_label,
                    'ttft_p50': round(r.ttft_p50, 2) if r.ttft_p50 else None,
                    'ttft_p90': round(r.ttft_p90, 2) if r.ttft_p90 else None,
                    'ttft_p95': round(r.ttft_p95, 2) if r.ttft_p95 else None,
                    'ttft_p99': round(r.ttft_p99, 2) if r.ttft_p99 else None,
                    'throughput_mean': round(r.throughput_mean, 2) if r.throughput_mean else None,
                    'throughput_p50': round(r.throughput_p50, 2) if r.throughput_p50 else None,
                    'throughput_p90': round(r.throughput_p90, 2) if r.throughput_p90 else None,
                    'throughput_p95': round(r.throughput_p95, 2) if r.throughput_p95 else None,
                    'throughput_p99': round(r.throughput_p99, 2) if r.throughput_p99 else None,
                    'itl_p90': round(r.itl_p90, 2) if r.itl_p90 else None,
                    'manifest_types': manifest_types,
                    'weights': weights,
                }
                if r.test_config_json:
                    try:
                        _tc = _json2.loads(r.test_config_json)
                        entry['concurrency'] = _tc.get('num_users')
                        entry['epp_config'] = _tc.get('epp_config')
                        entry['tp'] = _tc.get('tensor_parallelism')
                        entry['prefill_tp'] = _tc.get('prefill_tp')
                        entry['decode_tp'] = _tc.get('decode_tp')
                        entry['prefill_pods'] = _tc.get('prefill_replicas')
                        entry['decode_pods'] = _tc.get('decode_replicas')
                        entry['replicas'] = _tc.get('replicas')
                        entry['gpus'] = r.total_gpus
                    except Exception:
                        pass
                by_arch.setdefault(arch, []).append(entry)

            # Get run config for SLA target info
            target_ms = None
            target_pct = None
            if run_config:
                if run_config.get('latency_constraint_enabled'):
                    target_ms = run_config.get('latency_constraint_ms')
                    target_pct = run_config.get('latency_constraint_percentile', 'p99')

            # Find baseline results (best from Step 6/7) and add as comparison row
            baselines = {}
            non_epp = [r for r in results if not r.config_name.startswith('step11-epp-')
                       and not r.config_name.startswith(('step2', 'step3', 'step9', 'step10'))
                       and r.is_successful]
            for arch_key in by_arch:
                if arch_key == 'pd':
                    candidates = [r for r in non_epp if r.architecture == 'pd']
                else:
                    candidates = [r for r in non_epp if r.architecture == 'aggregated']
                if candidates:
                    best = min(candidates, key=lambda r: r.ttft_p99 or r.ttft_p90 or float('inf'))
                    baselines[arch_key] = {
                        'config_name': best.display_label,
                        'ttft_p50': round(best.ttft_p50, 2) if best.ttft_p50 else None,
                        'ttft_p90': round(best.ttft_p90, 2) if best.ttft_p90 else None,
                        'ttft_p95': round(best.ttft_p95, 2) if best.ttft_p95 else None,
                        'ttft_p99': round(best.ttft_p99, 2) if best.ttft_p99 else None,
                        'throughput_p90': round(best.throughput_p90, 2) if best.throughput_p90 else None,
                        'throughput_p95': round(best.throughput_p95, 2) if best.throughput_p95 else None,
                        'throughput_p99': round(best.throughput_p99, 2) if best.throughput_p99 else None,
                    }
                    # Insert baseline as first row in EPP table for comparison
                    baseline_entry = {
                        'name': f'baseline (default)',
                        'test_id': best.config_name,
                        'config_name': best.display_label,
                        'ttft_p50': round(best.ttft_p50, 2) if best.ttft_p50 else None,
                        'ttft_p90': round(best.ttft_p90, 2) if best.ttft_p90 else None,
                        'ttft_p95': round(best.ttft_p95, 2) if best.ttft_p95 else None,
                        'ttft_p99': round(best.ttft_p99, 2) if best.ttft_p99 else None,
                        'throughput_mean': round(best.throughput_mean, 2) if best.throughput_mean else None,
                        'throughput_p50': round(best.throughput_p50, 2) if best.throughput_p50 else None,
                        'throughput_p90': round(best.throughput_p90, 2) if best.throughput_p90 else None,
                        'throughput_p95': round(best.throughput_p95, 2) if best.throughput_p95 else None,
                        'throughput_p99': round(best.throughput_p99, 2) if best.throughput_p99 else None,
                        'itl_p90': round(best.itl_p90, 2) if best.itl_p90 else None,
                        'weights': {'prefix_cache': 3, 'kv_cache': 2, 'queue': 2, 'active_request': 2},
                        'manifest_types': [],
                        'is_baseline': True,
                    }
                    by_arch[arch_key].insert(0, baseline_entry)

            epp_tuning_data = {
                'by_architecture': by_arch,
                'baselines': baselines,
                'target_ms': target_ms,
                'target_percentile': target_pct,
            }

        return {
            'charts': charts,
            'summary': stats,
            'all_results': all_results,
            'recommendation': recommendation,
            'calibrated_qps': calibrated_qps_data,
            'latency_search': latency_search_data,
            'gpu_sizing': gpu_sizing,
            'run_config': run_config,
            'epp_tuning': epp_tuning_data,
        }
