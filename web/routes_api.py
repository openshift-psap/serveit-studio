"""REST API routes — /api/runs, /api/charts, /api/config, etc."""

import os
import sys
import json
import shutil
import logging
from datetime import datetime
from pathlib import Path
from flask import jsonify, request, render_template, Response

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from web.app_context import app, socketio, get_db, DB_PATH, TARGET_NAMESPACE, state, state_lock
from web.database import save_state, save_deployment_template, get_deployment_template, get_resumable_run

logger = logging.getLogger(__name__)

# --- Flask Routes ---

@app.route('/')
def index():
    """Serve the main UI page."""
    return render_template('index.html')

@app.route('/api/status')
def get_status():
    """Get current optimization status."""
    _goal_labels = {'balanced': 'full_coverage', 'ttft': 'ttft', 'throughput': 'throughput',
                    'aggregated_only': 'aggregated_only', 'pd_only': 'pd_only',
                    'ep_only': 'ep_only', 'single_test': 'single_test'}
    with state_lock:
        cfg = state['current_config'] or {}
        if cfg.get('goal') in _goal_labels:
            cfg = dict(cfg)
            cfg['goal'] = _goal_labels[cfg['goal']]
        return jsonify({
            'running': state['optimization_running'],
            'config_locked': state.get('config_locked', False),
            'config': cfg
        })

@app.route('/api/scan', methods=['POST'])
def api_scan_cluster():
    """Trigger a cluster scan and return results (REST alternative to Socket.IO scan_cluster)."""
    try:
        from core.system_scanner import SystemScanner
        from core.version_scanner import scan_versions
        from core.providers import ProviderRegistry
        from core.web_deployer import NetworkIntegrator

        scanner = SystemScanner(namespace=TARGET_NAMESPACE)
        resources = scanner.scan_cluster()

        provider_name = 'unknown'
        network_type = 'tcp'
        dranet_available = False
        try:
            provider = ProviderRegistry.detect_provider(kubectl_runner=scanner.kubectl)
            provider_name = provider.get_provider_id()
            integrator = NetworkIntegrator(provider, scanner.kubectl)
            selected_network = integrator._select_network_type()
            network_type = selected_network.value
            dranet_available = (network_type == 'dra')
        except Exception:
            pass

        infra_versions = {}
        try:
            infra_versions = scan_versions(scanner.kubectl)
        except Exception:
            pass

        nodes_detail = []
        for node in resources.nodes:
            node_nics = [{'name': n.name, 'type': n.type, 'vendor': n.vendor,
                          'model': n.model, 'speed_gbps': n.speed_gbps, 'count': n.count}
                         for n in node.network_interfaces]
            nodes_detail.append({
                'name': node.name, 'gpus': node.gpus, 'gpu_model': node.gpu_model,
                'gpu_memory_mb': node.gpu_memory_mb, 'cpu_cores': node.cpu_cores,
                'memory_gb': node.memory_gb, 'has_rdma': node.has_rdma, 'nics': node_nics,
            })

        from web.realtime import _scan_networks, _detect_gateway_class

        result = {
            'total_gpus': resources.total_gpus,
            'gpus_per_node': resources.gpus_per_node,
            'max_gpus_per_node': resources.max_gpus_per_node,
            'gpu_node_count': resources.gpu_node_count,
            'gpu_model': resources.gpu_model,
            'gpu_memory_per_gpu_mb': resources.gpu_memory_per_gpu_mb,
            'total_cpu_cores': resources.total_cpu_cores,
            'total_memory_gb': resources.total_memory_gb,
            'node_count': resources.node_count,
            'has_rdma': resources.has_rdma,
            'tp_options': resources.get_tp_options(),
            'nodes_detail': nodes_detail,
            'storage_classes': [
                {'name': sc.name, 'provisioner': sc.provisioner, 'is_local': getattr(sc, 'is_local', False),
                 'gpu_nodes_covered': getattr(sc, 'gpu_nodes_covered', 0),
                 'access_mode': getattr(sc, 'access_mode', 'ReadWriteOnce'),
                 'local_path': getattr(sc, 'local_path', '')}
                for sc in resources.storage_classes
                if sc.provisioner != 'kubernetes.io/no-provisioner'
            ],
            'provider': provider_name,
            'network_type': network_type,
            'dranet_available': dranet_available,
            'available_networks': _scan_networks(scanner),
            'gateway_class': _detect_gateway_class(scanner),
            'infra_versions': infra_versions,
        }

        # Save to config so UI can use it
        with state_lock:
            if not state['current_config']:
                state['current_config'] = {}
            state['current_config']['cluster_resources'] = result
            save_state()

        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def _emit_socketio_event(event_name, data):
    """Emit a Socket.IO event server-side, triggering the same handler as if a client sent it.
    This bridges REST calls to Socket.IO event handlers."""
    socketio.emit(event_name, data)


@app.route('/api/config/lock', methods=['POST'])
def api_lock_config():
    """Lock config to prevent UI from overwriting it. Use before saving config via REST."""
    with state_lock:
        state['config_locked'] = True
    return jsonify({'success': True, 'locked': True})


@app.route('/api/config/unlock', methods=['POST'])
def api_unlock_config():
    """Unlock config so the UI can save normally again."""
    with state_lock:
        state['config_locked'] = False
    return jsonify({'success': True, 'locked': False})


@app.route('/api/set_state', methods=['POST'])
def api_set_state():
    """Set the UI wizard step and running state. Persists to DB so the UI reflects the change."""
    data = request.get_json() or {}
    current_step = data.get('current_step')
    running = data.get('running')
    try:
        from datetime import datetime
        with get_db() as conn:
            if current_step is not None:
                conn.execute('UPDATE ui_session_state SET current_step = ?, updated_at = ? WHERE id = 1',
                             (current_step, datetime.now().isoformat()))
            if running is not None:
                conn.execute('UPDATE ui_session_state SET optimization_running = ?, updated_at = ? WHERE id = 1',
                             (1 if running else 0, datetime.now().isoformat()))
                with state_lock:
                    state['optimization_running'] = bool(running)
                    save_state()
                socketio.emit('status_update', {'running': bool(running)})
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/setup_storage', methods=['POST'])
def api_setup_storage():
    """Setup storage and start model download. Returns immediately, work runs async.
    Progress is logged to console_logs — poll GET /api/logs and GET /api/status to track."""
    data = request.get_json() or {}
    from gevent import spawn as gspawn
    from web.realtime import handle_setup_storage
    gspawn(handle_setup_storage, data)
    socketio.emit('status_update', {'running': True, 'message': 'Storage setup started'})
    return jsonify({'success': True, 'message': 'Storage setup started. Poll GET /api/logs and GET /api/status to track progress.'})


@app.route('/api/start_optimization', methods=['POST'])
def api_start_optimization():
    """Start a new optimization run. Returns immediately, optimization runs async."""
    with state_lock:
        if state.get('optimization_running'):
            return jsonify({'success': False, 'message': 'Optimization already running'}), 409
    data = request.get_json() or {}
    from gevent import spawn as gspawn
    from web.realtime import handle_start_optimization
    gspawn(handle_start_optimization, data)
    socketio.emit('status_update', {'running': True, 'message': 'Optimization started'})
    return jsonify({'success': True, 'message': 'Optimization started. Poll GET /api/status to track.'})


@app.route('/api/resume_optimization', methods=['POST'])
def api_resume_optimization():
    """Resume a stopped optimization run."""
    data = request.get_json() or {}
    from gevent import spawn as gspawn
    from web.realtime import handle_resume_optimization
    gspawn(handle_resume_optimization, data)
    return jsonify({'success': True, 'message': 'Optimization resumed. Poll GET /api/status to track.'})


@app.route('/api/generate_test_plan', methods=['POST'])
def api_generate_test_plan():
    """Generate a test plan based on model and cluster resources."""
    data = request.get_json() or {}
    from gevent import spawn as gspawn
    from web.realtime import handle_generate_test_plan
    gspawn(handle_generate_test_plan, data)
    return jsonify({'success': True, 'message': 'Test plan generation started. Poll GET /api/logs to track.'})


@app.route('/api/cleanup', methods=['POST'])
def api_cleanup_deployment():
    """Clean up deployed test pods and LWS resources."""
    from gevent import spawn as gspawn
    from web.realtime import handle_cleanup_deployment
    gspawn(handle_cleanup_deployment, {})
    return jsonify({'success': True, 'message': 'Cleanup started.'})


@app.route('/api/pvcs')
def api_list_pvcs():
    """List PVCs in the target namespace."""
    try:
        import subprocess
        r = subprocess.run(
            ['kubectl', 'get', 'pvc', '-n', TARGET_NAMESPACE, '-o', 'json'],
            capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            return jsonify({'success': False, 'pvcs': [], 'error': r.stderr[:200]}), 500
        data = json.loads(r.stdout)
        pvcs = []
        for pvc in data.get('items', []):
            pvcs.append({
                'name': pvc['metadata']['name'],
                'size': pvc['spec']['resources']['requests'].get('storage', '?'),
                'storage_class': pvc['spec'].get('storageClassName', '?'),
                'status': pvc['status'].get('phase', '?'),
            })
        return jsonify({'success': True, 'pvcs': pvcs})
    except Exception as e:
        return jsonify({'success': False, 'pvcs': [], 'error': str(e)}), 500


@app.route('/api/config', methods=['GET', 'POST'])
def handle_config():
    """Get or update configuration. POST saves to both in-memory state AND database."""

    if request.method == 'POST':
        data = request.json or {}
        with state_lock:
            if state['current_config']:
                state['current_config'].update(data)
            else:
                state['current_config'] = data
            save_state()

        # Also persist to DB so the config survives restarts and isn't overwritten by UI
        try:
            with get_db() as conn:
                row = conn.execute('SELECT config_json FROM ui_session_state WHERE id = 1').fetchone()
                existing = json.loads(row['config_json']) if row and row['config_json'] else {}
                existing.update(data)
                conn.execute(
                    'UPDATE ui_session_state SET config_json = ?, updated_at = ? WHERE id = 1',
                    (json.dumps(existing), datetime.now().isoformat()))
        except Exception as e:
            print(f"Warning: Could not persist config to DB: {e}")

        return jsonify({'success': True, 'config': state['current_config']})
    else:
        with state_lock:
            return jsonify(state['current_config'])

@app.route('/api/stop_optimization', methods=['POST'])
def api_stop_optimization():
    """Stop the running optimization (REST endpoint). Kills all backend processes."""
    import subprocess

    with state_lock:
        state['_stop_requested'] = True
        state['optimization_running'] = False
        state['config_locked'] = False
        greenlet = state.get('_optimization_greenlet')
        save_state()

    # Kill remote guidellm on workload pod
    ns = TARGET_NAMESPACE
    try:
        r = subprocess.run(
            ['kubectl', 'get', 'pod', '-l', 'app=serveit-workload', '-n', ns,
             '-o', 'jsonpath={.items[0].metadata.name}'],
            capture_output=True, text=True, timeout=10, check=False)
        wl_pod = r.stdout.strip()
        if wl_pod:
            subprocess.run(
                ['kubectl', 'exec', wl_pod, '-n', ns, '--', 'pkill', '-9', '-f', 'guidellm'],
                capture_output=True, timeout=10, check=False)
    except Exception:
        pass

    # Kill local kubectl subprocesses (except port-forward)
    try:
        subprocess.run(['bash', '-c', "ps aux | grep kubectl | grep -v port-forward | grep -v grep | awk '{print $2}' | xargs -r kill -9"],
                       capture_output=True, timeout=5, check=False)
    except Exception:
        pass

    # Kill the optimization greenlet
    if greenlet and not greenlet.dead:
        greenlet.kill(block=False)

    with state_lock:
        state['_optimization_greenlet'] = None

    # Update DB
    try:
        with get_db() as conn:
            conn.execute('UPDATE ui_session_state SET optimization_running = 0, updated_at = ? WHERE id = 1',
                         (datetime.now().isoformat(),))
            conn.execute("UPDATE optimization_runs SET status = 'stopped', completed_at = ? WHERE status = 'running'",
                         (datetime.now().isoformat(),))
    except Exception:
        pass

    socketio.emit('status_update', {'running': False, 'message': 'Optimization stopped'})
    socketio.emit('console_log', {'type': 'warning', 'message': '🛑 Optimization stopped by user'})

    return jsonify({'success': True, 'message': 'Optimization stopped'})

@app.route('/api/clear_console', methods=['POST'])
def api_clear_console():
    """Clear console display and stored logs."""
    try:
        with get_db() as conn:
            conn.execute('DELETE FROM console_logs')
    except Exception:
        pass
    socketio.emit('clear_console', {})
    return jsonify({'success': True, 'message': 'Console cleared'})

@app.route('/api/runs')
def get_runs():
    """Get all optimization runs from database."""
    try:
        with get_db() as conn:
            runs = conn.execute('SELECT * FROM optimization_runs ORDER BY created_at DESC').fetchall()
            return jsonify([dict(run) for run in runs])
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/runs_for_resume')
def get_runs_for_resume():
    """Get all optimization runs with step-level progress for resume overlay."""
    try:
        with get_db() as conn:
            runs = conn.execute('''
                SELECT r.*, COUNT(tc.id) as completed_tests
                FROM optimization_runs r
                LEFT JOIN test_configurations tc ON tc.run_id = r.id AND tc.status = 'completed'
                GROUP BY r.id
                ORDER BY r.created_at DESC
            ''').fetchall()

            result = []
            for run in runs:
                run_dict = dict(run)
                run_id = run_dict['id']

                # Determine which steps have completed tests
                steps = conn.execute('''
                    SELECT DISTINCT
                        CASE
                            WHEN config_name LIKE 'step2-%' THEN 2
                            WHEN config_name LIKE 'step3-%' THEN 3
                            WHEN config_name LIKE 'step7-%' THEN 7
                            WHEN config_name LIKE 'step6-%' THEN 6
                            WHEN config_name LIKE 'step8-%' THEN 8
                            WHEN config_name LIKE 'step9-%' THEN 9
                            ELSE 0
                        END as step_num
                    FROM test_configurations
                    WHERE run_id = ? AND status = 'completed'
                ''', (run_id,)).fetchall()

                completed_steps = sorted([s['step_num'] for s in steps if s['step_num'] > 0])
                run_dict['completed_steps'] = completed_steps
                run_dict['last_step'] = max(completed_steps) if completed_steps else 0
                result.append(run_dict)

            return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/delete_run/<int:run_id>', methods=['DELETE'])
def delete_run(run_id):
    """Delete an optimization run and all its test results."""
    try:
        with get_db() as conn:
            status = conn.execute(
                'SELECT status FROM optimization_runs WHERE id = ?', (run_id,)
            ).fetchone()
            if not status:
                return jsonify({'success': False, 'error': f'Run #{run_id} not found'}), 404
            if status['status'] == 'running':
                return jsonify({'success': False, 'error': f'Run #{run_id} is currently running — stop it first'}), 409
            test_ids = [r[0] for r in conn.execute(
                'SELECT config_name FROM test_configurations WHERE run_id = ?', (run_id,)
            ).fetchall()]
            deleted_tests = conn.execute(
                'DELETE FROM test_configurations WHERE run_id = ?', (run_id,)
            ).rowcount
            conn.execute(
                'DELETE FROM console_logs WHERE run_id = ?', (run_id,)
            )
            conn.execute(
                'DELETE FROM optimization_runs WHERE id = ?', (run_id,)
            )
        _cleanup_result_files(test_ids)
        return jsonify({'success': True, 'deleted_tests': deleted_tests})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


def _cleanup_result_files(test_ids):
    """Remove artifact directories for deleted tests."""
    results_dir = Path('/mnt/storage/results')
    for test_id in test_ids:
        artifact_dir = results_dir / test_id
        if artifact_dir.is_dir():
            shutil.rmtree(artifact_dir, ignore_errors=True)


@app.route('/api/restart_run/<int:run_id>', methods=['POST'])
def restart_run(run_id):
    """Clear all test results for a run and reset it so it can be re-run from the beginning."""
    try:
        with get_db() as conn:
            row = conn.execute(
                'SELECT status FROM optimization_runs WHERE id = ?', (run_id,)
            ).fetchone()
            if not row:
                return jsonify({'success': False, 'error': f'Run #{run_id} not found'}), 404
            if row['status'] == 'running':
                return jsonify({'success': False, 'error': f'Run #{run_id} is currently running — stop it first'}), 409
            test_ids = [r[0] for r in conn.execute(
                'SELECT config_name FROM test_configurations WHERE run_id = ?', (run_id,)
            ).fetchall()]
            deleted_tests = conn.execute(
                'DELETE FROM test_configurations WHERE run_id = ?', (run_id,)
            ).rowcount
            conn.execute(
                'DELETE FROM console_logs WHERE run_id = ?', (run_id,)
            )
            conn.execute('''
                UPDATE optimization_runs
                SET status = 'running', completed_at = NULL, optimal_config = NULL,
                    constraint_notes = NULL, current_test_index = 0,
                    last_deployed_config = NULL, deployment_status = NULL, pods_deployed = NULL
                WHERE id = ?
            ''', (run_id,))
        _cleanup_result_files(test_ids)
        return jsonify({'success': True, 'deleted_tests': deleted_tests})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/resumable_run')
def get_resumable_run_api():
    """Check if there's a resumable run."""
    try:
        run = get_resumable_run()
        if run:
            return jsonify({
                'resumable': True,
                'run': {
                    'id': run['id'],
                    'run_name': run['run_name'],
                    'model': run.get('model'),
                    'status': run['status'],
                },
                'completed_tests': run.get('completed_tests', 0),
            })
        else:
            return jsonify({'resumable': False})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/runs/<int:run_id>/notes', methods=['PUT'])
def update_run_notes(run_id):
    """Update the description/notes for a run."""
    try:
        data = request.get_json()
        notes = data.get('notes', '')
        with get_db() as conn:
            conn.execute('UPDATE optimization_runs SET notes = ? WHERE id = ?', (notes, run_id))
            conn.commit()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/runs/<int:run_id>/pod_errors')
def get_run_pod_errors(run_id):
    """Get pod error logs for a specific run."""
    try:
        with get_db() as conn:
            errors = conn.execute(
                'SELECT * FROM pod_error_logs WHERE run_id = ? ORDER BY created_at DESC',
                (run_id,)
            ).fetchall()
            return jsonify([dict(e) for e in errors])
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/runs/<int:run_id>/configurations')
def get_run_configurations(run_id):
    """Get all test configurations for a specific run."""
    try:
        with get_db() as conn:
            configs = conn.execute(
                'SELECT * FROM test_configurations WHERE run_id = ? ORDER BY id',
                (run_id,)
            ).fetchall()
            return jsonify([dict(config) for config in configs])
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/runs/<int:run_id>/charts')
def get_run_charts(run_id):
    """Get chart data, summary stats, and recommendation for an optimization run."""
    try:
        from core.report_data import ReportDataLoader
        from core.report_analysis import ReportAnalyzer

        analyzer = ReportAnalyzer()
        with ReportDataLoader(DB_PATH) as loader:
            data = analyzer.build_full_report_data(run_id, loader)
            if not data:
                return jsonify({'error': 'No results found for this run'}), 404
            return jsonify(data)

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/api/runs/<int:run_id>/report')
def get_run_report(run_id):
    """Download a self-contained HTML report for an optimization run.
    Available at any time — includes all results collected so far."""
    try:
        from core.report_data import ReportDataLoader
        from core.report_analysis import ReportAnalyzer

        analyzer = ReportAnalyzer()
        with ReportDataLoader(DB_PATH) as loader:
            data = analyzer.build_full_report_data(run_id, loader)
            if not data:
                return jsonify({'error': 'No results found for this run'}), 404

        # Prefer synced code on PVC, fall back to bundled image code
        app_root = '/mnt/storage/app' if os.path.isdir('/mnt/storage/app/web') else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        js_path = os.path.join(app_root, 'web', 'static', 'js', 'report-download.js')

        # Generate a self-contained HTML that embeds the data + JS
        # The report renders client-side when opened in a browser — same as the UI download
        html = None
        if os.path.exists(js_path):
            with open(js_path) as f:
                js_code = f.read()

            has_pd = any(r.get('architecture') == 'PD' for r in (data.get('all_results') or []))
            vllm_charts = (data.get('charts') or {}).get('vllm') or {}
            has_vllm = bool(vllm_charts.get('configs'))
            data_json = json.dumps(data)

            html = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<title>ServeIt Studio Report — Run #{run_id}</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<script>
{js_code}
</script>
</head><body>
<script>
window.addEventListener('load', function() {{
    var data = {data_json};
    var runId = {run_id};
    var hasPD = {str(has_pd).lower()};
    var hasVllm = {str(has_vllm).lower()};
    var summary = data.summary || {{}};
    var rec = data.recommendation || {{}};
    var best = summary.best_configs || {{}};
    var allRes = data.all_results || [];
    try {{
        var reportHtml = buildFullReport(
            runId, data, data.charts, rec, summary, best, allRes, hasPD, hasVllm
        );
        // innerHTML doesn't execute <script> tags — extract and run them separately
        document.body.innerHTML = reportHtml;
        var scripts = document.body.querySelectorAll('script');
        scripts.forEach(function(s) {{
            var newScript = document.createElement('script');
            newScript.textContent = s.textContent;
            s.parentNode.replaceChild(newScript, s);
        }});
    }} catch(e) {{
        document.body.innerHTML = '<h1>Report Error</h1><pre>' + e.stack + '</pre>';
    }}
}});
</script>
</body></html>"""

        if not html:
            from cli.inftune import build_python_html_report
            html = build_python_html_report(run_id, data)

        rec = data.get('recommendation') or {}
        model = rec.get('model', 'report') or 'report'
        model_short = model.split('/')[-1] if '/' in model else model
        filename = f'serveit-report-run{run_id}-{model_short}.html'

        return Response(html, mimetype='text/html',
                        headers={'Content-Disposition': f'attachment; filename="{filename}"'})

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/api/models')
def get_models():
    """Get available Red Hat AI models from JSON file."""
    try:
        models_file = os.path.join(os.path.dirname(__file__), 'data', 'models.json')
        with open(models_file, 'r') as f:
            models = json.load(f)
        return jsonify(models)
    except FileNotFoundError:
        return jsonify({'error': 'Models file not found'}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/deployment_templates')
def get_deployment_templates():
    """Get all active deployment templates."""
    try:
        model_name = request.args.get('model_name')
        architecture = request.args.get('architecture')

        query = 'SELECT * FROM deployment_templates WHERE is_active = 1'
        params = []

        if model_name:
            query += ' AND model_name = ?'
            params.append(model_name)

        if architecture:
            query += ' AND architecture = ?'
            params.append(architecture)

        query += ' ORDER BY created_at DESC'

        with get_db() as conn:
            templates = conn.execute(query, params).fetchall()
            return jsonify({
                'success': True,
                'count': len(templates),
                'templates': [dict(t) for t in templates]
            })

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/deployment_templates/<model_name>/<architecture>', methods=['GET'])
def get_deployment_template_by_key(model_name, architecture):
    """Get specific deployment template."""
    try:
        role = request.args.get('role')  # Optional role for PD
        template = get_deployment_template(model_name, architecture, role)

        if template:
            return jsonify({'success': True, 'template': template})
        else:
            return jsonify({'success': False, 'error': 'Template not found'}), 404

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/deployment_templates', methods=['PUT'])
def update_deployment_template():
    """Update deployment template."""
    try:
        data = request.json

        # Save updated template (will deactivate old and create new)
        template_id = save_deployment_template(
            model_name=data['model_name'],
            architecture=data['architecture'],
            role=data.get('role'),
            tensor_parallelism=data['tensor_parallelism'],
            replicas=data.get('replicas', 1),
            max_model_len=data.get('max_model_len', 8192),
            gpu_memory_utilization=data.get('gpu_memory_utilization', 0.95),
            image=data.get('image', 'vllm/vllm-openai:v0.26.0'),
            pvc_name=data.get('pvc_name', 'serveit-cache'),
            nccl_ib_hca=data.get('nccl_ib_hca', 'mlx'),
            isl=data.get('isl', 2000),
            osl=data.get('osl', 100),
            max_num_batched_tokens=data.get('max_num_batched_tokens'),
            gpus_per_pod=data.get('gpus_per_pod'),
            memory_limit=data.get('memory_limit', '512Gi'),
            cpu_request=data.get('cpu_request', '32'),
            namespace=data.get('namespace', TARGET_NAMESPACE)
        )

        return jsonify({
            'success': True,
            'template_id': template_id,
            'message': 'Template updated successfully'
        })

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/logs')
def get_console_logs():
    """Get console logs with optional filtering.

    Query parameters:
        run_id: Filter by optimization run ID
        job_name: Filter by job name
        since: ISO timestamp - get logs after this time
        limit: Maximum number of logs to return (default: 100, max: 1000)
    """
    try:
        # Parse query parameters
        run_id = request.args.get('run_id', type=int)
        job_name = request.args.get('job_name')
        since = request.args.get('since')
        limit = min(request.args.get('limit', default=100, type=int), 100000)

        # Build query
        query = 'SELECT id, timestamp, log_type, message, run_id, job_name FROM console_logs WHERE 1=1'
        params = []

        if run_id:
            query += ' AND run_id = ?'
            params.append(run_id)

        if job_name:
            query += ' AND job_name = ?'
            params.append(job_name)

        if since:
            query += ' AND timestamp > ?'
            params.append(since)

        query += ' ORDER BY id DESC LIMIT ?'
        params.append(limit)

        with get_db() as conn:
            logs = conn.execute(query, params).fetchall()
            return jsonify({
                'success': True,
                'count': len(logs),
                'logs': [dict(log) for log in reversed(logs)]  # Return in chronological order
            })

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500



# --- MLflow Integration ---

@app.route('/api/mlflow/config', methods=['GET'])
def get_mlflow_config():
    try:
        from core.database_manager import DatabaseManager
        db = DatabaseManager(db_path=DB_PATH)
        cfg = db.get_mlflow_config()
        if cfg:
            cfg_dict = dict(cfg)
            cfg_dict['password'] = '***' if cfg_dict.get('password') else ''
            cfg_dict['insecure_tls'] = bool(cfg_dict.get('insecure_tls', 1))
            return jsonify({'success': True, 'config': cfg_dict})
        return jsonify({'success': True, 'config': None})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/mlflow/config', methods=['POST'])
def save_mlflow_config():
    try:
        data = request.json
        from core.database_manager import DatabaseManager
        db = DatabaseManager(db_path=DB_PATH)
        db.save_mlflow_config(
            tracking_uri=data.get('tracking_uri', ''),
            username=data.get('username'),
            password=data.get('password'),
            experiment_name=data.get('experiment_name'),
            insecure_tls=data.get('insecure_tls', True),
        )
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/mlflow/runs', methods=['GET'])
def get_mlflow_runs():
    try:
        with get_db() as conn:
            runs = conn.execute(
                "SELECT id, run_name, model, status, created_at, notes, isl, osl, num_users, max_gpus, goal "
                "FROM optimization_runs ORDER BY id DESC"
            ).fetchall()
            return jsonify({'success': True, 'runs': [dict(r) for r in runs]})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/mlflow/tests/<int:run_id>', methods=['GET'])
def get_mlflow_tests(run_id):
    try:
        with get_db() as conn:
            tests = conn.execute(
                "SELECT config_name, status, architecture, tensor_parallelism, ttft_p90, throughput_p90 "
                "FROM test_configurations WHERE run_id = ? ORDER BY id",
                (run_id,)
            ).fetchall()
            return jsonify({'success': True, 'tests': [dict(t) for t in tests]})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/mlflow/export', methods=['POST'])
def export_to_mlflow():
    try:
        data = request.json
        from core.database_manager import DatabaseManager
        db = DatabaseManager(db_path=DB_PATH)
        cfg = db.get_mlflow_config()
        if not cfg:
            return jsonify({'success': False, 'error': 'MLflow not configured'}), 400

        cfg_dict = dict(cfg)
        export_args = dict(
            db_path=DB_PATH,
            tracking_uri=cfg_dict['tracking_uri'],
            username=cfg_dict.get('username'),
            password=cfg_dict.get('password'),
            experiment_name=data.get('experiment_name') or cfg_dict.get('experiment_name') or 'serveit-studio',
            run_id=data['run_id'],
            test_ids=data.get('test_ids'),
            insecure_tls=bool(cfg_dict.get('insecure_tls', 1)),
        )

        def _run_export():
            try:
                from core.mlflow_exporter import export_to_mlflow as do_export
                result = do_export(**export_args)
                socketio.emit('mlflow_export_complete', result)
            except Exception as e:
                socketio.emit('mlflow_export_complete', {'success': False, 'error': str(e)})

        from gevent import spawn as gspawn
        gspawn(_run_export)
        return jsonify({'success': True, 'message': 'MLflow export started in background'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/dataset/seed/<int:seed>')
def get_dataset_seed_config(seed):
    """Look up a dataset seed and return the full generation config."""
    try:
        with get_db() as conn:
            row = conn.execute('SELECT config_json, created_at FROM dataset_seeds WHERE seed = ?', (seed,)).fetchone()
            if not row:
                return jsonify({'success': False, 'error': f'Seed {seed} not found'}), 404
            config = json.loads(row['config_json'])
            config['seed'] = seed
            # Build the reproduction command
            if config.get('type') == 'multi_turn':
                cmd = f'generate_turn_dataset --model "{config["model"]}" --prompt-tokens {config["prompt_tokens"]} --output-tokens {config["output_tokens"]}'
                if config.get('prompt_tokens_stdev'): cmd += f' --prompt-tokens-stdev {config["prompt_tokens_stdev"]}'
                if config.get('output_tokens_stdev'): cmd += f' --output-tokens-stdev {config["output_tokens_stdev"]}'
                if config.get('first_prompt_tokens'): cmd += f' --first-prompt-tokens {config["first_prompt_tokens"]}'
                if config.get('first_prompt_tokens_stdev'): cmd += f' --first-prompt-tokens-stdev {config["first_prompt_tokens_stdev"]}'
                if config.get('first_prompt_tokens_min'): cmd += f' --first-prompt-tokens-min {config["first_prompt_tokens_min"]}'
                if config.get('first_prompt_tokens_max'): cmd += f' --first-prompt-tokens-max {config["first_prompt_tokens_max"]}'
                if config.get('prefix_tokens'): cmd += f' --prefix-tokens {config["prefix_tokens"]} --prefix-count {config.get("prefix_count", 1)}'
                cmd += f' --turns {config.get("turns", 1)} --rows {config.get("rows", 100)} --seed {seed}'
                if config.get('use_corpus'): cmd += ' --use-corpus'
            else:
                cmd = f'generate_dataset --model "{config["model"]}" --isl {config["isl"]} --osl {config["osl"]}'
                if config.get('isl_stdev'): cmd += f' --isl-stdev {config["isl_stdev"]}'
                if config.get('osl_stdev'): cmd += f' --osl-stdev {config["osl_stdev"]}'
                cmd += f' --seed {seed} --rows {config.get("rows", 100)}'
                mode = config.get('mode', 'random')
                if mode == 'cache':
                    cmd += f' --mode cache --hit-pct {config.get("hit_pct", 100)}'
                elif mode == 'prefix_group':
                    cmd += f' --mode prefix_group --hit-pct {config.get("hit_pct", 60)} --prefix-groups {config.get("prefix_groups", 10)}'
                elif mode == 'corpus':
                    cmd += ' --mode corpus'
                else:
                    cmd += ' --mode random'
                if config.get('use_corpus') and mode not in ('corpus', 'random'): cmd += ' --use-corpus'
            cmd += ' --output <output_path>'
            return jsonify({'success': True, 'seed': seed, 'config': config, 'command': cmd, 'created_at': row['created_at']})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# --- Backup/Restore endpoints (for launcher integration) ---

@app.route('/api/backup/database')
def backup_database():
    """Compress and download database in one HTTP call."""
    try:
        import gzip as gzip_mod
        import hashlib
        import io

        if not os.path.exists(DB_PATH):
            return jsonify({'error': 'Database file not found'}), 404

        buf = io.BytesIO()
        with open(DB_PATH, 'rb') as f_in, gzip_mod.GzipFile(fileobj=buf, mode='wb', compresslevel=6) as f_out:
            while True:
                chunk = f_in.read(256 * 1024)
                if not chunk:
                    break
                f_out.write(chunk)

        data = buf.getvalue()
        md5 = hashlib.md5(data).hexdigest()

        return Response(data, mimetype='application/gzip',
                        headers={'Content-Disposition': 'attachment; filename=serveit.db.gz',
                                 'X-MD5': md5, 'Content-Length': str(len(data))})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/backup/artifacts')
def backup_artifacts():
    """Compress and download test artifacts in one HTTP call."""
    try:
        import tarfile
        import hashlib
        import io

        artifacts_dir = '/mnt/storage/test-artifacts'
        results_dir = '/mnt/storage/results'

        dirs_to_pack = []
        if os.path.isdir(artifacts_dir):
            dirs_to_pack.append(('test-artifacts', artifacts_dir))
        if os.path.isdir(results_dir):
            dirs_to_pack.append(('results', results_dir))

        if not dirs_to_pack:
            return jsonify({'error': 'No test data found'}), 404

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode='w:gz', compresslevel=6) as tar:
            for arcname, d in dirs_to_pack:
                for root, _, files in os.walk(d):
                    for f in files:
                        fpath = os.path.join(root, f)
                        rel = os.path.relpath(fpath, os.path.dirname(d))
                        tar.add(fpath, arcname=rel)

        data = buf.getvalue()
        md5 = hashlib.md5(data).hexdigest()

        return Response(data, mimetype='application/gzip',
                        headers={'Content-Disposition': 'attachment; filename=serveit-artifacts.tar.gz',
                                 'X-MD5': md5, 'Content-Length': str(len(data))})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/restore/artifacts', methods=['POST'])
def restore_artifacts():
    """Restore test artifacts from a tar.gz archive."""
    import tarfile
    import tempfile
    try:
        if 'artifacts' not in request.files:
            return jsonify({'success': False, 'error': 'No artifacts file provided'}), 400

        file = request.files['artifacts']
        if not file.filename:
            return jsonify({'success': False, 'error': 'Empty filename'}), 400

        with tempfile.NamedTemporaryFile(suffix='.tar.gz', delete=False) as tmp:
            tmp_path = tmp.name
            file.save(tmp_path)

        try:
            with tarfile.open(tmp_path, 'r:gz') as tar:
                members = tar.getmembers()
                safe_members = [m for m in members if not m.name.startswith('/') and '..' not in m.name]
                tar.extractall('/mnt/storage', members=safe_members)
            return jsonify({'success': True, 'files_restored': len(safe_members)})
        finally:
            os.unlink(tmp_path)

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
