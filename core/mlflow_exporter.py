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
            # without these two methods the registry's in_context() call
            # returns None (falsy) and the provider is SILENTLY SKIPPED —
            # the workspace header never reached SDK requests and every
            # run-create failed with RESOURCE_DOES_NOT_EXIST
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
        exp = resp.json()['experiment']
        if exp.get('lifecycle_stage') == 'deleted':
            requests.post(
                f'{tracking_uri}/api/2.0/mlflow/experiments/restore',
                json={'experiment_id': exp['experiment_id']},
                headers=headers, auth=auth, verify=verify,
            )
        return exp['experiment_id']

    resp = requests.post(
        f'{tracking_uri}/api/2.0/mlflow/experiments/create',
        json={'name': name},
        headers=headers, auth=auth, verify=verify,
    )
    resp.raise_for_status()
    return resp.json()['experiment_id']


def _pod_node_map(namespace):
    """Best-effort pod -> node mapping via kubectl (pods may be gone)."""
    import subprocess
    try:
        r = subprocess.run(
            ['kubectl', 'get', 'pods', '-n', namespace, '-o',
             'custom-columns=NAME:.metadata.name,NODE:.spec.nodeName,PHASE:.status.phase'],
            capture_output=True, text=True, timeout=30, check=False)
        if r.returncode != 0:
            return {}
        out = {}
        for line in (r.stdout or '').splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2:
                out[parts[0]] = {'node': parts[1], 'phase': parts[2] if len(parts) > 2 else ''}
        return out
    except Exception:
        return {}


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
    project_name: Optional[str] = None,
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
        llmd_image = run_config.get('image', '')
        llmd_version = llmd_image.split(':')[-1] if ':' in llmd_image else ''
        scheduler_image = run_config.get('scheduler_image', '')
        scheduler_version = scheduler_image.split(':')[-1] if ':' in scheduler_image else ''

        tags = {
            'mlflow.source.name': 'serveit-studio',
            'mlflow.source.type': 'LOCAL',
            'serveit.model': model,
            'serveit.model_short': model_short,
            'serveit.status': run_dict['status'],
            'serveit.goal': goal,
            'serveit.run_id': str(run_id),
            'serveit.total_gpus': str(run_dict['max_gpus']),
        }
        if project_name:
            tags['serveit.project'] = project_name
            mlflow.log_param('project', project_name)
        # node correlation: capture the live pod -> node mapping for the
        # run's namespace (pods still up post-run); logged per test below
        _namespace = run_config.get('namespace') or ''
        _pod_nodes = _pod_node_map(_namespace) if _namespace else {}
        if llmd_version:
            tags['serveit.llmd_version'] = llmd_version
        if scheduler_version:
            tags['serveit.scheduler_version'] = scheduler_version
        if run_config.get('gpu_type'):
            tags['serveit.gpu_model'] = run_config['gpu_type']
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
                    child_tags = {
                        'mlflow.source.name': 'serveit-studio',
                        'serveit.model_short': model_short,
                        'serveit.architecture': arch,
                        'serveit.tp': str(tp),
                        'serveit.total_gpus': str(run_dict['max_gpus']),
                        'serveit.prefill_pods': str(test_dict['prefill_pods']),
                        'serveit.decode_pods': str(test_dict['decode_pods']),
                    }
                    if test_dict.get('decode_tp'):
                        child_tags['serveit.decode_tp'] = str(test_dict['decode_tp'])
                    if llmd_version:
                        child_tags['serveit.llmd_version'] = llmd_version
                    if run_config.get('gpu_type'):
                        child_tags['serveit.gpu_model'] = run_config['gpu_type']
                    if project_name:
                        child_tags['serveit.project'] = project_name
                    mlflow.set_tags(child_tags)

                    # ── Node correlation ──────────────────────────────
                    # Per-pod metrics keyed by pod AND node so MLflow
                    # searches can slice by either. Sources: torch profile
                    # summaries + resource snapshots (real runs), engine
                    # report instances (BLIS runs).
                    mj = {}
                    if test_dict.get('metrics_json'):
                        try:
                            mj = json.loads(test_dict['metrics_json'])
                        except (json.JSONDecodeError, TypeError):
                            mj = {}
                    node_metrics = {}
                    node_pods = {}
                    torch = ((mj.get('pprof') or {}).get('torch') or [])
                    for t in torch:
                        pod = t.get('pod') or ''
                        if not pod:
                            continue
                        node = (_pod_nodes.get(pod) or {}).get('node') or 'unknown'
                        node_pods.setdefault(node, []).append(pod)
                        if t.get('kernel_count'):
                            node_metrics[f'pod.{pod}.cuda_ms'] = float(t.get('total_cuda_ms') or 0)
                            node_metrics[f'pod.{pod}.kernels'] = float(t['kernel_count'])
                            node_metrics[f'node.{node}.cuda_ms'] = node_metrics.get(f'node.{node}.cuda_ms', 0) + float(t.get('total_cuda_ms') or 0)
                    snap = mj.get('resource_snapshot') or {}
                    for pod, s in snap.items():
                        node = (_pod_nodes.get(pod) or {}).get('node') or 'unknown'
                        node_pods.setdefault(node, []).append(pod)
                        if s.get('rss_mb'):
                            node_metrics[f'pod.{pod}.rss_mb'] = float(s['rss_mb'])
                        if s.get('cpu_m'):
                            node_metrics[f'pod.{pod}.cpu_m'] = float(s['cpu_m'])
                    am = mj.get('additional_metrics') or {}
                    if am.get('blis'):
                        for inst, rep in (am.get('instance_report') or {}).items():
                            node_metrics[f'instance.{inst}.completed'] = float(rep.get('completed_requests') or 0)
                            if rep.get('e2e_mean_ms') is not None:
                                node_metrics[f'instance.{inst}.e2e_mean_ms'] = float(rep['e2e_mean_ms'])
                        node_pods['simulation'] = sorted((am.get('instances') or {}).keys())
                    if node_metrics:
                        mlflow.log_metrics(node_metrics)
                    if node_pods:
                        mlflow.log_dict({'namespace': _namespace, 'nodes': node_pods,
                                         'pod_nodes': _pod_nodes}, 'pod-node-map.json')

                    # ── Artifacts ─────────────────────────────────────
                    # guidellm raw result + manifests + BLIS scenario/Result
                    if test_dict.get('guidellm_raw_json'):
                        mlflow.log_text(test_dict['guidellm_raw_json'],
                                        f'{test_id}-guidellm-result.json')
                    if test_dict.get('manifests_yaml'):
                        mlflow.log_text(test_dict['manifests_yaml'],
                                        f'{test_id}-manifests.yaml')
                    if am.get('blis'):
                        _blis_dir = Path(f'/mnt/storage/blis/runs/{run_id}')
                        if _blis_dir.is_dir():
                            for f in _blis_dir.glob(f'{test_id}.yaml'):
                                mlflow.log_artifact(str(f), artifact_path='blis-scenario')
                            for f in _blis_dir.glob('*.trace-header.yaml'):
                                mlflow.log_artifact(str(f), artifact_path='blis-trace')

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

                    # Log artifacts from disk — the per-test results dir
                    # (/mnt/storage/results/<test_id>) holds guidellm output,
                    # torch traces and pprof summaries
                    for base in (Path(artifact_dir), Path('/mnt/storage/results')):
                        test_artifact_dir = base / test_id
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
