"""
Metrics Analyzer - Post-processes collected metrics to fill gaps and calculate derived metrics.

Handles:
1. Missing vLLM metrics (calculates from available data)
2. Per-GPU, per-pod throughput breakdowns
3. Extracts summaries from time-series data
"""

import json
import logging
from typing import Dict, Any, Optional, List
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class PodMetrics:
    """Per-pod metrics breakdown."""
    pod_name: str
    gpu_count: int
    avg_gpu_utilization: float  # Average across all GPUs
    peak_gpu_memory_mb: float   # Peak across all GPUs
    avg_throughput_tokens_per_sec: float  # Total throughput
    avg_throughput_per_gpu: float  # Throughput / GPU count
    avg_power_watts: float  # Average power consumption


@dataclass
class AnalyzedMetrics:
    """Analyzed metrics with derived calculations."""
    # Overall summaries
    avg_gpu_utilization: Optional[float] = None
    peak_gpu_utilization: Optional[float] = None
    avg_gpu_memory_used_gb: Optional[float] = None
    peak_gpu_memory_used_gb: Optional[float] = None
    avg_power_watts: Optional[float] = None
    peak_power_watts: Optional[float] = None

    # KV cache (fallback from vLLM if available)
    kv_cache_usage_pct: Optional[float] = None

    # Per-pod breakdowns
    pods: List[PodMetrics] = None

    # Throughput per GPU (overall average)
    avg_throughput_per_gpu: Optional[float] = None

    # Metadata
    missing_vllm_metrics: List[str] = None
    data_sources: List[str] = None


class MetricsAnalyzer:
    """
    Analyzes collected metrics to:
    1. Calculate derived metrics when vLLM metrics are missing
    2. Extract per-GPU, per-pod breakdowns
    3. Summarize time-series data
    """

    def __init__(self):
        self.missing_metrics = []
        self.data_sources = []

    def analyze_metrics_file(
        self,
        metrics_json_path: Path,
        guidellm_result: Optional[Dict[str, Any]] = None,
        tensor_parallelism: int = 1
    ) -> AnalyzedMetrics:
        """
        Analyze a metrics.json file and calculate derived metrics.

        Args:
            metrics_json_path: Path to metrics.json file
            guidellm_result: Optional guidellm test result for fallback calculations
            tensor_parallelism: Number of GPUs per pod

        Returns:
            AnalyzedMetrics with calculated summaries
        """
        if not metrics_json_path.exists():
            logger.warning(f"Metrics file not found: {metrics_json_path}")
            return AnalyzedMetrics(
                missing_vllm_metrics=[],
                data_sources=[]
            )

        with open(metrics_json_path) as f:
            data = json.load(f)

        metrics = data.get('metrics', {})

        # Extract GPU metrics (DCGM - these work!)
        gpu_util = self._extract_gpu_utilization(metrics)
        gpu_memory = self._extract_gpu_memory(metrics)
        gpu_power = self._extract_gpu_power(metrics)

        # Try to get KV cache from vLLM, fallback if missing
        kv_cache = self._extract_kv_cache(metrics)

        # Calculate per-pod breakdowns
        pod_metrics = self._calculate_pod_metrics(
            metrics,
            guidellm_result,
            tensor_parallelism
        )

        # Calculate overall throughput per GPU
        avg_tput_per_gpu = None
        if pod_metrics:
            avg_tput_per_gpu = sum(p.avg_throughput_per_gpu for p in pod_metrics) / len(pod_metrics)

        return AnalyzedMetrics(
            avg_gpu_utilization=gpu_util.get('avg'),
            peak_gpu_utilization=gpu_util.get('peak'),
            avg_gpu_memory_used_gb=gpu_memory.get('avg_gb'),
            peak_gpu_memory_used_gb=gpu_memory.get('peak_gb'),
            avg_power_watts=gpu_power.get('avg'),
            peak_power_watts=gpu_power.get('peak'),
            kv_cache_usage_pct=kv_cache,
            pods=pod_metrics,
            avg_throughput_per_gpu=avg_tput_per_gpu,
            missing_vllm_metrics=self.missing_metrics,
            data_sources=self.data_sources
        )

    def _extract_gpu_utilization(self, metrics: Dict) -> Dict[str, float]:
        """Extract GPU utilization from DCGM metrics."""
        query_key = [k for k in metrics.keys() if 'DCGM_FI_DEV_GPU_UTIL' in k]
        if not query_key:
            return {'avg': None, 'peak': None}

        result = metrics[query_key[0]].get('data', {}).get('result', [])
        if not result:
            return {'avg': None, 'peak': None}

        self.data_sources.append('DCGM GPU Utilization')

        # Extract all values across all GPUs
        all_values = []
        for gpu_data in result:
            values = gpu_data.get('values', [])
            for timestamp, value in values:
                try:
                    all_values.append(float(value))
                except (ValueError, TypeError):
                    continue

        if all_values:
            return {
                'avg': sum(all_values) / len(all_values),
                'peak': max(all_values)
            }

        return {'avg': None, 'peak': None}

    def _extract_gpu_memory(self, metrics: Dict) -> Dict[str, float]:
        """Extract GPU memory usage from DCGM metrics."""
        query_key = [k for k in metrics.keys() if 'DCGM_FI_DEV_FB_USED' in k]
        if not query_key:
            return {'avg_gb': None, 'peak_gb': None}

        result = metrics[query_key[0]].get('data', {}).get('result', [])
        if not result:
            return {'avg_gb': None, 'peak_gb': None}

        self.data_sources.append('DCGM GPU Memory')

        # Extract all values (in MB)
        all_values = []
        for gpu_data in result:
            values = gpu_data.get('values', [])
            for timestamp, value in values:
                try:
                    all_values.append(float(value))
                except (ValueError, TypeError):
                    continue

        if all_values:
            # Convert MB to GB
            return {
                'avg_gb': (sum(all_values) / len(all_values)) / 1024,
                'peak_gb': max(all_values) / 1024
            }

        return {'avg_gb': None, 'peak_gb': None}

    def _extract_gpu_power(self, metrics: Dict) -> Dict[str, float]:
        """Extract GPU power usage from DCGM metrics."""
        query_key = [k for k in metrics.keys() if 'DCGM_FI_DEV_POWER_USAGE' in k]
        if not query_key:
            return {'avg': None, 'peak': None}

        result = metrics[query_key[0]].get('data', {}).get('result', [])
        if not result:
            return {'avg': None, 'peak': None}

        self.data_sources.append('DCGM GPU Power')

        # Extract all values
        all_values = []
        for gpu_data in result:
            values = gpu_data.get('values', [])
            for timestamp, value in values:
                try:
                    all_values.append(float(value))
                except (ValueError, TypeError):
                    continue

        if all_values:
            return {
                'avg': sum(all_values) / len(all_values),
                'peak': max(all_values)
            }

        return {'avg': None, 'peak': None}

    def _extract_kv_cache(self, metrics: Dict) -> Optional[float]:
        """
        Extract KV cache usage. Try vLLM first, fallback to inference pool.
        """
        # Try vLLM metric first
        vllm_key = [k for k in metrics.keys() if 'vllm:kv_cache_usage_perc' in k]
        if vllm_key:
            result = metrics[vllm_key[0]].get('data', {}).get('result', [])
            if result:
                self.data_sources.append('vLLM KV Cache')
                # Get latest value
                values = result[0].get('values', [])
                if values:
                    return float(values[-1][1])
        else:
            self.missing_metrics.append('vllm:kv_cache_usage_perc')

        # Try inference pool metric
        inf_key = [k for k in metrics.keys() if 'inference_pool_average_kv_cache_utilization' in k]
        if inf_key:
            result = metrics[inf_key[0]].get('data', {}).get('result', [])
            if result:
                self.data_sources.append('Inference Pool KV Cache')
                values = result[0].get('values', [])
                if values:
                    return float(values[-1][1])
        else:
            self.missing_metrics.append('inference_pool_average_kv_cache_utilization')

        return None

    def _calculate_pod_metrics(
        self,
        metrics: Dict,
        guidellm_result: Optional[Dict],
        tensor_parallelism: int
    ) -> List[PodMetrics]:
        """
        Calculate per-pod, per-GPU metrics breakdown.

        Args:
            metrics: Thanos metrics
            guidellm_result: guidellm results (for throughput)
            tensor_parallelism: GPUs per pod

        Returns:
            List of PodMetrics, one per pod
        """
        pod_metrics_list = []

        # Get GPU utilization per pod
        gpu_util_key = [k for k in metrics.keys() if 'DCGM_FI_DEV_GPU_UTIL' in k]
        if not gpu_util_key:
            return pod_metrics_list

        result = metrics[gpu_util_key[0]].get('data', {}).get('result', [])

        # Group by pod
        pods = {}
        for gpu_data in result:
            pod_name = gpu_data.get('metric', {}).get('exported_pod', 'unknown')
            if pod_name not in pods:
                pods[pod_name] = {
                    'gpu_util_values': [],
                    'gpu_count': 0
                }

            pods[pod_name]['gpu_count'] += 1

            # Collect GPU utilization values
            values = gpu_data.get('values', [])
            for timestamp, value in values:
                try:
                    pods[pod_name]['gpu_util_values'].append(float(value))
                except (ValueError, TypeError):
                    continue

        # Calculate throughput from guidellm (total for all pods)
        total_throughput_tokens_sec = 0
        if guidellm_result:
            # Get throughput from guidellm (requests/sec * tokens/request)
            throughput_rps = guidellm_result.get('throughput_p90', 0)
            output_tokens = guidellm_result.get('output_tokens', 1000)  # From test config
            total_throughput_tokens_sec = throughput_rps * output_tokens
            self.data_sources.append('guidellm throughput')

        # Calculate per-pod metrics
        num_pods = len(pods)
        for pod_name, pod_data in pods.items():
            gpu_count = pod_data['gpu_count']
            avg_util = sum(pod_data['gpu_util_values']) / len(pod_data['gpu_util_values']) if pod_data['gpu_util_values'] else 0

            # Assume throughput distributed equally across pods
            pod_throughput = total_throughput_tokens_sec / num_pods if num_pods > 0 else 0
            per_gpu_throughput = pod_throughput / gpu_count if gpu_count > 0 else 0

            pod_metrics_list.append(PodMetrics(
                pod_name=pod_name,
                gpu_count=gpu_count,
                avg_gpu_utilization=avg_util,
                peak_gpu_memory_mb=0,  # TODO: Extract from memory metrics
                avg_throughput_tokens_per_sec=pod_throughput,
                avg_throughput_per_gpu=per_gpu_throughput,
                avg_power_watts=0  # TODO: Extract from power metrics
            ))

        return pod_metrics_list

    @staticmethod
    def _extract_lora_adapter_info(query_result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Extract adapter residency/affinity from vllm:lora_requests_info.

        The gauge value is the timestamp at which that label-set state was
        last refreshed; the signal is in the labels. Each distinct
        running_lora_adapters label-set is a separate time series, so the
        number of distinct sets per pod = adapter churn (LRU evictions /
        swap-ins). vLLM never removes old series (see the exposition leak),
        so only samples whose gauge value is close to the scrape timestamp
        reflect CURRENT in-flight state — older frozen series are skipped.

        Returns per-pod residency plus request-aware affinity stats:
        consecutive fresh samples of an adapter on one pod collapse into
        one episode (~ one request), so adapters are only counted as
        single/multi-pod when they have enough episodes for the answer to
        be meaningful, and top-pod share measures stickiness independently
        of how many requests each adapter happened to receive.
        """
        result = query_result.get('data', {}).get('result', [])
        if not result:
            return None

        pods = {}
        affinity = {}
        # (adapter, pod) -> sorted fresh sample timestamps
        samples = {}
        for series in result:
            m = series.get('metric', {})
            pod = m.get('pod', 'unknown')
            running = [a for a in m.get('running_lora_adapters', '').split(',') if a]
            waiting = [a for a in m.get('waiting_lora_adapters', '').split(',') if a]
            p = pods.setdefault(pod, {
                'max_lora': None,
                'running_sets': set(),
                'last_ts': -1.0,
                'last_running': [],
                'last_waiting': [],
            })
            try:
                p['max_lora'] = int(m['max_lora'])
            except (KeyError, ValueError, TypeError):
                pass
            p['running_sets'].add(tuple(sorted(running)))
            for adapter in running:
                affinity.setdefault(adapter, set()).add(pod)
            values = series.get('values', [])
            if values:
                last_ts = float(values[-1][0])
                if last_ts >= p['last_ts']:
                    p['last_ts'] = last_ts
                    p['last_running'] = running
                    p['last_waiting'] = waiting
            for ts, v in values:
                ts_f = float(ts)
                # Fresh state only: gauge value tracks the scrape time while
                # the state persists; frozen series keep an old value.
                if abs(float(v) - ts_f) <= 10.0:
                    for adapter in running:
                        samples.setdefault((adapter, pod), []).append(ts_f)

        out_pods = {}
        for pod, p in pods.items():
            distinct = set()
            for s in p['running_sets']:
                distinct.update(s)
            out_pods[pod] = {
                'max_lora': p['max_lora'],
                'running_adapters_distinct': sorted(distinct),
                'running_adapters_last': sorted(p['last_running']),
                'waiting_adapters_last': sorted(p['last_waiting']),
                # Distinct residency sets seen over the window: churn from
                # LRU evictions or misrouted requests forcing swap-ins.
                'residency_set_changes': len(p['running_sets']),
            }

        # Collapse consecutive same-pod samples (gap <= 25s, ~ one request
        # spans several scrapes) into episodes, per adapter per pod.
        episode_gap = 25.0
        adapter_episodes = {}
        for (adapter, pod), ts_list in samples.items():
            ts_list.sort()
            pod_eps = adapter_episodes.setdefault(adapter, {})
            prev = None
            for ts in ts_list:
                if prev is None or ts - prev > episode_gap:
                    pod_eps[pod] = pod_eps.get(pod, 0) + 1
                prev = ts

        stats = {'adapters_total': len(adapter_episodes), 'adapters_measured': 0,
                 'single_pod': 0, 'multi_pod': 0, 'top_pod_share_mean': None}
        shares = []
        for adapter, pod_eps in adapter_episodes.items():
            total_eps = sum(pod_eps.values())
            if total_eps < 2:
                continue
            stats['adapters_measured'] += 1
            if len(pod_eps) == 1:
                stats['single_pod'] += 1
            else:
                stats['multi_pod'] += 1
            shares.append(max(pod_eps.values()) / total_eps)
        if shares:
            stats['top_pod_share_mean'] = round(sum(shares) / len(shares), 3)

        return {
            'pods': out_pods,
            'adapter_pod_affinity': {a: sorted(ps) for a, ps in sorted(affinity.items())},
            'adapter_episode_counts': {a: {p: n for p, n in sorted(pod_eps.items())}
                                       for a, pod_eps in sorted(adapter_episodes.items())},
            'affinity_stats': stats,
        }

    @staticmethod
    def extract_prometheus_summaries(metrics_json_path: Path) -> Dict[str, Any]:
        """
        Extract summary statistics from all Prometheus metrics in a metrics JSON file.

        For each query result, extracts avg/min/max/last value across all time-series.
        Returns a flat dict keyed by a short metric name.

        Args:
            metrics_json_path: Path to metrics.json file saved by MetricsCollector

        Returns:
            Dict mapping metric short names to their summary values
        """
        if not metrics_json_path.exists():
            return {}

        with open(metrics_json_path) as f:
            data = json.load(f)

        raw_metrics = data.get('metrics', {})
        summaries = {}

        # Map query substrings to short metric names
        metric_name_map = {
            # vLLM TTFT
            'time_to_first_token_seconds_sum': 'vllm_ttft_sum_rate',
            'time_to_first_token_seconds_count': 'vllm_ttft_count_rate',
            # vLLM ITL
            'inter_token_latency_seconds_sum': 'vllm_itl_sum_rate',
            'inter_token_latency_seconds_count': 'vllm_itl_count_rate',
            # vLLM E2E
            'e2e_request_latency_seconds_sum': 'vllm_e2e_sum_rate',
            'e2e_request_latency_seconds_count': 'vllm_e2e_count_rate',
            # vLLM timing
            'request_prefill_time_seconds_sum': 'vllm_prefill_time_rate',
            'request_decode_time_seconds_sum': 'vllm_decode_time_rate',
            'request_queue_time_seconds_sum': 'vllm_queue_time_rate',
            'request_queue_time_seconds_count': 'vllm_queue_count_rate',
            # vLLM scheduler
            'num_requests_running': 'vllm_requests_running',
            'num_requests_waiting': 'vllm_requests_waiting',
            'kv_cache_usage_perc': 'vllm_kv_cache_pct',
            # vLLM token throughput
            'prompt_tokens_total': 'vllm_prompt_tokens_rate',
            'generation_tokens_total': 'vllm_generation_tokens_rate',
            # vLLM extras
            'request_success_total': 'vllm_request_success',
            'request_max_num_generation_tokens_sum': 'vllm_max_gen_tokens_rate',
            'prefix_cache_hits_total': 'vllm_prefix_cache_hits_rate',
            'prefix_cache_queries_total': 'vllm_prefix_cache_queries_rate',
            'num_preemptions_total': 'vllm_preemptions_rate',
            'request_prompt_tokens_bucket': 'vllm_prompt_tokens_dist',
            'request_generation_tokens_bucket': 'vllm_generation_tokens_dist',
            # GPU (DCGM)
            'DCGM_FI_DEV_GPU_UTIL': 'gpu_utilization',
            'DCGM_FI_DEV_MEM_COPY_UTIL': 'gpu_mem_copy_util',
            'DCGM_FI_DEV_FB_USED': 'gpu_fb_used_mb',
            'DCGM_FI_DEV_POWER_USAGE': 'gpu_power_watts',
            # Pod resources
            'container_cpu_usage_seconds_total': 'pod_cpu_rate',
            'container_memory_working_set_bytes': 'pod_memory_bytes',
            'network_transmit_bytes_total': 'pod_network_tx_rate',
            'network_receive_bytes_total': 'pod_network_rx_rate',
            # InfiniBand
            'infiniband_port_data_transmitted_bytes_total': 'ib_tx_rate',
            'infiniband_port_data_received_bytes_total': 'ib_rx_rate',
            # Inference gateway
            'inference_objective_request_duration_seconds_count': 'gw_request_count_rate',
            'inference_objective_request_duration_seconds_sum': 'gw_request_duration_rate',
            'inference_extension_scheduler_e2e_duration_seconds_count': 'gw_scheduler_count_rate',
            'inference_extension_scheduler_e2e_duration_seconds_sum': 'gw_scheduler_duration_rate',
            'inference_objective_output_tokens_sum': 'gw_output_tokens_rate',
            'inference_objective_request_total': 'gw_request_total_rate',
            'inference_objective_request_error_total': 'gw_request_error_rate',
            'inference_objective_running_requests': 'gw_running_requests',
            'inference_pool_average_kv_cache_utilization': 'gw_pool_kv_cache',
            'inference_pool_average_queue_size': 'gw_pool_queue_size',
            'inference_pool_per_pod_queue_size': 'gw_per_pod_queue_size',
            'inference_pool_ready_pods': 'gw_ready_pods',
            # Gateway histograms (bucket distributions)
            'inference_objective_request_duration_seconds_bucket': 'gw_request_duration_hist',
            'inference_extension_scheduler_e2e_duration_seconds_bucket': 'gw_scheduler_duration_hist',
            # Istio gateway request duration (ms) — request traversal through gateway
            'istio_request_duration_milliseconds_sum': 'istio_request_duration_rate',
            'istio_request_duration_milliseconds_count': 'istio_request_count_rate',
            'istio_request_duration_milliseconds_bucket': 'istio_request_duration_hist',
            # Cluster TCP connect latency (ms) — OpenShift network diagnostics
            'sum by (checkName) (pod_network_connectivity_check_tcp_connect_latency_gauge)': 'tcp_connect_latency_by_check',
            'pod_network_connectivity_check_tcp_connect_latency_gauge': 'tcp_connect_latency_gauge',
        }

        # Detect histogram quantile queries (e.g., histogram_quantile(0.99, ...bucket...))
        quantile_patterns = {
            ('time_to_first_token_seconds_bucket', '0.99'): 'vllm_ttft_p99',
            ('time_to_first_token_seconds_bucket', '0.95'): 'vllm_ttft_p95',
            ('time_to_first_token_seconds_bucket', '0.90'): 'vllm_ttft_p90',
            ('time_to_first_token_seconds_bucket', '0.50'): 'vllm_ttft_p50',
            ('inter_token_latency_seconds_bucket', '0.99'): 'vllm_itl_p99',
            ('inter_token_latency_seconds_bucket', '0.95'): 'vllm_itl_p95',
            ('inter_token_latency_seconds_bucket', '0.90'): 'vllm_itl_p90',
            ('inter_token_latency_seconds_bucket', '0.50'): 'vllm_itl_p50',
            ('e2e_request_latency_seconds_bucket', '0.99'): 'vllm_e2e_p99',
            ('e2e_request_latency_seconds_bucket', '0.95'): 'vllm_e2e_p95',
            ('e2e_request_latency_seconds_bucket', '0.90'): 'vllm_e2e_p90',
            ('e2e_request_latency_seconds_bucket', '0.50'): 'vllm_e2e_p50',
            ('istio_request_duration_milliseconds_bucket', '0.99'): 'istio_request_duration_p99',
            ('istio_request_duration_milliseconds_bucket', '0.95'): 'istio_request_duration_p95',
            ('istio_request_duration_milliseconds_bucket', '0.90'): 'istio_request_duration_p90',
            ('istio_request_duration_milliseconds_bucket', '0.50'): 'istio_request_duration_p50',
        }

        # Per-pod quantile queries (sum by(pod, le)) — must match before the
        # namespace-aggregated quantile patterns above since they contain the
        # same metric substrings. min/max across the per-pod series is the
        # hot-pod vs cold-pod latency spread (routing signal).
        per_pod_quantile_patterns = {
            ('time_to_first_token_seconds_bucket', '0.99'): 'vllm_ttft_p99_per_pod',
            ('time_to_first_token_seconds_bucket', '0.50'): 'vllm_ttft_p50_per_pod',
        }

        # RDMA network queries have interface!="eth0" — must match before generic network patterns
        rdma_patterns = {
            ('network_transmit_bytes_total', 'interface!="eth0"'): 'rdma_network_tx_rate',
            ('network_receive_bytes_total', 'interface!="eth0"'): 'rdma_network_rx_rate',
        }

        for query_str, query_result in raw_metrics.items():
            # Determine the short name for this query
            short_name = None

            # LoRA adapter info first — the gauge value is a scrape timestamp
            # (meaningless); the signal is in the labels (adapter lists).
            if 'vllm:lora_requests_info' in query_str:
                summaries['vllm_lora_adapter_info'] = MetricsAnalyzer._extract_lora_adapter_info(
                    query_result
                )
                continue

            # Check per-pod quantile patterns (before aggregated quantile
            # patterns — same metric substrings, distinguished by the
            # `by(pod, le)` grouping)
            for (bucket_substr, quantile_val), name in per_pod_quantile_patterns.items():
                if (bucket_substr in query_str
                        and f'histogram_quantile({quantile_val}' in query_str
                        and 'pod, le' in query_str):
                    short_name = name
                    break

            # Check histogram quantile patterns
            if not short_name:
                for (bucket_substr, quantile_val), name in quantile_patterns.items():
                    if bucket_substr in query_str and f'histogram_quantile({quantile_val}' in query_str:
                        short_name = name
                        break

            # Check RDMA network patterns (before generic network match)
            if not short_name:
                for (metric_substr, filter_substr), name in rdma_patterns.items():
                    if metric_substr in query_str and filter_substr in query_str:
                        short_name = name
                        break

            # Check computed imbalance queries (before generic patterns since they contain the same substrings)
            if not short_name:
                if 'max(' in query_str and 'min(' in query_str and 'num_requests_running' in query_str:
                    short_name = 'vllm_routing_imbalance'
                elif 'stddev(' in query_str and 'num_requests_running' in query_str:
                    short_name = 'vllm_routing_cv'
                elif 'stddev(' in query_str and 'num_requests_waiting' in query_str:
                    # Waiting-queue imbalance CV: hot-pod overload queues
                    # while cold pods idle (LoRA affinity routing signal)
                    short_name = 'vllm_waiting_cv'

            # Check regular metric patterns
            if not short_name:
                for substr, name in metric_name_map.items():
                    if substr in query_str:
                        short_name = name
                        break

            if not short_name:
                continue

            # Extract values from query result
            result_data = query_result.get('data', {}).get('result', [])
            if not result_data:
                summaries[short_name] = None
                continue

            # Rate metrics that should be summed across pods (not averaged)
            sum_across_pods = short_name in (
                'vllm_prompt_tokens_rate', 'vllm_generation_tokens_rate',
                'vllm_ttft_sum_rate', 'vllm_ttft_count_rate',
                'vllm_itl_sum_rate', 'vllm_itl_count_rate',
                'vllm_e2e_sum_rate', 'vllm_e2e_count_rate',
                'vllm_request_success', 'vllm_max_gen_tokens_rate',
                'vllm_prefix_cache_hits_rate', 'vllm_prefix_cache_queries_rate',
                'vllm_preemptions_rate',
                'gw_request_count_rate', 'gw_request_duration_rate',
                'gw_output_tokens_rate', 'gw_request_total_rate', 'gw_request_error_rate',
                'istio_request_count_rate', 'istio_request_duration_rate',
            )

            if sum_across_pods and len(result_data) > 1:
                # Sum values across pods at each timestamp, then summarize
                from collections import defaultdict
                ts_sums = defaultdict(float)
                for series in result_data:
                    for timestamp, value in series.get('values', []):
                        try:
                            v = float(value)
                            if v != float('inf') and v != float('-inf') and v == v:
                                ts_sums[timestamp] += v
                        except (ValueError, TypeError):
                            continue
                all_values = list(ts_sums.values()) if ts_sums else []
            else:
                # Single series or per-pod metrics: collect all values
                all_values = []
                for series in result_data:
                    for _timestamp, value in series.get('values', []):
                        try:
                            v = float(value)
                            if v != float('inf') and v != float('-inf') and v == v:
                                all_values.append(v)
                        except (ValueError, TypeError):
                            continue

            if not all_values:
                summaries[short_name] = None
                continue

            summaries[short_name] = {
                'avg': round(sum(all_values) / len(all_values), 4),
                'min': round(min(all_values), 4),
                'max': round(max(all_values), 4),
                'last': round(all_values[-1], 4),
                'samples': len(all_values),
            }

        return summaries
