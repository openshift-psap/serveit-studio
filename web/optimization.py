"""Optimization runner — log_to_ui, run_optimization_background, deploy_and_test."""

import os
import sys
import json
import time
import logging
import subprocess
from datetime import datetime
from threading import RLock
from typing import Optional, Dict, List

from gevent import spawn

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from web.app_context import (
    app, socketio, get_db, DB_PATH, TARGET_NAMESPACE,
    OPTIMIZATION_OUTPUT_DIR, state, state_lock, APP_PATH
)
from web.database import save_state, load_state, get_deployment_template

from core import SystemScanner, TestResult, DeploymentManager, TestConfig, TestOrchestrator
from core.web_deployer import DeploymentOrchestrator
from core.k8s_utils import KubectlRunner
from core.template_manager import TemplateManager

logger = logging.getLogger(__name__)

# --- Optimization Runner ---

def log_to_ui(message: str, log_type: str = 'info', run_id: int = None, job_name: str = None, session_id: str = None):
    """Send log message to UI via SocketIO and persist to database.

    Args:
        message: Log message
        log_type: Log level (info, success, error, warning, decision)
        run_id: Optional optimization run ID
        job_name: Optional job name for filtering
        session_id: Optional session ID for tracking client sessions
    """
    # Skip empty messages
    if not message or not message.strip():
        return

    # Add timestamp prefix for info messages
    display_message = message
    if log_type == 'info':
        timestamp = datetime.now().strftime('%m/%d %H:%M:%S')
        display_message = f"[{timestamp}] {message}"

    # Emit to connected clients
    socketio.emit('console_log', {'type': log_type, 'message': display_message})

    # Persist to database
    try:
        with get_db() as conn:
            conn.execute(
                '''INSERT INTO console_logs (timestamp, log_type, message, run_id, job_name, session_id)
                   VALUES (?, ?, ?, ?, ?, ?)''',
                (datetime.now().isoformat(), log_type, message, run_id, job_name, session_id)
            )
    except Exception as e:
        # Don't fail the operation if logging fails, but print error
        print(f"Warning: Failed to persist log to database: {e}")

def _reset_optimization_state(reason: str = ''):
    """Reset optimization running state and notify the UI."""
    with state_lock:
        state['optimization_running'] = False
        save_state()
    try:
        with get_db() as conn:
            conn.execute('''
                UPDATE ui_session_state
                SET optimization_running = 0, updated_at = ?
                WHERE id = 1
            ''', (datetime.now().isoformat(),))
    except Exception as e:
        print(f"Warning: Failed to update optimization_running in database: {e}")
    socketio.emit('status_update', {'running': False, 'message': reason})

_active_stream_job = [None]  # mutable container for current active job name

def stream_job_logs(job_name: str, namespace: str):
    """
    Stream Kubernetes job logs to UI in real-time.

    Args:
        job_name: Name of the job
        namespace: Kubernetes namespace
    """

    # Register as the active streamer — any previous greenlet will see
    # _active_stream_job[0] != its job_name and exit silently.
    _active_stream_job[0] = job_name

    def _is_superseded():
        return _active_stream_job[0] != job_name

    try:
        # Determine kubectl command
        kubectl_cmd = 'oc'
        try:
            subprocess.run(['oc', 'version'], capture_output=True, timeout=5, check=True)
        except (FileNotFoundError, subprocess.CalledProcessError):
            kubectl_cmd = 'kubectl'

        import select as _select
        STALL_TIMEOUT = 3600  # 60 min silence = stalled (large models can write silently for 30+ min)
        MAX_RESTARTS = 5

        seen_pods = set()
        restart_count = 0

        while restart_count <= MAX_RESTARTS:
            if _is_superseded():
                return
            # --- Find the current (possibly new) pod for this job ---
            time.sleep(2)
            pod_name = None
            for _ in range(60):
                r = subprocess.run(
                    [kubectl_cmd, 'get', 'pods', '-n', namespace,
                     '-l', f'job-name={job_name}',
                     '-o', 'jsonpath={.items[*].metadata.name}'],
                    capture_output=True, timeout=10
                )
                if r.returncode == 0:
                    names = r.stdout.decode().strip().split()
                    # Pick a pod we haven't fully streamed yet
                    fresh = [n for n in names if n not in seen_pods]
                    if fresh:
                        pod_name = fresh[0]
                        break
                time.sleep(1)

            if not pod_name:
                log_to_ui('⚠️ Could not find download job pod', 'warning', job_name=job_name)
                return

            seen_pods.add(pod_name)

            # --- Wait for pod to be Running ---
            last_phase = None
            logged_waiting = False
            for _ in range(300):
                r = subprocess.run(
                    [kubectl_cmd, 'get', 'pod', pod_name, '-n', namespace,
                     '-o', 'jsonpath={.status.phase}'],
                    capture_output=True, timeout=10
                )
                phase = r.stdout.decode().strip() if r.returncode == 0 else ''
                if phase == 'Running':
                    log_to_ui('✅ Pod is ready', 'success', job_name=job_name)
                    break
                elif phase == 'Failed':
                    log_to_ui(f'❌ Pod {pod_name} failed', 'error', job_name=job_name)
                    _reset_optimization_state('Pod failed to start')
                    return
                elif phase == 'Succeeded':
                    log_to_ui('✅ Job completed', 'success', job_name=job_name)
                    return
                else:
                    if phase != last_phase:
                        if phase == 'Pending' and not logged_waiting:
                            log_to_ui('⏳ Waiting for pod to be scheduled...', 'info', job_name=job_name)
                            logged_waiting = True
                        elif phase == 'ContainerCreating':
                            log_to_ui('📦 Pulling container image...', 'info', job_name=job_name)
                        last_phase = phase
                time.sleep(1)

            log_to_ui(f'📡 Streaming logs from {pod_name}...', 'info', job_name=job_name)

            # --- Stream with stall watchdog ---
            log_cmd = [kubectl_cmd, 'logs', '-n', namespace, '-f', pod_name]
            log_proc = subprocess.Popen(log_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

            last_line_at = time.time()
            stalled = False

            while True:
                ready, _, _ = _select.select([log_proc.stdout], [], [], 1.0)
                if ready:
                    line = log_proc.stdout.readline()
                    if not line:
                        break  # EOF — pod exited normally
                    line = line.strip()
                    if line:
                        last_line_at = time.time()
                        if '[' in line and ']' in line and '%' in line:
                            log_to_ui(line, 'info', job_name=job_name)
                        elif '✅' in line or 'complete' in line.lower():
                            log_to_ui(line, 'success', job_name=job_name)
                        elif '❌' in line or 'error' in line.lower():
                            log_to_ui(line, 'error', job_name=job_name)
                        else:
                            log_to_ui(line, 'info', job_name=job_name)
                else:
                    if log_proc.poll() is not None:
                        break  # process exited
                    silent_for = time.time() - last_line_at
                    if silent_for > STALL_TIMEOUT:
                        restart_count += 1
                        log_to_ui(
                            f'⚠️  Download stalled ({int(silent_for//60)}m silence) — '
                            f'restarting pod (attempt {restart_count}/{MAX_RESTARTS}). '
                            f'HuggingFace will resume from last completed blob.',
                            'warning', job_name=job_name
                        )
                        subprocess.run(
                            [kubectl_cmd, 'delete', 'pod', pod_name, '-n', namespace],
                            capture_output=True, timeout=30
                        )
                        log_to_ui('🔄 Pod deleted — waiting for new pod...', 'info', job_name=job_name)
                        stalled = True
                        break

            log_proc.wait()

            if not stalled:
                break  # pod exited cleanly — done

        # If a newer streamer took over, exit silently
        if _is_superseded():
            return

        # Wait a moment for pod to transition to Succeeded
        time.sleep(2)

        # Check if job completed successfully
        status_cmd = [kubectl_cmd, 'get', 'pod', pod_name, '-n', namespace, '-o', 'jsonpath={.status.phase}']
        proc = subprocess.run(status_cmd, capture_output=True, timeout=10)

        pod_phase = proc.stdout.decode().strip() if proc.returncode == 0 else 'Unknown'
        log_to_ui(f'🔍 Final pod phase: {pod_phase}', 'info', job_name=job_name)

        if proc.returncode == 0 and pod_phase == 'Succeeded':
            log_to_ui('✅ Model download completed successfully!', 'success', job_name=job_name)
            socketio.emit('storage_download_complete', {'success': True, 'job_name': job_name})

            # Trigger aggregated deployment
            # Try to get model name from job metadata
            model_name = None

            # Method 1: Get from job annotations
            model_cmd = [kubectl_cmd, 'get', 'job', job_name, '-n', namespace, '-o', 'jsonpath={.metadata.annotations.description}']
            proc = subprocess.run(model_cmd, capture_output=True, timeout=10)
            model_desc = proc.stdout.decode().strip()

            log_to_ui(f'🔍 Job description: "{model_desc}"', 'info', job_name=job_name)

            # Extract model name from description: "Model download job for {model_name}"
            if 'for ' in model_desc:
                model_name = model_desc.split('for ')[-1].strip()
                log_to_ui(f'🔍 Extracted model name (method 1): {model_name}', 'info', job_name=job_name)

            # Method 2: Fallback - get from state['current_test_plan']
            if not model_name:
                with state_lock:
                    if state['current_test_plan']:
                        model_name = state['current_test_plan'].model_name
                        log_to_ui(f'🔍 Extracted model name (method 2 - test plan): {model_name}', 'info', job_name=job_name)

            if model_name:
                log_to_ui('', 'info', job_name=job_name)  # Blank line
                log_to_ui('🚀 Starting optimization...', 'info', job_name=job_name)

                # Get the test plan from global state, fallback to database
                test_plan = None
                with state_lock:
                    test_plan = state['current_test_plan']

                if not test_plan:
                    # Try to restore from database
                    load_state()
                    with state_lock:
                        test_plan = state['current_test_plan']

                # Build optimization data from test plan or saved config
                saved_config = {}
                try:
                    with get_db() as conn:
                        row = conn.execute('SELECT config_json FROM ui_session_state WHERE id = 1').fetchone()
                        if row and row['config_json']:
                            saved_config = json.loads(row['config_json'])
                except Exception:
                    pass

                optimization_data = {
                    'model': model_name,
                    'isl': test_plan.model_requirements.isl if test_plan else saved_config.get('isl', 3000),
                    'osl': test_plan.model_requirements.osl if test_plan else saved_config.get('osl', 100),
                    'isl_stdev': saved_config.get('isl_stdev'),
                    'osl_stdev': saved_config.get('osl_stdev'),
                    'turns': saved_config.get('turns', 1),
                    'num_users': saved_config.get('users', 100),
                    'optimization_metric': test_plan.optimization_goal if test_plan else saved_config.get('goal', 'ttft'),
                    'max_test_duration': saved_config.get('duration', 300),
                    'stop_mode': saved_config.get('stop_mode', 'duration'),
                    'max_requests': saved_config.get('max_requests'),
                    'hf_token': saved_config.get('hf_token'),
                    'max_gpus': test_plan.max_gpus_to_use if test_plan else saved_config.get('max_gpus', 16),
                    'use_achievable_qps': saved_config.get('use_achievable_qps', False),
                    'latency_constraint_enabled': saved_config.get('latency_constraint_enabled', False),
                    'latency_constraint_ms': saved_config.get('latency_constraint_ms', 500),
                    'latency_constraint_percentile': saved_config.get('latency_constraint_percentile', 'p90'),
                    'tp_pair_top_n': saved_config.get('tp_pair_top_n', 4),
                    'allow_asymmetric_tp': saved_config.get('allow_asymmetric_tp', False),
                    'pd_search_mode': saved_config.get('pd_search_mode', 'smart'),
                    'calibrated_load_enabled': saved_config.get('calibrated_load_enabled', False),
                    'inferencex_sweep_enabled': saved_config.get('inferencex_sweep_enabled', False),
                    'concurrency_sweep_levels': saved_config.get('concurrency_sweep_levels', None),
                    'concurrency_sweep_count': saved_config.get('concurrency_sweep_count', None),
                    'concurrency_sweep_step_pct': saved_config.get('concurrency_sweep_step_pct', 20),
                    'concurrency_sweep_all_configs': saved_config.get('concurrency_sweep_all_configs', False),
                    'concurrency_sweep_max_configs': saved_config.get('concurrency_sweep_max_configs', None),
                    'cache_sweep_enabled': saved_config.get('cache_sweep_enabled', False),
                    'cache_sweep_use_calibrated': saved_config.get('cache_sweep_use_calibrated', False),
                    'cache_sweep_mode': saved_config.get('cache_sweep_mode', 'identical'),
                    'cache_sweep_levels': saved_config.get('cache_sweep_levels', None),
                    'cache_sweep_count': saved_config.get('cache_sweep_count', None),
                    'cache_sweep_step_pct': saved_config.get('cache_sweep_step_pct', 10),
                    'cache_sweep_groups': saved_config.get('cache_sweep_groups', 5),
                    'cache_sweep_all_configs': saved_config.get('cache_sweep_all_configs', False),
                    'cache_sweep_max_configs': saved_config.get('cache_sweep_max_configs', None),
                    'run_description': saved_config.get('run_description', ''),
                    'advanced_vllm_custom_enabled': saved_config.get('advanced_vllm_custom_enabled', True),
                    'epp_custom_enabled': saved_config.get('epp_custom_enabled', True),
                    'epp_preset': saved_config.get('epp_preset', 'balanced'),
                    'epp_benchmark': saved_config.get('epp_benchmark', False),
                    'epp_config': saved_config.get('epp_config'),
                    'selected_nodes': saved_config.get('selected_nodes', []),
                    'workload_mode': saved_config.get('workload_mode', 'synthetic'),
                    'dataset_source': saved_config.get('dataset_source'),
                    'dataset_column': saved_config.get('dataset_column'),
                    'dataset_max_output': saved_config.get('dataset_max_output', 256),
                    'rate_type': saved_config.get('rate_type', 'concurrent'),
                    'prefix_cache_hit_pct': saved_config.get('prefix_cache_hit_pct', 0),
                    'prefix_cache_mode': saved_config.get('prefix_cache_mode', 'identical'),
                    'prefix_cache_groups': saved_config.get('prefix_cache_groups', 5),
                    'advanced_vllm': saved_config.get('advanced_vllm'),
                }

                # Start optimization in background
                spawn(run_optimization_background, optimization_data)
            else:
                log_to_ui('⚠️ Could not determine model name for deployment', 'warning', job_name=job_name)
                log_to_ui('   CURRENT_TEST_PLAN is None or has no model_name', 'warning', job_name=job_name)
        else:
            log_to_ui(f'⚠️ Pod did not reach Succeeded status (current: {pod_phase})', 'warning', job_name=job_name)
            socketio.emit('storage_download_complete', {'success': False, 'job_name': job_name})
            log_to_ui('   Deployment will not be triggered automatically', 'warning', job_name=job_name)
            _reset_optimization_state('Model download did not complete')

    except Exception as e:
        log_to_ui(f'⚠️ Log streaming error: {str(e)}', 'warning', job_name=job_name)
        _reset_optimization_state(f'Log streaming error: {str(e)}')

def deploy_and_test_inference(model_name: str, namespace: str, job_name: str = None):
    """
    Deploy aggregated inference and test it using the latest template from database.

    Args:
        model_name: HuggingFace model name
        namespace: Kubernetes namespace
        job_name: Optional job name for log context
    """

    try:
        # Step 1: Deploy prerequisite infrastructure (GAIE, Gateway, etc.)
        from core import PrereqManager, CleanupManager

        log_to_ui('', 'info', job_name=job_name)
        log_to_ui('=' * 60, 'info', job_name=job_name)
        log_to_ui('📋 Step 1: Deploying Prerequisite Infrastructure', 'info', job_name=job_name)
        log_to_ui('=' * 60, 'info', job_name=job_name)

        # Deploy prerequisites (GAIE, Gateway, InferencePool)
        # This function deploys aggregated architecture
        # Read gateway_class from saved UI config (set during cluster scan)
        try:
            with get_db() as _conn:
                _row = _conn.execute('SELECT config_json FROM ui_session_state WHERE id=1').fetchone()
                _cfg = json.loads(_row['config_json']) if _row and _row['config_json'] else {}
                gateway_class = _cfg.get('gateway_class', 'istio')
        except Exception:
            gateway_class = 'istio'
        prereq_mgr = PrereqManager(namespace=namespace, gateway_class=gateway_class,
                                    scheduler_image=_cfg.get('scheduler_image'))

        # Build a lightweight config for prereq phase (per-node PVC creation)
        class _PrereqConfig:
            pass
        _prereq_cfg = _PrereqConfig()
        _prereq_cfg.per_node_storage = _cfg.get('per_node_storage', False)
        _prereq_cfg.pvc_size = _cfg.get('pvc_size', '200Gi')
        _prereq_cfg.storage_class = _cfg.get('storage_class')
        _prereq_cfg.node_nfs_pvcs = []

        try:
            # Deploy prerequisites - this will create missing resources and skip existing ones
            success = prereq_mgr.deploy_prereqs(
                architecture='aggregated',
                log_callback=lambda msg: log_to_ui(msg, 'info', job_name=job_name),
                optimizer_config=_prereq_cfg if _prereq_cfg.per_node_storage else None,
            )

            if not success:
                log_to_ui('', 'error', job_name=job_name)
                log_to_ui('❌ Failed to deploy prerequisite infrastructure', 'error', job_name=job_name)
                log_to_ui('', 'error', job_name=job_name)
                return None

            # Persist node_nfs_pvcs so the optimizer config picks it up
            if _prereq_cfg.per_node_storage and _prereq_cfg.node_nfs_pvcs:
                try:
                    with get_db() as _conn:
                        _row = _conn.execute('SELECT config_json FROM ui_session_state WHERE id=1').fetchone()
                        _saved = json.loads(_row['config_json']) if _row and _row['config_json'] else {}
                        _saved['node_nfs_pvcs'] = _prereq_cfg.node_nfs_pvcs
                        _conn.execute('UPDATE ui_session_state SET config_json=? WHERE id=1',
                                      (json.dumps(_saved),))
                        _conn.commit()
                except Exception:
                    pass

            log_to_ui('', 'info', job_name=job_name)
            log_to_ui('ℹ️  Note: Gateway typically takes 1-2 minutes to become fully healthy', 'info', job_name=job_name)
            log_to_ui('   Waiting for gateway to be ready before proceeding...', 'info', job_name=job_name)
        except Exception as e:
            log_to_ui('', 'error', job_name=job_name)
            log_to_ui(f'❌ Failed to deploy prerequisites: {str(e)}', 'error', job_name=job_name)
            log_to_ui('', 'error', job_name=job_name)
            import traceback
            traceback.print_exc()
            return None

        log_to_ui('', 'info', job_name=job_name)
        log_to_ui('▶️  Prerequisites ready, continuing with inference pod deployment...', 'info', job_name=job_name)
        log_to_ui('', 'info', job_name=job_name)
        log_to_ui('=' * 60, 'info', job_name=job_name)
        log_to_ui('📋 Step 2: Deploying Inference Pods', 'info', job_name=job_name)
        log_to_ui('=' * 60, 'info', job_name=job_name)
        log_to_ui('', 'info', job_name=job_name)

        # Step 2: Check if there are existing ServeIt Studio deployments
        cleanup_mgr = CleanupManager(namespace=namespace)
        existing_resources = cleanup_mgr.get_deployed_resources()

        if existing_resources:
            log_to_ui(f'⚠️  Found {len(existing_resources)} existing deployment(s)', 'warning', job_name=job_name)
            for resource in existing_resources:
                log_to_ui(f'   • {resource}', 'info', job_name=job_name)
            log_to_ui('', 'info', job_name=job_name)
            log_to_ui('🧹 Cleaning up existing deployments first...', 'info', job_name=job_name)

            # Clean up existing deployments
            success = cleanup_mgr.cleanup_all_test_deployments(
                log_callback=lambda msg: log_to_ui(f'   {msg}', 'info', job_name=job_name)
            )

            if not success:
                log_to_ui('⚠️  Cleanup had warnings, but continuing...', 'warning', job_name=job_name)

            log_to_ui('', 'info', job_name=job_name)

        timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        test_id = f'serveit-inference-{timestamp}'

        log_to_ui('📋 Loading deployment template from database...', 'info', job_name=job_name)

        # Get the latest aggregated template from database
        template = get_deployment_template(model_name, 'aggregated', role=None)

        if template is None:
            log_to_ui('⚠️ No deployment template found in database. Using defaults.', 'warning', job_name=job_name)
            # Fallback to defaults
            tp = 2
            isl = 2000
            osl = 100
            replicas = 1
            max_model_len = 8192
            gpu_memory_utilization = 0.95
            image = 'ghcr.io/llm-d/llm-d-cuda:v0.8.0'
            pvc_name = 'serveit-cache'
            nccl_ib_hca = 'mlx'
            gpus_per_pod = tp
        else:
            # Use template from database
            tp = template['tensor_parallelism']
            isl = template['isl']
            osl = template['osl']
            replicas = template['replicas']
            max_model_len = template['max_model_len']
            gpu_memory_utilization = template['gpu_memory_utilization']
            image = template['image']
            pvc_name = template['pvc_name']
            nccl_ib_hca = template['nccl_ib_hca']
            gpus_per_pod = template['gpus_per_pod']
            log_to_ui('   ✅ Template loaded from database', 'success', job_name=job_name)
            log_to_ui('   • Architecture: aggregated', 'info', job_name=job_name)
            log_to_ui(f'   • Tensor Parallelism: TP={tp}', 'info', job_name=job_name)
            log_to_ui(f'   • GPUs per pod: {gpus_per_pod}', 'info', job_name=job_name)
            log_to_ui(f'   • Max model length: {max_model_len} tokens', 'info', job_name=job_name)
            log_to_ui(f'   • GPU memory utilization: {gpu_memory_utilization}', 'info', job_name=job_name)
            log_to_ui(f'   • ISL/OSL: {isl}/{osl} tokens', 'info', job_name=job_name)

        # Create a TestConfig for aggregated deployment
        config = TestConfig(
            test_id=test_id,
            architecture='aggregated',
            model_name=model_name,
            namespace=namespace,
            isl=isl,
            osl=osl,
            num_users=50,
            tensor_parallelism=tp,
            replicas=replicas,
            pvc_name=pvc_name,
            nccl_ib_hca=nccl_ib_hca,
            max_model_len=max_model_len,
            gpu_memory_utilization=gpu_memory_utilization,
            image=image,
            optimization_goal='balanced',
            test_duration=300
        )

        log_to_ui('', 'info', job_name=job_name)
        log_to_ui(f'📦 Deploying LeaderWorkerSet for {model_name}...', 'info', job_name=job_name)

        # Deploy using DeploymentManager
        deployment_mgr = DeploymentManager(namespace=namespace)

        def deployment_log(msg):
            log_to_ui(f'   {msg}', 'info', job_name=job_name)

        success = deployment_mgr.deploy_config(config, log_callback=deployment_log)

        if not success:
            log_to_ui('❌ Failed to deploy aggregated inference', 'error', job_name=job_name)
            return

        log_to_ui('✅ Deployment manifests applied', 'success', job_name=job_name)

        # Wait for deployment to be ready
        log_to_ui('', 'info', job_name=job_name)  # Blank line
        log_to_ui('⏳ Waiting for pods to reach inference serving state...', 'info', job_name=job_name)

        ready = deployment_mgr.wait_for_ready(
            test_id=test_id,
            architecture='aggregated',
            timeout=900,  # 15 minutes for model loading
            log_callback=deployment_log
        )

        if not ready:
            log_to_ui('⏱️ Timeout: Pods did not reach serving state', 'error', job_name=job_name)
            return

        # Pods are ready, now test inference
        log_to_ui('', 'info', job_name=job_name)  # Blank line
        log_to_ui('🧪 Testing inference endpoint...', 'info', job_name=job_name)

        # Get service endpoint
        service_name = f'{test_id}-aggregated'
        service_url = f'http://{service_name}.{namespace}.svc.cluster.local:8000'

        log_to_ui(f'   Service: {service_name}', 'info', job_name=job_name)
        log_to_ui(f'   Endpoint: {service_url}', 'info', job_name=job_name)

        # Wait a bit for service to be fully ready
        time.sleep(5)

        # Test with a simple curl to /v1/models
        try:
            # Determine kubectl command
            kubectl_cmd = 'oc'
            try:
                subprocess.run(['oc', 'version'], capture_output=True, timeout=5, check=True)
            except (FileNotFoundError, subprocess.CalledProcessError):
                kubectl_cmd = 'kubectl'

            # Run curl from within cluster (use a temporary pod)
            curl_cmd = [
                kubectl_cmd, 'run', 'serveit-curl-test', '-n', namespace,
                '--rm', '-i', '--restart=Never',
                '--image=registry.access.redhat.com/ubi9/ubi-minimal:latest',
                '--', 'curl', '-s', '-m', '30', f'{service_url}/v1/models'
            ]

            log_to_ui('   Running curl test...', 'info', job_name=job_name)
            proc = subprocess.run(curl_cmd, capture_output=True, timeout=60)

            if proc.returncode == 0:
                output = proc.stdout.decode().strip()
                # Check if we got a valid JSON response with model info
                if 'data' in output and model_name in output:
                    log_to_ui('✅ Inference endpoint is serving!', 'success', job_name=job_name)
                    log_to_ui(f'   Response: {output[:200]}...', 'info', job_name=job_name)
                else:
                    log_to_ui('⚠️ Endpoint responded but may not be fully ready', 'warning', job_name=job_name)
                    log_to_ui(f'   Response: {output[:200]}', 'info', job_name=job_name)
            else:
                log_to_ui(f'❌ Curl test failed: {proc.stderr.decode().strip()}', 'error', job_name=job_name)

        except subprocess.TimeoutExpired:
            log_to_ui('⏱️ Curl test timeout', 'warning', job_name=job_name)
        except Exception as curl_err:
            log_to_ui(f'⚠️ Could not run curl test: {curl_err}', 'warning', job_name=job_name)

        # Final summary
        log_to_ui('', 'info', job_name=job_name)  # Blank line
        log_to_ui('=' * 60, 'info', job_name=job_name)
        log_to_ui('🎉 Aggregated inference deployment complete!', 'success', job_name=job_name)
        log_to_ui(f'   Test ID: {test_id}', 'info', job_name=job_name)
        log_to_ui(f'   Service: {service_name}', 'info', job_name=job_name)
        log_to_ui(f'   Namespace: {namespace}', 'info', job_name=job_name)
        log_to_ui('=' * 60, 'info', job_name=job_name)

    except Exception as e:
        log_to_ui(f'❌ Deployment failed: {str(e)}', 'error', job_name=job_name)
        import traceback
        traceback.print_exc()


def run_optimization_background(data):
    """Run optimization in background greenlet using recipe-based approach."""

    # Re-assert the flag here — it may have been reset by load_state() or
    # page reload between handle_storage_setup and this greenlet starting.
    with state_lock:
        state['optimization_running'] = True

    resume_run_id = data.get('resume_run_id')  # If set, resume this run instead of creating new
    run_name = data.get('run_name', f"run-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    run_id = None

    try:
        # Merge UI session state as fallback for any field the caller omitted
        # (e.g. stale browser JS that doesn't send all fields yet)
        try:
            with get_db() as _conn:
                _row = _conn.execute('SELECT config_json FROM ui_session_state WHERE id=1').fetchone()
                _saved = json.loads(_row['config_json']) if _row and _row['config_json'] else {}
        except Exception:
            _saved = {}

        # On resume: merge saved run config on top of ui_session_state so advanced
        # settings (tool_call_parser, reasoning_parser, etc.) survive page reloads.
        if resume_run_id:
            try:
                with get_db() as _conn:
                    _rrow = _conn.execute(
                        'SELECT config_json FROM optimization_runs WHERE id=?', (resume_run_id,)
                    ).fetchone()
                    if _rrow and _rrow['config_json']:
                        _run_config = json.loads(_rrow['config_json'])
                        # Merge run config into _saved — run config wins over session state
                        _saved = {**_saved, **_run_config}
            except Exception:
                pass

        def _get(key, default=None, ui_key=None):
            """Return data[key] if present, else optimization_runs/ui_session_state[ui_key or key], else default."""
            v = data.get(key)
            if v is None:
                v = _saved.get(ui_key or key)
            return v if v is not None else default

        # Parse configuration — _get() falls back to ui_session_state for
        # any field the caller omitted (e.g. stale browser JS cache)
        model = data.get('model')
        isl = int(_get('isl', 3000))
        osl = int(_get('osl', 100))
        isl_stdev = _get('isl_stdev')
        osl_stdev = _get('osl_stdev')
        turns = int(_get('turns', 1))
        num_users = int(_get('num_users', 100, ui_key='users'))
        optimization_goal = _get('optimization_metric', 'ttft', ui_key='goal') or 'ttft'
        stop_mode = _get('stop_mode', 'duration')
        test_duration = int(_get('max_test_duration', 300, ui_key='duration'))
        max_requests = _get('max_requests')
        thanos_url = data.get('thanos_url', os.environ.get('THANOS_URL'))
        # Auto-detect Prometheus if not configured
        if not thanos_url:
            for candidate in [
                'http://prometheus.monitoring.svc.cluster.local:9090',
                'http://prometheus-k8s.openshift-monitoring.svc:9091',
                'http://thanos-querier.openshift-monitoring.svc:9091',
            ]:
                try:
                    import urllib.request
                    r = urllib.request.urlopen(candidate + '/api/v1/status/config', timeout=3)
                    if r.status == 200:
                        thanos_url = candidate
                        log_to_ui(f'📊 Auto-detected Prometheus at {candidate}', 'info')
                        break
                except Exception:
                    continue
        hf_token = _get('hf_token')
        max_gpus = _get('max_gpus', 16)
        use_achievable_qps = _get('use_achievable_qps', False)
        latency_constraint_enabled = _get('latency_constraint_enabled', False)
        latency_constraint_ms = int(_get('latency_constraint_ms', 500))
        latency_constraint_percentile = _get('latency_constraint_percentile', 'p90')
        tp_pair_top_n = int(_get('tp_pair_top_n', 4))
        allow_asymmetric_tp = _get('allow_asymmetric_tp', False)
        pd_search_mode = _get('pd_search_mode', 'smart')
        calibrated_load_enabled = _get('calibrated_load_enabled', False)
        inferencex_sweep_enabled = _get('inferencex_sweep_enabled', False)
        concurrency_sweep_levels = _get('concurrency_sweep_levels', None)
        if isinstance(concurrency_sweep_levels, str):
            concurrency_sweep_levels = [int(x.strip()) for x in concurrency_sweep_levels.split(',') if x.strip().isdigit()]
        if concurrency_sweep_levels is not None and not concurrency_sweep_levels:
            concurrency_sweep_levels = None
        concurrency_sweep_count = _get('concurrency_sweep_count', None)
        if concurrency_sweep_count is not None:
            concurrency_sweep_count = int(concurrency_sweep_count) if concurrency_sweep_count else None
        concurrency_sweep_step_pct = int(_get('concurrency_sweep_step_pct', 20))
        concurrency_sweep_all_configs = _get('concurrency_sweep_all_configs', False)
        concurrency_sweep_max_configs = _get('concurrency_sweep_max_configs', None)
        if concurrency_sweep_max_configs is not None:
            concurrency_sweep_max_configs = int(concurrency_sweep_max_configs) if concurrency_sweep_max_configs else None
        cache_sweep_enabled = _get('cache_sweep_enabled', False)
        cache_sweep_use_calibrated = _get('cache_sweep_use_calibrated', False)
        cache_sweep_mode = _get('cache_sweep_mode', 'identical')
        cache_sweep_levels = _get('cache_sweep_levels', None)
        if isinstance(cache_sweep_levels, str):
            cache_sweep_levels = [int(x.strip()) for x in cache_sweep_levels.split(',') if x.strip().isdigit()]
        if cache_sweep_levels is not None and not cache_sweep_levels:
            cache_sweep_levels = None
        cache_sweep_count = _get('cache_sweep_count', None)
        if cache_sweep_count is not None:
            cache_sweep_count = int(cache_sweep_count) if cache_sweep_count else None
        cache_sweep_step_pct = int(_get('cache_sweep_step_pct', 10))
        cache_sweep_groups = int(_get('cache_sweep_groups', 5))
        cache_sweep_all_configs = _get('cache_sweep_all_configs', False)
        cache_sweep_max_configs = _get('cache_sweep_max_configs', None)
        if cache_sweep_max_configs is not None:
            cache_sweep_max_configs = int(cache_sweep_max_configs) if cache_sweep_max_configs else None
        run_description = _get('run_description', '')
        advanced_vllm_custom_enabled = _get('advanced_vllm_custom_enabled', True)
        epp_custom_enabled = _get('epp_custom_enabled', True)
        epp_preset = _get('epp_preset', 'balanced')
        epp_benchmark = _get('epp_benchmark', False)
        epp_config = _get('epp_config')
        selected_nodes = _get('selected_nodes') or []
        workload_mode = _get('workload_mode', 'synthetic')
        dataset_source = _get('dataset_source')
        dataset_column = _get('dataset_column')
        dataset_max_output = int(_get('dataset_max_output', 256))
        rate_type = _get('rate_type', 'concurrent')
        prefix_cache_hit_pct = int(_get('prefix_cache_hit_pct', 0))
        prefix_cache_mode = _get('prefix_cache_mode', 'identical')
        prefix_cache_groups = int(_get('prefix_cache_groups', 5))
        structured_prefix = _get('structured_prefix', False)
        advanced_vllm = _get('advanced_vllm')
        extra_env_vars = _get('extra_env_vars')
        headroom_setting = (advanced_vllm or {}).get('headroom', {})
        headroom = float(headroom_setting.get('value')) if headroom_setting.get('mode') == 'custom' and headroom_setting.get('value') else 1.3
        mem_reserve_setting = (advanced_vllm or {}).get('memory-reserve-pct', {})
        memory_reserve_pct = float(mem_reserve_setting.get('value')) if mem_reserve_setting.get('mode') == 'custom' and mem_reserve_setting.get('value') else 0.0

        # Network selections
        network_type = _get('network_type', 'eth0')
        rdma_network_annotation = _get('rdma_network_annotation')
        selected_sriov_policies = _get('selected_sriov_policies') or []
        sriov_same_subnet = _get('sriov_same_subnet', False)
        exclusive_pf = _get('exclusive_pf', False)
        selected_shared_device = _get('selected_shared_device')
        selected_dra_classes = _get('selected_dra_classes') or []
        dra_gpu_resource_key = _get('dra_gpu_resource_key')

        # Auto-resolve dra_gpu_resource_key from the cluster if missing
        if network_type == 'dra' and selected_dra_classes and not dra_gpu_resource_key:
            # Try reading from a previous run in this namespace
            try:
                with get_db() as _conn:
                    prev = _conn.execute(
                        "SELECT config_json FROM optimization_runs WHERE status != 'running' ORDER BY id DESC LIMIT 10"
                    ).fetchall()
                    for prow in prev:
                        if prow['config_json']:
                            import json as _j
                            pcfg = _j.loads(prow['config_json'])
                            if pcfg.get('dra_gpu_resource_key'):
                                dra_gpu_resource_key = pcfg['dra_gpu_resource_key']
                                log_to_ui(f'Restored DRA resource key from previous run: {dra_gpu_resource_key}', 'info')
                                break
            except Exception:
                pass

            # Fallback: query the device class on the cluster
            if not dra_gpu_resource_key:
                for dc_name in selected_dra_classes:
                    try:
                        r = scanner.kubectl.run(
                            ['get', 'deviceclass', dc_name, '-o', 'jsonpath={.spec.extendedResourceName}'],
                            check=False)
                        if r.returncode == 0 and r.stdout.strip():
                            dra_gpu_resource_key = r.stdout.strip()
                            log_to_ui(f'Auto-resolved DRA resource key: {dra_gpu_resource_key}', 'info')
                            break
                    except Exception:
                        pass

            # Fallback: derive from class name convention (composite-gpu-nic-pair → composite.dra.io/gpu-nic-pair)
            if not dra_gpu_resource_key:
                for dc_name in selected_dra_classes:
                    if 'gpu-nic-pair' in dc_name:
                        prefix = dc_name.replace('-gpu-nic-pair', '')
                        dra_gpu_resource_key = f'{prefix}.dra.io/gpu-nic-pair'
                        log_to_ui(f'Derived DRA resource key from class name: {dra_gpu_resource_key}', 'info')
                        break
        gateway_class = _get('gateway_class', 'istio')
        per_node_storage = _get('per_node_storage', False)
        node_nfs_pvcs = _get('node_nfs_pvcs') or []
        storage_class = _get('storage_class')
        pvc_size_raw = _get('pvc_size')
        if pvc_size_raw:
            pvc_size_str = str(pvc_size_raw).replace('Gi', '').replace('G', '').strip()
            pvc_size = f"{int(pvc_size_str)}Gi" if pvc_size_str.isdigit() else str(pvc_size_raw)
        else:
            pvc_size = None
        vllm_image = _get('image') or 'ghcr.io/llm-d/llm-d-cuda:v0.8.0'
        scheduler_image = _get('scheduler_image') or 'ghcr.io/llm-d/llm-d-router-endpoint-picker:v0.9.0'
        single_test_architecture = _get('single_test_architecture')
        single_test_tp = _get('single_test_tp')
        single_test_replicas = _get('single_test_replicas')
        single_test_prefill_tp = _get('single_test_prefill_tp')
        single_test_decode_tp = _get('single_test_decode_tp')
        single_test_prefill_pods = _get('single_test_prefill_pods')
        single_test_decode_pods = _get('single_test_decode_pods')
        if optimization_goal == 'single_test':
            log_to_ui(f"Single test config: arch={single_test_architecture}, tp={single_test_tp}, "
                      f"prefill_tp={single_test_prefill_tp}, decode_tp={single_test_decode_tp}, "
                      f"prefill_pods={single_test_prefill_pods}, decode_pods={single_test_decode_pods}", 'info')

        # Create/update HuggingFace token secret if provided
        if hf_token and hf_token.strip():
            try:
                log_to_ui("🔑 Creating HuggingFace token secret...", 'info')
                import subprocess
                import base64

                # Create secret YAML
                secret_yaml = f"""apiVersion: v1
kind: Secret
metadata:
  name: llm-d-hf-token
  namespace: {TARGET_NAMESPACE}
type: Opaque
data:
  HF_TOKEN: {base64.b64encode(hf_token.strip().encode()).decode()}
"""

                # Apply secret (try oc first, fallback to kubectl)
                cmd = ['oc', 'apply', '-f', '-']
                try:
                    proc = subprocess.run(cmd, input=secret_yaml.encode(), capture_output=True, timeout=30)
                except FileNotFoundError:
                    cmd = ['kubectl', 'apply', '-f', '-']
                    proc = subprocess.run(cmd, input=secret_yaml.encode(), capture_output=True, timeout=30)

                if proc.returncode == 0:
                    log_to_ui("✅ HuggingFace token secret created/updated", 'success')
                else:
                    log_to_ui(f"⚠️  Warning: Failed to create secret: {proc.stderr.decode()}", 'warning')
            except Exception as e:
                log_to_ui(f"⚠️  Warning: Failed to create HF token secret: {str(e)}", 'warning')

        # Log startup
        log_to_ui(f"🚀 Starting optimization run: {run_name}", 'success')
        log_to_ui(f"Model: {model}", 'info')
        isl_str = f"ISL: {isl}" + (f" (σ={isl_stdev})" if isl_stdev else "")
        osl_str = f"OSL: {osl}" + (f" (σ={osl_stdev})" if osl_stdev else "")
        turns_str = f", Turns: {turns}" if turns > 1 else ""
        log_to_ui(f"{isl_str}, {osl_str}, Users: {num_users}{turns_str}", 'info')
        log_to_ui(f"Optimization goal: {optimization_goal}", 'decision')
        log_to_ui("Using recipe-based optimization (tests generated dynamically)", 'info')

        # Create or resume database entry
        if resume_run_id:
            run_id = resume_run_id
            with get_db() as conn:
                conn.execute(
                    'UPDATE optimization_runs SET status = ? WHERE id = ?',
                    ('running', run_id)
                )
            log_to_ui(f"📋 Resuming run #{run_id} — completed tests will be skipped", 'info')
        else:
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO optimization_runs
                    (run_name, model, isl, osl, num_users, status, created_at, goal, test_duration, max_gpus, use_achievable_qps, isl_stdev, osl_stdev, turns,
                     latency_constraint_enabled, latency_constraint_ms, latency_constraint_percentile,
                     workload_mode, dataset_source, dataset_column, dataset_max_output, rate_type, prefix_cache_hit_pct, notes)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (run_name, model, isl, osl, num_users, 'running',
                      datetime.now().isoformat(), optimization_goal, test_duration, max_gpus,
                      1 if use_achievable_qps else 0,
                      int(isl_stdev) if isl_stdev else None,
                      int(osl_stdev) if osl_stdev else None,
                      turns,
                      1 if latency_constraint_enabled else 0,
                      latency_constraint_ms,
                      latency_constraint_percentile,
                      workload_mode, dataset_source, dataset_column, dataset_max_output,
                      rate_type, prefix_cache_hit_pct, run_description or None))
                run_id = cursor.lastrowid

        # Step 1: Choose optimization approach
        log_to_ui("\n📋 Step 1: Selecting optimization approach...", 'info')
        log_to_ui(f"   Optimization goal: {optimization_goal}", 'info')

        # Use Recipe-based optimization for all goals
        if optimization_goal in ('ttft', 'throughput', 'balanced', 'aggregated_only', 'pd_only', 'ep_only', 'single_test'):
            goal_descriptions = {
                'ttft': {
                    'label': 'Response Time Priority (PD vs Aggregated)',
                    'steps': [
                        "Steps 2-3: Find optimal prefill/decode TP (exhaustive sweep)",
                        "Steps 4-5: Mathematical resource sizing",
                        "Step 7: Test feasible P/D splits near ideal ratio",
                        "Step 8: Validate best PD vs equivalent Aggregated",
                    ]
                },
                'throughput': {
                    'label': 'Throughput Priority (EP vs Aggregated)',
                    'steps': [
                        "Steps 2-3: Find optimal TP for throughput (exhaustive sweep)",
                        "Steps 4-5: EP configuration space enumeration",
                        "Step 7: Test EP configurations at full workload",
                        "Step 8: Validate best EP vs equivalent Aggregated",
                    ]
                },
                'balanced': {
                    'label': 'Full Coverage (PD + EP + PD-EP + Aggregated)',
                    'steps': [
                        "Steps 2-3: Find optimal TP (exhaustive sweep)",
                        "Steps 4-5: Resource sizing for PD + EP",
                        "Step 7: Test PD splits and EP configurations",
                        "Step 8: Three-way comparison (PD vs EP vs Aggregated)",
                    ]
                },
                'aggregated_only': {
                    'label': 'Aggregated Only (Standard)',
                    'steps': [
                        "Steps 2-3: Find optimal TP (exhaustive sweep)",
                        "Step 6: Test aggregated configurations at each TP",
                    ]
                },
                'pd_only': {
                    'label': 'Prefill/Decode Only',
                    'steps': [
                        "Steps 2-3: Find optimal prefill/decode TP (exhaustive sweep)",
                        "Steps 4-5: Calculate feasible P/D splits",
                        "Step 7: Test all feasible P/D splits",
                    ]
                },
                'ep_only': {
                    'label': 'Expert Parallelism Only',
                    'steps': [
                        "Steps 2-3: Find optimal TP (exhaustive sweep)",
                        "Steps 4-5: EP configuration space enumeration",
                        "Step 7: Test EP configurations at full workload",
                    ]
                },
                'single_test': {
                    'label': 'Single Test (user-defined configuration)',
                    'steps': [
                        "Deploy and benchmark a single user-specified configuration",
                    ]
                },
            }
            goal_info = goal_descriptions[optimization_goal]
            log_to_ui("", 'info')
            log_to_ui(f"✨ Using Recipe-based optimization — {goal_info['label']}", 'success')
            for step_desc in goal_info['steps']:
                log_to_ui(f"   • {step_desc}", 'info')
            log_to_ui("", 'info')

            # Import Recipe optimizer
            from core.recipe_optimizer import RecipeOptimizer, RecipeOptimizerConfig
            from core.system_scanner import SystemScanner

            # Scan cluster to get valid TP options based on actual hardware
            log_to_ui("🔍 Scanning cluster for valid TP options...", 'info')
            scanner = SystemScanner(namespace=TARGET_NAMESPACE)
            cluster_resources = scanner.scan_cluster()
            tp_options = cluster_resources.get_tp_options()
            log_to_ui(f"   Hardware TP options: {tp_options} (model-based filtering applied later)", 'info')
            log_to_ui("", 'info')

            # Create optimizer config
            recipe_config = RecipeOptimizerConfig(
                model_name=model,
                namespace=TARGET_NAMESPACE,
                isl=isl,
                osl=osl,
                isl_stdev=int(isl_stdev) if isl_stdev else None,
                osl_stdev=int(osl_stdev) if osl_stdev else None,
                turns=turns,
                qps=float(num_users),
                total_gpus=max_gpus,
                max_model_len=8192,
                test_duration=test_duration,
                stop_mode=stop_mode,
                max_requests=int(max_requests) if max_requests else None,
                # Steps 2 & 3 exhaustively test ALL valid TP values
                max_pd_splits=0,  # 0 = full coverage (test all valid splits)
                tp_pair_top_n=tp_pair_top_n,
                allow_asymmetric_tp=allow_asymmetric_tp,
                pd_search_mode=pd_search_mode,
                advanced_vllm_custom_enabled=advanced_vllm_custom_enabled,
                epp_custom_enabled=epp_custom_enabled,
                epp_preset=epp_preset,
                epp_benchmark=epp_benchmark,
                epp_config=epp_config,
                thanos_url=thanos_url,
                image=vllm_image,
                scheduler_image=scheduler_image,
                pvc_name='serveit-cache',
                nccl_ib_hca='mlx',
                hf_token=hf_token,
                tp_options=tp_options,  # Dynamic based on cluster hardware
                objective=optimization_goal,  # 'ttft' selects TP by lowest latency
                use_achievable_qps=use_achievable_qps,
                calibrated_load_enabled=calibrated_load_enabled,
                inferencex_sweep_enabled=inferencex_sweep_enabled,
                concurrency_sweep_levels=concurrency_sweep_levels,
                concurrency_sweep_count=concurrency_sweep_count,
                concurrency_sweep_step_pct=concurrency_sweep_step_pct,
                concurrency_sweep_all_configs=concurrency_sweep_all_configs,
                concurrency_sweep_max_configs=concurrency_sweep_max_configs,
                cache_sweep_enabled=cache_sweep_enabled,
                cache_sweep_use_calibrated=cache_sweep_use_calibrated,
                cache_sweep_mode=cache_sweep_mode,
                cache_sweep_levels=cache_sweep_levels,
                cache_sweep_count=cache_sweep_count,
                cache_sweep_step_pct=cache_sweep_step_pct,
                cache_sweep_groups=cache_sweep_groups,
                cache_sweep_all_configs=cache_sweep_all_configs,
                cache_sweep_max_configs=cache_sweep_max_configs,
                latency_constraint_enabled=latency_constraint_enabled,
                latency_constraint_ms=latency_constraint_ms,
                latency_constraint_percentile=latency_constraint_percentile,
                selected_nodes=selected_nodes,
                workload_mode=workload_mode,
                dataset_source=dataset_source,
                dataset_column=dataset_column,
                dataset_max_output=dataset_max_output,
                rate_type=rate_type,
                prefix_cache_hit_pct=prefix_cache_hit_pct,
                prefix_cache_mode=prefix_cache_mode,
                prefix_cache_groups=prefix_cache_groups,
                structured_prefix=structured_prefix,
                headroom=headroom,
                memory_reserve_pct=memory_reserve_pct,
                rdma_network_annotation=rdma_network_annotation,
                selected_sriov_policies=selected_sriov_policies,
                sriov_same_subnet=sriov_same_subnet,
                exclusive_pf=exclusive_pf,
                selected_shared_device=selected_shared_device,
                selected_dra_classes=selected_dra_classes,
                dra_gpu_resource_key=dra_gpu_resource_key,
                gateway_class=gateway_class,
                per_node_storage=per_node_storage,
                node_nfs_pvcs=node_nfs_pvcs,
                storage_class=storage_class,
                pvc_size=pvc_size,
                advanced_vllm=advanced_vllm,
                extra_env_vars=extra_env_vars,
                single_test_architecture=single_test_architecture,
                single_test_tp=int(single_test_tp) if single_test_tp else None,
                single_test_replicas=int(single_test_replicas) if single_test_replicas else None,
                single_test_prefill_tp=int(single_test_prefill_tp) if single_test_prefill_tp else None,
                single_test_decode_tp=int(single_test_decode_tp) if single_test_decode_tp else None,
                single_test_prefill_pods=int(single_test_prefill_pods) if single_test_prefill_pods else None,
                single_test_decode_pods=int(single_test_decode_pods) if single_test_decode_pods else None,
            )

            # Save full config to DB for resume
            with get_db() as conn:
                conn.execute(
                    'UPDATE optimization_runs SET config_json = ? WHERE id = ?',
                    (json.dumps(recipe_config.to_dict()), run_id))

            # Run optimization with database persistence
            # Use a dedicated stop flag — only set by explicit user stop action,
            # not by reconnects, state reloads, or error handlers
            with state_lock:
                state['_stop_requested'] = False

            def check_stopped():
                with state_lock:
                    return state.get('_stop_requested', False)

            optimizer = RecipeOptimizer(
                config=recipe_config,
                log_callback=log_to_ui,
                run_id=run_id,
                db_path=DB_PATH,
                stop_check=check_stopped
            )

            # Re-save config after optimizer init — auto-detection (network_type,
            # RDMA, MoE) updates recipe_config fields that were None before
            with get_db() as conn:
                conn.execute(
                    'UPDATE optimization_runs SET config_json = ? WHERE id = ?',
                    (json.dumps(recipe_config.to_dict()), run_id))

            try:
                results = optimizer.optimize(resume=bool(resume_run_id))

                # Log final results
                log_to_ui("", 'info')
                log_to_ui("=" * 80, 'success')
                log_to_ui("🎯 OPTIMIZATION COMPLETE", 'success')
                log_to_ui("=" * 80, 'success')
                log_to_ui(f"✅ Total tests run: {results['total_tests_run']}", 'success')
                log_to_ui(f"✅ Optimal decode TP: {results['optimal_decode_tp']}", 'success')
                log_to_ui(f"✅ Optimal prefill TP: {results['optimal_prefill_tp']}", 'success')

                # Goal-specific result summary
                opt_goal = results.get('optimization_goal', 'ttft')

                if results.get('pareto_configurations'):
                    log_to_ui(f"✅ PD Pareto configurations: {results['pareto_front_count']}", 'success')
                    log_to_ui("", 'info')
                    log_to_ui("📊 PD Configurations (Pareto Front):", 'decision')
                    for i, config in enumerate(results['pareto_configurations'], 1):
                        log_to_ui(f"   {i}. PD: {config['prefill_pods']}P×TP{config['prefill_tp']} + "
                                 f"{config['decode_pods']}D×TP{config['decode_tp']}", 'info')
                        tput = config.get('throughput_mean') or config.get('throughput_p90') or 0
                        log_to_ui(f"      TTFT p90: {config['ttft_p90']:.1f}ms, "
                                 f"Throughput mean: {tput:.2f} req/s", 'info')

                if results.get('ep_configurations'):
                    log_to_ui("", 'info')
                    log_to_ui("📊 EP Configurations:", 'decision')
                    for i, config in enumerate(results['ep_configurations'], 1):
                        log_to_ui(f"   {i}. EP: {config.get('prefill_pods',1)}P+{config.get('decode_pods',1)}D "
                                 f"PTP={config.get('prefill_tp','?')} DTP={config.get('decode_tp','?')} "
                                 f"({config['total_gpus']} GPUs)", 'info')
                        tput = config.get('throughput_mean') or config.get('throughput_p90') or 0
                        log_to_ui(f"      TTFT p90: {config['ttft_p90']:.1f}ms, "
                                 f"Throughput mean: {tput:.2f} req/s", 'info')

                best_ep = results.get('best_ep')
                if best_ep:
                    log_to_ui("", 'info')
                    log_to_ui(f"✅ Best EP: {best_ep.get('prefill_pods',1)}P+{best_ep.get('decode_pods',1)}D "
                             f"PTP={best_ep.get('prefill_tp','?')} DTP={best_ep.get('decode_tp','?')}", 'success')

                agg = results.get('aggregated_result')
                if agg:
                    log_to_ui("", 'info')
                    log_to_ui("📊 Aggregated Baseline (Step 8):", 'decision')
                    log_to_ui(f"   Aggregated: {agg['pods']} pods × TP{agg['tp']} ({agg['gpus']} GPUs)", 'info')
                    agg_ttft = agg.get('ttft_p90')
                    agg_tput = agg.get('throughput_mean') or agg.get('throughput_p90')
                    if agg_ttft is not None and agg_tput is not None:
                        log_to_ui(f"      TTFT p90: {agg_ttft:.1f}ms, "
                                 f"Throughput mean: {agg_tput:.2f} req/s", 'info')

                        # Compare based on goal
                        if opt_goal == 'ttft':
                            pareto_cfgs = results.get('pareto_configurations', [])
                            best_pd = min(pareto_cfgs, key=lambda p: p.get('ttft_p90') or 1e9) if pareto_cfgs else None
                            if best_pd:
                                pd_ttft = best_pd['ttft_p90']
                                if agg_ttft < pd_ttft:
                                    log_to_ui(f"   ⚡ Aggregated has better TTFT ({agg_ttft:.1f}ms vs {pd_ttft:.1f}ms)", 'warning')
                                else:
                                    log_to_ui(f"   ✅ PD has better TTFT ({pd_ttft:.1f}ms vs {agg_ttft:.1f}ms)", 'success')
                        elif opt_goal == 'throughput' and best_ep:
                            ep_tput = best_ep.get('throughput_mean') or best_ep.get('throughput_p90', 0)
                            if ep_tput and agg_tput:
                                if ep_tput > agg_tput:
                                    log_to_ui(f"   ✅ EP has better throughput ({ep_tput:.2f} vs {agg_tput:.2f} req/s)", 'success')
                                else:
                                    log_to_ui(f"   ⚡ Aggregated has better throughput ({agg_tput:.2f} vs {ep_tput:.2f} req/s)", 'warning')

                # Note: RecipeOptimizer already saves each test to DB in real-time
                # via _save_test_to_database(), so no need to re-save here
                log_to_ui("", 'info')
                log_to_ui(f"💾 All {results['total_tests_run']} test results already saved to database", 'success')

                # Update run status and save constraint notes
                constraint_notes_json = None
                if results.get('constraint_notes'):
                    import json as _json
                    constraint_notes_json = _json.dumps(results['constraint_notes'])

                optimal_config_json = None
                try:
                    import json as _json2
                    serializable_keys = {
                        'optimal_decode_tp', 'optimal_prefill_tp',
                        'decode_tpsg', 'prefill_tpsg',
                        'concurrency', 'total_gpus_available',
                        'gpu_sizing', 'feasible_splits_count', 'pareto_front_count',
                        'total_tests_run', 'pareto_configurations',
                        'aggregated_result',
                        'calibrated_qps', 'calibrated_concurrency', 'sustainable_throughput_rps',
                        'calibrated_pd_result', 'calibrated_agg_result',
                        'calibration_analysis', 'concurrency_sweep', 'cache_sweep',
                        'best_ep', 'calibrated_ep_result',
                        'optimization_goal', 'stopped',
                    }
                    optimal_config_json = _json2.dumps({
                        k: v for k, v in results.items() if k in serializable_keys
                    })
                except Exception:
                    pass

                if results.get('stopped'):
                    run_status = 'stopped'
                else:
                    # Check if any tests failed
                    has_failed = False
                    try:
                        with get_db() as _conn:
                            _fc = _conn.execute(
                                'SELECT COUNT(*) FROM test_configurations WHERE run_id = ? AND status = ?',
                                (run_id, 'failed')
                            ).fetchone()
                            has_failed = _fc and _fc[0] > 0
                    except Exception:
                        pass
                    run_status = 'completed_with_errors' if has_failed else 'completed'

                with get_db() as conn:
                    conn.execute('''
                        UPDATE optimization_runs
                        SET status = ?, completed_at = ?,
                            constraint_notes = ?,
                            optimal_config = ?
                        WHERE id = ?
                    ''', (run_status, datetime.now().isoformat(), constraint_notes_json, optimal_config_json, run_id))

                log_to_ui("", 'info')
                if results.get('stopped'):
                    log_to_ui("🛑 Optimization stopped by user — can be resumed later", 'warning')
                else:
                    log_to_ui("✅ Recipe optimization completed successfully!", 'success')
                    log_to_ui("", 'info')
                    log_to_ui("📈 Click 'Report Analytics' in the toolbar to see full results and breakdown", 'info')

            except Exception as e:
                from core.pod_error_scanner import PodErrorsDetected
                if isinstance(e, PodErrorsDetected):
                    log_to_ui("", 'info')
                    log_to_ui("=" * 80, 'error')
                    log_to_ui("🚨 OPTIMIZATION STOPPED — Critical Pod Errors Detected", 'error')
                    log_to_ui("=" * 80, 'error')
                    log_to_ui(f"Test: {e.test_id}", 'error')
                    log_to_ui("Pods left running for investigation.", 'error')
                    log_to_ui("Resume from the Resume page after investigating.", 'info')

                    with get_db() as conn:
                        conn.execute('''
                            UPDATE optimization_runs
                            SET status = 'error_stopped', completed_at = ?
                            WHERE id = ?
                        ''', (datetime.now().isoformat(), run_id))
                else:
                    log_to_ui(f"❌ Recipe optimization failed: {str(e)}", 'error')
                    import traceback
                    log_to_ui(traceback.format_exc(), 'error')

                    with get_db() as conn:
                        conn.execute('''
                            UPDATE optimization_runs
                            SET status = 'failed', completed_at = ?
                            WHERE id = ?
                        ''', (datetime.now().isoformat(), run_id))

            finally:
                # Mark optimization as stopped
                with state_lock:
                    state['optimization_running'] = False
                    save_state()
                socketio.emit('status_update', {'running': False})

            return

        # Otherwise, use traditional exhaustive approach (requires pre-generated test plan)
        log_to_ui("", 'info')
        log_to_ui("📋 Using exhaustive test plan approach", 'info')

        # Load pre-generated test plan for exhaustive mode
        test_plan = None
        with state_lock:
            test_plan = state['current_test_plan']

        if not test_plan:
            log_to_ui('❌ No test plan found for exhaustive mode. Use TTFT optimization instead.', 'error')
            with state_lock:
                state['optimization_running'] = False
                save_state()
            socketio.emit('status_update', {'running': False})
            return

        log_to_ui(f"   Recipe-based plan: {test_plan.estimated_total_tests}", 'info')
        log_to_ui("", 'info')

        from core.config_generator import TestConfig
        from core.system_scanner import SystemScanner

        # Get cluster resources for memory calculation
        scanner = SystemScanner(namespace=TARGET_NAMESPACE)
        cluster_resources = scanner.scan_cluster()
        total_memory_gb = cluster_resources.total_memory_gb
        gpu_nodes = cluster_resources.gpu_node_count
        max_gpus_per_node = cluster_resources.max_gpus_per_node
        log_to_ui(f"   Cluster: {gpu_nodes} GPU nodes, {max_gpus_per_node} GPUs/node, {total_memory_gb}GB total RAM", 'info')

        # Filter out placeholder tests and convert to TestConfig
        test_configs = []
        for idx, test in enumerate(test_plan.tests):
            # Skip placeholder tests (TP=0 or gpus_required=0)
            if test.tp == 0 or test.gpus_required == 0:
                continue

            # Generate test ID (lowercase for Kubernetes RFC 1123 compliance)
            arch_short = test.architecture.value.lower()[:3]
            test_id = f"{run_name}-{arch_short}-tp{test.tp}-{idx:02d}"

            # Get workload ISL/OSL (use override if specified, otherwise use plan defaults)
            test_isl = test.workload_isl if test.workload_isl is not None else isl
            test_osl = test.workload_osl if test.workload_osl is not None else osl

            # Calculate memory and CPU per pod based on per-node capacity
            # Memory and CPU are per-node, so calculate based on max pods that can fit on one node
            per_node_memory_gb = total_memory_gb / gpu_nodes
            per_node_usable_memory_gb = per_node_memory_gb * 0.8  # 80% usable

            # Get CPU info from GPU nodes only (filter nodes with GPUs)
            gpu_nodes_list = [n for n in cluster_resources.nodes if n.gpus > 0]
            if gpu_nodes_list:
                # Use average CPU cores from GPU nodes
                avg_cpu_per_gpu_node = sum(n.cpu_cores for n in gpu_nodes_list) / len(gpu_nodes_list)
            else:
                # Fallback: assume 48 cores per node (conservative)
                avg_cpu_per_gpu_node = 48
            per_node_usable_cpus = int(avg_cpu_per_gpu_node * 0.7)  # 70% usable

            # Max pods per node = GPUs per node / TP
            max_pods_per_node = max_gpus_per_node / test.tp
            memory_per_pod_gb = int(per_node_usable_memory_gb / max_pods_per_node)
            memory_per_pod = f"{memory_per_pod_gb}Gi"
            cpu_per_pod = max(1, int(per_node_usable_cpus / max_pods_per_node))  # At least 1 CPU
            cpu_request = str(cpu_per_pod)

            # Create TestConfig based on architecture
            if test.architecture.value == 'pd':
                config = TestConfig(
                    test_id=test_id,
                    architecture='pd',
                    model_name=model,
                    namespace=TARGET_NAMESPACE,
                    isl=test_isl,
                    osl=test_osl,
                    num_users=num_users,
                    tensor_parallelism=test.tp,
                    replicas=test.prefill_pods + test.decode_pods,
                    prefill_replicas=test.prefill_pods,
                    decode_replicas=test.decode_pods,
                    prefill_decode_ratio=f"{test.prefill_pods}:{test.decode_pods}",
                    max_model_len=8192,
                    gpu_memory_utilization=test_plan.model_requirements.gpu_memory_utilization,
                    memory_request=memory_per_pod,
                    memory_limit=memory_per_pod,
                    pvc_name='serveit-cache',
                    optimization_goal=optimization_goal,
                    test_duration=test_duration,
                    cpu_request=cpu_request
                )
            else:
                # AGGREGATED or EP
                config = TestConfig(
                    test_id=test_id,
                    architecture=test.architecture.value,
                    model_name=model,
                    namespace=TARGET_NAMESPACE,
                    isl=test_isl,
                    osl=test_osl,
                    num_users=num_users,
                    tensor_parallelism=test.tp,
                    replicas=test.ep_pods,
                    max_model_len=8192,
                    gpu_memory_utilization=test_plan.model_requirements.gpu_memory_utilization,
                    memory_request=memory_per_pod,
                    memory_limit=memory_per_pod,
                    pvc_name='serveit-cache',
                    optimization_goal=optimization_goal,
                    test_duration=test_duration,
                    cpu_request=cpu_request
                )

            test_configs.append(config)

        log_to_ui(f"✅ Prepared {len(test_configs)} test configurations for execution", 'success')

        # Step 2: Run tests with architecture-aware logging
        log_to_ui("\n🧪 Step 2: Running optimization tests...", 'info')
        if stop_mode == 'max_requests' and max_requests:
            log_to_ui(f"   Stop condition: {max_requests} requests per test", 'info')
        else:
            log_to_ui(f"   Test duration: {test_duration}s per test", 'info')
        log_to_ui(f"   Thanos URL: {thanos_url or 'Not configured'}", 'info')
        log_to_ui('', 'info')

        # Initialize deployment orchestrator with kubectl runner
        kubectl = KubectlRunner()
        deployment_orchestrator = DeploymentOrchestrator(kubectl)

        # Run tests one by one with detailed architecture logging
        results = []
        from core import CleanupManager

        for idx, test_config in enumerate(test_configs, 1):
            # Check if optimization was stopped
            with state_lock:
                if not state['optimization_running']:
                    log_to_ui('', 'info')
                    log_to_ui('🛑 Optimization stopped by user - cancelling remaining tests', 'warning')
                    break

            log_to_ui('', 'info')
            log_to_ui('═' * 60, 'info')
            log_to_ui(f'📊 Test {idx}/{len(test_configs)}: {test_config.architecture.upper()}', 'info')
            log_to_ui('═' * 60, 'info')
            log_to_ui(f'   Architecture: {test_config.architecture.upper()}', 'info')
            log_to_ui(f'   Tensor Parallelism: TP={test_config.tensor_parallelism}', 'info')
            log_to_ui(f'   Workload: ISL={test_config.isl}, OSL={test_config.osl}', 'info')
            log_to_ui(f'   Users: {test_config.num_users}', 'info')

            if test_config.architecture == 'pd':
                log_to_ui(f'   Prefill pods: {test_config.prefill_replicas}', 'info')
                log_to_ui(f'   Decode pods: {test_config.decode_replicas}', 'info')
                log_to_ui(f'   Ratio: {test_config.prefill_decode_ratio}', 'info')
            elif test_config.architecture == 'ep':
                log_to_ui(f'   Expert pods: {test_config.replicas}', 'info')
            else:  # aggregated
                log_to_ui(f'   Replicas: {test_config.replicas}', 'info')

            log_to_ui('', 'info')

            # Create test result tracker
            test_result = TestResult(
                test_id=test_config.test_id,
                architecture=test_config.architecture,
                deployment_success=False,
                deployment_ready=False,
                guidellm_success=False,
                metrics_collected=False,
                deployment_start_time=datetime.now().isoformat()
            )

            try:
                # Convert TestConfig to dict for DeploymentOrchestrator
                deployment_config = {
                    'test_id': test_config.test_id,
                    'namespace': test_config.namespace,
                    'architecture': test_config.architecture,
                    'model_name': test_config.model_name,
                    'image': test_config.image,
                    'tensor_parallelism': test_config.tensor_parallelism,
                    'pvc_name': test_config.pvc_name,
                    'gpu_memory_utilization': test_config.gpu_memory_utilization,
                    'max_model_len': test_config.max_model_len,
                    'kv_connector': test_config.kv_connector,
                    'memory_request': test_config.memory_request,
                    'memory_limit': test_config.memory_limit,
                    'cpu_request': getattr(test_config, 'cpu_request', '32'),
                    'cpu_limit': getattr(test_config, 'cpu_limit', None),
                    'extra_env': {
                        'INPUT_SEQUENCE_LENGTH': str(test_config.isl),
                        'OUTPUT_SEQUENCE_LENGTH': str(test_config.osl),
                        'NUM_CONCURRENT_USERS': str(test_config.num_users)
                    }
                }

                # Add architecture-specific fields
                if test_config.architecture == 'pd':
                    deployment_config['prefill_pods'] = test_config.prefill_replicas
                    deployment_config['decode_pods'] = test_config.decode_replicas
                    deployment_config['prefill_tp'] = test_config.tensor_parallelism
                    deployment_config['decode_tp'] = test_config.decode_tp or test_config.tensor_parallelism
                elif test_config.architecture == 'ep':
                    deployment_config['ep_pods'] = test_config.replicas
                else:  # aggregated
                    deployment_config['agg_pods'] = test_config.replicas

                # Deploy using new orchestrator (handles provider detection, network setup, prerequisites)
                log_to_ui(f'🚀 Deploying {test_config.architecture} configuration...', 'info')
                log_to_ui('   This includes waiting for all pods to be ready...', 'info')
                deployed_test_id = deployment_orchestrator.deploy(deployment_config)

                test_result.deployment_success = True
                test_result.deployment_ready = True
                test_result.deployment_end_time = datetime.now().isoformat()
                log_to_ui('✅ Deployment successful and all pods are ready!', 'success')

                # Patch services with InferencePool selector labels (required for Istio gateway routing)
                log_to_ui('🏷️  Patching services for InferencePool discovery...', 'info')

                # Determine kubectl command
                kubectl_cmd = 'oc'
                try:
                    subprocess.run(['oc', 'version'], capture_output=True, timeout=5, check=True)
                except (FileNotFoundError, subprocess.CalledProcessError):
                    kubectl_cmd = 'kubectl'

                try:
                    if test_config.architecture == 'pd':
                        # Patch both prefill and decode services
                        for role in ['prefill', 'decode']:
                            svc_name = f'{deployed_test_id}-{role}'
                            patch_cmd = [
                                kubectl_cmd, 'patch', 'svc', svc_name, '-n', test_config.namespace,
                                '-p', f'{{"metadata":{{"labels":{{"llm-d.ai/inference-serving":"true","llm-d.ai/role":"{role}"}}}}}}'
                            ]
                            result = subprocess.run(patch_cmd, capture_output=True, timeout=10)
                            if result.returncode == 0:
                                log_to_ui(f'   ✓ Patched {svc_name} service', 'success')
                            else:
                                log_to_ui(f'   ⚠️  Could not patch {svc_name}: {result.stderr.decode()[:100]}', 'warning')
                    else:
                        # For EP/aggregated, patch the main service
                        svc_name = deployed_test_id
                        patch_cmd = [
                            kubectl_cmd, 'patch', 'svc', svc_name, '-n', test_config.namespace,
                            '-p', '{"metadata":{"labels":{"llm-d.ai/inference-serving":"true"}}}'
                        ]
                        result = subprocess.run(patch_cmd, capture_output=True, timeout=10)
                        if result.returncode == 0:
                            log_to_ui(f'   ✓ Patched {svc_name} service', 'success')
                except Exception as e:
                    log_to_ui(f'   ⚠️  Service patching failed: {str(e)[:100]}', 'warning')

                # Validate inference endpoint is serving
                # Wait for vLLM to finish loading model in ALL pods
                log_to_ui('', 'info')
                log_to_ui('🧪 Waiting for vLLM to finish loading model...', 'info')

                inference_validated = False
                import subprocess

                # Get all pod IPs for this deployment
                try:
                    # Get pods by test-id label
                    get_pods_cmd = [
                        kubectl_cmd, 'get', 'pods', '-n', test_config.namespace,
                        '-l', f'test-id={deployed_test_id}',
                        '-o', 'jsonpath={range .items[*]}{.metadata.name}:{.status.podIP}{"\\n"}{end}'
                    ]
                    result = subprocess.run(get_pods_cmd, capture_output=True, timeout=10, check=True)
                    pod_info = []
                    for line in result.stdout.decode().strip().split('\n'):
                        if ':' in line:
                            name, ip = line.split(':')
                            pod_info.append({'name': name, 'ip': ip})

                    if not pod_info:
                        log_to_ui('⚠️ No pods found for validation', 'warning')
                        test_result.guidellm_success = False
                        test_result.error_message = "No pods found for inference validation"
                        log_to_ui('❌ Validation failed - no pods found', 'error')
                    else:
                        log_to_ui(f'   Found {len(pod_info)} pods to validate', 'info')

                        # Validate ALL pods (critical for PD deployments with many pods)
                        pods_to_test = pod_info
                        log_to_ui(f'   Validating all {len(pods_to_test)} pods...', 'info')

                        # Retry logic: try for up to 10 minutes
                        max_retries = 40  # 40 retries × 15 seconds = 10 minutes
                        retry_delay = 15  # seconds

                        all_pods_serving = False
                        last_progress = -1
                        for attempt in range(max_retries):
                            if attempt > 0:
                                time.sleep(retry_delay)

                            # Check pod status directly via Kubernetes API (no temporary pods)
                            status_cmd = [
                                kubectl_cmd, 'get', 'pods', '-n', test_config.namespace,
                                '-l', f'test-id={deployed_test_id}',
                                '-o', 'jsonpath={range .items[*]}{.metadata.name}:{.status.phase}:{.status.containerStatuses[0].ready}:{.status.containerStatuses[0].restartCount}{"\\n"}{end}'
                            ]

                            pods_ready = 0
                            pod_statuses = []

                            try:
                                result = subprocess.run(status_cmd, capture_output=True, timeout=10)
                                if result.returncode == 0:
                                    for line in result.stdout.decode().strip().split('\n'):
                                        if line and ':' in line:
                                            parts = line.split(':')
                                            if len(parts) >= 4:
                                                pod_name, phase, ready, _restart_count = parts[0], parts[1], parts[2], parts[3]

                                                # Pod is ready if: Running + container ready + no restarts
                                                if phase == 'Running' and ready == 'true':
                                                    # Check logs for "Application startup complete" to ensure vLLM is serving
                                                    log_cmd = [kubectl_cmd, 'logs', pod_name, '-n', test_config.namespace, '--tail=50']
                                                    log_result = subprocess.run(log_cmd, capture_output=True, timeout=10)

                                                    if log_result.returncode == 0:
                                                        logs = log_result.stdout.decode()
                                                        if 'Application startup complete' in logs:
                                                            pods_ready += 1
                                                            pod_statuses.append(f"{pod_name[:20]}: ✓")
                                                        else:
                                                            pod_statuses.append(f"{pod_name[:20]}: starting")
                                                    else:
                                                        pod_statuses.append(f"{pod_name[:20]}: log check failed")
                                                elif phase == 'Running' and ready == 'false':
                                                    pod_statuses.append(f"{pod_name[:20]}: not ready")
                                                elif phase != 'Running':
                                                    pod_statuses.append(f"{pod_name[:20]}: {phase}")
                                            else:
                                                # Fallback if containerStatuses not available yet
                                                pod_statuses.append("pod: initializing")
                            except Exception as e:
                                log_to_ui(f'⚠️  Status check error: {str(e)[:100]}', 'warning')

                            if pods_ready == len(pods_to_test):
                                all_pods_serving = True
                                log_to_ui(f'✅ All {pods_ready}/{len(pods_to_test)} pods are serving!', 'success')
                                break
                            else:
                                # Calculate progress percentage
                                progress = int((pods_ready / len(pods_to_test)) * 100)
                                elapsed = attempt * retry_delay

                                # Only log every 3rd attempt to reduce spam, or when progress changes
                                if attempt % 3 == 0 or progress != last_progress or attempt == 0:
                                    log_to_ui(f'   [{progress}%] {pods_ready}/{len(pods_to_test)} pods ready - {elapsed}s elapsed', 'info')
                                    # Show failed/loading pods only (not all pods, too verbose)
                                    failed = [s for s in pod_statuses if '✓' not in s]
                                    if failed and len(failed) <= 5:
                                        for status in failed:
                                            log_to_ui(f'      {status}', 'info')
                                    elif failed:
                                        log_to_ui(f'      {len(failed)} pods still loading...', 'info')
                                    last_progress = progress

                        if all_pods_serving:
                            # Pods are validated (Running + Ready + Application startup complete)
                            inference_validated = True
                            log_to_ui('✅ All pods validated - vLLM application started successfully!', 'success')
                        else:
                            log_to_ui(f'⏱️ Timeout: Pods did not finish loading model after {max_retries * retry_delay}s', 'error')

                except Exception as e:
                    log_to_ui(f'❌ Validation error: {str(e)[:200]}', 'error')
                    import traceback
                    traceback.print_exc()

                # Mark test as successful only if inference validated
                if not inference_validated:
                    test_result.guidellm_success = False
                    test_result.error_message = "Inference endpoint validation failed - model did not load"
                    log_to_ui('❌ Validation failed - model not serving properly', 'error')
                    log_to_ui('🛑 Stopping test - deployment not healthy', 'error')
                else:
                    log_to_ui('✅ Validation complete - all pods serving inference!', 'success')

                    # Run benchmark with crash detection using core module
                    log_to_ui('', 'info')

                    # Create test orchestrator instance
                    test_orchestrator = TestOrchestrator(
                        namespace=test_config.namespace,
                        deployment_timeout=3600,
                        test_duration=test_duration
                    )

                    # Run guidellm test with Istio gateway discovery, pod monitoring, and metrics collection
                    benchmark_success, result_file, metrics_file = test_orchestrator._run_guidellm_test(
                        endpoint=None,  # Auto-discover Istio gateway
                        config=test_config,
                        log_callback=log_to_ui,
                        monitor_pods=True,
                        expected_pod_count=len(pod_info),
                        collect_metrics=True  # Collect Prometheus/Thanos metrics
                    )

                    # Update test result
                    test_result.guidellm_success = benchmark_success
                    if metrics_file:
                        test_result.metrics_collected = True

                    if benchmark_success and result_file:
                        log_to_ui(f'   Results saved to: {result_file}', 'info')

                        # Parse guidellm results and extract metrics
                        try:
                            import json as _json3
                            with open(result_file, 'r') as f:
                                results = _json3.load(f)
                                if 'benchmarks' in results and len(results['benchmarks']) > 0:
                                    bench = results['benchmarks'][0]
                                    if 'summary' in bench:
                                        summary = bench['summary']

                                        # Extract TTFT percentiles (time_to_first_token in ms)
                                        if 'time_to_first_token' in summary:
                                            ttft = summary['time_to_first_token']
                                            test_result.ttft_p50 = ttft.get('p50')
                                            test_result.ttft_p90 = ttft.get('p90')
                                            test_result.ttft_p95 = ttft.get('p95')
                                            test_result.ttft_p99 = ttft.get('p99')

                                        # Extract ITL percentiles (inter_token_latency in ms)
                                        if 'inter_token_latency' in summary:
                                            itl = summary['inter_token_latency']
                                            test_result.itl_p50 = itl.get('p50')
                                            test_result.itl_p90 = itl.get('p90')
                                            test_result.itl_p95 = itl.get('p95')
                                            test_result.itl_p99 = itl.get('p99')

                                        # Extract throughput percentiles (req/s)
                                        if 'throughput' in summary:
                                            # guidellm may provide throughput as a single value or percentiles
                                            throughput_data = summary['throughput']
                                            if isinstance(throughput_data, dict):
                                                # If throughput has percentiles
                                                test_result.throughput_p50 = throughput_data.get('p50')
                                                test_result.throughput_p90 = throughput_data.get('p90')
                                                test_result.throughput_p95 = throughput_data.get('p95')
                                                test_result.throughput_p99 = throughput_data.get('p99')
                                            else:
                                                # If throughput is a single value (mean), store as p50
                                                test_result.throughput_p50 = throughput_data

                                        # Log summary
                                        log_to_ui(f'   Requests: {summary.get("total_count", "N/A")}', 'info')
                                        log_to_ui(f'   Success: {summary.get("success_count", "N/A")}', 'info')
                                        log_to_ui(f'   Errors: {summary.get("error_count", "N/A")}', 'info')
                                        if test_result.throughput_p50:
                                            log_to_ui(f'   Throughput p50: {test_result.throughput_p50:.2f} req/s', 'info')
                                            if test_result.throughput_p90:
                                                log_to_ui(f'   Throughput p90: {test_result.throughput_p90:.2f} req/s', 'info')
                                        if test_result.ttft_p50:
                                            log_to_ui(f'   TTFT p50: {test_result.ttft_p50:.2f}ms, p90: {test_result.ttft_p90:.2f}ms', 'info')
                                        if test_result.itl_p50:
                                            log_to_ui(f'   ITL p50: {test_result.itl_p50:.2f}ms, p90: {test_result.itl_p90:.2f}ms', 'info')
                        except Exception as e:
                            log_to_ui(f'   (Could not parse guidellm results: {str(e)[:50]})', 'info')

                    # Parse metrics file and extract GPU utilization
                    if metrics_file and os.path.exists(metrics_file):
                        try:
                            with open(metrics_file, 'r') as f:
                                metrics_data = _json3.load(f)
                                # Store raw JSON content for database
                                test_result.metrics_json_content = _json3.dumps(metrics_data)

                                # Extract GPU utilization (average across all GPUs)
                                if 'metrics' in metrics_data and 'gpu' in metrics_data['metrics']:
                                    gpu_metrics = metrics_data['metrics']['gpu']
                                    # Look for average utilization across all samples
                                    utilizations = []
                                    for gpu_data in gpu_metrics:
                                        if 'values' in gpu_data and len(gpu_data['values']) > 0:
                                            # Each value is [timestamp, utilization_percentage]
                                            for val in gpu_data['values']:
                                                if len(val) >= 2 and val[1] is not None:
                                                    utilizations.append(float(val[1]))

                                    if utilizations:
                                        test_result.gpu_utilization = sum(utilizations) / len(utilizations)
                                        log_to_ui(f'   GPU utilization: {test_result.gpu_utilization:.1f}%', 'info')

                                # Extract KV cache usage (average across all samples)
                                if 'metrics' in metrics_data and 'vllm' in metrics_data['metrics']:
                                    vllm_metrics = metrics_data['metrics']['vllm']
                                    # Look for kv_cache_usage_perc metric
                                    kv_cache_key = f'vllm:kv_cache_usage_perc{{namespace="{TARGET_NAMESPACE}"}}'
                                    if kv_cache_key in vllm_metrics:
                                        kv_cache_data = vllm_metrics[kv_cache_key]
                                        kv_values = []
                                        if 'prefill' in kv_cache_data:
                                            for val in kv_cache_data['prefill']:
                                                if 'values' in val and len(val['values']) > 0:
                                                    for v in val['values']:
                                                        if len(v) >= 2 and v[1] is not None:
                                                            kv_values.append(float(v[1]))
                                        if 'decode' in kv_cache_data:
                                            for val in kv_cache_data['decode']:
                                                if 'values' in val and len(val['values']) > 0:
                                                    for v in val['values']:
                                                        if len(v) >= 2 and v[1] is not None:
                                                            kv_values.append(float(v[1]))

                                        if kv_values:
                                            test_result.kv_cache_usage = sum(kv_values) / len(kv_values)
                                            log_to_ui(f'   KV cache usage: {test_result.kv_cache_usage:.1f}%', 'info')
                        except Exception as e:
                            log_to_ui(f'   (Could not parse metrics file: {str(e)[:50]})', 'info')
                    else:
                        if not test_result.error_message:
                            test_result.error_message = "Benchmark failed"
                        log_to_ui(f'❌ Benchmark failed: {test_result.error_message}', 'error')

            except Exception as e:
                test_result.deployment_success = False
                test_result.error_message = str(e)
                log_to_ui(f'❌ Deployment failed: {str(e)}', 'error')
                import traceback
                tb_str = ''.join(traceback.format_exception(type(e), e, e.__traceback__))
                print(f"Deployment traceback:\n{tb_str}")
                # Log first few lines of traceback to UI for debugging
                tb_lines = tb_str.split('\n')
                for line in tb_lines[-10:]:  # Last 10 lines
                    if line.strip():
                        print(line)
                traceback.print_exc()

            # Store result with config reference for later database storage
            results.append((test_config, test_result))

            # Cleanup deployment (always cleanup, whether success or failure)
            log_to_ui('🧹 Cleaning up deployment...', 'info')
            cleanup_mgr = CleanupManager(namespace=TARGET_NAMESPACE)
            cleanup_mgr.cleanup_test(test_config.test_id)
            log_to_ui('✅ Cleanup complete', 'success')

            # Log test result
            if test_result.deployment_success:
                log_to_ui(f'✅ Test {idx} completed successfully', 'success')
            else:
                log_to_ui(f'⚠️  Test {idx} failed: {test_result.error_message}', 'warning')

        log_to_ui('', 'info')
        log_to_ui(f'✅ All {len(results)} tests completed', 'success')

        # Step 3: Store results in database
        log_to_ui("\n💾 Step 3: Storing results in database...", 'info')

        # Prepare batch insert data
        test_config_rows = []
        for test_config, test_result in results:
            # Use test_config fields instead of parsing test_id
            tp = test_config.tensor_parallelism

            # Determine config name based on architecture
            # Include workload (ISL/OSL) to make name unique
            if test_config.architecture == 'aggregated':
                config_name = f"aggregated-tp{tp}-isl{test_config.isl}-osl{test_config.osl}"
                prefill_pods = 0
                decode_pods = 0
            elif test_config.architecture == 'pd':
                config_name = f"pd-{test_config.prefill_decode_ratio}-tp{tp}-isl{test_config.isl}-osl{test_config.osl}"
                prefill_pods = test_config.prefill_replicas
                decode_pods = test_config.decode_replicas
            else:  # ep
                config_name = f"ep-tp{tp}-isl{test_config.isl}-osl{test_config.osl}"
                prefill_pods = 0
                decode_pods = 0

            test_config_rows.append((
                run_id,
                config_name,
                prefill_pods,
                decode_pods,
                tp,
                'completed' if test_result.guidellm_success else 'failed',
                test_result.ttft_p50,
                test_result.ttft_p90,
                test_result.ttft_p95,
                test_result.ttft_p99,
                test_result.itl_p50,
                test_result.itl_p90,
                test_result.itl_p95,
                test_result.itl_p99,
                test_result.throughput_p50,
                test_result.throughput_p90,
                test_result.throughput_p95,
                test_result.throughput_p99,
                test_result.gpu_utilization,
                test_result.kv_cache_usage,
                test_result.test_start_time,
                test_result.test_end_time,
                test_result.metrics_json_content
            ))

        with get_db() as conn:
            cursor = conn.cursor()

            # Batch insert test configurations
            cursor.executemany('''
                INSERT INTO test_configurations
                (run_id, config_name, prefill_pods, decode_pods, tensor_parallelism,
                 status, ttft_p50, ttft_p90, ttft_p95, ttft_p99,
                 itl_p50, itl_p90, itl_p95, itl_p99,
                 throughput_p50, throughput_p90, throughput_p95, throughput_p99,
                 gpu_utilization, kv_cache_usage, started_at, completed_at, metrics_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', test_config_rows)

            # Update run status
            cursor.execute('''
                UPDATE optimization_runs
                SET status = ?, completed_at = ?
                WHERE id = ?
            ''', ('completed', datetime.now().isoformat(), run_id))

        log_to_ui("\n✅ Optimization run completed successfully!", 'success')
        log_to_ui(f"Results saved to database (run_id: {run_id})", 'success')

    except Exception as e:
        error_msg = f"Optimization failed: {str(e)}"
        log_to_ui(f"\n❌ {error_msg}", 'error')
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()

        # Update database if run was created
        if run_id:
            try:
                with get_db() as conn:
                    cursor = conn.cursor()
                    cursor.execute('''
                        UPDATE optimization_runs
                        SET status = ?, completed_at = ?, notes = ?
                        WHERE id = ?
                    ''', ('failed', datetime.now().isoformat(), error_msg, run_id))
            except Exception:
                pass

    finally:
        with state_lock:
            state['optimization_running'] = False
            save_state()

            try:
                with get_db() as conn:
                    conn.execute('''
                        UPDATE ui_session_state
                        SET optimization_running = 0,
                            updated_at = ?
                        WHERE id = 1
                    ''', (datetime.now().isoformat(),))
                    if run_id:
                        conn.execute('''
                            UPDATE optimization_runs
                            SET status = CASE WHEN status = 'running' THEN 'stopped' ELSE status END,
                                completed_at = COALESCE(completed_at, ?)
                            WHERE id = ?
                        ''', (datetime.now().isoformat(), run_id))
            except Exception as e:
                print(f"Warning: Failed to update optimization state in database: {e}")

        socketio.emit('status_update', {'running': False, 'message': 'Optimization finished'})

