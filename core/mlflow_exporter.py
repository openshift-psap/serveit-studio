"""MLflow exporter for ServeIt Studio test results."""

import os
import json
import logging
from pathlib import Path
from typing import List, Optional, Dict

logger = logging.getLogger(__name__)


def export_to_mlflow(
    db_path: str,
    tracking_uri: str,
    username: Optional[str],
    password: Optional[str],
    experiment_name: str,
    run_id: int,
    test_ids: Optional[List[str]] = None,
    artifact_dir: str = '/mnt/storage/test-artifacts',
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

    if username:
        os.environ['MLFLOW_TRACKING_USERNAME'] = username
    if password:
        os.environ['MLFLOW_TRACKING_PASSWORD'] = password

    mlflow.set_tracking_uri(tracking_uri)
    mlflow.set_experiment(experiment_name)

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

    run_name = run_row['run_name']
    model = run_row['model']
    run_config = json.loads(run_row['config_json']) if run_row['config_json'] else {}

    with mlflow.start_run(run_name=run_name, description=f"ServeIt Studio optimization: {model}") as parent_run:
        # Log run-level params
        mlflow.log_params({
            'model': model,
            'isl': run_row['isl'],
            'osl': run_row['osl'],
            'num_users': run_row['num_users'],
            'goal': run_row['goal'] or 'ttft',
            'test_duration': run_row['test_duration'],
            'max_gpus': run_row['max_gpus'],
        })

        for test in tests:
            test_id = test['config_name']
            status = test['status']

            if status not in ('completed', 'passed'):
                continue

            try:
                with mlflow.start_run(run_name=test_id, nested=True) as child_run:
                    # Parse test config
                    tc = json.loads(test['test_config_json']) if test['test_config_json'] else {}

                    # Log params
                    params = {
                        'architecture': test.get('architecture') or tc.get('architecture', ''),
                        'tensor_parallelism': test['tensor_parallelism'],
                        'prefill_pods': test['prefill_pods'],
                        'decode_pods': test['decode_pods'],
                        'gpu_memory_utilization': tc.get('gpu_memory_utilization', ''),
                        'max_num_seqs': tc.get('max_num_seqs', ''),
                        'block_size': tc.get('block_size', ''),
                        'max_model_len': tc.get('max_model_len', ''),
                        'network_type': tc.get('network_type', ''),
                        'enable_prefix_caching': tc.get('enable_prefix_caching', ''),
                    }
                    if test.get('decode_tp'):
                        params['decode_tp'] = test['decode_tp']
                    mlflow.log_params({k: v for k, v in params.items() if v != '' and v is not None})

                    # Log metrics
                    metrics = {}
                    for field in ['ttft_p50', 'ttft_p90', 'ttft_p95', 'ttft_p99',
                                  'itl_p50', 'itl_p90', 'itl_p95', 'itl_p99',
                                  'throughput_p50', 'throughput_p90', 'throughput_p95', 'throughput_p99',
                                  'gpu_utilization', 'kv_cache_usage']:
                        val = test[field]
                        if val is not None:
                            metrics[field] = float(val)

                    # Add metrics from metrics_json if available
                    if test['metrics_json']:
                        try:
                            mj = json.loads(test['metrics_json'])
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
