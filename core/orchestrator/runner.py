"""TestOrchestrator — main test runner and infrastructure management."""

import os
import sys
import json
import time
import socket
import logging
import subprocess
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Callable, Dict, Any

from core.orchestrator.result import TestResult
from core.config_generator import TestConfig, OptimizationPlan
from core.deployment_manager import DeploymentManager
from core.metrics_collector import MetricsCollector, MetricsConfig
from core.orchestrator.parser import ParserMixin
from core.orchestrator.guidellm import GuidellmMixin

logger = logging.getLogger(__name__)


class TestOrchestrator(ParserMixin, GuidellmMixin):
    """Orchestrates test deployment, benchmarking, and result collection."""

    def __init__(
        self,
        namespace: str = 'serveit',
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
        self._pf_process = None

        self.deployment_manager = DeploymentManager(
            namespace=namespace,
            kubeconfig=kubeconfig
        )

        # Enable namespace monitoring for metrics collection (OpenShift only)
        self._enable_namespace_monitoring(namespace)

        # Auto-discover Thanos URL if not provided
        if thanos_url is None:
            thanos_url = self._get_thanos_url()

        # Fallback: port-forward to remote Prometheus on vanilla K8s
        if thanos_url is None:
            thanos_url = self._start_prometheus_port_forward(kubeconfig)

        self.metrics_collector = None
        if thanos_url:
            logger.info(f"Initializing MetricsCollector with Thanos URL: {thanos_url}")

            # Port-forwarded Prometheus doesn't need auth tokens
            token = None
            if not thanos_url.startswith('http://localhost'):
                # For remote clusters, use the token from kubeconfig (not local SA)
                try:
                    r = subprocess.run(
                        ['kubectl', 'config', 'view', '--raw', '-o', 'jsonpath={.users[0].user.token}'],
                        capture_output=True, text=True, timeout=5
                    )
                    if r.returncode == 0 and r.stdout.strip():
                        token = r.stdout.strip()
                        logger.info("Loaded token from kubeconfig for Thanos authentication")
                except Exception:
                    pass
                # Fallback to local SA token
                if not token:
                    token_file = '/run/secrets/kubernetes.io/serviceaccount/token'
                    if os.path.exists(token_file):
                        try:
                            with open(token_file, 'r') as f:
                                token = f.read().strip()
                            logger.info("Loaded local SA token for Thanos authentication")
                        except Exception as e:
                            logger.warning(f"Failed to read service account token: {e}")

            metrics_config = MetricsConfig(
                thanos_url=thanos_url,
                namespace=namespace,
                pod_name_pattern='',
                step_seconds=5,
                token=token
            )
            self.metrics_collector = MetricsCollector(metrics_config)
        else:
            logger.warning("No Thanos URL provided - metrics collection will be disabled")

    def _start_prometheus_port_forward(self, kubeconfig: Optional[str]) -> Optional[str]:
        """Start kubectl port-forward to Prometheus on a remote cluster."""
        kubeconfig = kubeconfig or os.environ.get('KUBECONFIG')
        if not kubeconfig:
            return None

        s = socket.socket()
        s.bind(('', 0))
        port = s.getsockname()[1]
        s.close()

        env = os.environ.copy()
        env['KUBECONFIG'] = os.path.expanduser(kubeconfig)

        try:
            self._pf_process = subprocess.Popen(
                ['kubectl', 'port-forward', '-n', 'monitoring',
                 'svc/prometheus', f'{port}:9090'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=env
            )

            import urllib.request
            for _ in range(10):
                time.sleep(1)
                if self._pf_process.poll() is not None:
                    logger.warning("Prometheus port-forward exited early")
                    self._pf_process = None
                    return None
                try:
                    r = urllib.request.urlopen(
                        f'http://localhost:{port}/api/v1/status/config', timeout=2)
                    if r.status == 200:
                        url = f'http://localhost:{port}'
                        logger.info(f"Port-forwarding to Prometheus at {url}")
                        return url
                except Exception:
                    continue

            logger.warning("Prometheus port-forward: could not connect within 10s")
            self._pf_process.terminate()
            self._pf_process = None
        except Exception as e:
            logger.warning(f"Failed to start Prometheus port-forward: {e}")
            self._pf_process = None
        return None

    def cleanup(self):
        """Clean up resources (port-forwards, etc.)."""
        if self._pf_process and self._pf_process.poll() is None:
            logger.info("Stopping Prometheus port-forward")
            self._pf_process.terminate()
            try:
                self._pf_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._pf_process.kill()
            self._pf_process = None

    def cleanup_deployment(self, config: TestConfig, log_callback=None):
        """Clean up a test deployment (LWS + services). Used by sweeps to clean up after all levels."""
        if log_callback:
            log_callback(f"🧹 Cleaning up deployment: {config.test_id}")
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
        Run a curl test via the workload pod to verify the gateway endpoint.
        Uses kubectl exec so it works for both local and remote clusters.
        """
        try:
            if log_callback:
                log_callback(f"🔍 Testing endpoint: {endpoint}")

            def _remote_curl(url, method='GET', data=None, timeout=10):
                if data:
                    cmd = f"curl -s -o /dev/null -w '%{{http_code}}' --connect-timeout {timeout} -X {method} -H 'Content-Type: application/json' -d '{data}' '{url}'"
                else:
                    cmd = f"curl -s -o /dev/null -w '%{{http_code}}' --connect-timeout {timeout} '{url}'"
                r = self.deployment_manager.kubectl.run(
                    ['exec', self._guidellm_pod_name, '-n', self.namespace, '--', 'bash', '-c', cmd],
                    check=False
                )
                return r.stdout.strip() if r.returncode == 0 else '000'

            # Try models endpoint
            models_url = f"{endpoint}/models"
            if log_callback:
                log_callback(f"   Checking models endpoint: {models_url}")
            code = _remote_curl(models_url)
            if code == '200':
                if log_callback:
                    log_callback(f"   ✅ Models endpoint passed: {code}")
                return True
            elif log_callback:
                log_callback(f"   ⚠️  Models endpoint returned: {code}")

            # Try health endpoint
            health_url = endpoint.replace('/v1', '/health')
            if log_callback:
                log_callback(f"   Checking health endpoint: {health_url}")
            code = _remote_curl(health_url)
            if code == '200':
                if log_callback:
                    log_callback(f"   ✅ Health check passed: {code}")
                return True
            elif log_callback:
                log_callback(f"   ⚠️  Health endpoint returned: {code}")

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
    ) -> int:
        """Wait for all vLLM pods to finish loading the model by checking logs
        for 'Application startup complete'."""
        start_time = time.time()
        ready_pods = set()

        while time.time() - start_time < timeout:
            if stop_check and stop_check():
                if log_callback:
                    log_callback("🛑 Model load wait cancelled — optimization stopped")
                return -1

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

                    # Primary: check logs for startup message (use large tail so
                    # metrics scrape lines don't push the startup message out)
                    log_result = subprocess.run(
                        ['kubectl', 'logs', pod_name, '-n', self.namespace,
                         '-c', 'vllm', '--tail=2000'],
                        capture_output=True, text=True, timeout=30, check=False
                    )
                    startup_in_logs = 'Application startup complete' in log_result.stdout

                    # Fallback: if log scraping missed it, query /health directly
                    if not startup_in_logs:
                        health_result = subprocess.run(
                            ['kubectl', 'exec', pod_name, '-n', self.namespace,
                             '-c', 'vllm', '--',
                             'curl', '-sf', '--max-time', '5',
                             'http://localhost:8000/health'],
                            capture_output=True, text=True, timeout=15, check=False
                        )
                        startup_in_logs = health_result.returncode == 0

                    if startup_in_logs:
                        ready_pods.add(pod_name)
                        if log_callback:
                            log_callback(f"   {pod_name}: model loaded ({len(ready_pods)}/{len(pod_names)})")

                if len(ready_pods) >= len(pod_names) and len(pod_names) > 0:
                    elapsed = int(time.time() - start_time)
                    if log_callback:
                        n = len(pod_names)
                        log_callback(f"   {'Pod has' if n == 1 else f'All {n} pods have'} model loaded ({elapsed}s)")
                    return elapsed

                # Check for CrashLoopBackOff — early exit on OOM during loading
                for pod_name in pod_names:
                    if pod_name in ready_pods:
                        continue
                    phase_r = subprocess.run(
                        ['kubectl', 'get', 'pod', pod_name, '-n', self.namespace,
                         '-o', 'jsonpath={.status.containerStatuses[0].state.waiting.reason}'],
                        capture_output=True, text=True, timeout=10, check=False)
                    reason = phase_r.stdout.strip()
                    if reason in ('CrashLoopBackOff', 'Error'):
                        crash_logs = subprocess.run(
                            ['kubectl', 'logs', pod_name, '-n', self.namespace,
                             '-c', 'vllm', '--previous', '--tail=50'],
                            capture_output=True, text=True, timeout=15, check=False)
                        crash_text = crash_logs.stdout or ''
                        oom_keywords = ['out of memory', 'no available memory for the cache blocks',
                                        'cuda out of memory', 'oom']
                        if any(k in crash_text.lower() for k in oom_keywords):
                            if log_callback:
                                log_callback(f"   {pod_name}: OOM during model loading")
                            return -2
                        if log_callback:
                            log_callback(f"   {pod_name}: crashed during model loading ({reason})")
                        return -1

            except Exception as e:
                logger.warning(f"Failed to check model loading: {e}")

            time.sleep(15)

        elapsed = int(time.time() - start_time)
        if log_callback:
            log_callback(f"   Timeout after {elapsed}s waiting for model to load")
        return -1

    def _are_overload_errors(self, guidellm_output: str) -> bool:
        """Check if all sampled errors are 503/disconnect (overload, not infra failure)."""
        try:
            import json as _json
            data = _json.loads(guidellm_output)
            for bm in data.get('benchmarks', []):
                reqs = bm.get('requests', {})
                errored = reqs.get('errored', [])
                if not errored:
                    return True
                overload_patterns = ['503', 'service unavailable', 'disconnect',
                                     'jsondecodeerror', 'unterminated string',
                                     'remotedisconnected', 'readtimeout']
                for e in errored:
                    info = e.get('info', {})
                    err = str(info.get('error', '')).lower()
                    if not any(p in err for p in overload_patterns):
                        return False
                return True
        except Exception:
            return False

    def _collect_pod_timings(self, test_id: str, log_callback=None) -> Optional[dict]:
        """Collect per-pod creation time and model load time.

        Uses pod startTime and 'Application startup complete' log timestamp
        to compute how long each pod took from creation to serving.
        """
        from datetime import datetime as _dt
        kubectl = self.deployment_manager.kubectl
        try:
            r = kubectl.run(
                ['get', 'pods', '-n', self.namespace,
                 '-l', f'llm-d.ai/test-id={test_id}',
                 '-o', 'jsonpath={range .items[*]}{.metadata.name}|{.status.startTime}\\n{end}'],
                check=False
            )
            if r.returncode != 0 or not r.stdout.strip():
                return None

            pod_starts = {}
            for line in r.stdout.strip().splitlines():
                parts = line.split('|')
                if len(parts) == 2 and parts[0] and parts[1]:
                    pod_starts[parts[0]] = parts[1]

            timings = {}
            for pod_name, start_str in pod_starts.items():
                start_time = _dt.fromisoformat(start_str.replace('Z', '+00:00'))

                log_r = kubectl.run(
                    ['logs', pod_name, '-n', self.namespace,
                     '-c', 'vllm', '--timestamps', '--tail=200'],
                    check=False
                )
                model_ready_time = None
                if log_r.returncode == 0:
                    for log_line in log_r.stdout.splitlines():
                        if 'Application startup complete' in log_line:
                            ts = log_line.split(' ')[0].rstrip('Z')
                            try:
                                if '.' in ts and len(ts.split('.')[-1]) > 6:
                                    ts = ts[:ts.index('.') + 7]
                                model_ready_time = _dt.fromisoformat(ts + '+00:00')
                            except Exception:
                                pass
                            break

                pod_info = {'start_time': start_str}
                if model_ready_time:
                    load_s = int((model_ready_time - start_time).total_seconds())
                    pod_info['model_load_s'] = load_s
                    pod_info['ready_time'] = model_ready_time.isoformat()

                timings[pod_name] = pod_info

            if timings and log_callback:
                load_times = [v['model_load_s'] for v in timings.values() if 'model_load_s' in v]
                if load_times:
                    log_callback(f"   📊 Pod load times: min={min(load_times)}s, max={max(load_times)}s, avg={sum(load_times)//len(load_times)}s ({len(load_times)} pods)")

            return timings
        except Exception as e:
            logger.warning(f"Failed to collect pod timings: {e}")
            return None

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
                result = self.deployment_manager.kubectl.run(
                    ['get', 'pods', '-n', self.namespace,
                     '-l', f'llm-d.ai/test-id={config.test_id}',
                     '-o', 'json'],
                    check=False
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
            # Use kubectl exec on workload pod to reach the gateway (works for both local and remote clusters)
            try:
                models_url = endpoint.rstrip('/') + '/v1/models'
                curl_cmd = f"curl -s -o /dev/null -w '%{{http_code}}' --connect-timeout 5 '{models_url}'"
                r = self.deployment_manager.kubectl.run(
                    ['exec', self._guidellm_pod_name, '-n', self.namespace, '--', 'bash', '-c', curl_cmd],
                    check=False
                )
                http_code = r.stdout.strip() if r.returncode == 0 else '000'
                if http_code != '200':
                    if log_callback and not getattr(self, '_pool_wait_logged', False):
                        log_callback(f"   EPP gateway check: waiting for pool registration... (HTTP {http_code})")
                        self._pool_wait_logged = True
                    time.sleep(5)
                    continue
                elif not models_ok:
                    models_ok = True
                    if log_callback:
                        log_callback("   EPP gateway check: models endpoint OK, verifying routing...")
            except Exception as e:
                if log_callback and not getattr(self, '_gw_wait_logged', False):
                    log_callback(f"   EPP gateway check: waiting for gateway... (error: {e})")
                    self._gw_wait_logged = True
                time.sleep(5)
                continue

            # Step 3: Send a test completion to verify full routing through the pool
            try:
                turns = getattr(config, 'turns', 1) or 1
                if turns > 1:
                    completion_url = endpoint.rstrip('/') + '/v1/chat/completions'
                    payload_json = json.dumps({
                        "model": config.model_name,
                        "messages": [{"role": "user", "content": "Hello"}],
                        "max_tokens": 1,
                        "temperature": 0.0
                    })
                else:
                    completion_url = endpoint.rstrip('/') + '/v1/completions'
                    payload_json = json.dumps({
                        "model": config.model_name,
                        "prompt": "Hello",
                        "max_tokens": 1,
                        "temperature": 0.0
                    })
                curl_cmd = f"curl -s -w '\\n%{{http_code}}' --connect-timeout 10 -X POST -H 'Content-Type: application/json' -d '{payload_json}' '{completion_url}'"
                r = self.deployment_manager.kubectl.run(
                    ['exec', self._guidellm_pod_name, '-n', self.namespace, '--', 'bash', '-c', curl_cmd],
                    check=False
                )
                lines = r.stdout.strip().split('\n') if r.returncode == 0 else []
                http_code = lines[-1] if lines else '000'
                resp_body = '\n'.join(lines[:-1]) if len(lines) > 1 else ''

                # Simulate response object for downstream code
                class _RemoteResp:
                    def __init__(self, code, body):
                        self.status_code = int(code) if code.isdigit() else 0
                        self._body = body
                    def json(self):
                        return json.loads(self._body)
                resp = _RemoteResp(http_code, resp_body)
                if resp.status_code == 200:
                    elapsed = int(time.time() - start_time)
                    if log_callback:
                        p = 'pod' if ready_count == 1 else 'pods'
                        log_callback(f"   ✅ EPP ready — {ready_count} {p} registered in inference pool ({elapsed}s)")
                    return True
                else:
                    elapsed = int(time.time() - start_time)
                    if log_callback and elapsed % 30 < 6:
                        log_callback(f"   EPP pool registration: pods not yet routable (HTTP {resp.status_code}, {elapsed}s elapsed)")
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
                        gateway_class = gateway.get('spec', {}).get('gatewayClassName', 'istio')
                        if gateway_class == 'istio':
                            # Upstream Istio: creates service in same namespace with -istio suffix
                            svc = f'{gateway_name}-istio.{namespace}.svc.cluster.local'
                        else:
                            # Other gateway controllers (data-science, openshift-default):
                            # create service in same namespace as the Gateway resource
                            svc = f'{gateway_name}-{gateway_class}.{namespace}.svc.cluster.local'
                        service_url = f'http://{svc}'
                        logger.debug(f'Using gateway: {svc} (class: {gateway_class})')
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

    def _clear_gpu_cache(self, test_id: str, log_callback=None):
        """Clear GPU cache on all serving pods for a test (parallel)."""
        try:
            result = subprocess.run(
                ['kubectl', 'get', 'pods', '-n', self.namespace,
                 '-l', f'llm-d.ai/test-id={test_id}',
                 '-o', 'jsonpath={range .items[*]}{.metadata.name}{" "}{end}'],
                capture_output=True, text=True, timeout=15, check=False
            )
            pod_names = [p for p in result.stdout.strip().split() if p]
            if not pod_names:
                return

            procs = []
            for pod_name in pod_names:
                p = subprocess.Popen(
                    ['kubectl', 'exec', pod_name, '-n', self.namespace,
                     '-c', 'vllm', '--',
                     'python3', '-c', 'import torch; torch.cuda.empty_cache()'],
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
                )
                procs.append((pod_name, p))

            for pod_name, p in procs:
                p.wait(timeout=30)
                if p.returncode != 0:
                    logger.debug(f"GPU cache clear failed on {pod_name}: {p.stderr.read().decode()}")

            if log_callback:
                log_callback(f"   🧹 GPU cache cleared on {len(pod_names)} pod(s)")
        except Exception as e:
            logger.debug(f"Failed to clear GPU cache: {e}")

    # ── Persistent guidellm pod management ─────────────────────────────────


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

            # Update the pod_name_pattern — per_node_storage uses stable LWS names
            if getattr(config, 'per_node_storage', False):
                if config.architecture == 'aggregated':
                    self.metrics_collector.config.pod_name_pattern = f'aggregated-tp{config.tensor_parallelism}'
                else:
                    self.metrics_collector.config.pod_name_pattern = f'(prefill-tp{getattr(config, "prefill_tp", config.tensor_parallelism)}|decode-tp{getattr(config, "decode_tp", config.tensor_parallelism)})'
            else:
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

    def _archive_test_artifacts(
        self,
        config: TestConfig,
        result,
        log_callback: Optional[Callable[[str], None]] = None
    ):
        """Copy all test artifacts to an organized directory on the instance PVC.

        Structure: /mnt/storage/test-artifacts/{run_name}/{test_id}/
            guidellm-raw.json      — Raw guidellm output from workload pod
            metrics-prometheus.json — Prometheus/Thanos metrics (if collected)
            test-config.json       — Full TestConfig used for this test
            test-result.json       — Parsed results summary
            manifests.yaml         — K8s manifests deployed
        """
        try:
            artifact_dir = Path(f"/mnt/storage/test-artifacts/{config.test_id}")
            artifact_dir.mkdir(parents=True, exist_ok=True)

            # 1. Copy guidellm raw JSON from workload pod with MD5 verification
            remote_path = f"/mnt/storage/tmp/guidellm-{config.test_id}.json"
            local_raw = artifact_dir / "guidellm-raw.json"
            try:
                kubectl = self.deployment_manager.kubectl
                env = os.environ.copy()
                env['KUBECONFIG'] = os.path.expanduser(kubectl.kubeconfig)
                import subprocess as _sp
                import hashlib

                # Get remote MD5
                md5_r = kubectl.run(
                    ['exec', self._guidellm_pod_name, '-n', self.namespace, '--',
                     'md5sum', remote_path], check=False)
                remote_md5 = md5_r.stdout.strip().split()[0] if md5_r.returncode == 0 else None

                max_retries = 3
                for attempt in range(max_retries):
                    _sp.run(
                        [kubectl.kubectl_cmd, 'cp',
                         f'{self._guidellm_pod_name}:{remote_path}',
                         str(local_raw), '-n', self.namespace],
                        env=env, check=False, timeout=120
                    )
                    if not local_raw.exists() or local_raw.stat().st_size == 0:
                        continue

                    if remote_md5:
                        local_md5 = hashlib.md5(local_raw.read_bytes()).hexdigest()
                        if local_md5 == remote_md5:
                            if log_callback:
                                log_callback(f"   📁 Archived guidellm raw output ({local_raw.stat().st_size // 1024}KB, MD5 verified)")
                            break
                        else:
                            if log_callback:
                                log_callback(f"   ⚠️  MD5 mismatch (attempt {attempt+1}/{max_retries}), retrying copy...")
                            local_raw.unlink(missing_ok=True)
                    else:
                        if log_callback:
                            log_callback(f"   📁 Archived guidellm raw output ({local_raw.stat().st_size // 1024}KB)")
                        break
                else:
                    if log_callback:
                        log_callback(f"   ❌ Failed to copy guidellm output after {max_retries} attempts")
                    local_raw.unlink(missing_ok=True)
            except Exception:
                pass

            # 2. Copy Prometheus metrics if collected
            if result.metrics_file and Path(result.metrics_file).exists():
                import shutil
                shutil.copy2(result.metrics_file, artifact_dir / "metrics-prometheus.json")

            # 3. Save test config
            from dataclasses import asdict
            tc_dict = asdict(config)
            for key in ('hf_token', 'selected_nodes'):
                tc_dict.pop(key, None)
            with open(artifact_dir / "test-config.json", 'w') as f:
                json.dump(tc_dict, f, indent=2, default=str)

            # 4. Save parsed results summary
            result_summary = {
                'test_id': config.test_id,
                'architecture': config.architecture,
                'ttft_p50': result.ttft_p50, 'ttft_p90': result.ttft_p90,
                'ttft_p95': result.ttft_p95, 'ttft_p99': result.ttft_p99,
                'throughput_p50': result.throughput_p50, 'throughput_p90': result.throughput_p90,
                'throughput_p95': result.throughput_p95, 'throughput_p99': result.throughput_p99,
                'itl_p50': result.itl_p50, 'itl_p90': result.itl_p90,
                'tpot_p50': result.tpot_p50, 'tpot_p90': result.tpot_p90,
                'request_total': result.request_total,
                'request_successful': result.request_successful,
                'request_errored': result.request_errored,
                'guidellm_success': result.guidellm_success,
                'error_message': result.error_message,
                'test_start_time': result.test_start_time,
                'test_end_time': result.test_end_time,
            }
            with open(artifact_dir / "test-result.json", 'w') as f:
                json.dump(result_summary, f, indent=2, default=str)

            # 5. Save manifests
            if hasattr(result, 'manifests') and result.manifests:
                with open(artifact_dir / "manifests.yaml", 'w') as f:
                    if isinstance(result.manifests, dict):
                        for name, yaml_content in result.manifests.items():
                            f.write(f"---\n# {name}\n{yaml_content}\n")
                    else:
                        f.write(str(result.manifests))

        except Exception as e:
            logger.warning(f"Failed to archive test artifacts: {e}")

    def run_test(
        self,
        config: TestConfig,
        log_callback: Optional[Callable[[str], None]] = None,
        cleanup: bool = True,
        skip_workload: bool = False,
        stop_check: Optional[Callable[[], bool]] = None,
        skip_prereqs: bool = False,
        skip_deploy: bool = False
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
            if config.architecture in ('pd', 'ep') and (config.prefill_tp or config.decode_tp):
                log_callback(f"   Prefill TP: {config.prefill_tp or config.tensor_parallelism}, Decode TP: {config.decode_tp or config.tensor_parallelism}")
                log_callback(f"   Prefill Pods: {config.prefill_replicas}, Decode Pods: {config.decode_replicas}")
            else:
                log_callback(f"   TP: {config.tensor_parallelism}")
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
            # Early stop check — catches stop pressed right after starting
            if stop_check and stop_check():
                if log_callback:
                    log_callback("🛑 Optimization stopped before test started")
                result.error_message = "Stopped by user"
                return result

            # Step 0: Check/Deploy prerequisite infrastructure
            if not skip_prereqs and not skip_deploy:
                if log_callback:
                    log_callback('')
                    log_callback('=' * 60)
                    log_callback('📋 Step 1: Deploying Prerequisite Infrastructure')
                    log_callback('=' * 60)

                from core import PrereqManager
                prereq_mgr = PrereqManager(
                    namespace=self.namespace,
                    kubectl_runner=self.deployment_manager.kubectl,
                    scheduler_image=getattr(config, 'scheduler_image', None),
                    gateway_class=getattr(config, 'gateway_class', 'istio'),
                )

                try:
                    success = prereq_mgr.deploy_prereqs(
                        architecture=config.architecture,
                        log_callback=lambda msg: log_callback(msg) if log_callback else None,
                        epp_config=getattr(config, 'epp_config', None),
                        optimizer_config=config
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

            if stop_check and stop_check():
                if log_callback:
                    log_callback("🛑 Optimization stopped before pod deployment")
                result.error_message = "Stopped by user"
                return result

            if skip_deploy:
                if log_callback:
                    log_callback('⏩ Reusing existing deployment (sweep mode)')
                result.deployment_success = True
                result.deployment_ready = True

            if not skip_deploy:
                if log_callback:
                    log_callback('')
                    if skip_prereqs:
                        log_callback('⏩ Skipping prerequisite deployment (reusing existing)')
                    else:
                        log_callback('▶️  Prerequisites ready, continuing with inference pod deployment...')
                    log_callback('')
                    log_callback('=' * 60)
                    log_callback('📋 Step 2: Deploying Inference Pods')
                    log_callback('=' * 60)
                    log_callback('')

            # Step 1a: Check if existing deployment is already serving
            # Reuse running pods to avoid 30+ min model reload on large models
            if not skip_deploy:
                existing = self.deployment_manager.get_deployment_status(
                    config.test_id, config.architecture
                )
                if existing.deployed:
                    # Check if ALL pods are serving
                    r = subprocess.run(
                        ['kubectl', 'get', 'pods', '-n', self.namespace,
                         '-l', f'llm-d.ai/test-id={config.test_id}',
                         '-o', 'jsonpath={range .items[*]}{.metadata.name}{" "}{end}'],
                        capture_output=True, text=True, timeout=15, check=False)
                    pod_names = [p for p in r.stdout.strip().split() if p]
                    all_serving = len(pod_names) > 0
                    for pn in pod_names:
                        log_r = subprocess.run(
                            ['kubectl', 'logs', pn, '-n', self.namespace,
                             '-c', 'vllm', '--tail=2000'],
                            capture_output=True, text=True, timeout=30, check=False)
                        if 'Application startup complete' not in log_r.stdout:
                            all_serving = False
                            break

                    if all_serving:
                        if log_callback:
                            log_callback(f"♻️  Reusing {len(pod_names)} existing serving pod(s) — skipping deployment")
                        skip_deploy = True
                        result.deployment_success = True
                        result.deployment_ready = True
                    else:
                        if log_callback:
                            log_callback(f"🧹 Cleaning up leftover deployment (not all pods serving): {config.test_id}")
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

            if not skip_deploy:
                deployment_success = self.deployment_manager.deploy_config(
                    config,
                    log_callback=log_callback
                )

                result.deployment_success = deployment_success

                if not deployment_success:
                    result.error_message = "Deployment failed"
                    return result

                if stop_check and stop_check():
                    if log_callback:
                        log_callback("🛑 Optimization stopped — cleaning up deployed pods")
                    result.error_message = "Stopped by user"
                    return result

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

                if log_callback:
                    log_callback("\n⏳ Step 3b: Waiting for vLLM model loading...")

                model_load_time = self._wait_for_model_loaded(
                    config.test_id,
                    timeout=self.deployment_timeout,
                    log_callback=log_callback,
                    stop_check=stop_check
                )

                if model_load_time == -2:
                    result.error_message = "OOM: No available memory for the cache blocks"
                    return result
                if model_load_time < 0:
                    result.error_message = "vLLM model did not finish loading in time"
                    return result

                result.model_load_time_s = model_load_time

                pod_timings = self._collect_pod_timings(config.test_id, log_callback=log_callback)
                if pod_timings:
                    result.pod_timings = pod_timings

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

            # Ensure workload pod exists (needed for gateway health checks via kubectl exec)
            self.ensure_guidellm_pod(config, log_callback=log_callback)

            # Step 4b: Wait for gateway to register all pods in EPP (skip if reusing deployment)
            if skip_deploy:
                expected_pods = 0
            elif config.architecture == 'pd':
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
                        log_callback("❌ EPP gateway failed to register all pods — aborting test")
                    return result

            if stop_check and stop_check():
                if log_callback:
                    log_callback("🛑 Optimization stopped before benchmark")
                result.error_message = "Stopped by user"
                return result

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
                # Step 5: Run guidellm test (retry up to 3 times on 2-4% error rate)
                max_guidellm_retries = 3
                for guidellm_attempt in range(1, max_guidellm_retries + 1):
                    if stop_check and stop_check():
                        if log_callback:
                            log_callback("🛑 Optimization stopped")
                        result.error_message = "Stopped by user"
                        return result

                    if guidellm_attempt > 1:
                        if log_callback:
                            log_callback(f"\n🔄 Retrying guidellm (attempt {guidellm_attempt}/{max_guidellm_retries}) — pods still running")

                    if log_callback:
                        log_callback("\n🧪 Step 5: Running guidellm load test...")

                    result.test_start_time = datetime.now().isoformat()

                    use_job = os.environ.get('GUIDELLM_USE_JOB', 'true').lower() == 'true'
                    if use_job:
                        guidellm_success, guidellm_output, metrics_output = self._run_guidellm_job(
                            endpoint, config, log_callback=log_callback,
                            stop_check=stop_check,
                        )
                    else:
                        guidellm_success, guidellm_output, metrics_output = self._run_guidellm_test(
                            endpoint, config, log_callback=log_callback,
                        )

                    result.test_end_time = datetime.now().isoformat()
                    result.guidellm_success = guidellm_success
                    result.guidellm_output = guidellm_output
                    result.metrics_output = metrics_output

                    if guidellm_success and guidellm_output:
                        self._parse_guidellm_results(guidellm_output, result)

                    if not result.guidellm_success or (result.request_successful or 0) == 0:
                        if guidellm_attempt < max_guidellm_retries:
                            if log_callback:
                                log_callback(f"⚠️  guidellm failed ({result.request_successful or 0} completed requests) — retrying ({guidellm_attempt}/{max_guidellm_retries})")
                            import time; time.sleep(10)
                            continue
                        result.error_message = "guidellm test failed after retries"
                        break

                    total = result.request_total or 0
                    errored = result.request_errored or 0
                    if total > 0 and errored > 0:
                        error_pct = errored / total * 100
                        if error_pct > 2.0 and guidellm_attempt < max_guidellm_retries:
                            # Check if errors are all overload/disconnection (503, DC)
                            if self._are_overload_errors(guidellm_output):
                                if log_callback:
                                    log_callback(f"⚠️  Error rate {error_pct:.1f}% ({errored}/{total}) — all 503/disconnect errors, config at capacity")
                                result.guidellm_retries = guidellm_attempt - 1
                                break
                            if log_callback:
                                log_callback(f"⚠️  Error rate {error_pct:.1f}% ({errored}/{total}) — retrying guidellm")
                            result.guidellm_retries = guidellm_attempt
                            continue
                    result.guidellm_retries = guidellm_attempt - 1
                    break

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

                # Archive test artifacts to PVC for MLflow/analysis
                self._archive_test_artifacts(config, result, log_callback=log_callback)

                # Scan pod logs for critical errors
                try:
                    from core.pod_error_scanner import scan_pod_logs
                    if log_callback:
                        log_callback("\n🔍 Scanning pod logs for critical errors...")
                    scan_result = scan_pod_logs(self.namespace, config.test_id)
                    if scan_result.nixl_error_count > 0:
                        result.nixl_errors = scan_result.nixl_error_count
                    if scan_result.has_errors:
                        result.pod_errors_json = scan_result.to_json()
                        if scan_result.has_critical_errors:
                            result.pod_errors_detected = True
                            if log_callback:
                                log_callback(f"🚨 CRITICAL POD ERRORS DETECTED: {scan_result.summary}")
                                for report in scan_result.pod_reports:
                                    log_callback(f"   Pod: {report.pod_name}")
                                    for err in report.errors[:5]:
                                        log_callback(f"      [{err.pattern_name}] {err.line[:150]}")
                                log_callback(f"\n⚠️  Pods left running for investigation:")
                                log_callback(f"   kubectl logs -n {self.namespace} -l llm-d.ai/test-id={config.test_id} -c vllm")
                        else:
                            if log_callback:
                                log_callback(f"⚠️  Pod warnings: {scan_result.summary}")
                                if scan_result.nixl_error_count > 0:
                                    log_callback(f"   NIXL transfer errors: {scan_result.nixl_error_count} (non-critical, vLLM retries automatically)")
                except ImportError:
                    pass

                # Clear GPU cache on all serving pods after each test
                self._clear_gpu_cache(config.test_id, log_callback=log_callback)

        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            error_msg = f"Test execution failed: {e}"
            logger.error(f"{error_msg}\n{tb}")
            result.error_message = error_msg
            if log_callback:
                log_callback(f"\n❌ {error_msg}")
                log_callback(f"📋 Full traceback:\n{tb}")

        finally:
            # Step 7: Cleanup
            # Skip cleanup if pod errors or high request error rate — user needs to investigate
            high_error_rate = False

            if result.pod_errors_detected:
                if log_callback:
                    log_callback("\n⚠️  Skipping cleanup — pods left running due to critical errors")
                    log_callback(f"🧹 Manual cleanup: kubectl delete lws -n {self.namespace} -l test-id={config.test_id}")
            elif high_error_rate:
                if log_callback:
                    log_callback(f"\n⚠️  Skipping cleanup — high request error rate ({result.request_errored}/{result.request_total} errored)")
                    log_callback(f"   Pods left running for investigation.")
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
                    log_callback("\n⚠️  Test failed — deployment left running for investigation")
                    log_callback(f"🔍 kubectl logs -n {self.namespace} -l test-id={config.test_id} -c vllm --tail=50")
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
        description='Run ServeIt Studio test orchestration'
    )
    parser.add_argument('--plan-file', required=True,
                        help='Path to optimization plan JSON file')
    parser.add_argument('--namespace', default='serveit',
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
    from core.config_generator import TestConfig, OptimizationPlan, ClusterResources
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
