"""Guidellm execution — persistent pod management and benchmark running."""

import os
import time
import subprocess
import logging
from pathlib import Path
from typing import Optional, Callable

from core.config_generator import TestConfig

logger = logging.getLogger(__name__)


class GuidellmMixin:
    """Mixin providing guidellm pod management and benchmark execution."""

    _guidellm_pod_name = 'inferecipe-workload'

    def ensure_guidellm_pod(self, config: TestConfig, log_callback=None):
        """Deploy the persistent guidellm pod if not already running."""
        kubectl = self.deployment_manager.kubectl

        # Check if already running
        r = kubectl.run(['get', 'pod', self._guidellm_pod_name, '-n', self.namespace,
                         '-o', 'jsonpath={.status.phase}'], check=False)
        if r.returncode == 0 and r.stdout.strip() == 'Running':
            return True

        if log_callback:
            log_callback('📦 Deploying guidellm benchmark pod...')

        # Clean up any stale pod
        kubectl.run(['delete', 'pod', self._guidellm_pod_name, '-n', self.namespace,
                     '--ignore-not-found=true'], check=False)
        time.sleep(3)

        from core.template_manager import TemplateManager
        tmgr = TemplateManager()
        pod_yaml = tmgr.render_template('benchmark/guidellm-pod.yaml.j2',
            namespace=self.namespace,
            image=os.environ.get('GUIDELLM_IMAGE', 'quay.io/bbenshab/vllm:inferecipe'),
            pvc_name=self._get_storage_pvc_name(config),
            hf_token=os.environ.get('HF_TOKEN', ''),
        )

        result = kubectl.run(['apply', '-f', '-', '-n', self.namespace], input_data=pod_yaml)
        if result.returncode != 0:
            if log_callback:
                log_callback(f'❌ Failed to create guidellm pod: {result.stderr}')
            return False

        # Wait for pod to be running
        for _ in range(120):
            r = kubectl.run(['get', 'pod', self._guidellm_pod_name, '-n', self.namespace,
                             '-o', 'jsonpath={.status.phase}'], check=False)
            phase = r.stdout.strip() if r.returncode == 0 else ''
            if phase == 'Running':
                if log_callback:
                    log_callback(f'✅ Guidellm pod ready')
                return True
            if phase in ('Failed', 'Error'):
                if log_callback:
                    log_callback(f'❌ Guidellm pod failed: {phase}')
                return False
            time.sleep(5)

        if log_callback:
            log_callback('❌ Guidellm pod did not start within 10 minutes')
        return False

    def teardown_guidellm_pod(self, log_callback=None):
        """Delete the persistent guidellm pod."""
        try:
            self.deployment_manager.kubectl.run(
                ['delete', 'pod', self._guidellm_pod_name, '-n', self.namespace,
                 '--ignore-not-found=true'], check=False)
            if log_callback:
                log_callback('🧹 Guidellm pod cleaned up')
        except Exception:
            pass

    def _get_storage_pvc_name(self, config):
        """Get the PVC name for guidellm pod — must be RWX for multi-pod access."""
        kubectl = self.deployment_manager.kubectl
        # Get the optimizer pod's PVC
        try:
            r = kubectl.run(
                ['get', 'pod', '-l', 'app=inferecipe-optimizer', '-n', self.namespace,
                 '-o', 'jsonpath={.items[0].spec.volumes[?(@.persistentVolumeClaim)].persistentVolumeClaim.claimName}'],
                check=False)
            if r.returncode == 0 and r.stdout.strip():
                pvc = r.stdout.strip().split()[0]
                # Verify it's RWX
                mode_r = kubectl.run(
                    ['get', 'pvc', pvc, '-n', self.namespace,
                     '-o', 'jsonpath={.spec.accessModes[0]}'], check=False)
                if mode_r.returncode == 0 and 'ReadWriteMany' in mode_r.stdout:
                    return pvc
                # PVC is RWO — look for an RWX alternative
                rwx_r = kubectl.run(
                    ['get', 'pvc', '-n', self.namespace, '-o',
                     'jsonpath={range .items[*]}{.metadata.name}{" "}{.spec.accessModes[0]}{"\\n"}{end}'],
                    check=False)
                if rwx_r.returncode == 0:
                    for line in rwx_r.stdout.strip().split('\n'):
                        parts = line.strip().split()
                        if len(parts) == 2 and parts[1] == 'ReadWriteMany':
                            return parts[0]
        except Exception:
            pass
        return getattr(config, 'pvc_name', None) or 'inferecipe-model-cache'

    def _run_guidellm_job(
        self,
        endpoint: str,
        config: TestConfig,
        log_callback: Optional[Callable[[str], None]] = None,
        monitor_pods: bool = False,
        expected_pod_count: int = 0,
        collect_metrics: bool = True,
        stop_check: Optional[Callable[[], bool]] = None,
    ) -> tuple[bool, Optional[str], Optional[str]]:
        """Run guidellm via kubectl exec on the persistent guidellm pod.

        Same interface as _run_guidellm_test. The persistent pod is deployed
        once at optimization start and reused for all tests.
        """
        kubectl = self.deployment_manager.kubectl

        # Ensure guidellm pod is running
        if not self.ensure_guidellm_pod(config, log_callback):
            return False, None, None

        if endpoint is None:
            endpoint = self._discover_istio_gateway(
                self.namespace, config.test_id, config.architecture, log_callback)

        # Build guidellm command
        rate_type_map = {'synchronous': 'constant', 'concurrent': 'concurrent',
                         'throughput': 'throughput', 'constant': 'constant', 'poisson': 'poisson'}
        rate_type = rate_type_map.get(getattr(config, 'request_type', 'constant'), 'constant')
        request_rate = getattr(config, 'request_rate', 1)
        turns = getattr(config, 'turns', 1) or 1

        workload_mode = getattr(config, 'workload_mode', 'synthetic') or 'synthetic'
        data_payload = ''
        column_args = ''
        if workload_mode == 'dataset' and getattr(config, 'dataset_source', None):
            data_payload = config.dataset_source
            col = getattr(config, 'dataset_column', None) or 'prompt'
            column_args = f' --data-column-mapper \'{{"text_column": "{col}"}}\''
        else:
            data_payload = f'prompt_tokens={config.isl},output_tokens={config.osl}'
            if getattr(config, 'isl_stdev', None):
                data_payload += f',prompt_tokens_stdev={config.isl_stdev}'
            if getattr(config, 'osl_stdev', None):
                data_payload += f',output_tokens_stdev={config.osl_stdev}'
            if turns > 1:
                data_payload += f',turns={turns}'
            max_model_len = getattr(config, 'max_model_len', 0)
            if max_model_len and (getattr(config, 'isl_stdev', None) or getattr(config, 'osl_stdev', None)):
                prompt_max = max_model_len - config.osl - 200
                if prompt_max > 0:
                    data_payload += f',prompt_tokens_max={prompt_max}'

        request_format = '/v1/chat/completions' if turns > 1 else '/v1/completions'
        stop_mode = getattr(config, 'stop_mode', 'duration')
        max_requests = getattr(config, 'max_requests', None)
        warmup = min(60, max(0, config.test_duration - 30)) if hasattr(config, 'test_duration') else 60

        output_path = f'/mnt/storage/guidellm-results/{config.test_id}.json'
        Path('/mnt/storage/guidellm-results').mkdir(parents=True, exist_ok=True)

        # Build the shell command to exec
        stop_arg = f'--max-requests {max_requests}' if (stop_mode == 'max_requests' and max_requests) else f'--max-seconds {config.test_duration}'
        exec_cmd = (
            f'guidellm benchmark run'
            f' --target "{endpoint}"'
            f' --model "{config.model_name}"'
            f' --processor "{config.model_name}"'
            f' --data "{data_payload}"'
            f' --backend-kwargs \'{{"http2": false}}\''
            f' --request-format "{request_format}"'
            f' --rate-type {rate_type}'
            f' --rate {request_rate}'
            f' {stop_arg}'
            f' --output-path {output_path}'
            f' --warmup {warmup}'
            f' --sample-requests 20'
            f'{column_args}'
        )

        if log_callback:
            rate_label = f'{request_rate} concurrent users' if rate_type == 'concurrent' else f'{request_rate} req/s ({rate_type})'
            log_callback(f'🏃 Running guidellm on pod {self._guidellm_pod_name}')
            log_callback(f'   Target: {endpoint}')
            log_callback(f'   Load: {rate_label}')

        benchmark_start = time.time()
        monitor_timeout = 3600 if (stop_mode == 'max_requests' and max_requests) else config.test_duration
        monitor_timeout += warmup + 120

        try:
            # Launch kubectl exec as a subprocess to stream output
            exec_full_cmd = [
                kubectl.kubectl_cmd, 'exec', self._guidellm_pod_name,
                '-n', self.namespace, '--', 'bash', '-c', exec_cmd
            ]
            env = os.environ.copy()
            env['KUBECONFIG'] = os.path.expanduser(kubectl.kubeconfig)

            process = subprocess.Popen(
                exec_full_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, env=env
            )

            benchmark_success = True
            last_pod_check = benchmark_start

            while time.time() - benchmark_start < monitor_timeout:
                if stop_check and stop_check():
                    if log_callback:
                        log_callback('   Stopping guidellm...')
                    process.kill()
                    return False, None, None

                if process.poll() is not None:
                    if process.returncode == 0:
                        if log_callback:
                            elapsed = int(time.time() - benchmark_start)
                            log_callback(f'ℹ️  Guidellm completed ({elapsed}s)')
                    else:
                        remaining = process.stdout.read() if process.stdout else ''
                        if log_callback:
                            log_callback(f'❌ Guidellm exited with code {process.returncode}')
                            if remaining:
                                for line in remaining.strip().splitlines()[-10:]:
                                    log_callback(f'   {line.strip()[:200]}')
                        benchmark_success = False
                    break

                # Read output (non-blocking)
                try:
                    import select
                    if select.select([process.stdout], [], [], 0)[0]:
                        line = process.stdout.readline()
                        if line and log_callback:
                            stripped = line.strip()
                            if stripped and ('error' in stripped.lower() or 'warning' in stripped.lower()):
                                log_callback(f'   Guidellm: {stripped[:100]}')
                except Exception:
                    pass

                # Check inference pod health
                if monitor_pods and time.time() - last_pod_check >= 30:
                    pod_error = self._monitor_pods_during_benchmark(
                        self.namespace, config.test_id, expected_pod_count,
                        benchmark_start, config.test_duration, 30, log_callback)
                    if pod_error:
                        benchmark_success = False
                        process.kill()
                        break
                    last_pod_check = time.time()

                time.sleep(5)
            else:
                if log_callback:
                    log_callback(f'   ⚠️  Guidellm timed out after {int(monitor_timeout)}s')
                process.kill()
                benchmark_success = False

            if process.poll() is None:
                process.wait(timeout=10)

            elapsed_total = int(time.time() - benchmark_start)
            result_path = Path(output_path)
            output_exists = result_path.exists() and result_path.stat().st_size > 0

            if benchmark_success and (process.returncode == 0 or output_exists):
                if log_callback:
                    log_callback(f'✅ Benchmark completed ({elapsed_total}s)')

                metrics_path = None
                if collect_metrics:
                    try:
                        if self.metrics_collector:
                            results_dir = Path(f'/mnt/storage/results/{config.test_id}')
                            results_dir.mkdir(parents=True, exist_ok=True)
                            metrics_file = str(results_dir / 'metrics.json')
                            self.metrics_collector.collect_all_metrics(
                                namespace=self.namespace, test_id=config.test_id,
                                start_time=benchmark_start, end_time=time.time(),
                                output_file=metrics_file)
                            metrics_path = metrics_file
                    except Exception as e:
                        if log_callback:
                            log_callback(f'   ⚠️  Metrics collection failed: {str(e)[:100]}')

                return True, str(result_path), metrics_path

            return False, None, None

        except Exception as e:
            if log_callback:
                log_callback(f'❌ Guidellm exec failed: {str(e)[:200]}')
            return False, None, None

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
                col = getattr(config, 'dataset_column', None) or 'prompt'
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

