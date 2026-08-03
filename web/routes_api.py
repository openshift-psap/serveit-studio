"""REST API routes — /api/runs, /api/charts, /api/config, etc."""

import os
import sys
import json
import shutil
import subprocess
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict
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
    with state_lock:
        return jsonify({
            'running': state['optimization_running'],
            'config': state['current_config']
        })

@app.route('/api/config', methods=['GET', 'POST'])
def handle_config():
    """Get or update configuration."""

    if request.method == 'POST':
        with state_lock:
            state['current_config'] = request.json
            save_state()
        return jsonify({'success': True, 'config': state['current_config']})
    else:
        with state_lock:
            return jsonify(state['current_config'])

@app.route('/api/stop_optimization', methods=['POST'])
def api_stop_optimization():
    """Stop the running optimization (REST endpoint). Idempotent — safe to call even if not running."""

    with state_lock:
        state['optimization_running'] = False
        save_state()

    socketio.emit('status_update', {'running': False, 'message': 'Optimization stopped'})
    socketio.emit('console_log', {'type': 'warning', 'message': '🛑 Optimization stopped by user'})

    return jsonify({'success': True, 'message': 'Optimization stopped'})

@app.route('/api/clear_console', methods=['POST'])
def api_clear_console():
    """Clear console display (UI only — logs are preserved in database)."""
    socketio.emit('clear_console', {})
    return jsonify({'success': True, 'message': 'Console display cleared'})

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
            image=data.get('image', 'ghcr.io/llm-d/llm-d-cuda:v0.8.0'),
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

        from core.mlflow_exporter import export_to_mlflow as do_export
        cfg_dict = dict(cfg)
        result = do_export(
            db_path=DB_PATH,
            tracking_uri=cfg_dict['tracking_uri'],
            username=cfg_dict.get('username'),
            password=cfg_dict.get('password'),
            experiment_name=data.get('experiment_name') or cfg_dict.get('experiment_name') or 'serveit-studio',
            run_id=data['run_id'],
            test_ids=data.get('test_ids'),
            insecure_tls=bool(cfg_dict.get('insecure_tls', 1)),
        )
        return jsonify(result)
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
