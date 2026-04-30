"""
In-S8 Test Orchestrator

Orchestrates the complete test flow: deploy → run guidellm → collect metrics → cleanup.
Coordinates between deployment_manager, metrics_collector, and database.
"""

import os
import time
import json
import logging
import subprocess
from typing import List, Optional, Callable
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path

from .config_generator import TestConfig, OptimizationPlan
from .deployment_manager import DeploymentManager
from .metrics_collector import MetricsCollector, MetricsConfig

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


@dataclass
class TestResult:
    """Result of a single test run."""
    test_id: str
    architecture: str
    deployment_success: bool
    deployment_ready: bool
    guidellm_success: bool
    metrics_collected: bool

    # Timing
    deployment_start_time: str
    deployment_ready_time: Optional[str] = None
    test_start_time: Optional[str] = None
    test_end_time: Optional[str] = None
    cleanup_time: Optional[str] = None

    # Results
    service_endpoint: Optional[str] = None
    guidellm_output: Optional[str] = None
    metrics_file: Optional[str] = None
    error_message: Optional[str] = None

    # Parsed metrics from guidellm results (percentiles)
    ttft_p50: Optional[float] = None
    ttft_p90: Optional[float] = None
    ttft_p95: Optional[float] = None
    ttft_p99: Optional[float] = None
    itl_p50: Optional[float] = None
    itl_p90: Optional[float] = None
    itl_p95: Optional[float] = None
    itl_p99: Optional[float] = None
    throughput_p50: Optional[float] = None
    throughput_p90: Optional[float] = None
    throughput_p95: Optional[float] = None
    throughput_p99: Optional[float] = None

    # Extended guidellm metrics
    guidellm_raw_json: Optional[str] = None

    # Request counts
    request_total: Optional[int] = None
    request_successful: Optional[int] = None
    request_incomplete: Optional[int] = None
    request_errored: Optional[int] = None

    # Benchmark timing
    benchmark_duration_s: Optional[float] = None
    warmup_duration_s: Optional[float] = None

    # TPOT (Time Per Output Token) — avg time per decoded token
    tpot_mean: Optional[float] = None
    tpot_p50: Optional[float] = None
    tpot_p90: Optional[float] = None
    tpot_p95: Optional[float] = None
    tpot_p99: Optional[float] = None

    # E2E request latency (seconds)
    e2e_latency_mean: Optional[float] = None
    e2e_latency_p50: Optional[float] = None
    e2e_latency_p90: Optional[float] = None
    e2e_latency_p95: Optional[float] = None
    e2e_latency_p99: Optional[float] = None

    # Token throughput (tokens/sec)
    output_tps_mean: Optional[float] = None
    output_tps_p50: Optional[float] = None
    output_tps_p90: Optional[float] = None
    output_tps_p95: Optional[float] = None
    output_tps_p99: Optional[float] = None

    # Token counts
    prompt_tokens_mean: Optional[float] = None
    output_tokens_mean: Optional[float] = None

    # Concurrency
    concurrency_mean: Optional[float] = None
    concurrency_p50: Optional[float] = None
    concurrency_p90: Optional[float] = None

    # Extended TTFT/ITL percentiles (full distribution)
    ttft_mean: Optional[float] = None
    ttft_min: Optional[float] = None
    ttft_max: Optional[float] = None
    ttft_std_dev: Optional[float] = None
    ttft_p25: Optional[float] = None
    ttft_p75: Optional[float] = None
    itl_mean: Optional[float] = None
    itl_min: Optional[float] = None
    itl_max: Optional[float] = None
    itl_std_dev: Optional[float] = None

    # Throughput (req/s) extended stats
    throughput_mean: Optional[float] = None

    # Metrics from Prometheus/Thanos
    gpu_utilization: Optional[float] = None
    kv_cache_usage: Optional[float] = None
    metrics_json_content: Optional[str] = None

    # vLLM memory profiling (measured from pod logs after startup)
    vllm_gpu_blocks: Optional[int] = None
    vllm_available_kv_gb: Optional[float] = None
    vllm_fixed_overhead_gb: Optional[float] = None

    # Pod error scanning
    pod_errors_detected: bool = False
    pod_errors_json: Optional[str] = None


class TestOrchestrator:
    """Orchestrates the complete test flow for optimization runs."""

    @staticmethod
    def _parse_guidellm_results(guidellm_output: str, result: TestResult) -> None:
        """
        Parse guidellm JSON output and populate ALL TestResult metrics.

        Extracts the complete set of metrics from guidellm output so the
        raw JSON file is not needed after parsing.
        """
        try:
            import json
            from pathlib import Path

            if not guidellm_output or not Path(guidellm_output).exists():
                logger.warning(f"guidellm output file not found: {guidellm_output}")
                return

            with open(guidellm_output, 'r') as f:
                raw_content = f.read()

            result.guidellm_raw_json = raw_content
            data = json.loads(raw_content)

            benchmarks = data.get('benchmarks', [])
            if not benchmarks:
                logger.warning("No benchmarks found in guidellm output")
                return

            bench = benchmarks[0]
            metrics = bench.get('metrics', {})

            def _get_dist(metric_key):
                """Get the successful DistributionSummary for a metric."""
                return metrics.get(metric_key, {}).get('successful', {})

            def _pcts(dist):
                """Extract percentiles dict from a DistributionSummary."""
                return dist.get('percentiles', {})

            # --- TTFT (Time to First Token, ms) ---
            ttft_dist = _get_dist('time_to_first_token_ms')
            ttft_p = _pcts(ttft_dist)
            result.ttft_p50 = ttft_p.get('p50')
            result.ttft_p90 = ttft_p.get('p90')
            result.ttft_p95 = ttft_p.get('p95')
            result.ttft_p99 = ttft_p.get('p999')
            result.ttft_mean = ttft_dist.get('mean')
            result.ttft_min = ttft_dist.get('min')
            result.ttft_max = ttft_dist.get('max')
            result.ttft_std_dev = ttft_dist.get('std_dev')
            result.ttft_p25 = ttft_p.get('p25')
            result.ttft_p75 = ttft_p.get('p75')

            # --- ITL (Inter-Token Latency, ms) ---
            itl_dist = _get_dist('inter_token_latency_ms')
            itl_p = _pcts(itl_dist)
            result.itl_p50 = itl_p.get('p50')
            result.itl_p90 = itl_p.get('p90')
            result.itl_p95 = itl_p.get('p95')
            result.itl_p99 = itl_p.get('p999')
            result.itl_mean = itl_dist.get('mean')
            result.itl_min = itl_dist.get('min')
            result.itl_max = itl_dist.get('max')
            result.itl_std_dev = itl_dist.get('std_dev')

            # --- Throughput (requests/sec) ---
            rps_dist = _get_dist('requests_per_second')
            rps_p = _pcts(rps_dist)
            result.throughput_p50 = rps_p.get('p50')
            result.throughput_p90 = rps_p.get('p90')
            result.throughput_p95 = rps_p.get('p95')
            result.throughput_p99 = rps_p.get('p999')
            result.throughput_mean = rps_dist.get('mean')

            # --- TPOT (Time Per Output Token, ms) ---
            tpot_dist = _get_dist('time_per_output_token_ms')
            tpot_p = _pcts(tpot_dist)
            result.tpot_mean = tpot_dist.get('mean')
            result.tpot_p50 = tpot_p.get('p50')
            result.tpot_p90 = tpot_p.get('p90')
            result.tpot_p95 = tpot_p.get('p95')
            result.tpot_p99 = tpot_p.get('p999')

            # --- E2E Request Latency (seconds) ---
            lat_dist = _get_dist('request_latency')
            lat_p = _pcts(lat_dist)
            result.e2e_latency_mean = lat_dist.get('mean')
            result.e2e_latency_p50 = lat_p.get('p50')
            result.e2e_latency_p90 = lat_p.get('p90')
            result.e2e_latency_p95 = lat_p.get('p95')
            result.e2e_latency_p99 = lat_p.get('p999')

            # --- Output Tokens Per Second (decode throughput) ---
            otps_dist = _get_dist('output_tokens_per_second')
            otps_p = _pcts(otps_dist)
            result.output_tps_mean = otps_dist.get('mean')
            result.output_tps_p50 = otps_p.get('p50')
            result.output_tps_p90 = otps_p.get('p90')
            result.output_tps_p95 = otps_p.get('p95')
            result.output_tps_p99 = otps_p.get('p999')

            # --- Token Counts ---
            prompt_dist = _get_dist('prompt_token_count')
            result.prompt_tokens_mean = prompt_dist.get('mean')
            output_dist = _get_dist('output_token_count')
            result.output_tokens_mean = output_dist.get('mean')

            # --- Concurrency ---
            conc_dist = _get_dist('request_concurrency')
            conc_p = _pcts(conc_dist)
            result.concurrency_mean = conc_dist.get('mean')
            result.concurrency_p50 = conc_p.get('p50')
            result.concurrency_p90 = conc_p.get('p90')

            # --- Request Totals ---
            totals = metrics.get('request_totals', {})
            result.request_total = totals.get('total')
            result.request_successful = totals.get('successful')
            result.request_incomplete = totals.get('incomplete')
            result.request_errored = totals.get('errored')

            # --- Benchmark Timing ---
            result.benchmark_duration_s = bench.get('duration')
            result.warmup_duration_s = bench.get('warmup_duration')

            logger.info(
                f"Parsed guidellm metrics: TTFT p90={result.ttft_p90}ms, "
                f"Throughput p90={result.throughput_p90} req/s, "
                f"TPOT p90={result.tpot_p90}ms, "
                f"Requests: {result.request_successful}/{result.request_total} ok"
            )

        except Exception as e:
            logger.error(f"Failed to parse guidellm results: {e}")

    @staticmethod
    def _get_thanos_url() -> Optional[str]:
        """
        Dynamically fetch Thanos URL from OpenShift cluster.

        Returns:
            Thanos URL or None if not found
        """
        try:
            # Try kubectl (works in both OpenShift and vanilla k8s)
            cmd = ['kubectl', 'get', 'route', 'thanos-querier', '-n', 'openshift-monitoring', '-o', 'jsonpath={.spec.host}']
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=False)

            if result.returncode == 0 and result.stdout.strip():
                host = result.stdout.strip()
                thanos_url = f"https://{host}"
                logger.info(f"Discovered Thanos URL: {thanos_url}")
                return thanos_url
            else:
                logger.warning(f"Could not discover Thanos URL from cluster: {result.stderr}")
                return None
        except Exception as e:
            logger.error(f"Failed to get Thanos URL: {e}")
            return None

    @staticmethod
    def _enable_namespace_monitoring(namespace: str) -> bool:
        """
        Enable User Workload Monitoring on OpenShift namespace.

        This labels the namespace with openshift.io/user-monitoring=true
        which allows Prometheus to scrape metrics from pods in the namespace.

        Args:
            namespace: Kubernetes namespace to label

        Returns:
            True if successful or not on OpenShift, False if failed
        """
        try:
            # Check if running on OpenShift (has 'oc' command or route.openshift.io API)
            check_cmd = ['kubectl', 'api-resources', '--api-group=route.openshift.io']
            result = subprocess.run(check_cmd, capture_output=True, text=True, timeout=10, check=False)

            if result.returncode != 0 or not result.stdout.strip():
                logger.info("Not running on OpenShift - skipping namespace monitoring label")
                return True

            # Running on OpenShift - label the namespace
            logger.info(f"Enabling user workload monitoring on namespace: {namespace}")
            label_cmd = ['kubectl', 'label', 'namespace', namespace,
                        'openshift.io/user-monitoring=true', '--overwrite']
            result = subprocess.run(label_cmd, capture_output=True, text=True, timeout=10, check=False)

            if result.returncode == 0:
                logger.info(f"Successfully enabled monitoring on namespace: {namespace}")
                return True
            else:
                logger.warning(f"Failed to label namespace: {result.stderr}")
                return False
        except Exception as e:
            logger.warning(f"Failed to enable namespace monitoring: {e}")
            return False

    def _profile_vllm_memory(
        self,
        test_id: str,
        gpu_memory_utilization: float,
        gpu_vram_gb: float,
        result: TestResult,
        log_callback: Optional[Callable[[str], None]] = None
    ) -> None:
        """Read vLLM pod logs after startup to measure actual GPU memory overhead.

        Parses 'Available KV cache memory' and '# GPU blocks' from vLLM logs.
        Computes fixed_overhead = (gpu_vram × U) - available_kv_memory, which
        is the total non-KV memory (model weights + CUDA graphs + workspace).
        """
        try:
            pods_result = subprocess.run(
                ['kubectl', 'get', 'pods', '-n', self.namespace,
                 '-l', f'llm-d.ai/test-id={test_id}',
                 '-o', 'jsonpath={range .items[*]}{.metadata.name}{"\\n"}{end}'],
                capture_output=True, text=True, timeout=15, check=False
            )
            if pods_result.returncode != 0 or not pods_result.stdout.strip():
                return

            pod_name = pods_result.stdout.strip().splitlines()[0]
            logs_result = subprocess.run(
                ['kubectl', 'logs', '-n', self.namespace, pod_name,
                 '-c', 'vllm', '--tail=200'],
                capture_output=True, text=True, timeout=30, check=False
            )
            if logs_result.returncode != 0:
                return

            import re
            for line in logs_result.stdout.splitlines():
                # Parse "Available KV cache memory: X.XX GiB"
                m = re.search(r'Available KV cache memory:\s*([-\d.]+)\s*GiB', line)
                if m:
                    result.vllm_available_kv_gb = float(m.group(1))

                # Parse "# GPU blocks: XXXX"
                m = re.search(r'#\s*GPU blocks:\s*(\d+)', line)
                if m:
                    result.vllm_gpu_blocks = int(m.group(1))

            if result.vllm_available_kv_gb is not None:
                budget_gb = gpu_vram_gb * gpu_memory_utilization
                result.vllm_fixed_overhead_gb = budget_gb - result.vllm_available_kv_gb
                if log_callback:
                    log_callback(
                        f"   📐 vLLM memory profile: overhead={result.vllm_fixed_overhead_gb:.1f}GB "
                        f"(budget={budget_gb:.1f}GB, KV available={result.vllm_available_kv_gb:.1f}GB)"
                    )

        except Exception as e:
            logger.debug(f"Failed to profile vLLM memory: {e}")

    def __init__(
        self,
        namespace: str = 'llm-d',
        kubeconfig: Optional[str] = None,
        thanos_url: Optional[str] = None,
        deployment_timeout: int = 3600,
        test_duration: int = 300
    ):
        """
        Initialize TestOrchestrator.

        Args:
            namespace: Kubernetes namespace
            kubeconfig: Path to kubeconfig file
            thanos_url: Thanos/Prometheus URL for metrics collection (if None, will auto-discover)
            deployment_timeout: Timeout for deployment readiness (seconds)
            test_duration: Default test duration (seconds)
        """
        self.namespace = namespace
        self.deployment_timeout = deployment_timeout
        self.test_duration = test_duration

        self.deployment_manager = DeploymentManager(
            namespace=namespace,
            kubeconfig=kubeconfig
        )

        # Enable namespace monitoring for metrics collection (OpenShift only)
        self._enable_namespace_monitoring(namespace)

        # Auto-discover Thanos URL if not provided
        if thanos_url is None:
            thanos_url = self._get_thanos_url()

        self.metrics_collector = None
        if thanos_url:
            logger.info(f"Initializing MetricsCollector with Thanos URL: {thanos_url}")

            # Read service account token for Thanos authentication
            token = None
            token_file = '/run/secrets/kubernetes.io/serviceaccount/token'
            if os.path.exists(token_file):
                try:
                    with open(token_file, 'r') as f:
                        token = f.read().strip()
                    logger.info("Successfully loaded service account token for Thanos authentication")
                except Exception as e:
                    logger.warning(f"Failed to read service account token: {e}")
            else:
                logger.warning(f"Service account token file not found at {token_file}")

            # Create MetricsConfig object
            metrics_config = MetricsConfig(
                thanos_url=thanos_url,
                namespace=namespace,
                pod_name_pattern='',  # Will be set per test
                step_seconds=5,
                token=token
            )
            self.metrics_collector = MetricsCollector(metrics_config)
        else:
            logger.warning("No Thanos URL provided - metrics collection will be disabled")

    def _get_service_endpoint(
        self,
        test_id: str,
        architecture: str,
        log_callback: Optional[Callable[[str], None]] = None
    ) -> Optional[str]:
        """
        Get service endpoint for a deployed configuration via Istio gateway.

        Args:
            test_id: Test ID
            architecture: Architecture type
            log_callback: Optional callback for logging

        Returns:
            Service endpoint URL or None if not found
        """
        # Always use gateway discovery - NEVER use direct pod/service IPs
        return self._discover_istio_gateway(
            namespace=self.namespace,
            test_id=test_id,
            architecture=architecture,
            log_callback=log_callback
        )

    def _get_pod_ip(
        self,
        test_id: str,
        architecture: str,
        log_callback: Optional[Callable[[str], None]] = None
    ) -> Optional[str]:
        """
        Get pod IP directly when service is not available.

        Args:
            test_id: Test ID
            architecture: Architecture type
            log_callback: Optional callback for logging

        Returns:
            Pod IP endpoint URL or None if not found
        """
        try:
            # Get pods by label
            result = self.deployment_manager.kubectl.run(
                [
                    'get', 'pods',
                    '-l', f'llm-d.ai/test-id={test_id}',
                    '-n', self.namespace,
                    '-o', 'json'
                ],
                check=False
            )

            if result.returncode != 0:
                return None

            pods_data = json.loads(result.stdout)
            items = pods_data.get('items', [])

            if not items:
                return None

            # Get first running pod IP
            for pod in items:
                status = pod.get('status', {})
                phase = status.get('phase')
                pod_ip = status.get('podIP')

                if phase == 'Running' and pod_ip:
                    endpoint = f"http://{pod_ip}:8000/v1"
                    if log_callback:
                        log_callback(f"✅ Pod endpoint: {endpoint}")
                    return endpoint

            return None

        except Exception as e:
            logger.error(f"Failed to get pod IP: {e}")
            return None

    def _run_curl_test(
        self,
        endpoint: str,
        config: TestConfig,
        log_callback: Optional[Callable[[str], None]] = None
    ) -> bool:
        """
        Run a simple curl test to verify endpoint is responding.

        Args:
            endpoint: Service endpoint URL
            config: Test configuration
            log_callback: Optional callback for logging

        Returns:
            True if endpoint responds successfully
        """
        try:
            if log_callback:
                log_callback(f"🔍 Testing endpoint: {endpoint}")

            # Try to hit the /health or /v1/models endpoint
            import requests

            # Try health endpoint first
            health_url = endpoint.replace('/v1', '/health')

            if log_callback:
                log_callback(f"   Checking health endpoint: {health_url}")

            try:
                response = requests.get(health_url, timeout=10)
                if response.status_code == 200:
                    if log_callback:
                        log_callback(f"   ✅ Health check passed: {response.status_code}")
                    return True
            except Exception as e:
                if log_callback:
                    log_callback(f"   ⚠️  Health endpoint failed: {e}")

            # Try models endpoint
            models_url = f"{endpoint}/models"
            if log_callback:
                log_callback(f"   Checking models endpoint: {models_url}")

            try:
                response = requests.get(models_url, timeout=10)
                if response.status_code == 200:
                    if log_callback:
                        log_callback(f"   ✅ Models endpoint passed: {response.status_code}")
                        data = response.json()
                        if 'data' in data and len(data['data']) > 0:
                            model_id = data['data'][0].get('id', 'unknown')
                            log_callback(f"   📦 Model available: {model_id}")
                    return True
            except Exception as e:
                if log_callback:
                    log_callback(f"   ⚠️  Models endpoint failed: {e}")

            # Try simple completion request
            if log_callback:
                log_callback("   Attempting test completion request...")

            completion_url = f"{endpoint}/completions"
            payload = {
                "model": config.model_name,
                "prompt": "Hello",
                "max_tokens": 5,
                "temperature": 0.0
            }

            try:
                response = requests.post(completion_url, json=payload, timeout=30)
                if response.status_code == 200:
                    if log_callback:
                        log_callback(f"   ✅ Completion request passed: {response.status_code}")
                    return True
                else:
                    if log_callback:
                        log_callback(f"   ❌ Completion failed with status: {response.status_code}")
                        log_callback(f"   Response: {response.text[:200]}")
            except Exception as e:
                if log_callback:
                    log_callback(f"   ❌ Completion request failed: {e}")

            return False

        except Exception as e:
            error_msg = f"Curl test failed: {e}"
            logger.error(error_msg)
            if log_callback:
                log_callback(f"❌ {error_msg}")
            return False

    def _wait_for_model_loaded(
        self,
        test_id: str,
        timeout: int = 3600,
        log_callback: Optional[Callable[[str], None]] = None,
        stop_check: Optional[Callable[[], bool]] = None
    ) -> bool:
        """Wait for all vLLM pods to finish loading the model by checking logs
        for 'Application startup complete'."""
        start_time = time.time()
        ready_pods = set()

        while time.time() - start_time < timeout:
            if stop_check and stop_check():
                if log_callback:
                    log_callback("🛑 Model load wait cancelled — optimization stopped")
                return False

            try:
                result = subprocess.run(
                    ['kubectl', 'get', 'pods', '-n', self.namespace,
                     '-l', f'llm-d.ai/test-id={test_id}',
                     '-o', 'jsonpath={range .items[*]}{.metadata.name}{" "}{end}'],
                    capture_output=True, text=True, timeout=15, check=False
                )
                pod_names = result.stdout.strip().split()
                pod_names = [p for p in pod_names if p]

                if not pod_names:
                    time.sleep(10)
                    continue

                for pod_name in pod_names:
                    if pod_name in ready_pods:
                        continue
                    log_result = subprocess.run(
                        ['kubectl', 'logs', pod_name, '-n', self.namespace,
                         '-c', 'vllm', '--tail=50'],
                        capture_output=True, text=True, timeout=15, check=False
                    )
                    if 'Application startup complete' in log_result.stdout:
                        ready_pods.add(pod_name)
                        if log_callback:
                            log_callback(f"   {pod_name}: model loaded ({len(ready_pods)}/{len(pod_names)})")

                if len(ready_pods) >= len(pod_names) and len(pod_names) > 0:
                    elapsed = int(time.time() - start_time)
                    if log_callback:
                        n = len(pod_names)
                        log_callback(f"   {'Pod has' if n == 1 else f'All {n} pods have'} model loaded ({elapsed}s)")
                    return True

            except Exception as e:
                logger.warning(f"Failed to check model loading: {e}")

            time.sleep(15)

        elapsed = int(time.time() - start_time)
        if log_callback:
            log_callback(f"   Timeout after {elapsed}s waiting for model to load")
        return False

    def _wait_for_gateway_ready(
        self,
        endpoint: str,
        config: TestConfig,
        expected_pods: int,
        timeout: int = 300,
        log_callback: Optional[Callable[[str], None]] = None
    ) -> bool:
        """
        Wait for the gateway to have all expected pods registered and serving.

        After K8s reports pods as Ready, the EPP still needs time to discover
        and register them in its datastore. This method polls until:
        1. All expected pods with the test-id label are Ready in K8s
        2. A test completion request through the gateway succeeds

        Args:
            endpoint: Gateway endpoint URL
            config: Test configuration
            expected_pods: Number of pods expected to be ready
            timeout: Max seconds to wait
            log_callback: Optional callback for logging

        Returns:
            True if gateway is ready with all pods
        """
        import requests as req_lib

        if log_callback:
            log_callback(f"🔄 Waiting for EPP to register {'1 pod' if expected_pods == 1 else f'all {expected_pods} pods'} in the inference pool...")

        start_time = time.time()
        last_ready_count = -1
        models_ok = False
        elapsed_logged = set()
        self._pool_wait_logged = False
        self._gw_wait_logged = False
        self._routing_wait_logged = False

        while time.time() - start_time < timeout:
            elapsed = int(time.time() - start_time)

            # Step 1: Count Ready pods matching the test-id label
            try:
                result = subprocess.run(
                    ['kubectl', 'get', 'pods', '-n', self.namespace,
                     '-l', f'llm-d.ai/test-id={config.test_id}',
                     '-o', 'json'],
                    capture_output=True, text=True, timeout=15, check=False
                )

                ready_count = 0
                total_count = 0
                if result.returncode == 0 and result.stdout.strip():
                    pods_data = json.loads(result.stdout)
                    for pod in pods_data.get('items', []):
                        total_count += 1
                        conditions = pod.get('status', {}).get('conditions', [])
                        for cond in conditions:
                            if cond.get('type') == 'Ready' and cond.get('status') == 'True':
                                ready_count += 1
                                break

                if ready_count != last_ready_count:
                    if log_callback:
                        p = 'pod' if expected_pods == 1 else 'pods'
                        log_callback(f"   EPP pod discovery: {ready_count}/{expected_pods} {p} ready in K8s")
                    last_ready_count = ready_count

                if ready_count < expected_pods:
                    time.sleep(10)
                    continue

            except Exception as e:
                logger.warning(f"Failed to count ready pods: {e}")
                time.sleep(10)
                continue

            # Step 2: All pods Ready in K8s — verify gateway routing works
            try:
                models_url = endpoint.rstrip('/') + '/v1/models'
                resp = req_lib.get(models_url, timeout=10, verify=False)
                if resp.status_code != 200:
                    if log_callback and not getattr(self, '_pool_wait_logged', False):
                        log_callback("   EPP gateway check: waiting for pool registration...")
                        self._pool_wait_logged = True
                    time.sleep(5)
                    continue
                elif not models_ok:
                    models_ok = True
                    if log_callback:
                        log_callback("   EPP gateway check: models endpoint OK, verifying routing...")
            except Exception as e:
                if log_callback and not getattr(self, '_gw_wait_logged', False):
                    log_callback("   EPP gateway check: waiting for gateway...")
                    self._gw_wait_logged = True
                time.sleep(5)
                continue

            # Step 3: Send a test completion to verify full routing through the pool
            try:
                completion_url = endpoint.rstrip('/') + '/v1/chat/completions'
                payload = {
                    "model": config.model_name,
                    "messages": [{"role": "user", "content": "Hello"}],
                    "max_tokens": 1,
                    "temperature": 0.0
                }
                resp = req_lib.post(completion_url, json=payload, timeout=30, verify=False)
                if resp.status_code == 200:
                    elapsed = int(time.time() - start_time)
                    if log_callback:
                        p = 'pod' if ready_count == 1 else 'pods'
                        log_callback(f"   ✅ EPP ready — {ready_count} {p} registered in inference pool ({elapsed}s)")
                    return True
                else:
                    if log_callback and not getattr(self, '_routing_wait_logged', False):
                        log_callback(f"   EPP pool registration: pods not yet routable (HTTP {resp.status_code}), waiting...")
                        self._routing_wait_logged = True
                    time.sleep(5)
            except Exception as e:
                if log_callback and not getattr(self, '_routing_wait_logged', False):
                    log_callback(f"   EPP pool registration: routing test failed ({e}), waiting...")
                    self._routing_wait_logged = True
                time.sleep(5)

        elapsed = int(time.time() - start_time)
        if log_callback:
            p = 'pod' if expected_pods == 1 else 'pods'
            log_callback(f"   ⏱️ Timeout after {elapsed}s waiting for EPP pool registration ({last_ready_count}/{expected_pods} {p})")
        return False

    def _discover_istio_gateway(
        self,
        namespace: str,
        test_id: str,
        architecture: str,
        log_callback: Optional[Callable[[str], None]] = None
    ) -> str:
        """
        Discover Istio gateway endpoint or fallback to direct service.

        Args:
            namespace: Kubernetes namespace
            test_id: Test ID
            architecture: Architecture type
            log_callback: Optional callback for logging

        Returns:
            Service URL
        """
        logger.debug('Discovering Istio gateway endpoint...')

        # Query for Gateway API gateways in namespace
        # Map architecture to expected gateway prefix
        gateway_mapping = {
            'pd': 'infra-pd',
            'ep': 'infra-ep',
            'aggregated': 'infra-aggregated'
        }

        gateway_prefix = gateway_mapping.get(architecture, 'infra-aggregated')
        logger.debug(f'Architecture: {architecture} -> gateway prefix: {gateway_prefix}')

        try:
            # Query all gateways in namespace
            result = self.deployment_manager.kubectl.run(
                ['get', 'gateway', '-n', namespace, '-o', 'json'],
                check=False
            )

            if result.returncode == 0 and result.stdout.strip():
                import json
                gateways = json.loads(result.stdout)

                available = [gw['metadata']['name'] for gw in gateways.get('items', [])]
                logger.debug(f'Available gateways: {", ".join(available) if available else "none"}')

                # Find gateway matching architecture
                for gateway in gateways.get('items', []):
                    gateway_name = gateway['metadata']['name']
                    if gateway_name.startswith(gateway_prefix):
                        # Istio gateway service follows pattern: {gateway-name}-istio
                        istio_gateway_svc = f'{gateway_name}-istio.{namespace}.svc.cluster.local'
                        service_url = f'http://{istio_gateway_svc}'
                        logger.debug(f'Using gateway: {istio_gateway_svc}')
                        return service_url
            else:
                error_msg = f"kubectl get gateway failed: {result.stderr}"
                logger.error(error_msg)
                if log_callback:
                    log_callback(f'   ❌ {error_msg}')
                raise RuntimeError(error_msg)
        except Exception as e:
            error_msg = f"Failed to query gateways: {e}"
            logger.error(error_msg)
            if log_callback:
                log_callback(f'   ❌ {error_msg}')
            raise RuntimeError(error_msg)

        # Gateway not found - fail the test
        error_msg = f"No gateway found with prefix '{gateway_prefix}' in namespace {namespace} for architecture '{architecture}'"
        logger.error(error_msg)
        if log_callback:
            log_callback(f'   ❌ {error_msg}')
        raise RuntimeError(error_msg)

    def _monitor_pods_during_benchmark(
        self,
        namespace: str,
        test_id: str,
        expected_pod_count: int,
        benchmark_start: float,
        test_duration: int,
        check_interval: int,
        log_callback: Optional[Callable[[str], None]] = None
    ) -> Optional[str]:
        """
        Monitor pods for crashes during benchmark.

        Args:
            namespace: Kubernetes namespace
            test_id: Test ID
            expected_pod_count: Expected number of pods
            benchmark_start: Benchmark start time
            test_duration: Test duration in seconds
            check_interval: How often to check (seconds)
            log_callback: Optional callback for logging

        Returns:
            Error message if pods crashed, None if healthy
        """
        try:
            # Get current pod status
            result = self.deployment_manager.kubectl.run(
                [
                    'get', 'pods', '-n', namespace,
                    '-l', f'test-id={test_id}',
                    '-o', 'jsonpath={range .items[*]}{.metadata.name}:{.status.phase}:{.status.containerStatuses[0].restartCount}{"\\n"}{end}'
                ],
                check=False,
                timeout=10
            )

            if result.returncode != 0:
                return None

            crashed_pods = []
            restarted_pods = []
            current_pods = []

            for line in result.stdout.strip().split('\n'):
                if line and ':' in line:
                    parts = line.split(':')
                    if len(parts) >= 3:
                        pod_name, phase, restart_count = parts[0], parts[1], parts[2]
                        current_pods.append(pod_name)
                        if phase != 'Running':
                            crashed_pods.append(f"{pod_name} ({phase})")
                        elif int(restart_count) > 0:
                            restarted_pods.append(f"{pod_name} (restarts: {restart_count})")

            # Check if pods disappeared (deleted/terminated)
            current_pod_count = len(current_pods)
            if current_pod_count < expected_pod_count:
                missing_count = expected_pod_count - current_pod_count
                crashed_pods.append(f"{missing_count} pod(s) disappeared/deleted")

            if crashed_pods:
                if log_callback:
                    log_callback('❌ Pod crashes detected:')
                    for pod in crashed_pods:
                        log_callback(f'   {pod}')
                    log_callback('🛑 Stopping test - pods crashed during benchmark')
                return f"Pods crashed: {', '.join(crashed_pods)}"

            if restarted_pods and log_callback:
                log_callback('⚠️  Pod restarts detected:')
                for pod in restarted_pods:
                    log_callback(f'   {pod}')

            # Log progress
            elapsed = int(time.time() - benchmark_start)
            if log_callback:
                log_callback(f'   [{elapsed}s/{test_duration}s] All {current_pod_count} pods healthy')

            return None

        except Exception as e:
            if log_callback:
                log_callback(f'⚠️  Warning: Pod health check failed: {str(e)[:100]}')
            return None

    def _run_guidellm_test(
        self,
        endpoint: str,
        config: TestConfig,
        log_callback: Optional[Callable[[str], None]] = None,
        monitor_pods: bool = False,
        expected_pod_count: int = 0,
        collect_metrics: bool = True
    ) -> tuple[bool, Optional[str], Optional[str]]:
        """
        Run guidellm load test with optional pod crash monitoring and metrics collection.

        Args:
            endpoint: Service endpoint URL (can be None to auto-discover Istio gateway)
            config: Test configuration
            log_callback: Optional callback for logging
            monitor_pods: Whether to monitor pods for crashes during benchmark
            expected_pod_count: Expected number of pods (for crash detection)
            collect_metrics: Whether to collect Prometheus/Thanos metrics (default: True)

        Environment Variables:
            HOME_STORAGE_DIR: Storage mount point (set by deploy.sh, default: /mnt/storage)
            HF_HOME: HuggingFace cache directory (optional)
                     If not set, uses ${HOME_STORAGE_DIR}/.cache/huggingface
                     Falls back to /tmp/huggingface_cache if mount unavailable

        Returns:
            Tuple of (success, guidellm_output_file_path, metrics_file_path)
        """
        try:
            if log_callback:
                stop_mode = getattr(config, 'stop_mode', 'duration')
                max_reqs = getattr(config, 'max_requests', None)
                if stop_mode == 'max_requests' and max_reqs:
                    log_callback(f'🏃 Running guidellm benchmark for {max_reqs} requests...')
                else:
                    log_callback(f'🏃 Running guidellm benchmark for {config.test_duration}s...')

            # Auto-discover Istio gateway if endpoint not provided
            if endpoint is None:
                endpoint = self._discover_istio_gateway(
                    self.namespace,
                    config.test_id,
                    config.architecture,
                    log_callback
                )

            # Get request rate type and rate (with defaults)
            # Map old profile names to rate-type for backward compatibility
            rate_type_map = {
                'synchronous': 'constant',
                'concurrent': 'concurrent',
                'throughput': 'throughput',
                'constant': 'constant',
                'poisson': 'poisson'
            }
            request_profile = getattr(config, 'request_type', 'constant')
            rate_type = rate_type_map.get(request_profile, 'constant')
            request_rate = getattr(config, 'request_rate', 1)

            if log_callback:
                log_callback(f'   Target: {endpoint}')
                rate_label = f'{request_rate} concurrent users' if rate_type == 'concurrent' else f'{request_rate} req/s ({rate_type})'
                log_callback(f'   Load: {rate_label}')

            # Create output file path (using --output-path like A-AYE-Benchmark)
            output_dir = Path(f'/tmp/guidellm-{config.test_id}')
            output_dir.mkdir(parents=True, exist_ok=True)
            output_file = output_dir / f'{config.test_id}.json'

            # Prepare data config
            workload_mode = getattr(config, 'workload_mode', 'synthetic') or 'synthetic'
            turns = getattr(config, 'turns', 1) or 1
            data_args = None
            column_mapper = None

            if workload_mode == 'dataset' and getattr(config, 'dataset_source', None):
                # Custom dataset mode
                data_payload = config.dataset_source
                max_output = getattr(config, 'dataset_max_output', 256) or 256
                data_args = f'{{"output_tokens": {max_output}}}'
                col = getattr(config, 'dataset_column', None)
                if col:
                    column_mapper = f'{{"text_column": "{col}"}}'
                log_callback(f'   Using dataset: {data_payload}')
            else:
                # Synthetic workload mode
                data_payload = f'prompt_tokens={config.isl},output_tokens={config.osl}'
                if getattr(config, 'isl_stdev', None):
                    data_payload += f',prompt_tokens_stdev={config.isl_stdev}'
                if getattr(config, 'osl_stdev', None):
                    data_payload += f',output_tokens_stdev={config.osl_stdev}'
                if turns > 1:
                    data_payload += f',turns={turns}'

                # Clamp distribution tails to fit within max_model_len
                max_model_len = getattr(config, 'max_model_len', 0)
                if max_model_len and (getattr(config, 'isl_stdev', None) or getattr(config, 'osl_stdev', None)):
                    overhead = 200
                    prompt_max = max_model_len - config.osl - overhead
                    if prompt_max > 0:
                        data_payload += f',prompt_tokens_max={prompt_max}'

            # Use chat completions for multi-turn, completions for single-turn
            request_format = '/v1/chat/completions' if turns > 1 else '/v1/completions'

            # Build guidellm command
            # --backend-kwargs '{"http2": false}' is critical for PD deployments:
            # Istio ext_proc (EPP) cannot unmarshal HTTP/2 streamed request bodies,
            # causing 400 "Error unmarshaling request body" on PD gateway
            cmd = [
                'guidellm', 'benchmark', 'run',
                '--target', endpoint,
                '--model', config.model_name,
                '--processor', config.model_name,
                '--data', data_payload,
                '--backend-kwargs', '{"http2": false}',
                '--request-format', request_format,
                '--rate-type', rate_type,
                '--rate', str(request_rate),
            ]

            # Dataset-specific args
            if data_args:
                cmd.extend(['--data-args', data_args])
            if column_mapper:
                cmd.extend(['--data-column-mapper', column_mapper])

            # Stop condition: duration or max requests
            stop_mode = getattr(config, 'stop_mode', 'duration')
            max_requests = getattr(config, 'max_requests', None)
            if stop_mode == 'max_requests' and max_requests:
                cmd.extend(['--max-requests', str(max_requests)])
            else:
                cmd.extend(['--max-seconds', str(config.test_duration)])

            cmd.extend(['--output-path', str(output_file)])
            warmup = min(60, max(0, config.test_duration - 30)) if hasattr(config, 'test_duration') else 60
            cmd.extend(['--warmup', str(warmup)])
            cmd.extend(['--sample-requests', '20'])

            # Start guidellm in background
            logger.debug('Starting guidellm...')

            # Set environment variables for guidellm
            env = os.environ.copy()

            # Determine HuggingFace cache directory dynamically
            # Priority: 1) HF_HOME already set, 2) HOME_STORAGE_DIR/.cache/huggingface, 3) /tmp fallback
            hf_home = env.get('HF_HOME')
            if not hf_home:
                # Use HOME_STORAGE_DIR from deploy.sh configuration
                storage_dir = env.get('HOME_STORAGE_DIR', '/mnt/storage')
                hf_home = f'{storage_dir}/.cache/huggingface'

                # Create cache directory if parent exists
                if os.path.exists(storage_dir):
                    os.makedirs(hf_home, exist_ok=True)
                else:
                    # Fallback to /tmp if storage mount doesn't exist
                    hf_home = '/tmp/huggingface_cache'
                    os.makedirs(hf_home, exist_ok=True)
                    if log_callback:
                        log_callback(f'   ⚠️  HOME_STORAGE_DIR not found, using temporary cache: {hf_home}')

            env['HF_HOME'] = hf_home
            env['HF_DATASETS_CACHE'] = f'{hf_home}/datasets'
            env['TRANSFORMERS_CACHE'] = f'{hf_home}/transformers'

            logger.debug(f'HF cache directory: {hf_home}')

            benchmark_start = time.time()

            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=env
            )

            # Monitor pods and guidellm output during benchmark
            check_interval = 30  # Check every 30 seconds
            last_check = benchmark_start
            benchmark_success = True
            error_message = None

            # For max_requests mode, use a generous timeout (1h) since we don't know how long it'll take
            _stop_mode = getattr(config, 'stop_mode', 'duration')
            _max_reqs = getattr(config, 'max_requests', None)
            monitor_timeout = 3600 if (_stop_mode == 'max_requests' and _max_reqs) else config.test_duration

            while time.time() - benchmark_start < monitor_timeout:
                # Check if guidellm process ended
                if process.poll() is not None:
                    returncode = process.returncode
                    if returncode == 0:
                        if log_callback:
                            elapsed = int(time.time() - benchmark_start)
                            log_callback(f'ℹ️  Guidellm completed ({elapsed}s)')
                        break
                    else:
                        remaining_output = process.stdout.read() if process.stdout else ''
                        if log_callback:
                            log_callback(f'❌ Guidellm process exited early (code: {returncode})')
                            if remaining_output:
                                for line in remaining_output.strip().splitlines()[-20:]:
                                    log_callback(f'   Guidellm: {line.strip()[:200]}')
                        benchmark_success = False
                        error_message = f"Guidellm crashed with exit code {returncode}"
                        if remaining_output:
                            error_message += f"\n{remaining_output[-500:]}"
                        break

                # Read guidellm output (non-blocking)
                try:
                    import select
                    if select.select([process.stdout], [], [], 0)[0]:
                        line = process.stdout.readline()
                        if line:
                            # Log interesting guidellm output
                            if 'error' in line.lower() or 'warning' in line.lower():
                                if log_callback:
                                    log_callback(f'   Guidellm: {line.strip()[:100]}')
                except:
                    pass

                # Sleep until next check
                remaining = monitor_timeout - (time.time() - benchmark_start)
                time.sleep(min(5, check_interval, max(0.1, remaining)))

                # Check pod health
                current_time = time.time()
                if monitor_pods and current_time - last_check >= check_interval:
                    pod_error = self._monitor_pods_during_benchmark(
                        self.namespace,
                        config.test_id,
                        expected_pod_count,
                        benchmark_start,
                        config.test_duration,
                        check_interval,
                        log_callback
                    )

                    if pod_error:
                        benchmark_success = False
                        error_message = pod_error
                        break

                    last_check = current_time

            # Wait for guidellm to finish — no timeout, let it complete naturally
            if process.poll() is None:
                if log_callback:
                    log_callback('   Waiting for guidellm to finish...')
                process.wait()

            elapsed_total = int(time.time() - benchmark_start)

            # Check final status
            result_file = output_file
            output_exists = result_file.exists() and result_file.stat().st_size > 0

            if benchmark_success and (process.returncode == 0 or output_exists):
                if log_callback:
                    log_callback(f'✅ Benchmark completed successfully ({elapsed_total}s)')

                # Extract actual benchmark time window from guidellm output
                # guidellm records precise start_time/end_time (epoch) of the
                # active benchmark, excluding data generation and result writing
                metrics_file = None
                if collect_metrics and self.metrics_collector and output_exists:
                    try:
                        import json as _json
                        with open(result_file) as f:
                            guidellm_data = _json.load(f)
                        bench = guidellm_data.get('benchmarks', [{}])[0]
                        gl_start = bench.get('start_time')
                        gl_end = bench.get('end_time')
                        if gl_start and gl_end:
                            metrics_start = datetime.fromtimestamp(gl_start)
                            metrics_end = datetime.fromtimestamp(gl_end)
                            if log_callback:
                                log_callback(f'   Using guidellm benchmark window for metrics: {metrics_start.strftime("%H:%M:%S")} - {metrics_end.strftime("%H:%M:%S")} ({gl_end - gl_start:.0f}s)')
                            metrics_file = self._collect_metrics(
                                config=config,
                                start_time=metrics_start.isoformat(),
                                end_time=metrics_end.isoformat(),
                                log_callback=log_callback
                            )
                        else:
                            if log_callback:
                                log_callback('⚠️  No benchmark timestamps in guidellm output, skipping metrics collection')
                    except Exception as e:
                        logger.warning(f'Failed to parse guidellm timestamps: {e}')
                        if log_callback:
                            log_callback(f'⚠️  Failed to parse guidellm timestamps: {e}')
                elif collect_metrics and not self.metrics_collector:
                    if log_callback:
                        log_callback('⚠️  Metrics collection requested but Thanos URL not configured')

                return True, str(result_file), metrics_file
            else:
                if log_callback:
                    log_callback(f'❌ Benchmark failed: {error_message or "unknown error"}')
                return False, None, None

        except Exception as e:
            error_msg = f"Failed to run guidellm test: {e}"
            logger.error(error_msg)
            if log_callback:
                log_callback(f"❌ {error_msg}")
            return False, None, None

    def _collect_metrics(
        self,
        config: TestConfig,
        start_time: str,
        end_time: str,
        log_callback: Optional[Callable[[str], None]] = None
    ) -> Optional[str]:
        """
        Collect metrics from Prometheus/Thanos.

        Args:
            config: Test configuration
            start_time: Test start time (ISO format)
            end_time: Test end time (ISO format)
            log_callback: Optional callback for logging

        Returns:
            Path to metrics file or None if failed
        """
        if not self.metrics_collector:
            if log_callback:
                log_callback("⚠️ Metrics collector not configured, skipping...")
            return None

        try:
            if log_callback:
                log_callback("📊 Collecting metrics from Prometheus/Thanos...")

            output_dir = Path(f"/mnt/storage/results/{config.test_id}")
            output_dir.mkdir(parents=True, exist_ok=True)
            output_file = output_dir / "metrics.json"

            # Convert ISO format strings to datetime objects
            from datetime import datetime
            start_dt = datetime.fromisoformat(start_time)
            end_dt = datetime.fromisoformat(end_time)

            # Update the pod_name_pattern in the config for this test
            self.metrics_collector.config.pod_name_pattern = config.test_id

            # Collect metrics
            self.metrics_collector.collect_all_metrics(
                start_time=start_dt,
                end_time=end_dt,
                output_file=str(output_file)
            )

            if log_callback:
                log_callback(f"✅ Metrics saved to {output_file}")

            return str(output_file)

        except Exception as e:
            error_msg = f"Failed to collect metrics: {e}"
            logger.error(error_msg)
            if log_callback:
                log_callback(f"❌ {error_msg}")
            return None

    def run_test(
        self,
        config: TestConfig,
        log_callback: Optional[Callable[[str], None]] = None,
        cleanup: bool = True,
        skip_workload: bool = False,
        stop_check: Optional[Callable[[], bool]] = None
    ) -> TestResult:
        """
        Run a complete test for a single configuration.

        Args:
            config: Test configuration
            log_callback: Optional callback for logging
            cleanup: Whether to cleanup deployment after test

        Returns:
            TestResult with test outcome
        """
        if log_callback:
            log_callback(f"\n{'='*70}")
            log_callback(f"🚀 Starting Test: {config.test_id}")
            log_callback(f"   Architecture: {config.architecture}")
            log_callback(f"   Model: {config.model_name}")
            log_callback(f"   TP: {config.tensor_parallelism}")
            if config.architecture == 'pd':
                log_callback(f"   PD Ratio: {config.prefill_decode_ratio}")
            log_callback(f"{'='*70}\n")

        result = TestResult(
            test_id=config.test_id,
            architecture=config.architecture,
            deployment_success=False,
            deployment_ready=False,
            guidellm_success=False,
            metrics_collected=False,
            deployment_start_time=datetime.now().isoformat()
        )

        try:
            # Step 0: Check/Deploy prerequisite infrastructure
            if log_callback:
                log_callback('')
                log_callback('=' * 60)
                log_callback('📋 Step 1: Deploying Prerequisite Infrastructure')
                log_callback('=' * 60)

            from core import PrereqManager
            prereq_mgr = PrereqManager(namespace=self.namespace)

            try:
                # Deploy prerequisites - this will create missing resources and skip existing ones
                success = prereq_mgr.deploy_prereqs(
                    architecture=config.architecture,
                    log_callback=lambda msg: log_callback(msg) if log_callback else None
                )

                if not success:
                    if log_callback:
                        log_callback('')
                        log_callback('❌ Failed to deploy prerequisite infrastructure')
                        log_callback('')
                    result.error_message = "Failed to deploy prerequisite infrastructure"
                    return result

                if log_callback:
                    log_callback('')
                    log_callback('ℹ️  Note: Gateway typically takes 1-2 minutes to become fully healthy')
                    log_callback('   Waiting for gateway to be ready before proceeding...')
            except Exception as e:
                if log_callback:
                    log_callback('')
                    log_callback(f'❌ Failed to deploy prerequisites: {str(e)}')
                    log_callback('')
                result.error_message = f"Failed to deploy prerequisites: {str(e)}"
                return result

            if log_callback:
                log_callback('')
                log_callback('▶️  Prerequisites ready, continuing with inference pod deployment...')
                log_callback('')
                log_callback('=' * 60)
                log_callback('📋 Step 2: Deploying Inference Pods')
                log_callback('=' * 60)
                log_callback('')

            # Step 1a: Clean up any leftover deployment from a previous failed attempt
            # This prevents resume from hitting the same stuck LWS
            existing = self.deployment_manager.get_deployment_status(
                config.test_id, config.architecture
            )
            if existing.deployed:
                if log_callback:
                    log_callback(f"🧹 Cleaning up leftover deployment from previous attempt: {config.test_id}")
                self.deployment_manager.delete_deployment(
                    config.test_id,
                    config.architecture,
                    log_callback=log_callback
                )
                # Wait for all pods to fully terminate before deploying new ones
                # DRA resources (GPU-NIC pairs) are held until pods are gone
                self.deployment_manager.wait_for_pods_terminated(
                    config.test_id,
                    timeout=300,
                    log_callback=log_callback
                )

            # Step 1b: Deploy configuration
            deployment_success = self.deployment_manager.deploy_config(
                config,
                log_callback=log_callback
            )

            result.deployment_success = deployment_success

            if not deployment_success:
                result.error_message = "Deployment failed"
                return result

            # Step 3: Wait for deployment to be ready
            if log_callback:
                log_callback("\n⏳ Step 3: Waiting for deployment to be ready...")

            ready = self.deployment_manager.wait_for_ready(
                config.test_id,
                config.architecture,
                timeout=self.deployment_timeout,
                log_callback=log_callback,
                stop_check=stop_check
            )

            result.deployment_ready = ready
            result.deployment_ready_time = datetime.now().isoformat()

            if not ready:
                result.error_message = "Deployment did not become ready in time"
                return result

            # Step 3b: Wait for vLLM to finish loading the model
            if log_callback:
                log_callback("\n⏳ Step 3b: Waiting for vLLM model loading...")

            model_loaded = self._wait_for_model_loaded(
                config.test_id,
                timeout=self.deployment_timeout,
                log_callback=log_callback,
                stop_check=stop_check
            )

            if not model_loaded:
                result.error_message = "vLLM model did not finish loading in time"
                return result

            # Profile vLLM memory after startup (measures actual fixed overhead)
            gpu_mem_util = getattr(config, 'gpu_memory_utilization', 0.95)
            gpu_vram = getattr(config, 'gpu_vram_gb', None)
            if gpu_vram:
                self._profile_vllm_memory(
                    config.test_id, gpu_mem_util, gpu_vram, result,
                    log_callback=log_callback
                )

            # Step 4: Get service endpoint

            endpoint = self._get_service_endpoint(
                config.test_id,
                config.architecture,
                log_callback=log_callback
            )

            result.service_endpoint = endpoint

            if not endpoint:
                result.error_message = "Failed to get service endpoint"
                return result

            # Step 4b: Wait for gateway to register all pods in EPP
            if config.architecture == 'pd':
                expected_pods = (config.prefill_replicas or 0) + (config.decode_replicas or 0)
            else:
                expected_pods = config.replicas

            if expected_pods > 0:
                if log_callback:
                    p = 'pod' if expected_pods == 1 else 'pods'
                    log_callback(f"\n🔄 Step 4b: Waiting for {expected_pods} {p} to register in EPP inference pool...")

                gateway_ready = self._wait_for_gateway_ready(
                    endpoint=endpoint,
                    config=config,
                    expected_pods=expected_pods,
                    timeout=300,
                    log_callback=log_callback
                )

                if not gateway_ready:
                    result.error_message = "Gateway did not register all pods in time"
                    if log_callback:
                        log_callback("⚠️  Proceeding anyway — some pods may not be routable")

            if skip_workload:
                # Step 5 (Simplified): Curl test only - skip guidellm and metrics
                if log_callback:
                    log_callback("\n🧪 Step 5: Running curl verification test...")

                result.test_start_time = datetime.now().isoformat()

                # Simple curl test to verify endpoint is responding
                curl_success = self._run_curl_test(
                    endpoint,
                    config,
                    log_callback=log_callback
                )

                result.test_end_time = datetime.now().isoformat()
                result.guidellm_success = curl_success

                if curl_success:
                    if log_callback:
                        log_callback("✅ Curl test passed - endpoint is serving")
                else:
                    result.error_message = "Curl test failed - endpoint not responding"
                    if log_callback:
                        log_callback("❌ Curl test failed")

                # Skip metrics collection
                if log_callback:
                    log_callback("\n⏭️  Skipping guidellm workload and metrics collection (validation mode)")

            else:
                # Step 5: Run guidellm test
                if log_callback:
                    log_callback("\n🧪 Step 5: Running guidellm load test...")

                result.test_start_time = datetime.now().isoformat()

                guidellm_success, guidellm_output, metrics_output = self._run_guidellm_test(
                    endpoint,
                    config,
                    log_callback=log_callback
                )

                result.test_end_time = datetime.now().isoformat()
                result.guidellm_success = guidellm_success
                result.guidellm_output = guidellm_output
                result.metrics_output = metrics_output

                # Parse guidellm results and populate metrics
                if guidellm_success and guidellm_output:
                    self._parse_guidellm_results(guidellm_output, result)

                if not guidellm_success:
                    result.error_message = "guidellm test failed"
                    # Continue to cleanup

                # Step 6: Collect metrics (if configured)
                if self.metrics_collector and result.test_start_time and result.test_end_time:
                    if log_callback:
                        log_callback("\n📊 Step 6: Collecting metrics...")

                    metrics_file = self._collect_metrics(
                        config,
                        result.test_start_time,
                        result.test_end_time,
                        log_callback=log_callback
                    )

                    result.metrics_file = metrics_file
                    result.metrics_collected = metrics_file is not None

                # Scan pod logs for critical errors
                from .pod_error_scanner import scan_pod_logs
                if log_callback:
                    log_callback("\n🔍 Scanning pod logs for critical errors...")
                scan_result = scan_pod_logs(self.namespace, config.test_id)
                if scan_result.has_errors:
                    result.pod_errors_detected = True
                    result.pod_errors_json = scan_result.to_json()
                    if log_callback:
                        log_callback(f"🚨 CRITICAL POD ERRORS DETECTED: {scan_result.summary}")
                        for report in scan_result.pod_reports:
                            log_callback(f"   Pod: {report.pod_name}")
                            for err in report.errors[:5]:
                                log_callback(f"      [{err.pattern_name}] {err.line[:150]}")
                        log_callback(f"\n⚠️  Pods left running for investigation:")
                        log_callback(f"   kubectl logs -n {self.namespace} -l llm-d.ai/test-id={config.test_id} -c vllm")

        except Exception as e:
            error_msg = f"Test execution failed: {e}"
            logger.error(error_msg)
            result.error_message = error_msg
            if log_callback:
                log_callback(f"\n❌ {error_msg}")

        finally:
            # Step 7: Cleanup
            # Skip cleanup if pod errors detected — user needs to investigate
            if result.pod_errors_detected:
                if log_callback:
                    log_callback("\n⚠️  Skipping cleanup — pods left running due to critical errors")
                    log_callback(f"🧹 Manual cleanup: kubectl delete lws -n {self.namespace} -l test-id={config.test_id}")
            elif cleanup and result.guidellm_success:
                if log_callback:
                    log_callback("\n🧹 Step 7: Cleaning up deployment...")

                self.deployment_manager.delete_deployment(
                    config.test_id,
                    config.architecture,
                    log_callback=log_callback
                )
                self.deployment_manager.wait_for_pods_terminated(
                    config.test_id,
                    timeout=300,
                    log_callback=log_callback
                )

                result.cleanup_time = datetime.now().isoformat()
            elif cleanup and not result.guidellm_success:
                if log_callback:
                    log_callback("\n⚠️  Test failed - keeping deployment for debugging")
                    log_callback(f"🔍 kubectl logs -n {self.namespace} -l test-id={config.test_id}")
                    log_callback(f"🧹 kubectl delete lws -n {self.namespace} -l test-id={config.test_id}")

        # Final summary
        if log_callback:
            log_callback(f"\n{'='*70}")
            if result.guidellm_success and result.deployment_success:
                log_callback(f"✅ Test completed successfully: {config.test_id}")
            else:
                log_callback(f"❌ Test failed: {config.test_id}")
                if result.error_message:
                    log_callback(f"   Error: {result.error_message}")
            log_callback(f"{'='*70}\n")

        return result

    def run_optimization_plan(
        self,
        plan: OptimizationPlan,
        log_callback: Optional[Callable[[str], None]] = None,
        cleanup_between_tests: bool = True
    ) -> List[TestResult]:
        """
        Run all tests in an optimization plan.

        Args:
            plan: Optimization plan with test configurations
            log_callback: Optional callback for logging
            cleanup_between_tests: Whether to cleanup between tests

        Returns:
            List of TestResult objects
        """
        if log_callback:
            log_callback(f"\n{'#'*70}")
            log_callback(f"# Optimization Run: {plan.run_name}")
            log_callback(f"# Model: {plan.model_name}")
            log_callback(f"# Total Tests: {len(plan.test_configs)}")
            log_callback(f"{'#'*70}\n")

        results = []

        for i, config in enumerate(plan.test_configs, 1):
            if log_callback:
                log_callback(f"\n>>> Test {i}/{len(plan.test_configs)} <<<\n")

            result = self.run_test(
                config,
                log_callback=log_callback,
                cleanup=cleanup_between_tests
            )

            results.append(result)

            # Brief pause between tests for API server to settle
            if cleanup_between_tests and i < len(plan.test_configs):
                time.sleep(2)

        # Final summary
        if log_callback:
            successful = sum(1 for r in results if r.guidellm_success)
            log_callback(f"\n{'#'*70}")
            log_callback("# Optimization Run Complete")
            log_callback(f"# Successful Tests: {successful}/{len(results)}")
            log_callback(f"{'#'*70}\n")

        return results


def main():
    """Main entry point for standalone execution."""
    import argparse

    parser = argparse.ArgumentParser(
        description='Run In-S8 test orchestration'
    )
    parser.add_argument('--plan-file', required=True,
                        help='Path to optimization plan JSON file')
    parser.add_argument('--namespace', default='llm-d',
                        help='Kubernetes namespace')
    parser.add_argument('--thanos-url',
                        help='Thanos/Prometheus URL for metrics collection')
    parser.add_argument('--no-cleanup', action='store_true',
                        help='Do not cleanup deployments after tests')

    args = parser.parse_args()

    # Load optimization plan
    with open(args.plan_file, 'r') as f:
        plan_dict = json.load(f)

    # Reconstruct plan
    from .config_generator import TestConfig, OptimizationPlan, ClusterResources
    test_configs = [TestConfig(**cfg) for cfg in plan_dict['test_configs']]
    cluster_resources = ClusterResources(**plan_dict['cluster_resources'])

    plan = OptimizationPlan(
        run_name=plan_dict['run_name'],
        model_name=plan_dict['model_name'],
        isl=plan_dict['isl'],
        osl=plan_dict['osl'],
        num_users=plan_dict['num_users'],
        optimization_goal=plan_dict['optimization_goal'],
        test_configs=test_configs,
        cluster_resources=cluster_resources,
        created_at=plan_dict['created_at']
    )

    # Run orchestration
    orchestrator = TestOrchestrator(
        namespace=args.namespace,
        thanos_url=args.thanos_url
    )

    results = orchestrator.run_optimization_plan(
        plan,
        log_callback=print,
        cleanup_between_tests=not args.no_cleanup
    )

    # Save results
    results_file = f"results/{plan.run_name}_results.json"
    with open(results_file, 'w') as f:
        json.dump([asdict(r) for r in results], f, indent=2)

    print(f"\n✅ Results saved to {results_file}")


if __name__ == '__main__':
    main()
