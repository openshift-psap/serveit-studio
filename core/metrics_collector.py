"""
ServeIt Studio Metrics Collector
Collects metrics from Prometheus/Thanos for LLM inference optimization.
"""

import os
import json
import logging
from datetime import datetime
from typing import Dict, Optional, Any
from dataclasses import dataclass
import requests
from urllib.parse import quote

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


@dataclass
class MetricsConfig:
    """Configuration for metrics collection."""
    thanos_url: str
    namespace: str
    pod_name_pattern: str
    step_seconds: int = 5
    token: Optional[str] = None

    @classmethod
    def from_env(cls) -> 'MetricsConfig':
        """Create config from environment variables."""
        # Try to get token from env var, fallback to service account token file
        token = os.environ.get('THANOS_TOKEN')
        if not token:
            token_file = '/run/secrets/kubernetes.io/serviceaccount/token'
            if os.path.exists(token_file):
                try:
                    with open(token_file, 'r') as f:
                        token = f.read().strip()
                except Exception:
                    pass

        return cls(
            thanos_url=os.environ.get('THANOS_URL', ''),
            namespace=os.environ.get('TARGET_NAMESPACE', 'serveit'),
            pod_name_pattern=os.environ.get('POD_NAME_PATTERN', 'wide-ep-'),
            step_seconds=int(os.environ.get('METRIC_STEP_SECONDS', '5')),
            token=token
        )


class MetricsCollector:
    """Collects metrics from Prometheus/Thanos."""

    def __init__(self, config: MetricsConfig):
        self.config = config
        self.session = requests.Session()
        if config.token:
            self.session.headers['Authorization'] = f'Bearer {config.token}'

    def _query_prometheus(
        self,
        query: str,
        start_time: int,
        end_time: int
    ) -> Dict[str, Any]:
        """
        Execute a Prometheus query over a time range.

        Args:
            query: PromQL query string
            start_time: Start timestamp (unix seconds)
            end_time: End timestamp (unix seconds)

        Returns:
            Query response as dict
        """
        encoded_query = quote(query)
        url = (
            f"{self.config.thanos_url}/api/v1/query_range?"
            f"query={encoded_query}&"
            f"start={start_time}&"
            f"end={end_time}&"
            f"step={self.config.step_seconds}"
        )

        try:
            logger.info(f"Querying: {query[:100]}...")
            response = self.session.get(url, verify=False, timeout=30)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            logger.error(f"Query failed for '{query[:50]}...': {e}")
            return {}

    def _get_rate_window(self, start_time: int, end_time: int) -> str:
        """
        Calculate rate window as 1/8 of benchmark duration.

        Args:
            start_time: Start timestamp (unix seconds)
            end_time: End timestamp (unix seconds)

        Returns:
            Rate window string (e.g., "120s")
        """
        duration = end_time - start_time
        # Minimum 120s: PodMonitor scrape interval is 30s, and rate() needs
        # at least 2 data points (60s min), with 4x buffer for reliability
        rate_window = max(duration // 8, 120)
        logger.info(f"Benchmark duration: {duration}s, using rate window: {rate_window}s")
        return f"{rate_window}s"

    def collect_gpu_metrics(
        self,
        start_time: int,
        end_time: int
    ) -> Dict[str, Any]:
        """Collect DCGM GPU metrics."""
        logger.info("--- Collecting DCGM GPU metrics ---")

        metrics = {}
        gpu_metric_names = [
            'DCGM_FI_DEV_GPU_UTIL',
            'DCGM_FI_DEV_MEM_COPY_UTIL',
            'DCGM_FI_DEV_FB_USED',
            'DCGM_FI_DEV_POWER_USAGE'
        ]

        for metric_name in gpu_metric_names:
            query = (
                f'{metric_name}{{'
                f'exported_namespace="{self.config.namespace}", '
                f'exported_pod=~"{self.config.pod_name_pattern}.*"'
                f'}}'
            )
            result = self._query_prometheus(query, start_time, end_time)
            metrics[query] = result

        return metrics

    def collect_pod_metrics(
        self,
        start_time: int,
        end_time: int,
        rate_window: str
    ) -> Dict[str, Any]:
        """Collect pod-level metrics (CPU, memory, network)."""
        logger.info("--- Collecting Pod CPU, Memory, Network metrics ---")

        metrics = {}
        pod_queries = [
            # CPU
            f'irate(container_cpu_usage_seconds_total{{pod=~"{self.config.pod_name_pattern}.*", namespace="{self.config.namespace}"}}[{rate_window}])',

            # Memory
            f'container_memory_working_set_bytes{{pod=~"{self.config.pod_name_pattern}.*", namespace="{self.config.namespace}"}}',

            # Network — eth0 only (management traffic, excludes RDMA/DRA interfaces)
            f'sum by (pod) (irate(container_network_transmit_bytes_total{{pod=~"{self.config.pod_name_pattern}.*", namespace="{self.config.namespace}", interface="eth0"}}[{rate_window}]))',
            f'sum by (pod) (irate(container_network_receive_bytes_total{{pod=~"{self.config.pod_name_pattern}.*", namespace="{self.config.namespace}", interface="eth0"}}[{rate_window}]))',
            f'sum by (pod) (irate(container_network_receive_bytes_total{{pod=~"{self.config.pod_name_pattern}.*", namespace="{self.config.namespace}", interface="eth0"}}[{rate_window}]) + irate(container_network_transmit_bytes_total{{pod=~"{self.config.pod_name_pattern}.*", namespace="{self.config.namespace}", interface="eth0"}}[{rate_window}]))',

            # RDMA/NIXL network — non-eth0 interfaces (DRA-injected enp* NICs)
            f'sum by (pod) (irate(container_network_transmit_bytes_total{{pod=~"{self.config.pod_name_pattern}.*", namespace="{self.config.namespace}", interface!="eth0"}}[{rate_window}]))',
            f'sum by (pod) (irate(container_network_receive_bytes_total{{pod=~"{self.config.pod_name_pattern}.*", namespace="{self.config.namespace}", interface!="eth0"}}[{rate_window}]))',
        ]

        for query in pod_queries:
            result = self._query_prometheus(query, start_time, end_time)
            metrics[query] = result

        return metrics

    def collect_infiniband_metrics(
        self,
        start_time: int,
        end_time: int,
        rate_window: str
    ) -> Dict[str, Any]:
        """Collect InfiniBand network metrics."""
        logger.info("--- Collecting InfiniBand metrics ---")

        metrics = {}
        ib_queries = [
            f'sum by (instance) (rate(node_infiniband_port_data_transmitted_bytes_total[{rate_window}]))',
            f'sum by (instance) (rate(node_infiniband_port_data_received_bytes_total[{rate_window}]))',
        ]

        for query in ib_queries:
            result = self._query_prometheus(query, start_time, end_time)
            metrics[query] = result

        return metrics

    def collect_vllm_metrics(
        self,
        start_time: int,
        end_time: int,
        rate_window: str
    ) -> Dict[str, Any]:
        """Collect vLLM-specific metrics."""
        logger.info("--- Collecting vLLM metrics ---")

        metrics = {}

        # Time to First Token (TTFT)
        ttft_queries = [
            f'rate(vllm:time_to_first_token_seconds_sum{{namespace="{self.config.namespace}"}}[{rate_window}])',
            f'rate(vllm:time_to_first_token_seconds_count{{namespace="{self.config.namespace}"}}[{rate_window}])',
            f'histogram_quantile(0.99, sum by(le) (rate(vllm:time_to_first_token_seconds_bucket{{namespace="{self.config.namespace}"}}[{rate_window}])))',
            f'histogram_quantile(0.95, sum by(le) (rate(vllm:time_to_first_token_seconds_bucket{{namespace="{self.config.namespace}"}}[{rate_window}])))',
            f'histogram_quantile(0.90, sum by(le) (rate(vllm:time_to_first_token_seconds_bucket{{namespace="{self.config.namespace}"}}[{rate_window}])))',
            f'histogram_quantile(0.50, sum by(le) (rate(vllm:time_to_first_token_seconds_bucket{{namespace="{self.config.namespace}"}}[{rate_window}])))',
        ]

        # Inter Token Latency (ITL)
        itl_queries = [
            f'rate(vllm:inter_token_latency_seconds_sum{{namespace="{self.config.namespace}"}}[{rate_window}])',
            f'rate(vllm:inter_token_latency_seconds_count{{namespace="{self.config.namespace}"}}[{rate_window}])',
            f'histogram_quantile(0.99, sum by(le) (rate(vllm:inter_token_latency_seconds_bucket{{namespace="{self.config.namespace}"}}[{rate_window}])))',
            f'histogram_quantile(0.95, sum by(le) (rate(vllm:inter_token_latency_seconds_bucket{{namespace="{self.config.namespace}"}}[{rate_window}])))',
            f'histogram_quantile(0.90, sum by(le) (rate(vllm:inter_token_latency_seconds_bucket{{namespace="{self.config.namespace}"}}[{rate_window}])))',
            f'histogram_quantile(0.50, sum by(le) (rate(vllm:inter_token_latency_seconds_bucket{{namespace="{self.config.namespace}"}}[{rate_window}])))',
        ]

        # E2E Request Latency
        e2e_queries = [
            f'rate(vllm:e2e_request_latency_seconds_sum{{namespace="{self.config.namespace}"}}[{rate_window}])',
            f'rate(vllm:e2e_request_latency_seconds_count{{namespace="{self.config.namespace}"}}[{rate_window}])',
            f'histogram_quantile(0.99, sum by(le) (rate(vllm:e2e_request_latency_seconds_bucket{{namespace="{self.config.namespace}"}}[{rate_window}])))',
            f'histogram_quantile(0.95, sum by(le) (rate(vllm:e2e_request_latency_seconds_bucket{{namespace="{self.config.namespace}"}}[{rate_window}])))',
            f'histogram_quantile(0.90, sum by(le) (rate(vllm:e2e_request_latency_seconds_bucket{{namespace="{self.config.namespace}"}}[{rate_window}])))',
            f'histogram_quantile(0.50, sum by(le) (rate(vllm:e2e_request_latency_seconds_bucket{{namespace="{self.config.namespace}"}}[{rate_window}])))',
        ]

        # Prefill and Decode Time
        timing_queries = [
            f'rate(vllm:request_prefill_time_seconds_sum{{namespace="{self.config.namespace}"}}[{rate_window}])',
            f'rate(vllm:request_decode_time_seconds_sum{{namespace="{self.config.namespace}"}}[{rate_window}])',
            f'rate(vllm:request_queue_time_seconds_sum{{namespace="{self.config.namespace}"}}[{rate_window}])',
            f'rate(vllm:request_queue_time_seconds_count{{namespace="{self.config.namespace}"}}[{rate_window}])',
        ]

        # Scheduler State
        scheduler_queries = [
            f'vllm:num_requests_running{{namespace="{self.config.namespace}"}}',
            f'vllm:num_requests_waiting{{namespace="{self.config.namespace}"}}',
            f'vllm:kv_cache_usage_perc{{namespace="{self.config.namespace}"}}',
        ]

        # Token Throughput
        throughput_queries = [
            f'rate(vllm:prompt_tokens_total{{namespace="{self.config.namespace}"}}[{rate_window}])',
            f'rate(vllm:generation_tokens_total{{namespace="{self.config.namespace}"}}[{rate_window}])',
        ]

        # Request Success
        success_queries = [
            f'sum by(finished_reason) (increase(vllm:request_success_total{{namespace="{self.config.namespace}"}}[{rate_window}]))',
            f'sum by(le) (increase(vllm:request_prompt_tokens_bucket{{namespace="{self.config.namespace}"}}[{rate_window}]))',
            f'sum by(le) (increase(vllm:request_generation_tokens_bucket{{namespace="{self.config.namespace}"}}[{rate_window}]))',
        ]

        # Additional Metrics
        extra_queries = [
            f'rate(vllm:request_max_num_generation_tokens_sum{{namespace="{self.config.namespace}"}}[{rate_window}])',
            f'rate(vllm:prefix_cache_hits_total{{namespace="{self.config.namespace}"}}[{rate_window}])',
            f'rate(vllm:prefix_cache_queries_total{{namespace="{self.config.namespace}"}}[{rate_window}])',
            f'rate(vllm:num_preemptions_total{{namespace="{self.config.namespace}"}}[{rate_window}])',
        ]

        # Collect all vLLM metrics
        all_queries = (
            ttft_queries + itl_queries + e2e_queries +
            timing_queries + scheduler_queries + throughput_queries +
            success_queries + extra_queries
        )

        for query in all_queries:
            result = self._query_prometheus(query, start_time, end_time)
            metrics[query] = result

        return metrics

    def collect_inference_metrics(
        self,
        start_time: int,
        end_time: int,
        rate_window: str
    ) -> Dict[str, Any]:
        """Collect inference gateway/GAIE metrics."""
        logger.info("--- Collecting Inference Gateway metrics ---")

        metrics = {}
        inference_queries = [
            f'sum(rate(inference_objective_request_duration_seconds_bucket{{namespace="{self.config.namespace}"}}[{rate_window}])) by (le)',
            f'sum(rate(inference_extension_scheduler_e2e_duration_seconds_bucket{{namespace="{self.config.namespace}"}}[{rate_window}])) by (le)',
            f'sum(rate(inference_objective_request_duration_seconds_count{{namespace="{self.config.namespace}"}}[{rate_window}]))',
            f'sum(rate(inference_objective_request_duration_seconds_sum{{namespace="{self.config.namespace}"}}[{rate_window}]))',
            f'sum(rate(inference_extension_scheduler_e2e_duration_seconds_count{{namespace="{self.config.namespace}"}}[{rate_window}]))',
            f'sum(rate(inference_extension_scheduler_e2e_duration_seconds_sum{{namespace="{self.config.namespace}"}}[{rate_window}]))',
            f'sum by (pod) (irate(inference_objective_output_tokens_sum{{namespace="{self.config.namespace}"}}[{rate_window}]))',
            f'sum by (pod) (irate(inference_objective_request_total{{namespace="{self.config.namespace}"}}[{rate_window}]))',
            f'sum by (pod) (irate(inference_objective_request_error_total{{namespace="{self.config.namespace}"}}[{rate_window}]))',
            f'inference_objective_running_requests{{namespace="{self.config.namespace}"}}',
            f'inference_pool_average_kv_cache_utilization{{namespace="{self.config.namespace}"}}',
            f'inference_pool_average_queue_size{{namespace="{self.config.namespace}"}}',
            f'sum by (pod) (inference_pool_per_pod_queue_size{{namespace="{self.config.namespace}"}})',
            f'inference_pool_ready_pods{{namespace="{self.config.namespace}"}}',
        ]

        for query in inference_queries:
            result = self._query_prometheus(query, start_time, end_time)
            metrics[query] = result

        return metrics

    def collect_all_metrics(
        self,
        start_time: datetime,
        end_time: datetime,
        output_file: str
    ) -> Dict[str, Any]:
        """
        Collect all metrics and save to file.

        Args:
            start_time: Benchmark start time
            end_time: Benchmark end time
            output_file: Path to output JSON file

        Returns:
            Complete metrics data
        """
        start_unix = int(start_time.timestamp())
        end_unix = int(end_time.timestamp())
        rate_window = self._get_rate_window(start_unix, end_unix)

        logger.info(f"Collecting metrics from {start_time} to {end_time}")

        # Collect all metric categories
        all_metrics = {
            'benchmark_start': start_time.isoformat(),
            'benchmark_end': end_time.isoformat(),
            'metrics': {}
        }

        # GPU metrics
        all_metrics['metrics'].update(
            self.collect_gpu_metrics(start_unix, end_unix)
        )

        # Pod metrics
        all_metrics['metrics'].update(
            self.collect_pod_metrics(start_unix, end_unix, rate_window)
        )

        # InfiniBand metrics
        all_metrics['metrics'].update(
            self.collect_infiniband_metrics(start_unix, end_unix, rate_window)
        )

        # vLLM metrics
        all_metrics['metrics'].update(
            self.collect_vllm_metrics(start_unix, end_unix, rate_window)
        )

        # Inference gateway metrics
        all_metrics['metrics'].update(
            self.collect_inference_metrics(start_unix, end_unix, rate_window)
        )

        # Save to file
        with open(output_file, 'w') as f:
            json.dump(all_metrics, f, indent=2)

        logger.info(f"--- Metrics collection complete. Data saved to {output_file} ---")
        return all_metrics


def main():
    """Main entry point for standalone execution."""
    import argparse
    from dateutil import parser as date_parser

    arg_parser = argparse.ArgumentParser(
        description='Collect Prometheus/Thanos metrics for LLM inference benchmarking'
    )
    arg_parser.add_argument('--start-time', required=True, help='Start time (ISO format or parseable string)')
    arg_parser.add_argument('--end-time', required=True, help='End time (ISO format or parseable string)')
    arg_parser.add_argument('--output', required=True, help='Output JSON file path')
    arg_parser.add_argument('--thanos-url', help='Thanos URL (overrides env var)')
    arg_parser.add_argument('--namespace', help='Target namespace (overrides env var)')
    arg_parser.add_argument('--pod-pattern', help='Pod name pattern (overrides env var)')

    args = arg_parser.parse_args()

    # Parse times
    start_time = date_parser.parse(args.start_time)
    end_time = date_parser.parse(args.end_time)

    # Create config
    config = MetricsConfig.from_env()
    if args.thanos_url:
        config.thanos_url = args.thanos_url
    if args.namespace:
        config.namespace = args.namespace
    if args.pod_pattern:
        config.pod_name_pattern = args.pod_pattern

    # Collect metrics
    collector = MetricsCollector(config)
    collector.collect_all_metrics(start_time, end_time, args.output)


if __name__ == '__main__':
    main()
