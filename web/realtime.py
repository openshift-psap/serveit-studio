"""SocketIO event handlers — real-time browser communication."""

import os
import sys
import json
import time
import sqlite3
import logging
import subprocess
from datetime import datetime
from typing import Optional, Dict, List

from flask import session, request, jsonify, send_file
from flask_socketio import emit
from gevent import spawn

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from web.app_context import (
    app, socketio, get_db, DB_PATH, TARGET_NAMESPACE,
    OPTIMIZATION_OUTPUT_DIR, STATE_DIR, STATE_FILE,
    state, state_lock, APP_PATH,
    _session_lock, _SESSION_TIMEOUT_SECS
)

from core import SystemScanner, TestResult, DeploymentManager, TestConfig, TestOrchestrator
from core.web_deployer import DeploymentOrchestrator
from core.k8s_utils import KubectlRunner
from core.template_manager import TemplateManager

from web.optimization import log_to_ui, run_optimization_background, stream_job_logs
from web.database import save_state, save_deployment_template, get_resumable_run

logger = logging.getLogger(__name__)

# --- SocketIO Events ---

@socketio.on('connect')
def handle_connect():
    """Handle client connection — enforce single active UI tab."""
    import time as _time
    from flask import request as flask_request
    sid = flask_request.sid
    username = session.get('user', 'unknown')
    tab_id = flask_request.args.get('tab_id', '')

    with _session_lock:
        # Check if existing session is stale (no heartbeat)
        if state['active_ui_session'] and state['active_ui_session']['sid'] != sid:
            elapsed = _time.time() - state['active_ui_session'].get('last_heartbeat', 0)
            if elapsed > _SESSION_TIMEOUT_SECS:
                print(f"Stale session from {state['active_ui_session']['username']} (no heartbeat for {elapsed:.0f}s) — clearing")
                state['active_ui_session'] = None

        if state['active_ui_session'] and state['active_ui_session']['sid'] != sid:
            # Same tab reconnecting after socket drop — update the SID
            if tab_id and state['active_ui_session'].get('tab_id') == tab_id:
                state['active_ui_session']['sid'] = sid
                state['active_ui_session']['last_heartbeat'] = _time.time()
                print(f'Client {sid} ({username}) reconnected same tab')
                _replay_state_to_client()
                return

            emit('session_locked', {
                'username': state['active_ui_session']['username'],
                'connected_at': state['active_ui_session']['connected_at'],
            })
            print(f"Client {sid} ({username}) blocked — UI in use by {state['active_ui_session']['username']}")
            return

        state['active_ui_session'] = {
            'sid': sid,
            'tab_id': tab_id,
            'username': username,
            'connected_at': datetime.now().strftime('%H:%M:%S'),
            'last_heartbeat': _time.time(),
        }
    print(f'Client {sid} ({username}) is now the active UI session')
    _replay_state_to_client()


def _replay_state_to_client():
    """Send current optimization status and recent logs to the calling client."""
    optimization_running = state['optimization_running']
    try:
        with get_db() as conn:
            row = conn.execute('''
                SELECT optimization_running
                FROM ui_session_state
                WHERE id = 1
            ''').fetchone()
            if row:
                optimization_running = bool(row['optimization_running'])
    except Exception as e:
        print(f"Warning: Could not load optimization_running from database: {e}")

    emit('status_update', {
        'running': optimization_running,
        'config': state['current_config']
    })

    try:
        with get_db() as conn:
            logs = conn.execute(
                '''SELECT timestamp, log_type, message
                   FROM console_logs
                   ORDER BY id DESC
                   LIMIT 100'''
            ).fetchall()
            for log in reversed(logs):
                emit('console_log', {
                    'type': log['log_type'],
                    'message': log['message'],
                    'replayed': True,
                })
            if len(logs) > 0:
                emit('console_log', {
                    'type': 'info',
                    'message': f'Replayed {len(logs)} recent log messages',
                    'replayed': False,
                })
    except Exception as e:
        print(f"Warning: Could not replay logs: {e}")


@socketio.on('take_over')
def handle_take_over():
    """New client takes over — kick old session, keep optimization running."""
    import time as _time
    from flask import request as flask_request
    sid = flask_request.sid
    username = session.get('user', 'unknown')
    tab_id = flask_request.args.get('tab_id', '')

    with _session_lock:
        if state['active_ui_session'] and state['active_ui_session']['sid'] != sid:
            old_sid = state['active_ui_session']['sid']
            old_user = state['active_ui_session']['username']
            socketio.emit('session_kicked', {
                'taken_by': username,
            }, to=old_sid)
            try:
                socketio.server.disconnect(old_sid, namespace='/')
            except Exception:
                pass
            print(f'Session takeover: {username} kicked {old_user}')

        state['active_ui_session'] = {
            'sid': sid,
            'tab_id': tab_id,
            'username': username,
            'connected_at': datetime.now().strftime('%H:%M:%S'),
            'last_heartbeat': _time.time(),
        }

    emit('session_granted')
    _replay_state_to_client()


@socketio.on('disconnect')
def handle_disconnect():
    """Clear active session if the disconnecting client was the active one."""
    from flask import request as flask_request
    sid = flask_request.sid
    with _session_lock:
        if state['active_ui_session'] and state['active_ui_session']['sid'] == sid:
            print(f"Active UI session disconnected ({state['active_ui_session']['username']})")
            state['active_ui_session'] = None
        else:
            print(f'Non-active client disconnected ({sid})')


@socketio.on('heartbeat')
def handle_heartbeat():
    """Update last heartbeat timestamp for the active session."""
    import time as _time
    from flask import request as flask_request
    sid = flask_request.sid
    with _session_lock:
        if state['active_ui_session'] and state['active_ui_session']['sid'] == sid:
            state['active_ui_session']['last_heartbeat'] = _time.time()

@socketio.on('save_config')
def handle_save_config(data):
    """Save UI configuration and step to database.

    Args:
        data: Dictionary containing:
            - config: User configuration (model, isl, osl, etc.)
            - current_step: Current wizard step (0-4)
    """
    try:
        config = data.get('config', {})
        current_step = data.get('current_step', 0)

        with get_db() as conn:
            conn.execute('''
                UPDATE ui_session_state
                SET config_json = ?,
                    current_step = ?,
                    updated_at = ?
                WHERE id = 1
            ''', (json.dumps(config), current_step, datetime.now().isoformat()))

        # Broadcast config update to all connected clients (except sender)
        socketio.emit('config_updated', {
            'config': config,
            'current_step': current_step
        }, skip_sid=request.sid)

        emit('save_config_result', {'success': True})

    except Exception as e:
        print(f"Error saving config: {e}")
        emit('save_config_result', {'success': False, 'error': str(e)})

@socketio.on('load_config')
def handle_load_config():
    """Load UI configuration and step from database."""
    try:
        with get_db() as conn:
            row = conn.execute('''
                SELECT config_json, current_step, optimization_running
                FROM ui_session_state
                WHERE id = 1
            ''').fetchone()

            # Check if there's a running optimization
            running_opt = conn.execute('''
                SELECT id FROM optimization_runs
                WHERE status = 'running'
                LIMIT 1
            ''').fetchone()

            is_running = bool(running_opt)

            if row:
                config = json.loads(row['config_json']) if row['config_json'] else {}
                current_step = row['current_step']

                # If there's a running optimization, force step to 6 (monitoring)
                if is_running:
                    current_step = 7

                emit('load_config_result', {
                    'success': True,
                    'config': config,
                    'current_step': current_step,
                    'optimization_running': is_running,
                    'namespace': TARGET_NAMESPACE
                })
            else:
                # No saved session - check if there's a running optimization
                current_step = 7 if is_running else 1

                emit('load_config_result', {
                    'success': True,
                    'config': {},
                    'current_step': current_step,
                    'optimization_running': is_running,
                    'namespace': TARGET_NAMESPACE
                })

            # Emit button state update to ensure UI is in sync
            emit('status_update', {
                'running': is_running,
                'message': 'Optimization running' if is_running else 'Ready to start'
            })

    except Exception as e:
        print(f"Error loading config: {e}")
        emit('load_config_result', {'success': False, 'error': str(e)})

@socketio.on('start_optimization')
def handle_start_optimization(data):
    """Start an optimization run."""

    with state_lock:
        if state['optimization_running']:
            emit('error', {'message': 'Optimization already running'})
            return

        # Validate that test plan exists and is ready
        if not state['current_test_plan'] or not state['current_test_plan'].can_proceed:
            error_msg = 'Cannot start: No valid test plan. Please generate a test plan first.'
            if state['current_test_plan'] and state['current_test_plan'].error_message:
                error_msg = f"Cannot start: {state['current_test_plan'].error_message}"
            log_to_ui(f'❌ {error_msg}', 'error')
            emit('error', {'message': error_msg})
            return

        state['optimization_running'] = True
        state['current_config'] = data
        save_state()

        # Update database to reflect optimization running
        try:
            with get_db() as conn:
                conn.execute('''
                    UPDATE ui_session_state
                    SET optimization_running = 1,
                        updated_at = ?
                    WHERE id = 1
                ''', (datetime.now().isoformat(),))
        except Exception as e:
            print(f"Warning: Failed to update optimization_running in database: {e}")

    # Broadcast to all clients that optimization started
    socketio.emit('status_update', {'running': True, 'message': 'Optimization started'})

    # Start optimization in background greenlet
    spawn(run_optimization_background, data)

@socketio.on('resume_optimization')
def handle_resume_optimization(data):
    """Resume a previous optimization run directly from DB state."""

    with state_lock:
        if state['optimization_running']:
            emit('error', {'message': 'Optimization already running. Stop it first before resuming another run.'})
            return

    run_id = data.get('run_id')
    if not run_id:
        emit('error', {'message': 'No run_id provided for resume'})
        return

    try:
        # Load run info from database
        with get_db() as conn:
            run = conn.execute(
                'SELECT * FROM optimization_runs WHERE id = ?', (run_id,)
            ).fetchone()

        if not run:
            log_to_ui(f'❌ Run #{run_id} not found in database', 'error')
            emit('status_update', {'running': False})
            return

        run = dict(run)

        # Set optimization as running
        with state_lock:
            state['optimization_running'] = True
            save_state()

        # Update database to reflect optimization running
        try:
            with get_db() as conn:
                conn.execute('''
                    UPDATE ui_session_state
                    SET optimization_running = 1,
                        updated_at = ?
                    WHERE id = 1
                ''', (datetime.now().isoformat(),))
        except Exception as e:
            print(f"Warning: Failed to update optimization_running in database: {e}")

        socketio.emit('status_update', {'running': True, 'message': 'Optimization resumed'})

        log_to_ui(f'📦 Resuming optimization run #{run_id}...', 'info')
        log_to_ui(f'   Model: {run["model"]}', 'info')
        isl_str = f'ISL: {run["isl"]}' + (f' (σ={run["isl_stdev"]})' if run.get("isl_stdev") else '')
        osl_str = f'OSL: {run["osl"]}' + (f' (σ={run["osl_stdev"]})' if run.get("osl_stdev") else '')
        turns_str = f', Turns: {run["turns"]}' if run.get("turns", 1) > 1 else ''
        log_to_ui(f'   {isl_str}, {osl_str}, Users: {run["num_users"]}{turns_str}', 'info')
        if run.get('workload_mode') == 'dataset' and run.get('dataset_source'):
            log_to_ui(f'   Dataset: {run["dataset_source"]}', 'info')

        # Restore selected_nodes and advanced settings from saved config_json
        saved_selected_nodes = []
        saved_advanced_vllm = None
        saved_pd_search_mode = 'smart'
        saved_epp_preset = 'balanced'
        saved_epp_benchmark = False
        saved_epp_config = None
        saved_prefix_cache_mode = 'identical'
        saved_prefix_cache_groups = 5
        saved_image = None
        if run.get('config_json'):
            try:
                import json as _json
                saved_cfg = _json.loads(run['config_json'])
                saved_selected_nodes = saved_cfg.get('selected_nodes', [])
                saved_advanced_vllm = saved_cfg.get('advanced_vllm')
                saved_pd_search_mode = saved_cfg.get('pd_search_mode', 'smart')
                saved_epp_preset = saved_cfg.get('epp_preset', 'balanced')
                saved_epp_benchmark = saved_cfg.get('epp_benchmark', False)
                saved_epp_config = saved_cfg.get('epp_config')
                saved_prefix_cache_mode = saved_cfg.get('prefix_cache_mode', 'identical')
                saved_prefix_cache_groups = saved_cfg.get('prefix_cache_groups', 5)
                saved_image = saved_cfg.get('image')
            except Exception:
                pass

        # Read dataset fields from dedicated columns (fall back to config_json for old runs)
        saved_workload_mode = run.get('workload_mode') or 'synthetic'
        saved_dataset_source = run.get('dataset_source')
        saved_dataset_column = run.get('dataset_column')
        saved_dataset_max_output = run.get('dataset_max_output') or 256

        # Build optimization data from DB run record
        optimization_data = {
            'model': run['model'],
            'isl': run['isl'],
            'osl': run['osl'],
            'isl_stdev': run.get('isl_stdev'),
            'osl_stdev': run.get('osl_stdev'),
            'turns': run.get('turns', 1),
            'num_users': run['num_users'],
            'optimization_metric': run.get('goal') or 'ttft',
            'max_test_duration': run.get('test_duration') or 300,
            'stop_mode': run.get('stop_mode', 'duration'),
            'max_requests': run.get('max_requests'),
            'hf_token': data.get('hf_token'),
            'max_gpus': run.get('max_gpus') or 16,
            'use_achievable_qps': bool(run.get('use_achievable_qps', 0)),
            'selected_nodes': saved_selected_nodes,
            'workload_mode': saved_workload_mode,
            'dataset_source': saved_dataset_source,
            'dataset_column': saved_dataset_column,
            'dataset_max_output': saved_dataset_max_output,
            'rate_type': run.get('rate_type') or 'concurrent',
            'prefix_cache_hit_pct': run.get('prefix_cache_hit_pct') or 0,
            'prefix_cache_mode': saved_prefix_cache_mode,
            'prefix_cache_groups': saved_prefix_cache_groups,
            'prefix_cache_seed': run.get('prefix_cache_seed'),
            'latency_constraint_enabled': bool(run.get('latency_constraint_enabled', 0)),
            'latency_constraint_ms': run.get('latency_constraint_ms', 500),
            'latency_constraint_percentile': run.get('latency_constraint_percentile', 'p90'),
            'pd_search_mode': saved_pd_search_mode,
            'epp_preset': saved_epp_preset,
            'epp_benchmark': saved_epp_benchmark,
            'epp_config': saved_epp_config,
            'advanced_vllm': saved_advanced_vllm,
            'image': saved_image,
            'resume_run_id': run_id
        }

        # Start optimization in background
        spawn(run_optimization_background, optimization_data)

    except Exception as e:
        log_to_ui(f'❌ Failed to resume run #{run_id}: {str(e)}', 'error')
        with state_lock:
            state['optimization_running'] = False
            save_state()
        emit('status_update', {'running': False})


@socketio.on('stop_optimization')
def handle_stop_optimization():
    """Stop the running optimization."""

    with state_lock:
        if not state['optimization_running']:
            emit('error', {'message': 'No optimization running'})
            return

        state['optimization_running'] = False
        save_state()

        # Update database to reflect optimization stopped
        try:
            with get_db() as conn:
                conn.execute('''
                    UPDATE ui_session_state
                    SET optimization_running = 0,
                        updated_at = ?
                    WHERE id = 1
                ''', (datetime.now().isoformat(),))
        except Exception as e:
            print(f"Warning: Failed to update optimization_running in database: {e}")

    # Broadcast to all clients that optimization stopped
    socketio.emit('status_update', {'running': False, 'message': 'Optimization stopped'})
    socketio.emit('console_log', {'type': 'warning', 'message': '🛑 Optimization stopped by user'})

@socketio.on('cleanup_deployment')
def handle_cleanup_deployment(data):
    """Clean up last deployed test configuration."""
    try:
        log_to_ui('🧹 Checking for deployments to clean up...', 'info')

        # Get resumable run to find what to clean up
        run = get_resumable_run()
        if not run:
            log_to_ui('ℹ️  No previous deployments found to clean up', 'info')
            emit('cleanup_result', {'success': True, 'message': 'Nothing to clean up'})
            return

        log_to_ui(f'✅ Found deployment to clean up: {run["run_name"]}', 'success')

        # Get pods to clean up
        pods_to_cleanup = []
        if run['pods_deployed']:
            pods_to_cleanup = json.loads(run['pods_deployed'])
            log_to_ui(f'   Pods to remove: {len(pods_to_cleanup)}', 'info')

        if run['last_deployed_config']:
            config_dict = json.loads(run['last_deployed_config'])
            log_to_ui(f'   Configuration: {config_dict.get("architecture")} (TP={config_dict.get("tensor_parallelism")})', 'info')

        # Perform cleanup
        from core import CleanupManager
        cleanup_mgr = CleanupManager(namespace=TARGET_NAMESPACE)

        def cleanup_log(msg):
            log_to_ui(f'   {msg}', 'info')

        if pods_to_cleanup:
            success = cleanup_mgr.cleanup_last_deployment(pods_to_cleanup, log_callback=cleanup_log)
        else:
            # No specific pods, try cleaning up all Inftune Studio test deployments
            success = cleanup_mgr.cleanup_all_test_deployments(log_callback=cleanup_log)

        if success:
            # Update database to mark as cleaned up
            with get_db() as conn:
                conn.execute('''
                    UPDATE optimization_runs
                    SET deployment_status = 'cleaned_up',
                        pods_deployed = NULL
                    WHERE id = ?
                ''', (run['id'],))

            log_to_ui('', 'info')
            log_to_ui('✅ Cleanup completed successfully!', 'success')
            emit('cleanup_result', {'success': True, 'message': 'Cleanup successful'})
        else:
            log_to_ui('⚠️  Cleanup completed with warnings', 'warning')
            emit('cleanup_result', {'success': False, 'error': 'Cleanup had warnings'})

    except Exception as e:
        error_msg = f"Cleanup failed: {str(e)}"
        log_to_ui(f'❌ {error_msg}', 'error')
        import traceback
        traceback.print_exc()
        emit('cleanup_result', {'success': False, 'error': error_msg})


def _scan_networks(scanner):
    """Scan available network types using the scanner's kubectl runner."""
    try:
        from core.networking import scan_available_networks
        return scan_available_networks(scanner.kubectl)
    except Exception as e:
        print(f"Network scan error: {e}")
        return [{'id': 'eth0', 'name': 'Pod Network (TCP)', 'description': 'Standard pod networking',
                 'available': True, 'reason': '', 'rdma': False}]


@socketio.on('scan_cluster')
def handle_scan_cluster(data):
    """Scan cluster resources (GPUs, CPUs, RAM, storage classes)."""
    try:
        log_to_ui('🔍 Scanning cluster resources...', 'info')

        # Initialize system scanner
        scanner = SystemScanner(namespace=TARGET_NAMESPACE)

        # Scan cluster
        resources = scanner.scan_cluster()

        # Calculate currently used GPUs
        gpus_in_use = 0
        try:
            import json
            pods_result = scanner.kubectl.run(['get', 'pods', '--all-namespaces', '-o', 'json'], check=False)
            if pods_result.returncode == 0:
                pods_data = json.loads(pods_result.stdout)
                for pod in pods_data.get('items', []):
                    # Only count Running pods (skip Pending, Succeeded, Failed, etc.)
                    if pod.get('status', {}).get('phase') != 'Running':
                        continue
                    # Sum GPU requests from all containers
                    for container in pod.get('spec', {}).get('containers', []):
                        container_resources = container.get('resources', {})
                        if container_resources:
                            requests = container_resources.get('requests', {})
                            if requests and 'nvidia.com/gpu' in requests:
                                gpu_count = requests['nvidia.com/gpu']
                                # Handle both string and int values
                                if gpu_count and str(gpu_count) != '0':
                                    gpus_in_use += int(gpu_count)
        except Exception as e:
            log_to_ui(f'⚠️ Could not calculate GPU usage: {e}', 'warning')
            print(f"GPU usage calculation error: {e}")
            import traceback
            traceback.print_exc()

        # Detect provider and network type
        provider_name = 'unknown'
        network_type = 'nad'  # default
        dranet_available = False

        try:
            from core.providers import ProviderRegistry
            from core.web_deployer import NetworkIntegrator

            provider = ProviderRegistry.detect_provider(kubectl_runner=scanner.kubectl)
            provider_name = provider.get_provider_id()

            # Detect network type
            integrator = NetworkIntegrator(provider, scanner.kubectl)
            selected_network = integrator._select_network_type()
            network_type = selected_network.value
            dranet_available = (network_type == 'dra')

            log_to_ui(f'   Provider: {provider.get_display_name()}', 'info')
            log_to_ui(f'   Network type: {network_type.upper()}', 'info')
        except Exception as e:
            print(f"Could not detect provider/network: {e}")

        # Build per-node NIC details for database and UI
        nodes_detail = []
        all_nics_detail = []
        for node in resources.nodes:
            node_nics = []
            for nic in node.network_interfaces:
                nic_entry = {
                    'name': nic.name,
                    'type': nic.type,
                    'vendor': nic.vendor,
                    'model': nic.model,
                    'speed_gbps': nic.speed_gbps,
                    'count': nic.count,
                }
                node_nics.append(nic_entry)
                all_nics_detail.append({**nic_entry, 'node': node.name})
            nodes_detail.append({
                'name': node.name,
                'gpus': node.gpus,
                'gpu_vendor': node.gpu_vendor,
                'gpu_model': node.gpu_model,
                'gpu_memory_mb': node.gpu_memory_mb,
                'cpu_cores': node.cpu_cores,
                'cpu_model': node.cpu_model,
                'memory_gb': node.memory_gb,
                'has_rdma': node.has_rdma,
                'nics': node_nics
            })

        # Save hardware scan to database
        try:
            with get_db() as conn:
                conn.execute('''
                    INSERT INTO hardware_scans
                    (scan_timestamp, cloud_provider, node_count, gpu_node_count,
                     total_gpus, gpu_vendor, gpu_model, gpu_memory_per_gpu_mb,
                     total_gpu_memory_gb, max_gpus_per_node, total_cpu_cores,
                     total_memory_gb, cpu_model, host_model, has_rdma,
                     rdma_capable_nodes, total_nics, nodes_json, nics_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    datetime.now().isoformat(),
                    provider_name,
                    resources.node_count,
                    resources.gpu_node_count,
                    resources.total_gpus,
                    resources.gpu_vendor,
                    resources.gpu_model,
                    resources.gpu_memory_per_gpu_mb,
                    resources.total_gpu_memory_gb,
                    resources.max_gpus_per_node,
                    resources.total_cpu_cores,
                    resources.total_memory_gb,
                    resources.cpu_model,
                    resources.host_model,
                    1 if resources.has_rdma else 0,
                    resources.rdma_capable_nodes,
                    resources.total_network_interfaces,
                    json.dumps(nodes_detail),
                    json.dumps(all_nics_detail)
                ))
            log_to_ui('💾 Hardware scan saved to database', 'info')
        except Exception as db_err:
            log_to_ui(f'⚠️ Could not save hardware scan to database: {db_err}', 'warning')
            print(f"Hardware DB save error: {db_err}")

        # Convert to dict for JSON serialization
        result = {
            'total_gpus': resources.total_gpus,
            'gpus_in_use': gpus_in_use,
            'gpus_available': resources.total_gpus - gpus_in_use,
            'gpus_per_node': resources.gpus_per_node,
            'max_gpus_per_node': resources.max_gpus_per_node,
            'min_gpus_per_node': resources.min_gpus_per_node,
            'total_gpu_memory_gb': resources.total_gpu_memory_gb,
            'gpu_memory_per_gpu_mb': resources.gpu_memory_per_gpu_mb,
            'total_cpu_cores': resources.total_cpu_cores,
            'total_memory_gb': resources.total_memory_gb,
            'node_count': resources.node_count,
            'gpu_node_count': resources.gpu_node_count,
            'has_rdma': resources.has_rdma,
            'rdma_capable_nodes': resources.rdma_capable_nodes,
            'gpu_type': resources.gpu_type,
            'gpu_vendor': resources.gpu_vendor,
            'gpu_model': resources.gpu_model,
            'total_network_interfaces': resources.total_network_interfaces,
            'network_interfaces_by_type': resources.network_interfaces_by_type,
            'network_interfaces_by_vendor': resources.network_interfaces_by_vendor,
            'tp_options': resources.get_tp_options(),
            'cpu_model': resources.cpu_model,
            'host_model': resources.host_model,
            'nic_models': resources.nic_models if resources.nic_models else [],
            'nic_speeds': resources.nic_speeds if resources.nic_speeds else {},
            'nodes_detail': nodes_detail,
            'storage_classes': [
                {
                    'name': sc.name,
                    'provisioner': sc.provisioner,
                    'reclaim_policy': sc.reclaim_policy,
                    'volume_binding_mode': sc.volume_binding_mode,
                    'allow_volume_expansion': sc.allow_volume_expansion
                }
                for sc in resources.storage_classes
            ],
            # Provider and network information
            'provider': provider_name,
            'network_type': network_type,
            'dranet_available': dranet_available,
            # All available network options for user selection
            'available_networks': _scan_networks(scanner),
            # Preset values from launcher (empty when running standalone)
            'preset_max_gpus': int(os.environ.get('PRESET_MAX_GPUS', 0)) or None,
            'preset_nodes': os.environ.get('PRESET_NODES', '').split(',') if os.environ.get('PRESET_NODES') else None,
        }

        emit('cluster_scan_result', result)
        log_to_ui('✅ Cluster scan complete', 'success')

    except Exception as e:
        error_msg = f"Cluster scan failed: {str(e)}"
        log_to_ui(f'❌ {error_msg}', 'error')
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
        emit('error', {'message': error_msg})

def read_config_from_pvc(pvc_name: str, model_name: str, namespace: Optional[str] = None) -> Optional[Dict]:
    """
    Read model config.json from existing PVC by deploying a temporary pod.

    Args:
        pvc_name: Name of the PVC containing the model
        model_name: HuggingFace model name (e.g., "Qwen/Qwen2.5-72B-Instruct")
        namespace: Kubernetes namespace

    Returns:
        Model config dict, or None if read fails
    """
    if namespace is None:
        namespace = TARGET_NAMESPACE
    import json
    import time
    from core.k8s_utils import KubectlRunner

    kubectl = KubectlRunner(namespace=namespace)
    pod_name = f"config-reader-{int(time.time())}"

    try:
        log_to_ui(f'📖 Reading model config from PVC: {pvc_name}', 'info')

        # Create temporary pod manifest
        pod_manifest = f"""
apiVersion: v1
kind: Pod
metadata:
  name: {pod_name}
  namespace: {namespace}
spec:
  restartPolicy: Never
  containers:
  - name: reader
    image: registry.access.redhat.com/ubi9/ubi:9.4
    command: ['sh', '-c', 'sleep 3600']
    volumeMounts:
    - name: model-storage
      mountPath: /models
      readOnly: true
  volumes:
  - name: model-storage
    persistentVolumeClaim:
      claimName: {pvc_name}
"""

        # Deploy pod
        result = kubectl.run(['apply', '-f', '-', '-n', namespace], input_data=pod_manifest)
        if result.returncode != 0:
            log_to_ui(f'❌ Failed to create config reader pod: {result.stderr}', 'error')
            return None

        log_to_ui('⏳ Waiting for config reader pod to be ready...', 'info')

        # Wait for pod to be ready (max 180 seconds)
        pod_ready = False
        for i in range(180):
            result = kubectl.run_json(['get', 'pod', pod_name, '-n', namespace])
            status = result.get('status', {})
            phase = status.get('phase')

            # Check container ready state, not just pod phase
            container_statuses = status.get('containerStatuses', [])
            if phase == 'Running' and container_statuses:
                if all(cs.get('ready', False) for cs in container_statuses):
                    log_to_ui('✅ Config reader pod ready', 'success')
                    pod_ready = True
                    break
            time.sleep(1)

        if not pod_ready:
            log_to_ui('❌ Config reader pod did not become ready', 'error')
            kubectl.run(['delete', 'pod', pod_name, '-n', namespace], check=False)
            return None

        # Give filesystem a moment to fully mount
        time.sleep(2)

        # First check what's in /models directory
        ls_result = kubectl.run(
            ['exec', pod_name, '-n', namespace, '--', 'ls', '-la', '/models'],
            check=False
        )
        if ls_result.returncode == 0:
            log_to_ui('📂 /models directory contents:', 'info')
            for line in ls_result.stdout.strip().split('\n')[:10]:  # Show first 10 items
                log_to_ui(f'   {line}', 'info')
        else:
            log_to_ui(f'⚠️  Could not list /models directory: {ls_result.stderr}', 'warning')

        # Search for config.json files in the PVC with retry logic
        find_result = None
        for attempt in range(3):
            find_result = kubectl.run(
                ['exec', pod_name, '-n', namespace, '--', 'find', '/models', '-name', 'config.json'],
                check=False
            )

            if find_result.returncode == 0 and find_result.stdout.strip():
                break

            if attempt < 2:
                log_to_ui(f'   Retry {attempt + 1}/3: Searching for config.json files...', 'info')
                time.sleep(2)

        if not find_result or find_result.returncode != 0 or not find_result.stdout.strip():
            log_to_ui('❌ No config.json files found in PVC', 'error')
            if find_result and find_result.stderr:
                log_to_ui(f'   Find error: {find_result.stderr}', 'error')
            kubectl.run(['delete', 'pod', pod_name, '-n', namespace], check=False)
            return None

        # Get all config.json paths
        all_config_paths = find_result.stdout.strip().split('\n')

        # Filter for paths containing the model name
        # Support both formats: "RedHatAI/Model-Name" and "RedHatAI--Model-Name"
        model_name_normalized = model_name.replace('/', '--')
        model_name_parts = model_name.split('/')

        matching_paths = []
        for path in all_config_paths:
            # Check if path contains model name in any format
            if (model_name in path or
                model_name_normalized in path or
                all(part in path for part in model_name_parts)):
                matching_paths.append(path)

        if not matching_paths:
            log_to_ui(f'⚠️  No config.json found for model "{model_name}"', 'warning')
            log_to_ui(f'   Available configs: {", ".join(all_config_paths)}', 'info')
            kubectl.run(['delete', 'pod', pod_name, '-n', namespace], check=False)
            return None

        # Use the first matching path
        config_path = matching_paths[0]
        log_to_ui(f'✅ Found config.json for {model_name}', 'success')

        # Read the config file
        config_content = None
        result = kubectl.run(
            ['exec', pod_name, '-n', namespace, '--', 'cat', config_path],
            check=False
        )
        if result.returncode == 0:
            config_content = result.stdout
            log_to_ui('✅ Successfully read config.json', 'success')
        else:
            log_to_ui(f'❌ Failed to read {config_path}', 'error')

        # Cleanup pod
        log_to_ui('🧹 Cleaning up config reader pod...', 'info')
        kubectl.run(['delete', 'pod', pod_name, '-n', namespace], check=False)

        if not config_content:
            log_to_ui('❌ Could not read config.json from PVC', 'error')
            return None

        # Parse JSON
        config = json.loads(config_content)
        log_to_ui('✅ Successfully read model config from PVC', 'success')
        return config

    except Exception as e:
        log_to_ui(f'❌ Failed to read config from PVC: {str(e)}', 'error')
        # Cleanup pod if it exists
        kubectl.run(['delete', 'pod', pod_name, '-n', namespace], check=False)
        return None

@socketio.on('generate_test_plan')
def handle_generate_test_plan(data):
    """Generate test plan based on model and resources."""

    try:
        from core import TestPlanner
        from core.system_scanner import SystemScanner

        model = data.get('model')
        optimization_goal = data.get('optimization_goal')
        max_gpus = data.get('max_gpus')
        isl = data.get('isl', 2048)
        osl = data.get('osl', 512)
        num_users = data.get('num_users', 100)
        use_existing_pvc = data.get('use_existing_pvc', False)
        existing_pvc_name = data.get('existing_pvc_name')
        hf_token = data.get('hf_token')

        # Get cluster info to cap TP at max_gpus_per_node
        scanner = SystemScanner(namespace=TARGET_NAMESPACE)
        cluster_resources = scanner.scan_cluster()
        max_gpus_per_node = cluster_resources.max_gpus_per_node

        # Use VRAM from cluster scan instead of UI default
        gpu_vram_gb = cluster_resources.gpu_memory_per_gpu_mb / 1024 if cluster_resources.gpu_memory_per_gpu_mb > 0 else 80
        log_to_ui(f'   GPU VRAM per GPU: {gpu_vram_gb:.0f} GB (from cluster scan)', 'info')

        log_to_ui(f'🧪 Generating test plan for {model}...', 'info')
        if hf_token:
            log_to_ui('🔑 HuggingFace token provided for authentication', 'info')

        # ALWAYS try to read config from PVC first (if available)
        # This ensures we use accurate architecture details for gpu_memory_utilization
        model_config = None

        # Priority 1: Use explicitly specified PVC
        if use_existing_pvc and existing_pvc_name:
            log_to_ui(f'📦 Using existing PVC: {existing_pvc_name}', 'info')
            model_config = read_config_from_pvc(existing_pvc_name, model)
            if model_config:
                log_to_ui('✅ Model config loaded from PVC', 'success')
            else:
                log_to_ui('⚠️  Could not read config from PVC, using fallback estimates', 'warning')

        # Priority 2: Auto-detect PVC from session state
        if not model_config:
            try:
                with get_db() as conn:
                    row = conn.execute('SELECT config_json FROM ui_session_state WHERE id = 1').fetchone()
                    if row and row['config_json']:
                        session_config = json.loads(row['config_json'])
                        pvc_name = session_config.get('pvc_name') or session_config.get('existing_pvc_name')
                        if pvc_name:
                            log_to_ui(f'📦 Auto-detected PVC from session: {pvc_name}', 'info')
                            model_config = read_config_from_pvc(pvc_name, model)
                            if model_config:
                                log_to_ui('✅ Model config loaded from PVC', 'success')
            except Exception as _e:
                # If PVC auto-detection fails, continue without it
                pass

        # Priority 3: List all PVCs and try to find one with the model
        if not model_config:
            try:
                log_to_ui('📦 Searching for PVCs with model cache...', 'info')
                scanner_ns = TARGET_NAMESPACE
                pvcs = scanner.kubectl.run(['get', 'pvc', '-n', scanner_ns, '-o', 'name'], check=False)
                if pvcs.returncode == 0 and pvcs.stdout:
                    for pvc_line in pvcs.stdout.strip().split('\n'):
                        pvc_name = pvc_line.replace('persistentvolumeclaim/', '').strip()
                        if pvc_name:
                            # Try to read config from this PVC
                            temp_config = read_config_from_pvc(pvc_name, model, namespace=scanner_ns)
                            if temp_config:
                                log_to_ui(f'📦 Found model in PVC: {pvc_name}', 'info')
                                model_config = temp_config
                                log_to_ui('✅ Model config loaded from PVC', 'success')
                                break
            except Exception as _e:
                # If PVC listing fails, continue without it
                pass

        # If still no model_config, proceed with HuggingFace fallback
        if not model_config:
            log_to_ui('ℹ️  No PVC with model found - will use HuggingFace config', 'info')

        # Check if test plan parameters have changed (avoid duplicate generation)
        test_plan_params = {
            'model': model,
            'optimization_goal': optimization_goal,
            'max_gpus': max_gpus,
            'gpu_vram_gb': gpu_vram_gb,
            'isl': isl,
            'osl': osl,
            'num_users': num_users,
            'has_model_config': bool(model_config),
            'model_config_vram': model_config.get('vram_gb') if model_config else None
        }

        # Check if we already have a cached test plan with same parameters
        with state_lock:
            if state['current_test_plan'] is not None:
                cached_params = getattr(state['current_test_plan'], '_params', None)
                if cached_params == test_plan_params:
                    log_to_ui('✅ Using cached test plan (parameters unchanged)', 'success')
                    socketio.emit('test_plan_ready', {
                        'test_plan': state['current_test_plan'].to_dict(),
                        'can_proceed': state['current_test_plan'].can_proceed,
                        'estimated_total_tests': state['current_test_plan'].estimated_total_tests,
                        'model_requirements': state['current_test_plan'].model_requirements.__dict__
                    })
                    return

        log_to_ui('🔧 Calculating VRAM requirements and planning tests...', 'info')

        planner = TestPlanner()

        # Pass model_config to planner if available
        if model_config:
            # We need to pass this to calculate_model_requirements
            # For now, we'll set it on the planner instance
            planner._pvc_model_config = model_config

        test_plan = planner.plan_tests(
            model_name=model,
            optimization_goal=optimization_goal,
            max_gpus_to_use=max_gpus,
            gpu_vram_gb=gpu_vram_gb,
            isl=isl,
            osl=osl,
            num_users=num_users,
            hf_token=hf_token,
            max_gpus_per_node=max_gpus_per_node,
            cloud_provider=cluster_resources.cloud_provider,
            node_count=cluster_resources.gpu_node_count,
            kubectl_runner=scanner.kubectl
        )

        if test_plan.can_proceed:
            # Log VRAM calculation breakdown to console FIRST (before showing tests)
            req = test_plan.model_requirements
            bytes_per_param = {'fp16': 2, 'fp8': 1, 'int8': 1, 'int4': 0.5, 'fp32': 4}
            bytes = bytes_per_param.get(req.dtype, 2)
            model_weights_gb = (req.model_size_b * 1e9 * bytes) / (1024**3)
            kv_cache_percent = (req.kv_cache_gb / model_weights_gb * 100) if model_weights_gb > 0 else 0
            activations_percent = (req.activations_gb / model_weights_gb * 100) if model_weights_gb > 0 else 0
            cuda_percent = (req.cuda_overhead_gb / model_weights_gb * 100) if model_weights_gb > 0 else 0

            log_to_ui('', 'info')  # Empty line for spacing
            log_to_ui('═══════════════════════════════════════════════════════════', 'info')
            log_to_ui('📊 VRAM CALCULATION BREAKDOWN', 'info')
            log_to_ui('═══════════════════════════════════════════════════════════', 'info')
            log_to_ui('', 'info')

            # Section 1: Model Information
            log_to_ui('🔹 MODEL INFORMATION', 'info')
            log_to_ui(f'   Model Name: {req.model_name}', 'info')
            log_to_ui(f'   Total Parameters: {req.model_size_b}B ({req.model_size_b * 1e9:,.0f})', 'info')
            log_to_ui(f'   Data Type: {req.dtype.upper()} ({bytes} bytes per parameter)', 'info')
            log_to_ui(f'   Why {req.dtype.upper()}? Detected from model name/config', 'info')
            log_to_ui('', 'info')

            # Section 2: Workload Configuration
            log_to_ui('🔹 WORKLOAD CONFIGURATION', 'info')
            log_to_ui(f'   Input Sequence Length (ISL): {req.isl} tokens', 'info')
            log_to_ui(f'   Output Sequence Length (OSL): {req.osl} tokens', 'info')
            log_to_ui(f'   Total Sequence Length: {req.isl + req.osl} tokens', 'info')
            log_to_ui('   Why this matters? KV cache scales with sequence length', 'info')
            log_to_ui('', 'info')

            # Section 3: Architecture Details (if config was loaded)
            # Check both PVC config and HuggingFace config
            actual_config = model_config or getattr(planner, '_last_model_config', None)
            if actual_config:
                num_layers = actual_config.get('num_hidden_layers', 'unknown')
                num_kv_heads = actual_config.get('num_key_value_heads', actual_config.get('num_attention_heads', 'unknown'))
                num_attn_heads = actual_config.get('num_attention_heads', 'unknown')
                hidden_size = actual_config.get('hidden_size', 'unknown')
                config_source = 'PVC' if model_config else 'HuggingFace'

                log_to_ui(f'🔹 MODEL ARCHITECTURE (from {config_source})', 'info')
                log_to_ui(f'   Layers: {num_layers} (transformer blocks stacked sequentially)', 'info')
                log_to_ui(f'   Attention Heads: {num_attn_heads} (parallel attention computations)', 'info')
                log_to_ui(f'   KV Heads: {num_kv_heads} (cached key/value attention heads)', 'info')
                if num_kv_heads != 'unknown' and num_attn_heads != 'unknown':
                    if num_kv_heads < num_attn_heads:
                        log_to_ui('   Strategy: GQA (Grouped Query Attention)', 'info')
                        log_to_ui(f'   Efficiency: {num_attn_heads // num_kv_heads} queries share 1 KV head = Lower memory', 'info')
                    else:
                        log_to_ui('   Strategy: MHA (Multi-Head Attention)', 'info')
                        log_to_ui('   Efficiency: 1:1 query to KV ratio = Higher memory', 'info')
                log_to_ui(f'   Hidden Size: {hidden_size} (dimension of internal representations)', 'info')
                log_to_ui('', 'info')
            else:
                log_to_ui('🔹 MODEL ARCHITECTURE', 'warning')
                log_to_ui('   ⚠️  WARNING: Could not fetch model config!', 'error')
                log_to_ui('', 'warning')

                # Get specific error from planner
                fetch_error = getattr(planner, '_config_fetch_error', 'unknown')

                if fetch_error == 'auth':
                    log_to_ui('   Reason: Authentication required (401/403)', 'error')
                    log_to_ui('   This is a PRIVATE model requiring HuggingFace token', 'error')
                    log_to_ui('', 'warning')
                    log_to_ui('   💡 Solution:', 'warning')
                    log_to_ui('   1. Go back to Step 2', 'warning')
                    log_to_ui('   2. Enter your HuggingFace token (HF_TOKEN)', 'warning')
                    log_to_ui('   3. Get token from: https://huggingface.co/settings/tokens', 'warning')
                elif fetch_error == 'connection':
                    log_to_ui('   Reason: No internet connectivity', 'error')
                    log_to_ui('   Cannot reach HuggingFace servers', 'error')
                    log_to_ui('', 'warning')
                    log_to_ui('   💡 Solution:', 'warning')
                    log_to_ui('   1. Check network connectivity', 'warning')
                    log_to_ui('   2. OR use "Existing PVC" mode in Step 5 (air-gapped)', 'warning')
                elif fetch_error == 'timeout':
                    log_to_ui('   Reason: Request timeout (>10 seconds)', 'error')
                    log_to_ui('   HuggingFace may be slow or unreachable', 'error')
                    log_to_ui('', 'warning')
                    log_to_ui('   💡 Solution:', 'warning')
                    log_to_ui('   1. Try again in a few moments', 'warning')
                    log_to_ui('   2. Check network connectivity', 'warning')
                elif fetch_error == 'not_found':
                    log_to_ui('   Reason: Model or config.json not found (404)', 'error')
                    log_to_ui('   Model name may be incorrect', 'error')
                    log_to_ui('', 'warning')
                    log_to_ui('   💡 Solution:', 'warning')
                    log_to_ui('   1. Verify model name in Step 2', 'warning')
                    log_to_ui('   2. Check model exists at: https://huggingface.co/{model}', 'warning')
                else:
                    log_to_ui(f'   Reason: {fetch_error}', 'error')
                    log_to_ui('', 'warning')
                    log_to_ui('   💡 Possible solutions:', 'warning')
                    log_to_ui('   1. If private model: Set HF_TOKEN in Step 2', 'warning')
                    log_to_ui('   2. If air-gapped: Use "Existing PVC" mode in Step 5', 'warning')
                    log_to_ui('   3. Check internet connectivity', 'warning')

                log_to_ui('', 'warning')
                log_to_ui('   Using FALLBACK estimates (may be inaccurate):', 'warning')
                log_to_ui('   Layers: ~40 (transformer blocks stacked sequentially)', 'warning')
                log_to_ui('   KV Heads: ~8 (cached key/value attention heads)', 'warning')
                log_to_ui('   Head Dim: ~128 (dimension of each attention head)', 'warning')
                log_to_ui('', 'warning')
                log_to_ui('   ⚠️  VRAM calculation may be INACCURATE!', 'error')
                log_to_ui('   Real model architecture may differ significantly.', 'error')
                log_to_ui('', 'info')

            # Section 4: VRAM Components
            log_to_ui('🔹 VRAM COMPONENTS', 'info')
            log_to_ui('', 'info')
            log_to_ui(f'   1️⃣  MODEL WEIGHTS: {model_weights_gb:.1f} GB', 'info')
            log_to_ui(f'       Formula: {req.model_size_b}B params × {bytes} bytes/param ÷ 1024³', 'info')
            log_to_ui('       Purpose: Store model parameters in GPU memory', 'info')
            log_to_ui('', 'info')

            log_to_ui(f'   2️⃣  KV CACHE (per request): {req.kv_cache_gb:.1f} GB ({kv_cache_percent:.1f}% of weights)', 'info')
            log_to_ui('       Formula: 2 × layers × kv_heads × head_dim × seq_len × bytes', 'info')
            log_to_ui('       Purpose: Store attention key/value tensors for ONE request', 'info')
            log_to_ui(f'       Note: Scales with sequence length ({req.isl + req.osl} tokens)', 'info')
            log_to_ui(f'       Note: Actual cache = {req.kv_cache_gb:.1f} GB × concurrent users', 'info')
            log_to_ui('', 'info')

            log_to_ui(f'   3️⃣  ACTIVATIONS: {req.activations_gb:.1f} GB ({activations_percent:.1f}% of weights)', 'info')
            log_to_ui('       Formula: Model weights × 15%', 'info')
            log_to_ui('       Purpose: Intermediate computations during inference', 'info')
            log_to_ui('       Note: Conservative estimate for batch inference', 'info')
            log_to_ui('', 'info')

            log_to_ui(f'   4️⃣  CUDA OVERHEAD: {req.cuda_overhead_gb:.1f} GB ({cuda_percent:.1f}% of weights)', 'info')
            log_to_ui('       Formula: Model weights × 5%', 'info')
            log_to_ui('       Purpose: CUDA kernels, framework overhead, buffers', 'info')
            log_to_ui('', 'info')

            log_to_ui('   ─────────────────────────────────────────────────────', 'info')
            log_to_ui(f'   TOTAL VRAM REQUIRED: {req.total_vram_gb:.1f} GB', 'info')
            log_to_ui(f'   Breakdown: {model_weights_gb:.1f} + {req.kv_cache_gb:.1f} + {req.activations_gb:.1f} + {req.cuda_overhead_gb:.1f} GB', 'info')
            log_to_ui('', 'info')

            # Section 5: GPU Requirements
            log_to_ui('🔹 GPU REQUIREMENTS', 'info')
            log_to_ui(f'   Available VRAM per GPU: {req.gpu_vram_gb:.0f} GB', 'info')
            log_to_ui(f'   Total VRAM Needed: {req.total_vram_gb:.1f} GB', 'info')
            log_to_ui('', 'info')
            log_to_ui(f'   Minimum GPUs = ⌈{req.total_vram_gb:.1f} ÷ {req.gpu_vram_gb:.0f}⌉ = {req.min_gpus} GPUs', 'info')
            log_to_ui('   Why? Model won\'t fit on fewer GPUs', 'info')
            log_to_ui('', 'info')

            # Section 6: Tensor Parallelism
            log_to_ui('🔹 TENSOR PARALLELISM (TP)', 'info')
            log_to_ui(f'   Minimum TP: {req.min_tp}', 'info')
            log_to_ui(f'   Why {req.min_tp}? TP must be power of 2 (≥ min GPUs)', 'info')
            if req.min_tp != req.min_gpus:
                log_to_ui(f'   Note: Rounded up from {req.min_gpus} to nearest power of 2', 'info')
            log_to_ui(f'   Recommended TP options: {", ".join(map(str, req.recommended_tp_options))}', 'info')
            log_to_ui('', 'info')

            # Section 7: GPU Memory Utilization
            log_to_ui('🔹 GPU MEMORY UTILIZATION (vLLM Setting)', 'info')

            # Calculate components for display
            base_gb = model_weights_gb + req.cuda_overhead_gb + req.activations_gb
            safety_buffer_gb = req.gpu_vram_gb * 0.05
            concurrent_users = num_users  # Get from user input
            total_cache_gb = concurrent_users * req.kv_cache_gb
            _total_target_gb = base_gb + total_cache_gb + safety_buffer_gb
            gpu_mem_util = req.gpu_memory_utilization

            log_to_ui('   Dynamic Setting (Capacity-Based Formula):', 'info')
            log_to_ui(f'   gpu_memory_utilization = {gpu_mem_util:.2f} ({gpu_mem_util*100:.0f}%)', 'info')
            log_to_ui('', 'info')
            log_to_ui('   Formula Breakdown:', 'info')
            log_to_ui('   Setting = (Base + Cache + Buffer) / Total_VRAM', 'info')
            log_to_ui('', 'info')
            log_to_ui('   Components:', 'info')
            log_to_ui(f'   • Base (weights + overhead + activations): {base_gb:.1f} GB', 'info')
            log_to_ui(f'   • Cache (per request): {req.kv_cache_gb:.2f} GB', 'info')
            log_to_ui(f'   • Concurrent users: {concurrent_users}', 'info')
            log_to_ui(f'   • Total cache ({concurrent_users} users × {req.kv_cache_gb:.2f}): {total_cache_gb:.1f} GB', 'info')
            log_to_ui(f'   • Safety buffer (5% of {req.gpu_vram_gb:.0f}GB): {safety_buffer_gb:.1f} GB', 'info')
            log_to_ui('', 'info')
            log_to_ui('   Calculation:', 'info')
            log_to_ui(f'   ({base_gb:.1f} + {total_cache_gb:.1f} + {safety_buffer_gb:.1f}) / {req.gpu_vram_gb:.0f} = {gpu_mem_util:.2f}', 'info')
            log_to_ui('', 'info')
            log_to_ui('   Why this matters?', 'info')
            log_to_ui('   This tells vLLM what % of GPU memory to use for the model.', 'info')
            log_to_ui('   Setting too high → OOM errors with concurrent requests', 'info')
            log_to_ui('   Setting too low → Wasted GPU memory capacity', 'info')
            log_to_ui('', 'info')

            # Section 8: Final Allocation
            log_to_ui('🔹 MINIMUM RESOURCE REQUIREMENTS', 'info')
            log_to_ui(f'   Minimum Configuration: TP={req.min_tp} (using {req.min_tp} GPUs)', 'info')
            log_to_ui(f'   VRAM per GPU: {req.estimated_vram_gb:.1f} GB / {req.gpu_vram_gb:.0f} GB available', 'info')
            utilization = (req.estimated_vram_gb / req.gpu_vram_gb * 100) if req.gpu_vram_gb > 0 else 0
            log_to_ui(f'   GPU Memory Utilization: {utilization:.1f}%', 'info')
            log_to_ui('', 'info')
            log_to_ui('🔹 OPTIMIZATION STRATEGY', 'info')
            log_to_ui('   Inftune Studio will now test multiple configurations:', 'info')
            log_to_ui('   • Different GPU counts (scaling up from minimum)', 'info')
            log_to_ui('   • Different architectures (Aggregated, EP, PD)', 'info')
            log_to_ui('   • Different pod ratios (prefill/decode balance)', 'info')
            log_to_ui('   The optimal configuration will be determined through actual', 'info')
            log_to_ui('   performance testing, not just memory calculations.', 'info')
            log_to_ui('', 'info')
            log_to_ui('═══════════════════════════════════════════════════════════', 'info')
            log_to_ui('', 'info')
            log_to_ui(f'✅ Test plan ready: {len(test_plan.tests)} tests planned', 'success')
            log_to_ui('', 'info')

            # Display cloud provider constraint warning if tests were filtered
            if test_plan.cloud_filtered_count > 0:
                total_generated = len(test_plan.tests) + test_plan.cloud_filtered_count
                log_to_ui('=' * 80, 'warning')
                log_to_ui(f'⚠️  CLOUD PROVIDER CONSTRAINT: {test_plan.cloud_provider.upper()}', 'warning')
                log_to_ui('=' * 80, 'warning')
                log_to_ui(f'{total_generated} tests were generated, but {test_plan.cloud_filtered_count} tests', 'warning')
                log_to_ui('were FILTERED OUT due to cloud provider constraints:', 'warning')
                log_to_ui('', 'warning')
                log_to_ui(f'  {test_plan.cloud_filter_reason}', 'warning')
                log_to_ui('', 'warning')
                log_to_ui(f'Tests after filtering: {len(test_plan.tests)}', 'warning')
                log_to_ui('=' * 80, 'warning')
                log_to_ui('', 'info')

            # Display detailed test plan in console
            log_to_ui('📋 Planned Tests', 'info')
            log_to_ui('', 'info')
            for idx, test in enumerate(test_plan.tests, 1):
                first_test_label = ' (First Test)' if idx == 1 else ''
                log_to_ui(f'{idx}. {test.test_name}{first_test_label}', 'info')
                log_to_ui(f'   {test.description}', 'info')
                log_to_ui(f'   Architecture: {test.architecture.value.upper()} | GPUs: {test.gpus_required} | TP: {test.tp}', 'info')
                log_to_ui('', 'info')

            log_to_ui(f'📝 Preparing test configurations with gpu_memory_utilization={req.gpu_memory_utilization:.2f}...', 'info')
            log_to_ui('', 'info')
        else:
            log_to_ui('❌ Resource validation failed', 'error')

        # Convert to dict for JSON serialization
        result = {
            'model_name': test_plan.model_name,
            'total_gpus_available': test_plan.total_gpus_available,
            'max_gpus_to_use': test_plan.max_gpus_to_use,
            'optimization_goal': test_plan.optimization_goal,
            'can_proceed': test_plan.can_proceed,
            'error_message': test_plan.error_message,
            'model_requirements': {
                'model_name': test_plan.model_requirements.model_name,
                'estimated_vram_gb': test_plan.model_requirements.estimated_vram_gb,
                'min_gpus': test_plan.model_requirements.min_gpus,
                'min_tp': test_plan.model_requirements.min_tp,
                'recommended_tp_options': test_plan.model_requirements.recommended_tp_options,
                'model_size_b': test_plan.model_requirements.model_size_b,
                'dtype': test_plan.model_requirements.dtype,
                'total_vram_gb': test_plan.model_requirements.total_vram_gb,
                'gpu_vram_gb': test_plan.model_requirements.gpu_vram_gb,
                'kv_cache_gb': test_plan.model_requirements.kv_cache_gb,
                'activations_gb': test_plan.model_requirements.activations_gb,
                'cuda_overhead_gb': test_plan.model_requirements.cuda_overhead_gb,
                'isl': test_plan.model_requirements.isl,
                'osl': test_plan.model_requirements.osl,
                'gpu_memory_utilization': test_plan.model_requirements.gpu_memory_utilization
            },
            'tests': [
                {
                    'test_name': test.test_name,
                    'architecture': test.architecture.value,
                    'gpus_required': test.gpus_required,
                    'tp': test.tp,
                    'prefill_pods': test.prefill_pods,
                    'decode_pods': test.decode_pods,
                    'ep_pods': test.ep_pods,
                    'description': test.description
                }
                for test in test_plan.tests
            ]
        }

        # Store test plan globally for later use during deployment
        with state_lock:
            state['current_test_plan'] = test_plan
            # Cache parameters to avoid duplicate generation
            state['current_test_plan']._params = test_plan_params

        # Persist test plan to database so it survives server restarts
        try:
            with get_db() as conn:
                # Get current config
                row = conn.execute('SELECT config_json FROM ui_session_state WHERE id = 1').fetchone()
                config = json.loads(row['config_json']) if row and row['config_json'] else {}

                # Add test plan to config
                config['test_plan'] = result

                # Save back to database
                conn.execute('''
                    UPDATE ui_session_state
                    SET config_json = ?, updated_at = ?
                    WHERE id = 1
                ''', (json.dumps(config), datetime.now().isoformat()))

                print("✅ Test plan persisted to database")
        except Exception as e:
            print(f"Warning: Failed to persist test plan to database: {e}")

        # Emit test plan result early so UI can display structure
        emit('test_plan_result', result)

        # Save deployment templates to database
        if test_plan.can_proceed:
            log_to_ui('', 'info')
            log_to_ui('💾 Saving deployment templates to database...', 'info')

            # Scan cluster to get resource information
            from core import SystemScanner
            scanner = SystemScanner(namespace=TARGET_NAMESPACE)
            cluster_resources = scanner.scan_cluster()

            # Calculate per-node resources based on cluster scan
            max_gpus_per_node = cluster_resources.max_gpus_per_node

            # Get CPU info from GPU nodes only (not all cluster nodes)
            gpu_nodes_list = [n for n in cluster_resources.nodes if n.gpus > 0]
            if gpu_nodes_list:
                # Use average from actual GPU nodes
                avg_memory_per_gpu_node_gb = sum(n.memory_gb for n in gpu_nodes_list) / len(gpu_nodes_list)
                avg_cpu_per_gpu_node = sum(n.cpu_cores for n in gpu_nodes_list) / len(gpu_nodes_list)
            else:
                # Fallback if no GPU nodes detected
                avg_memory_per_gpu_node_gb = 512
                avg_cpu_per_gpu_node = 48

            for test in test_plan.tests:
                # Skip placeholder tests (TP=0 or gpus_required=0)
                if test.tp == 0 or test.gpus_required == 0:
                    continue

                arch = test.architecture.value

                # Calculate resource requests PER POD based on how many pods fit on one node
                # Max pods per node = GPUs per node / TP
                max_pods_per_node = max_gpus_per_node / test.tp

                # Usable resources per node (leave headroom for system)
                usable_memory_gb = avg_memory_per_gpu_node_gb * 0.80  # 80% usable
                usable_cpus = int(avg_cpu_per_gpu_node * 0.70)  # 70% usable

                # Resources per pod = node resources / pods per node
                memory_request_gb = int(usable_memory_gb / max_pods_per_node)
                cpu_request = max(1, int(usable_cpus / max_pods_per_node))

                memory_limit = f'{memory_request_gb}Gi'
                cpu_request_str = str(cpu_request)

                if arch == 'aggregated' or arch == 'ep':
                    # Single template for aggregated/EP
                    save_deployment_template(
                        model_name=test_plan.model_name,
                        architecture=arch,
                        role=None,
                        tensor_parallelism=test.tp,
                        replicas=1,  # Single replica for initial deployment
                        max_model_len=8192,
                        gpu_memory_utilization=0.95,
                        isl=test_plan.model_requirements.isl,
                        osl=test_plan.model_requirements.osl,
                        gpus_per_pod=test.tp,
                        memory_limit=memory_limit,
                        cpu_request=cpu_request_str
                    )
                    log_to_ui(f'   ✅ Saved {arch.upper()} template (TP={test.tp}, CPU={cpu_request}, Memory={memory_limit})', 'success')

                elif arch == 'pd':
                    # Two templates for PD: prefill and decode
                    # Prefill template
                    save_deployment_template(
                        model_name=test_plan.model_name,
                        architecture='pd',
                        role='prefill',
                        tensor_parallelism=test.tp,
                        replicas=test.prefill_pods,
                        max_model_len=8192,
                        gpu_memory_utilization=0.95,
                        isl=test_plan.model_requirements.isl,
                        osl=test_plan.model_requirements.osl,
                        gpus_per_pod=test.tp,
                        memory_limit=memory_limit,
                        cpu_request=cpu_request_str
                    )

                    # Decode template (with max_num_batched_tokens)
                    save_deployment_template(
                        model_name=test_plan.model_name,
                        architecture='pd',
                        role='decode',
                        tensor_parallelism=test.tp,
                        replicas=test.decode_pods,
                        max_model_len=8192,
                        gpu_memory_utilization=0.95,
                        isl=test_plan.model_requirements.isl,
                        osl=test_plan.model_requirements.osl,
                        max_num_batched_tokens=8192,  # Decode-specific
                        gpus_per_pod=test.tp,
                        memory_limit=memory_limit,
                        cpu_request=cpu_request_str
                    )
                    log_to_ui(f'   ✅ Saved PD templates (TP={test.tp}, Prefill={test.prefill_pods}, Decode={test.decode_pods}, CPU={cpu_request}, Memory={memory_limit})', 'success')

            log_to_ui('', 'info')
            log_to_ui('🚀 All deployment templates saved - ready to deploy!', 'success')
            log_to_ui('', 'info')

            # Signal that everything is ready
            emit('test_plan_ready', {'ready': True})

    except Exception as e:
        error_msg = f"Failed to generate test plan: {str(e)}"
        log_to_ui(f'❌ {error_msg}', 'error')
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
        emit('test_plan_result', {
            'can_proceed': False,
            'error_message': error_msg
        })

@socketio.on('list_pvcs')
def handle_list_pvcs(data):
    """List all PVCs in the namespace."""
    try:
        from core.k8s_utils import KubectlRunner

        namespace = TARGET_NAMESPACE
        kubectl = KubectlRunner(namespace=namespace)

        logger.debug(f'Listing PVCs in namespace: {namespace}')

        # Get PVCs as JSON
        result = kubectl.run_json(['get', 'pvc', '-n', namespace])

        pvcs = []
        if result and 'items' in result:
            for pvc in result['items']:
                metadata = pvc.get('metadata', {})
                spec = pvc.get('spec', {})
                status = pvc.get('status', {})

                pvc_info = {
                    'name': metadata.get('name', 'unknown'),
                    'size': status.get('capacity', {}).get('storage', 'unknown'),
                    'storage_class': spec.get('storageClassName', 'unknown'),
                    'status': status.get('phase', 'unknown')
                }
                pvcs.append(pvc_info)

        # Log PVC names
        pvc_names = ', '.join([p['name'] for p in pvcs])
        logger.debug(f'Found {len(pvcs)} PVC(s): {pvc_names}')

        emit('pvc_list_result', {
            'success': True,
            'pvcs': pvcs
        })

    except Exception as e:
        error_msg = f'Failed to list PVCs: {str(e)}'
        log_to_ui(f'❌ {error_msg}', 'error')
        emit('pvc_list_result', {
            'success': False,
            'error': error_msg,
            'pvcs': []
        })

@socketio.on('fetch_image_tags')
def handle_fetch_image_tags(data):
    """Fetch available container image tags from a registry."""
    repo = data.get('repo', 'ghcr.io/llm-d/llm-d-cuda').strip()
    try:
        import requests as _req

        # Parse registry, namespace, and image from repo string
        parts = repo.split('/')
        if len(parts) >= 3:
            registry = parts[0]
            image_path = '/'.join(parts[1:])
        else:
            emit('image_tags_result', {'error': 'Invalid repo format. Use registry/org/image'})
            return

        # Get anonymous token (works for public repos on ghcr.io, quay.io, docker.io)
        token = None
        if 'ghcr.io' in registry:
            r = _req.get(f'https://ghcr.io/token?scope=repository:{image_path}:pull', timeout=10)
            if r.ok:
                token = r.json().get('token')
            api_url = f'https://ghcr.io/v2/{image_path}/tags/list'
        elif 'quay.io' in registry:
            api_url = f'https://quay.io/v2/{image_path}/tags/list'
        elif 'docker.io' in registry or 'registry-1.docker.io' in registry:
            r = _req.get(f'https://auth.docker.io/token?service=registry.docker.io&scope=repository:{image_path}:pull', timeout=10)
            if r.ok:
                token = r.json().get('token')
            api_url = f'https://registry-1.docker.io/v2/{image_path}/tags/list'
        else:
            # Generic v2 registry (internal registries)
            api_url = f'https://{registry}/v2/{image_path}/tags/list'

        headers = {}
        if token:
            headers['Authorization'] = f'Bearer {token}'

        r = _req.get(api_url, headers=headers, timeout=15)
        if not r.ok:
            emit('image_tags_result', {'error': f'Registry returned {r.status_code}'})
            return

        tags = r.json().get('tags', [])
        # Sort: stable releases first (vX.Y.Z), then RCs, then others
        import re
        def tag_sort_key(t):
            if re.match(r'^v\d+\.\d+\.\d+$', t):
                return (0, t)
            elif 'rc' in t:
                return (1, t)
            else:
                return (2, t)
        tags = sorted(tags, key=tag_sort_key, reverse=True)

        emit('image_tags_result', {'tags': tags, 'repo': repo})
    except Exception as e:
        emit('image_tags_result', {'error': str(e)[:200]})


@socketio.on('setup_storage')
def handle_setup_storage(data):
    """Create PVC and start model download job."""

    try:
        from core import TemplateManager

        existing_pvc = data.get('existing_pvc')
        storage_class = data.get('storage_class')
        pvc_size = int(data.get('pvc_size', 256))
        if pvc_size < 50:
            log_to_ui(f'⚠️ Model cache PVC size is {pvc_size}Gi — this may be too small for large models. Minimum recommended: 100Gi.', 'warning')
        model = data.get('model')
        hf_token = data.get('hf_token')
        namespace = TARGET_NAMESPACE

        # Fallback to saved config if frontend didn't send existing_pvc
        if not existing_pvc:
            try:
                with get_db() as conn:
                    row = conn.execute('SELECT config_json FROM ui_session_state WHERE id = 1').fetchone()
                    if row and row['config_json']:
                        saved = json.loads(row['config_json'])
                        if saved.get('use_existing_pvc') and saved.get('existing_pvc_name'):
                            existing_pvc = saved['existing_pvc_name']
                            log_to_ui(f'📦 Using saved PVC config: {existing_pvc}', 'info')
                        if not model and saved.get('model'):
                            model = saved['model']
            except Exception:
                pass

        log_to_ui('📦 Setting up persistent model cache...', 'info')

        # Set optimization as running
        with state_lock:
            state['optimization_running'] = True
            save_state()

        # Update database to reflect optimization running
        try:
            with get_db() as conn:
                conn.execute('''
                    UPDATE ui_session_state
                    SET optimization_running = 1,
                        updated_at = ?
                    WHERE id = 1
                ''', (datetime.now().isoformat(),))
        except Exception as e:
            print(f"Warning: Failed to update optimization_running in database: {e}")

        socketio.emit('status_update', {'running': True})

        # Check if user specified an existing PVC
        if existing_pvc:
            log_to_ui(f'✅ Using existing PVC: {existing_pvc}', 'success')
            emit('storage_setup_result', {
                'success': True,
                'pvc_name': existing_pvc,
                'pvc_size': 'existing',
                'storage_class': 'existing',
                'model': model,
                'existing': True
            })

            # Skip model download and go straight to optimization
            log_to_ui('✅ Model already available in PVC', 'success')
            log_to_ui('', 'info')
            log_to_ui('🚀 Starting optimization...', 'info')

            # Prepare optimization data from client config
            resume_run_id = data.get('resume_run_id')
            optimization_data = {
                'model': model,
                'isl': data.get('isl', 3000),
                'osl': data.get('osl', 100),
                'isl_stdev': data.get('isl_stdev'),
                'osl_stdev': data.get('osl_stdev'),
                'turns': data.get('turns', 1),
                'num_users': data.get('num_users', 100),
                'optimization_metric': data.get('optimization_goal', 'ttft'),
                'max_test_duration': data.get('duration', 300),
                'stop_mode': data.get('stop_mode', 'duration'),
                'max_requests': data.get('max_requests'),
                'hf_token': hf_token,
                'max_gpus': data.get('max_gpus', 16),
                'use_achievable_qps': data.get('use_achievable_qps', False),
                'latency_constraint_enabled': data.get('latency_constraint_enabled', False),
                'latency_constraint_ms': data.get('latency_constraint_ms', 500),
                'latency_constraint_percentile': data.get('latency_constraint_percentile', 'p90'),
                'tp_pair_top_n': data.get('tp_pair_top_n', 2),
                'pd_search_mode': data.get('pd_search_mode', 'smart'),
                'run_description': data.get('run_description', ''),
                'epp_preset': data.get('epp_preset', 'balanced'),
                'epp_benchmark': data.get('epp_benchmark', False),
                'epp_config': data.get('epp_config'),
                'selected_nodes': data.get('selected_nodes') or [],
                'workload_mode': data.get('workload_mode', 'synthetic'),
                'dataset_source': data.get('dataset_source'),
                'dataset_column': data.get('dataset_column'),
                'dataset_max_output': int(data.get('dataset_max_output', 256)),
                'rate_type': data.get('rate_type', 'concurrent'),
                'prefix_cache_hit_pct': int(data.get('prefix_cache_hit_pct', 0)),
                'advanced_vllm': data.get('advanced_vllm'),
                'image': data.get('image'),
                'single_test_architecture': data.get('single_test_architecture'),
                'single_test_tp': data.get('single_test_tp'),
                'single_test_replicas': data.get('single_test_replicas'),
                'single_test_prefill_tp': data.get('single_test_prefill_tp'),
                'single_test_decode_tp': data.get('single_test_decode_tp'),
                'single_test_prefill_pods': data.get('single_test_prefill_pods'),
                'single_test_decode_pods': data.get('single_test_decode_pods'),
            }
            if resume_run_id:
                optimization_data['resume_run_id'] = resume_run_id

            # Start optimization in background
            spawn(run_optimization_background, optimization_data)
            return

        # Create new PVC and download model
        pvc_name = 'inftune-model-cache'
        timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        job_name = f'inftune-model-download-{timestamp}'
        test_id = f'inftune-setup-{timestamp}'

        # Initialize TemplateManager
        template_mgr = TemplateManager()

        # Check if PVC already exists
        check_cmd = ['kubectl', 'get', 'pvc', pvc_name, '-n', namespace]
        proc = subprocess.run(check_cmd, capture_output=True, timeout=10)
        pvc_exists = proc.returncode == 0

        if not pvc_exists:
            log_to_ui(f'📦 Creating PVC {pvc_name} ({pvc_size}Gi)...', 'info')

            # Render PVC template
            pvc_yaml = template_mgr.render_template(
                'prereq/model-cache-pvc.yaml.j2',
                pvc_name=pvc_name,
                namespace=namespace,
                test_id=test_id,
                model_name=model,
                storage_class=storage_class,
                storage_size=pvc_size
            )

            cmd = ['kubectl', 'apply', '-f', '-']
            proc = subprocess.run(cmd, input=pvc_yaml.encode(), capture_output=True, timeout=30)

            if proc.returncode != 0:
                raise Exception(f"PVC creation failed: {proc.stderr.decode()}")

            log_to_ui(f'✅ PVC {pvc_name} created', 'success')
        else:
            log_to_ui(f'✅ PVC {pvc_name} already exists (reusing)', 'success')

        # Create model download job
        log_to_ui(f'📥 Starting model download: {model}', 'info')

        # Render job template (uses lightweight Red Hat UBI Python image by default)
        job_yaml = template_mgr.render_template(
            'prereq/model-download-job.yaml.j2',
            job_name=job_name,
            namespace=namespace,
            test_id=test_id,
            model_name=model,
            pvc_name=pvc_name,
            hf_token=hf_token
        )

        cmd = ['kubectl', 'apply', '-f', '-']
        proc = subprocess.run(cmd, input=job_yaml.encode(), capture_output=True, timeout=30)

        if proc.returncode != 0:
            raise Exception(f"Job creation failed: {proc.stderr.decode()}")

        log_to_ui(f'✅ Model download job {job_name} started', 'success')
        log_to_ui('⏳ Streaming download progress...', 'info')

        emit('storage_setup_result', {
            'success': True,
            'pvc_name': pvc_name,
            'pvc_size': pvc_size,
            'storage_class': storage_class,
            'model': model,
            'job_name': job_name,
            'existing': False
        })

        # Start background task to stream job logs
        spawn(stream_job_logs, job_name, namespace)

    except Exception as e:
        error_msg = f"Storage setup failed: {str(e)}"
        log_to_ui(f'❌ {error_msg}', 'error')
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()

        # Reset optimization state on error
        with state_lock:
            state['optimization_running'] = False
            save_state()
        emit('status_update', {'running': False})
        emit('storage_setup_result', {'success': False, 'error': str(e)})

@socketio.on('reset_database')
def handle_reset_database(data):
    """Reset database and all settings."""
    try:
        log_to_ui('🔄 Resetting database...', 'warning')

        # Close all existing database connections
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        # Delete all data from tables
        cursor.execute('DELETE FROM console_logs')
        cursor.execute('DELETE FROM test_configurations')
        cursor.execute('DELETE FROM optimization_runs')
        cursor.execute('DELETE FROM deployment_templates')
        cursor.execute('DELETE FROM ui_session_state')

        conn.commit()
        conn.close()

        # Clear state file
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)

        # Reset global variables
        with state_lock:
            state['optimization_running'] = False
            state['current_config'] = {}
            state['current_test_plan'] = None
            save_state()

        log_to_ui('✅ Database reset complete', 'success')
        emit('reset_complete', {'success': True})

    except Exception as e:
        error_msg = f"Reset failed: {str(e)}"
        log_to_ui(f'❌ {error_msg}', 'error')
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
        emit('reset_complete', {'success': False, 'error': str(e)})

@app.route('/api/optuna_trials/<int:run_id>')
def get_optuna_trials(run_id):
    """Get Optuna trial history for a run."""
    try:
        step = request.args.get('step', 'step9_latency_bounded')
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute('''
            SELECT * FROM optuna_trials
            WHERE run_id = ? AND optimization_step = ?
            ORDER BY trial_number ASC
        ''', (run_id, step))
        trials = [dict(row) for row in cursor.fetchall()]

        cursor.execute('''
            SELECT * FROM optuna_studies
            WHERE run_id = ? AND optimization_step = ?
        ''', (run_id, step))
        study_row = cursor.fetchone()
        study = dict(study_row) if study_row else None

        conn.close()
        return jsonify({'trials': trials, 'study': study})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/latency_search/<int:run_id>')
def get_latency_search_trials(run_id):
    """Get latency binary search trials for a run — used for cost-of-latency charting."""
    try:
        arch_filter = request.args.get('architecture')
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        if arch_filter:
            cursor.execute('''
                SELECT * FROM latency_search_trials
                WHERE run_id = ? AND architecture = ?
                ORDER BY trial_number ASC
            ''', (run_id, arch_filter))
        else:
            cursor.execute('''
                SELECT * FROM latency_search_trials
                WHERE run_id = ?
                ORDER BY architecture, trial_number ASC
            ''', (run_id,))

        trials = [dict(row) for row in cursor.fetchall()]
        conn.close()

        # Group by architecture for charting
        by_arch = {}
        for t in trials:
            arch = t['architecture']
            by_arch.setdefault(arch, []).append(t)

        return jsonify({'trials': trials, 'by_architecture': by_arch})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@socketio.on('compress_database')
def handle_compress_database():
    """Compress the database file and emit progress events."""
    try:
        import gzip as gzip_mod

        if not os.path.exists(DB_PATH):
            emit('compression_error', {'error': 'Database file not found'})
            return

        total_size = os.path.getsize(DB_PATH)
        compressed_path = '/tmp/inftune-optimizer.db.gz'

        emit('compression_progress', {'percent': 0, 'status': 'Compressing...',
             'original_size': total_size})

        chunk_size = 256 * 1024
        bytes_read = 0

        with open(DB_PATH, 'rb') as f_in, gzip_mod.open(compressed_path, 'wb', compresslevel=6) as f_out:
            while True:
                chunk = f_in.read(chunk_size)
                if not chunk:
                    break
                f_out.write(chunk)
                bytes_read += len(chunk)
                percent = min(int((bytes_read / total_size) * 100), 99)
                emit('compression_progress', {'percent': percent, 'status': 'Compressing...'})
                socketio.sleep(0)

        compressed_size = os.path.getsize(compressed_path)
        ratio = ((total_size - compressed_size) / total_size) * 100

        emit('compression_progress', {'percent': 100, 'status': 'Done!'})
        emit('compression_complete', {
            'original_size': total_size,
            'compressed_size': compressed_size,
            'ratio': round(ratio, 1)
        })

    except Exception as e:
        print(f"ERROR compressing database: {e}")
        import traceback
        traceback.print_exc()
        emit('compression_error', {'error': str(e)})

@app.route('/api/download_database')
def download_database():
    """Download the compressed database file."""
    try:
        from flask import send_file, after_this_request

        instance_name = os.environ.get('INSTANCE_NAME', 'inftune')
        download_filename = f'{instance_name}.db.gz'

        compressed_path = '/tmp/inftune-optimizer.db.gz'
        if os.path.exists(compressed_path):
            @after_this_request
            def cleanup(response):
                try:
                    os.remove(compressed_path)
                except Exception:
                    pass
                return response

            return send_file(
                compressed_path,
                mimetype='application/gzip',
                as_attachment=True,
                download_name=download_filename
            )

        if not os.path.exists(DB_PATH):
            return jsonify({'error': 'Database file not found'}), 404

        return send_file(
            DB_PATH,
            mimetype='application/x-sqlite3',
            as_attachment=True,
            download_name=f'{instance_name}.db'
        )
    except Exception as e:
        print(f"ERROR downloading database: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/api/upload-dataset', methods=['POST'])
def upload_dataset():
    """Upload a custom dataset file for benchmarking."""
    try:
        if 'file' not in request.files:
            return jsonify({'success': False, 'error': 'No file provided'})
        f = request.files['file']
        if not f.filename:
            return jsonify({'success': False, 'error': 'Empty filename'})
        allowed = {'.csv', '.json', '.jsonl', '.txt'}
        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in allowed:
            return jsonify({'success': False, 'error': f'Unsupported file type: {ext}'})
        dataset_dir = os.path.join(APP_PATH, 'data', 'datasets')
        os.makedirs(dataset_dir, exist_ok=True)
        safe_name = f.filename.replace('/', '_').replace('\\', '_')
        dest = os.path.join(dataset_dir, safe_name)
        f.save(dest)
        return jsonify({'success': True, 'path': dest, 'filename': safe_name})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/upload_database', methods=['POST'])
def upload_database():
    """Import optimization runs and test results from an uploaded database file."""
    import tempfile
    try:
        if 'database' not in request.files:
            return jsonify({'success': False, 'error': 'No database file provided'}), 400

        file = request.files['database']
        if not file.filename:
            return jsonify({'success': False, 'error': 'Empty filename'}), 400

        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as tmp:
            tmp_path = tmp.name
            file.save(tmp_path)

        # Decompress .gz files
        if file.filename.endswith('.gz'):
            import gzip as gzip_mod
            decompressed_path = tmp_path + '.decompressed.db'
            try:
                with gzip_mod.open(tmp_path, 'rb') as gz_in, open(decompressed_path, 'wb') as db_out:
                    while True:
                        chunk = gz_in.read(256 * 1024)
                        if not chunk:
                            break
                        db_out.write(chunk)
                os.remove(tmp_path)
                tmp_path = decompressed_path
            except Exception as e:
                os.remove(tmp_path)
                if os.path.exists(decompressed_path):
                    os.remove(decompressed_path)
                return jsonify({'success': False, 'error': f'Failed to decompress .gz file: {e}'}), 400

        try:
            src_conn = sqlite3.connect(tmp_path)
            src_conn.row_factory = sqlite3.Row

            src_tables = [r[0] for r in src_conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()]
            if 'optimization_runs' not in src_tables or 'test_configurations' not in src_tables:
                src_conn.close()
                return jsonify({'success': False, 'error': 'Not a valid Inftune Studio database (missing required tables)'}), 400

            src_runs = src_conn.execute('SELECT * FROM optimization_runs ORDER BY id').fetchall()
            if not src_runs:
                src_conn.close()
                return jsonify({'success': False, 'error': 'Database contains no optimization runs'}), 400

            imported_runs = 0
            imported_tests = 0
            skipped_runs = 0

            source_label = os.path.splitext(file.filename)[0]

            with get_db() as dst_conn:
                dst_cols = [r[1] for r in dst_conn.execute('PRAGMA table_info(optimization_runs)').fetchall()]
                src_cols = [r['name'] for r in src_conn.execute('PRAGMA table_info(optimization_runs)').fetchall()]
                common_run_cols = [c for c in src_cols if c in dst_cols and c != 'id']

                dst_test_cols = [r[1] for r in dst_conn.execute('PRAGMA table_info(test_configurations)').fetchall()]
                src_test_cols = [r['name'] for r in src_conn.execute('PRAGMA table_info(test_configurations)').fetchall()]
                common_test_cols = [c for c in src_test_cols if c in dst_test_cols and c not in ('id', 'run_id')]

                for src_run in src_runs:
                    src_run_dict = dict(src_run)
                    old_run_id = src_run_dict['id']

                    orig_name = src_run_dict.get('run_name', f'run-{old_run_id}')
                    new_name = f"[{source_label}] {orig_name}"

                    existing = dst_conn.execute(
                        'SELECT id FROM optimization_runs WHERE run_name = ?', (new_name,)
                    ).fetchone()
                    if existing:
                        skipped_runs += 1
                        continue

                    col_names = [c for c in common_run_cols if c != 'run_name']
                    placeholders = ', '.join(['?'] * (len(col_names) + 1))
                    col_str = 'run_name, ' + ', '.join(col_names)
                    values = [new_name] + [src_run_dict.get(c) for c in col_names]

                    cursor = dst_conn.execute(
                        f'INSERT INTO optimization_runs ({col_str}) VALUES ({placeholders})',
                        values
                    )
                    new_run_id = cursor.lastrowid
                    imported_runs += 1

                    src_tests = src_conn.execute(
                        'SELECT * FROM test_configurations WHERE run_id = ? ORDER BY id',
                        (old_run_id,)
                    ).fetchall()

                    for src_test in src_tests:
                        src_test_dict = dict(src_test)
                        test_col_str = 'run_id, ' + ', '.join(common_test_cols)
                        test_placeholders = ', '.join(['?'] * (len(common_test_cols) + 1))
                        test_values = [new_run_id] + [src_test_dict.get(c) for c in common_test_cols]

                        dst_conn.execute(
                            f'INSERT INTO test_configurations ({test_col_str}) VALUES ({test_placeholders})',
                            test_values
                        )
                        imported_tests += 1

            src_conn.close()
            return jsonify({
                'success': True,
                'imported_runs': imported_runs,
                'imported_tests': imported_tests,
                'skipped_runs': skipped_runs,
                'source': file.filename,
            })

        finally:
            os.unlink(tmp_path)

    except Exception as e:
        print(f"ERROR uploading database: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/run/<int:run_id>/config/<config_name>/manifest/<manifest_type>')
def download_manifest(run_id, config_name, manifest_type):
    """Download a rendered LWS YAML manifest for a specific test configuration.

    Args:
        run_id: Optimization run ID
        config_name: Test config name (e.g. step7-pd-p2d6-ptp4-dtp2)
        manifest_type: Manifest key (e.g. lws, service, prefill, decode, prefill-service, decode-service)
    """
    try:
        import json
        from flask import Response

        with get_db() as conn:
            row = conn.execute(
                'SELECT manifests_yaml FROM test_configurations WHERE run_id = ? AND config_name = ?',
                (run_id, config_name)
            ).fetchone()

        if not row or not row['manifests_yaml']:
            return jsonify({'error': 'No manifests found for this configuration'}), 404

        manifests = json.loads(row['manifests_yaml'])

        if manifest_type not in manifests:
            return jsonify({
                'error': f'Manifest type "{manifest_type}" not found',
                'available': list(manifests.keys())
            }), 404

        yaml_content = manifests[manifest_type]
        filename = f"{config_name}-{manifest_type}.yaml"

        return Response(
            yaml_content,
            mimetype='application/x-yaml',
            headers={'Content-Disposition': f'attachment; filename="{filename}"'}
        )

    except Exception as e:
        print(f"ERROR downloading manifest: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/run/<int:run_id>/config/<config_name>/manifests')
def get_manifest_list(run_id, config_name):
    """List available manifests for a test configuration."""
    try:
        import json

        with get_db() as conn:
            row = conn.execute(
                'SELECT manifests_yaml FROM test_configurations WHERE run_id = ? AND config_name = ?',
                (run_id, config_name)
            ).fetchone()

        if not row or not row['manifests_yaml']:
            return jsonify({'available': []})

        manifests = json.loads(row['manifests_yaml'])
        return jsonify({'available': list(manifests.keys())})

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# --- Application Entry Point ---

