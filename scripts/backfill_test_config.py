#!/usr/bin/env python3
"""Backfill test_config_json for existing test_configurations rows."""
import sqlite3, json, sys

db_path = sys.argv[1] if len(sys.argv) > 1 else '/mnt/storage/serveit.db'
run_id = int(sys.argv[2]) if len(sys.argv) > 2 else 3

conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row

run_row = conn.execute('SELECT config_json FROM optimization_runs WHERE id = ?', (run_id,)).fetchone()
if not run_row or not run_row['config_json']:
    print(f'Run #{run_id} not found or has no config_json')
    sys.exit(1)

run_cfg = json.loads(run_row['config_json'])
tests = conn.execute(
    'SELECT id, config_name, architecture, tensor_parallelism, decode_tp, prefill_pods, decode_pods FROM test_configurations WHERE run_id = ?',
    (run_id,)
).fetchall()

updated = 0
for t in tests:
    arch = t['architecture'] or 'aggregated'
    tp = t['tensor_parallelism']
    dtp = t['decode_tp'] or tp
    pp = t['prefill_pods']
    dp = t['decode_pods']
    is_cal = t['config_name'].startswith('step2-') or t['config_name'].startswith('step3-')

    tc = {
        'test_id': t['config_name'],
        'architecture': arch,
        'model_name': run_cfg['model_name'],
        'namespace': run_cfg['namespace'],
        'isl': 1 if t['config_name'].startswith('step2-') else run_cfg['isl'],
        'osl': 1 if t['config_name'].startswith('step3-') else run_cfg['osl'],
        'num_users': int(run_cfg['qps']),
        'tensor_parallelism': tp,
        'replicas': pp + dp,
        'image': run_cfg['image'],
        'pvc_name': run_cfg['pvc_name'],
        'max_model_len': run_cfg['max_model_len'],
        'gpu_memory_utilization': run_cfg['gpu_memory_utilization'],
        'block_size': 128,
        'network_type': run_cfg.get('network_type'),
        'request_type': run_cfg['rate_type'],
        'request_rate': int(run_cfg['qps']),
        'test_duration': run_cfg['test_duration'],
        'stop_mode': run_cfg['stop_mode'],
        'isl_stdev': None if is_cal else run_cfg.get('isl_stdev'),
        'osl_stdev': None if is_cal else run_cfg.get('osl_stdev'),
        'turns': 1 if is_cal else run_cfg.get('turns', 1),
        'workload_mode': run_cfg.get('workload_mode', 'synthetic'),
        'dataset_source': run_cfg.get('dataset_source'),
        'enable_prefix_caching': True,
        'trust_remote_code': True,
        'disable_log_requests': True,
        'dtype': None,
        'kv_cache_dtype': None,
        'kv_connector': 'NixlConnector',
        'memory_request': '64Gi',
        'cpu_request': '16',
    }

    if arch == 'pd':
        tc['prefill_replicas'] = pp
        tc['decode_replicas'] = dp
        tc['prefill_tp'] = tp
        tc['decode_tp'] = dtp
        tc['prefill_decode_ratio'] = f'{pp}:{dp}'

    conn.execute('UPDATE test_configurations SET test_config_json = ? WHERE id = ?',
                 (json.dumps(tc), t['id']))
    updated += 1

conn.commit()
row = conn.execute(
    'SELECT count(*) FROM test_configurations WHERE run_id = ? AND test_config_json IS NOT NULL',
    (run_id,)
).fetchone()
print(f'Backfilled {updated} tests. Verified: {row[0]} have test_config_json')
conn.close()
