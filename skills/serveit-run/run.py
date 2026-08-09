#!/usr/bin/env python3
"""ServeIt Studio automation — drives a full optimization run via Socket.IO."""

import argparse
import json
import os
import sys
import threading
import time
import urllib3

import requests
import socketio

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

STATE = {
    'connected': False,
    'scan': None,
    'storage': None,
    'storage_done': False,
    'plan': None,
    'running': None,
    'finished': threading.Event(),
    'error': None,
}


def log(msg, level='info'):
    icons = {'info': ' ', 'ok': '+', 'warn': '!', 'err': 'X', 'step': '=>'}
    ts = time.strftime('%H:%M:%S')
    print(f"[{ts}] {icons.get(level, ' ')} {msg}", flush=True)


def build_parser():
    p = argparse.ArgumentParser(description='ServeIt Studio — run optimization via API')
    p.add_argument('--url', required=True, help='ServeIt Studio instance URL')
    p.add_argument('--model', required=True, help='HuggingFace model name')
    p.add_argument('--username', default='admin', help='Login username')
    p.add_argument('--password', default='admin', help='Login password')
    p.add_argument('--isl', type=int, default=2000, help='Input sequence length')
    p.add_argument('--isl-stdev', type=int, default=None, help='ISL standard deviation')
    p.add_argument('--osl', type=int, default=2000, help='Output sequence length')
    p.add_argument('--osl-stdev', type=int, default=None, help='OSL standard deviation')
    p.add_argument('--users', type=int, default=100, help='Concurrent users')
    p.add_argument('--gpus', type=int, default=16, help='Max GPUs')
    p.add_argument('--objective', default='ttft',
                   choices=['ttft', 'throughput', 'balanced', 'aggregated_only', 'pd_only'],
                   help='Optimization goal')
    p.add_argument('--duration', type=int, default=300, help='Test duration (seconds)')
    p.add_argument('--storage-class', default=None, help='Storage class (auto-detected)')
    p.add_argument('--local-disk-path', default=None, help='hostPath for local disk')
    p.add_argument('--hf-token', default=None, help='HuggingFace token')
    p.add_argument('--epp-preset', default='balanced',
                   choices=['balanced', 'cache_optimized', 'queue_balanced', 'latency_aware'],
                   help='EPP routing preset')
    p.add_argument('--epp-benchmark', action='store_true', help='Benchmark EPP strategies')
    p.add_argument('--image', default='ghcr.io/llm-d/llm-d-cuda:v0.8.0', help='vLLM image')
    p.add_argument('--scheduler-image', default='ghcr.io/llm-d/llm-d-router-endpoint-picker:v0.9.0',
                   help='EPP scheduler image')
    p.add_argument('--prefix-cache-pct', type=int, default=0, help='Prefix cache hit %% (0=off)')
    p.add_argument('--prefix-cache-mode', default='identical',
                   choices=['identical', 'shared_prefix', 'multi_group'])
    p.add_argument('--prefix-cache-groups', type=int, default=5)
    p.add_argument('--timeout', type=int, default=180, help='Max wait time (minutes)')
    p.add_argument('--dry-run', action='store_true', help='Scan + plan only')
    p.add_argument('--auto-tune', action='store_true', default=True,
                   help='Enable vLLM auto-tuning (default: on)')
    p.add_argument('--no-auto-tune', dest='auto_tune', action='store_false')
    return p


def login(url, username, password):
    session = requests.Session()
    session.verify = False
    r = session.post(f'{url}/login', data={'username': username, 'password': password},
                     allow_redirects=False)
    if r.status_code not in (200, 302):
        log(f'Login failed: HTTP {r.status_code}', 'err')
        sys.exit(1)
    cookies = session.cookies.get_dict()
    if not cookies:
        log('Login failed: no session cookie returned', 'err')
        sys.exit(1)
    log(f'Logged in as {username}', 'ok')
    return session, cookies


def auto_detect_storage(scan_data):
    """Pick the best storage class from scan results."""
    scs = scan_data.get('storage_classes', [])
    local = [sc for sc in scs if sc.get('is_local') and sc.get('gpu_nodes_covered', 0) > 0]
    if local:
        sc = local[0]
        return sc['name'], sc.get('local_path', '')
    rwx = [sc for sc in scs if sc.get('access_mode') == 'ReadWriteMany']
    if rwx:
        return rwx[0]['name'], ''
    if scs:
        return scs[0]['name'], ''
    return None, ''


def main():
    args = build_parser().parse_args()
    url = args.url.rstrip('/')
    hf_token = args.hf_token or os.environ.get('HF_TOKEN', '')

    log(f'ServeIt Studio Automation', 'step')
    log(f'Instance: {url}')
    log(f'Model:    {args.model}')
    log(f'Workload: ISL={args.isl} OSL={args.osl} Users={args.users}')
    log(f'GPUs:     {args.gpus}  Objective: {args.objective}')
    if args.dry_run:
        log('Mode:     DRY RUN (scan + plan only)', 'warn')
    print()

    # --- Login ---
    session, cookies = login(url, args.username, args.password)
    cookie_header = '; '.join(f'{k}={v}' for k, v in cookies.items())

    # --- Socket.IO ---
    sio = socketio.Client(ssl_verify=False, logger=False, engineio_logger=False)

    @sio.on('session_granted')
    def on_granted():
        STATE['connected'] = True
        log('Session granted', 'ok')

    @sio.on('session_locked')
    def on_locked(data):
        log(f'Session locked by {data.get("username")} — taking over...', 'warn')
        sio.emit('take_over')

    @sio.on('cluster_scan_result')
    def on_scan(data):
        STATE['scan'] = data

    @sio.on('storage_setup_result')
    def on_storage(data):
        STATE['storage'] = data

    @sio.on('storage_download_complete')
    def on_download_done(data):
        STATE['storage_done'] = True

    @sio.on('test_plan_result')
    def on_plan(data):
        STATE['plan'] = data

    @sio.on('test_plan_ready')
    def on_plan_ready(data):
        if data.get('test_plan'):
            STATE['plan'] = data['test_plan']

    @sio.on('status_update')
    def on_status(data):
        STATE['running'] = data.get('running')
        if not data.get('running') and STATE['running'] is not None:
            STATE['finished'].set()

    @sio.on('console_log')
    def on_log(data):
        if data.get('replayed'):
            return
        msg = data.get('message', '')
        lvl = data.get('type', 'info')
        if lvl == 'error':
            log(msg, 'err')
        elif lvl == 'warning':
            log(msg, 'warn')
        elif lvl == 'success':
            log(msg, 'ok')
        else:
            log(msg)
        if 'Starting optimization' in msg or 'RECIPE-BASED OPTIMIZATION' in msg:
            STATE['auto_started'] = True

    @sio.on('error')
    def on_error(data):
        STATE['error'] = data.get('message', str(data))
        log(f'Server error: {STATE["error"]}', 'err')

    log('Connecting to Socket.IO...', 'step')
    try:
        sio.connect(url, headers={'Cookie': cookie_header},
                    transports=['websocket', 'polling'])
    except Exception as e:
        log(f'Connection failed: {e}', 'err')
        sys.exit(1)

    # Wait for session
    for _ in range(30):
        if STATE['connected']:
            break
        time.sleep(0.5)
    if not STATE['connected']:
        log('Timed out waiting for session', 'err')
        sys.exit(1)

    # --- Step 1: Scan Cluster ---
    log('Scanning cluster...', 'step')
    sio.emit('scan_cluster', {})
    for _ in range(120):
        if STATE['scan']:
            break
        time.sleep(1)
    if not STATE['scan']:
        log('Cluster scan timed out', 'err')
        sys.exit(1)

    scan = STATE['scan']
    log(f'Cluster: {scan.get("gpu_node_count", 0)} GPU nodes, '
        f'{scan.get("total_gpus", 0)} GPUs ({scan.get("gpu_model", "?")}), '
        f'RDMA={"yes" if scan.get("has_rdma") else "no"}', 'ok')
    log(f'GPU VRAM: {round(scan.get("gpu_memory_per_gpu_mb", 0) / 1024, 1)} GB, '
        f'TP options: {scan.get("tp_options", [])}')
    scs = scan.get('storage_classes', [])
    for sc in scs:
        tag = ' [LOCAL]' if sc.get('is_local') else ''
        log(f'  SC: {sc["name"]} ({sc["provisioner"]}){tag}')
    print()

    # --- Auto-detect storage ---
    storage_class = args.storage_class
    local_disk_path = args.local_disk_path
    if not storage_class:
        storage_class, local_disk_path = auto_detect_storage(scan)
        if storage_class:
            log(f'Auto-detected storage: {storage_class}' +
                (f' (hostPath: {local_disk_path})' if local_disk_path else ''), 'ok')
        else:
            log('No storage class found — will use default PVC', 'warn')

    per_node = bool(local_disk_path)

    # --- Step 2: Setup Storage ---
    log('Setting up storage and downloading model...', 'step')
    storage_data = {
        'model': args.model,
        'storage_class': storage_class,
        'pvc_size': 256,
        'hf_token': hf_token,
        'per_node_storage': per_node,
    }
    if local_disk_path:
        storage_data['local_disk_path'] = local_disk_path

    sio.emit('setup_storage', storage_data)

    # Wait for download to complete
    timeout_s = args.timeout * 60
    start = time.time()
    while not STATE['storage_done'] and (time.time() - start) < timeout_s:
        if STATE['error']:
            log(f'Storage setup failed: {STATE["error"]}', 'err')
            sio.disconnect()
            sys.exit(1)
        time.sleep(5)

    if not STATE['storage_done']:
        log(f'Storage setup timed out after {args.timeout} min', 'err')
        sio.disconnect()
        sys.exit(1)

    log('Model download complete', 'ok')
    print()

    # Check if the server auto-started optimization after download
    if STATE.get('auto_started'):
        log('Server auto-started optimization after download', 'ok')
        if args.dry_run:
            log('Dry run requested but optimization already started — monitoring', 'warn')
        # Skip plan generation and start — just monitor
        STATE['finished'].clear()
        timeout_s = args.timeout * 60
        finished = STATE['finished'].wait(timeout=timeout_s)
        if not finished:
            log(f'Optimization timed out after {args.timeout} min', 'err')
            sio.disconnect()
            sys.exit(1)
        # Fall through to results
    else:
        # --- Step 3: Generate Test Plan ---
        log('Generating test plan...', 'step')
        sio.emit('generate_test_plan', {
            'model': args.model,
            'optimization_goal': args.objective,
            'max_gpus': args.gpus,
            'isl': args.isl,
            'osl': args.osl,
            'num_users': args.users,
            'hf_token': hf_token,
        })

        for _ in range(60):
            if STATE['plan']:
                break
            time.sleep(1)
        if not STATE['plan']:
            log('Test plan generation timed out', 'err')
            sio.disconnect()
            sys.exit(1)

        plan = STATE['plan']
        tests = plan.get('tests', [])
        can_proceed = plan.get('can_proceed', False)
        log(f'Test plan: {len(tests)} tests, can_proceed={can_proceed}', 'ok')
        reqs = plan.get('model_requirements', {})
        if reqs:
            log(f'Model VRAM: {reqs.get("estimated_vram_gb", "?")} GB, '
                f'min TP: {reqs.get("min_tp", "?")}, '
                f'recommended TP: {reqs.get("recommended_tp_options", [])}')
        for t in tests[:5]:
            log(f'  {t.get("test_name", "?")} — {t.get("architecture", "?")} '
                f'TP{t.get("tp", "?")} ({t.get("gpus_required", "?")} GPUs)')
        if len(tests) > 5:
            log(f'  ... and {len(tests) - 5} more tests')
        print()

        if not can_proceed:
            log(f'Cannot proceed: {plan.get("error_message", "unknown")}', 'err')
            sio.disconnect()
            sys.exit(1)

        if args.dry_run:
            log('Dry run complete — stopping before optimization', 'step')
            result = {
                'status': 'dry_run',
                'cluster': {
                    'gpu_nodes': scan.get('gpu_node_count', 0),
                    'total_gpus': scan.get('total_gpus', 0),
                    'gpu_model': scan.get('gpu_model', ''),
                },
                'plan': {
                    'test_count': len(tests),
                    'model_vram_gb': reqs.get('estimated_vram_gb'),
                    'min_tp': reqs.get('min_tp'),
                },
            }
            print(f'\n__RESULT_JSON__\n{json.dumps(result, indent=2)}')
            sio.disconnect()
            sys.exit(0)

        # --- Step 4: Start Optimization ---
        log('Starting optimization...', 'step')
        STATE['finished'].clear()

        opt_data = {
            'model': args.model,
            'isl': args.isl,
            'osl': args.osl,
            'isl_stdev': args.isl_stdev,
            'osl_stdev': args.osl_stdev,
            'num_users': args.users,
            'optimization_metric': args.objective,
            'max_test_duration': args.duration,
            'stop_mode': 'duration',
            'hf_token': hf_token,
            'max_gpus': args.gpus,
            'use_achievable_qps': False,
            'selected_nodes': [],
            'workload_mode': 'synthetic',
            'rate_type': 'concurrent',
            'prefix_cache_hit_pct': args.prefix_cache_pct,
            'prefix_cache_mode': args.prefix_cache_mode,
            'prefix_cache_groups': args.prefix_cache_groups,
            'latency_constraint_enabled': False,
            'tp_pair_top_n': 4,
            'pd_search_mode': 'smart',
            'advanced_vllm_custom_enabled': args.auto_tune,
            'advanced_vllm': None,
            'epp_custom_enabled': True,
            'epp_preset': args.epp_preset,
            'epp_benchmark': args.epp_benchmark,
            'epp_config': None,
            'image': args.image,
            'scheduler_image': args.scheduler_image,
            'per_node_storage': per_node,
            'local_disk_path': local_disk_path or None,
            'storage_class': storage_class,
        }

        sio.emit('start_optimization', opt_data)

        # Wait for completion
        timeout_s = args.timeout * 60
        finished = STATE['finished'].wait(timeout=timeout_s)
        if not finished:
            log(f'Optimization timed out after {args.timeout} min', 'err')
            sio.disconnect()
            sys.exit(1)

    print()
    log('Optimization complete — fetching results...', 'step')

    # --- Step 5: Fetch Results ---
    try:
        runs = session.get(f'{url}/api/runs', verify=False).json()
        if not runs:
            log('No runs found', 'err')
            sio.disconnect()
            sys.exit(1)

        latest = runs[0]
        run_id = latest['id']
        log(f'Run #{run_id}: {latest["status"]}', 'ok')

        charts = session.get(f'{url}/api/runs/{run_id}/charts', verify=False).json()
        summary = charts.get('summary', {})
        best = summary.get('best_configs', {})

        result = {
            'status': latest['status'],
            'run_id': run_id,
            'tests_run': summary.get('successful_tests', 0),
            'total_tests': summary.get('total_tests', 0),
        }

        if best.get('lowest_latency'):
            ll = best['lowest_latency']
            result['best_ttft'] = {
                'config': ll.get('config_name', ''),
                'ttft_p90_ms': ll.get('ttft_p90'),
                'throughput_mean': ll.get('throughput_mean'),
            }
            log(f'Best TTFT: {ll.get("ttft_p90")}ms — {ll.get("config_name")} '
                f'({ll.get("throughput_mean")} req/s)', 'ok')

        if best.get('highest_throughput'):
            ht = best['highest_throughput']
            result['best_throughput'] = {
                'config': ht.get('config_name', ''),
                'ttft_p90_ms': ht.get('ttft_p90'),
                'throughput_mean': ht.get('throughput_mean'),
            }
            log(f'Best Throughput: {ht.get("throughput_mean")} req/s — {ht.get("config_name")} '
                f'({ht.get("ttft_p90")}ms TTFT)', 'ok')

        print(f'\n__RESULT_JSON__\n{json.dumps(result, indent=2)}')

    except Exception as e:
        log(f'Failed to fetch results: {e}', 'err')

    sio.disconnect()


if __name__ == '__main__':
    main()
