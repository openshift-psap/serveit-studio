"""MLflow exporter for ServeIt Studio test results."""

import os
import json
import logging
from pathlib import Path
from typing import List, Optional, Dict

logger = logging.getLogger(__name__)


def _register_workspace_header(workspace):
    """Register a request header provider that injects X-Mlflow-Workspace into all SDK calls."""
    from mlflow.tracking.request_header.abstract_request_header_provider import RequestHeaderProvider
    from mlflow.tracking.request_header.registry import _request_header_provider_registry

    for p in _request_header_provider_registry:
        if getattr(p, '_serveit_workspace', False):
            p._workspace = workspace
            return

    class _Provider(RequestHeaderProvider):
        _serveit_workspace = True
        def __init__(self):
            self._workspace = workspace
        def in_context(self):
            return True
        def request_headers(self):
            return {'X-Mlflow-Workspace': self._workspace}

    _request_header_provider_registry.register(_Provider)


def _get_or_create_experiment(tracking_uri, name, workspace, username, password, insecure_tls):
    """Create or find an MLflow experiment in the target workspace via REST API."""
    import requests
    headers = {'Content-Type': 'application/json', 'X-Mlflow-Workspace': workspace}
    auth = (username, password) if username else None
    verify = not insecure_tls

    resp = requests.get(
        f'{tracking_uri}/api/2.0/mlflow/experiments/get-by-name',
        params={'experiment_name': name},
        headers=headers, auth=auth, verify=verify,
    )
    if resp.ok:
        return resp.json()['experiment']['experiment_id']

    resp = requests.post(
        f'{tracking_uri}/api/2.0/mlflow/experiments/create',
        json={'name': name},
        headers=headers, auth=auth, verify=verify,
    )
    resp.raise_for_status()
    return resp.json()['experiment_id']


def export_to_mlflow(
    db_path: str,
    tracking_uri: str,
    username: Optional[str],
    password: Optional[str],
    experiment_name: str,
    run_id: int,
    test_ids: Optional[List[str]] = None,
    artifact_dir: str = '/mnt/storage/test-artifacts',
    insecure_tls: bool = True,
) -> Dict:
    """Export test results to MLflow.

    Args:
        db_path: Path to SQLite database
        tracking_uri: MLflow tracking server URI
        username: MLflow username (optional)
        password: MLflow password (optional)
        experiment_name: MLflow experiment name
        run_id: Database run ID to export
        test_ids: Specific test IDs to export (None = all)
        artifact_dir: Base directory for test artifacts

    Returns:
        Dict with export results
    """
    try:
        import mlflow
    except ImportError:
        return {'success': False, 'error': 'mlflow package not installed. Run: pip install mlflow'}

    import sqlite3

    workspace = username or 'default'
    if username:
        os.environ['MLFLOW_TRACKING_USERNAME'] = username
    if password:
        os.environ['MLFLOW_TRACKING_PASSWORD'] = password
    if insecure_tls:
        os.environ['MLFLOW_TRACKING_INSECURE_TLS'] = 'true'
    else:
        os.environ.pop('MLFLOW_TRACKING_INSECURE_TLS', None)

    _register_workspace_header(workspace)
    mlflow.set_tracking_uri(tracking_uri)

    experiment_id = _get_or_create_experiment(
        tracking_uri, experiment_name, workspace, username, password, insecure_tls,
    )
    mlflow.set_experiment(experiment_id=experiment_id)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Get run info
    run_row = conn.execute(
        'SELECT * FROM optimization_runs WHERE id = ?', (run_id,)
    ).fetchone()
    if not run_row:
        conn.close()
        return {'success': False, 'error': f'Run {run_id} not found'}

    # Get tests
    query = 'SELECT * FROM test_configurations WHERE run_id = ?'
    params = [run_id]
    if test_ids:
        placeholders = ','.join('?' for _ in test_ids)
        query += f' AND config_name IN ({placeholders})'
        params.extend(test_ids)
    query += ' ORDER BY id'

    tests = conn.execute(query, params).fetchall()
    if not tests:
        conn.close()
        return {'success': False, 'error': 'No tests found for this run'}

    exported = []
    errors = []

    run_dict = dict(run_row)
    model = run_dict['model']
    model_short = model.split('/')[-1] if model else 'unknown'
    run_config = json.loads(run_dict['config_json']) if run_dict['config_json'] else {}
    created = run_dict['created_at'][:10] if run_dict['created_at'] else ''
    goal = run_dict['goal'] or 'ttft'
    notes = run_dict.get('notes') or ''

    mlflow_run_name = f"{model_short} — {run_dict['num_users']}users — ISL{run_dict['isl']}/OSL{run_dict['osl']} — {run_dict['max_gpus']}GPU — {created}"

    description_lines = [
        f"**Model:** {model}",
        f"**Workload:** ISL={run_dict['isl']}, OSL={run_dict['osl']}, {run_dict['num_users']} concurrent users, {run_dict['test_duration']}s duration",
        f"**Goal:** {goal} | **GPUs:** {run_dict['max_gpus']} | **Status:** {run_dict['status']}",
    ]
    if run_dict.get('workload_mode'):
        description_lines.append(f"**Workload mode:** {run_dict['workload_mode']} | **Rate:** {run_dict.get('rate_type', 'concurrent')}")
    if run_dict.get('prefix_cache_hit_pct'):
        description_lines.append(f"**Prefix cache:** {run_dict['prefix_cache_hit_pct']}% hit rate")
    if notes:
        description_lines.append(f"**Notes:** {notes}")
    if run_dict.get('optimal_config'):
        try:
            opt = json.loads(run_dict['optimal_config'])
            opt_summary = ', '.join(f'{k}={v}' for k, v in opt.items() if not isinstance(v, (dict, list)))
            description_lines.append(f"**Optimal:** {opt_summary}")
        except (json.JSONDecodeError, TypeError):
            pass

    with mlflow.start_run(run_name=mlflow_run_name, description='\n\n'.join(description_lines)) as parent_run:
        mlflow.log_params({
            'model': model,
            'isl': run_dict['isl'],
            'osl': run_dict['osl'],
            'num_users': run_dict['num_users'],
            'goal': goal,
            'test_duration': run_dict['test_duration'],
            'max_gpus': run_dict['max_gpus'],
        })
        extra_params = {}
        if run_dict.get('workload_mode'):
            extra_params['workload_mode'] = run_dict['workload_mode']
        if run_dict.get('rate_type'):
            extra_params['rate_type'] = run_dict['rate_type']
        if run_dict.get('prefix_cache_hit_pct'):
            extra_params['prefix_cache_hit_pct'] = run_dict['prefix_cache_hit_pct']
        if run_dict.get('turns') and run_dict['turns'] > 1:
            extra_params['turns'] = run_dict['turns']
        if run_dict.get('speculative_method'):
            extra_params['speculative_method'] = run_dict['speculative_method']
        if extra_params:
            mlflow.log_params(extra_params)
        tags = {
            'mlflow.source.name': 'serveit-studio',
            'mlflow.source.type': 'LOCAL',
            'serveit.model_short': model_short,
            'serveit.status': run_dict['status'],
            'serveit.goal': goal,
            'serveit.run_id': str(run_id),
        }
        if notes:
            tags['serveit.notes'] = notes[:250]
        if run_config.get('cluster_name'):
            tags['serveit.cluster'] = run_config['cluster_name']
        if run_config.get('namespace'):
            tags['serveit.namespace'] = run_config['namespace']
        mlflow.set_tags(tags)

        for test in tests:
            test_id = test['config_name']
            status = test['status']

            if status not in ('completed', 'passed'):
                continue

            try:
                tc = json.loads(test['test_config_json']) if test['test_config_json'] else {}
                test_dict = dict(test)
                arch = test_dict.get('architecture') or tc.get('architecture', '')
                tp = test_dict['tensor_parallelism']
                child_name = f"{test_id} ({arch} TP{tp})" if arch else f"{test_id} (TP{tp})"

                with mlflow.start_run(run_name=child_name, nested=True) as child_run:
                    mlflow.set_tags({
                        'mlflow.source.name': 'serveit-studio',
                        'serveit.architecture': arch,
                        'serveit.tp': str(tp),
                    })

                    # Log params
                    params = {
                        'architecture': test_dict.get('architecture') or tc.get('architecture', ''),
                        'tensor_parallelism': test_dict['tensor_parallelism'],
                        'prefill_pods': test_dict['prefill_pods'],
                        'decode_pods': test_dict['decode_pods'],
                        'gpu_memory_utilization': tc.get('gpu_memory_utilization', ''),
                        'max_num_seqs': tc.get('max_num_seqs', ''),
                        'block_size': tc.get('block_size', ''),
                        'max_model_len': tc.get('max_model_len', ''),
                        'network_type': tc.get('network_type', ''),
                        'enable_prefix_caching': tc.get('enable_prefix_caching', ''),
                    }
                    if test_dict.get('decode_tp'):
                        params['decode_tp'] = test_dict['decode_tp']
                    mlflow.log_params({k: v for k, v in params.items() if v != '' and v is not None})

                    # Log metrics
                    metrics = {}
                    for field in ['ttft_p50', 'ttft_p90', 'ttft_p95', 'ttft_p99',
                                  'itl_p50', 'itl_p90', 'itl_p95', 'itl_p99',
                                  'throughput_p50', 'throughput_p90', 'throughput_p95', 'throughput_p99',
                                  'gpu_utilization', 'kv_cache_usage']:
                        val = test_dict.get(field)
                        if val is not None:
                            metrics[field] = float(val)

                    # Add metrics from metrics_json if available
                    if test_dict.get('metrics_json'):
                        try:
                            mj = json.loads(test_dict['metrics_json'])
                            for k, v in mj.items():
                                if isinstance(v, (int, float)) and k not in metrics:
                                    metrics[k] = float(v)
                        except (json.JSONDecodeError, TypeError):
                            pass

                    if metrics:
                        mlflow.log_metrics(metrics)

                    # Log artifacts from disk
                    test_artifact_dir = Path(artifact_dir) / test_id
                    if test_artifact_dir.exists():
                        for f in test_artifact_dir.iterdir():
                            if f.is_file() and f.stat().st_size < 50 * 1024 * 1024:
                                mlflow.log_artifact(str(f), artifact_path='test-artifacts')

                    exported.append(test_id)
                    logger.info(f"Exported {test_id} to MLflow run {child_run.info.run_id}")

            except Exception as e:
                errors.append({'test_id': test_id, 'error': str(e)})
                logger.error(f"Failed to export {test_id}: {e}")

    conn.close()

    return {
        'success': True,
        'parent_run_id': parent_run.info.run_id,
        'exported': exported,
        'errors': errors,
        'total': len(exported),
    }
