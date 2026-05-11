#!/usr/bin/env python3
"""
Inftune Studio CLI — run LLM inference optimization from the command line.

Usage:
    # Register clusters
    inftune cluster add --name local
    inftune cluster add --name prod --kubeconfig ~/.kube/prod.yaml
    inftune cluster list
    inftune cluster scan prod
    inftune cluster remove prod

    # Run optimization
    inftune run --model RedHatAI/gpt-oss-20b --cluster local
    inftune run --model RedHatAI/gpt-oss-20b --cluster prod \\
        --isl 9000 --osl 50 --users 100 --gpus 16 --objective ttft

    # Resume a previous run
    inftune run --resume 7 --cluster prod
"""

import argparse
import sys
import os
from datetime import datetime


# ── Cluster commands ─────────────────────────────────────────────────────────

def _setup_paths():
    app_root = os.environ.get('INFTUNE_PATH', '/mnt/storage/app')
    if app_root not in sys.path:
        sys.path.insert(0, app_root)


def _ensure_launcher_db():
    """Initialize the launcher DB and return the module."""
    _setup_paths()
    from launcher.database import init_db
    init_db()


def _get_cli_owner_id():
    """Get or create a CLI user in the launcher DB. Returns owner_id."""
    from launcher.database import get_db
    with get_db() as conn:
        row = conn.execute("SELECT id FROM users WHERE username = 'cli'").fetchone()
        if row:
            return row['id']
        import secrets
        from werkzeug.security import generate_password_hash
        conn.execute(
            "INSERT INTO users (username, password_hash, is_admin, must_reset, created_at) VALUES (?, ?, 0, 0, ?)",
            ('cli', generate_password_hash(secrets.token_urlsafe(32)), datetime.now().isoformat())
        )
        return conn.execute('SELECT last_insert_rowid()').fetchone()[0]


def _resolve_cluster(name):
    """Look up a cluster by name. Returns cluster dict or exits with error."""
    from launcher.database import get_db
    owner_id = _get_cli_owner_id()
    with get_db() as conn:
        row = conn.execute(
            'SELECT * FROM clusters WHERE owner_id = ? AND name = ?',
            (owner_id, name)
        ).fetchone()
    if not row:
        print(f"Error: cluster '{name}' not found. Run 'inftune cluster list' to see registered clusters.")
        sys.exit(1)
    return dict(row)


def cmd_cluster_add(args):
    """Register a cluster."""
    _ensure_launcher_db()
    from launcher import instance_manager

    owner_id = _get_cli_owner_id()

    kubeconfig_data = None
    if args.kubeconfig:
        path = os.path.expanduser(args.kubeconfig)
        if not os.path.exists(path):
            print(f"Error: kubeconfig file not found: {path}")
            return 1
        with open(path) as f:
            kubeconfig_data = f.read()

    try:
        result = instance_manager.create_cluster(
            owner_id=owner_id,
            name=args.name,
            namespace=args.namespace,
            kubeconfig_data=kubeconfig_data,
            storage_class=args.storage_class,
        )
        target = result.get('target_cluster', 'local')
        print(f"Cluster '{args.name}' registered ({target})")
        return 0
    except RuntimeError as e:
        print(f"Error: {e}")
        return 1


def cmd_cluster_list(args):
    """List registered clusters."""
    _ensure_launcher_db()
    from launcher import instance_manager

    owner_id = _get_cli_owner_id()
    clusters = instance_manager.list_clusters(owner_id)

    if not clusters:
        print("No clusters registered. Use 'inftune cluster add' to register one.")
        return 0

    fmt = '{:<4} {:<20} {:<30} {:<15} {:<20}'
    print(fmt.format('ID', 'Name', 'Target', 'Storage Class', 'Last Scanned'))
    print('-' * 89)
    for c in clusters:
        scanned = c.get('scanned_at', '') or 'never'
        if scanned != 'never':
            scanned = scanned[:19]
        print(fmt.format(
            c['id'],
            c['name'][:20],
            (c.get('target_cluster') or 'local')[:30],
            (c.get('storage_class') or '-')[:15],
            scanned,
        ))
    return 0


def cmd_cluster_remove(args):
    """Remove a registered cluster."""
    _ensure_launcher_db()
    from launcher import instance_manager

    owner_id = _get_cli_owner_id()
    cluster = _resolve_cluster(args.name)

    if instance_manager.delete_cluster(cluster['id'], owner_id):
        print(f"Cluster '{args.name}' removed")
        return 0
    else:
        print(f"Error: could not remove cluster '{args.name}'")
        return 1


def cmd_cluster_scan(args):
    """Scan cluster resources."""
    _ensure_launcher_db()
    import json as _json
    from launcher.cluster_scanner import scan_cluster_resources
    from launcher.database import get_db

    cluster = _resolve_cluster(args.name)

    print(f"Scanning cluster '{args.name}'...")
    try:
        result = scan_cluster_resources(cluster, args.namespace)
    except Exception as e:
        print(f"Error: {e}")
        return 1

    # Cache scan results in DB
    with get_db() as conn:
        conn.execute(
            'UPDATE clusters SET scan_data = ?, scanned_at = ? WHERE id = ?',
            (_json.dumps(result), datetime.now().isoformat(), cluster['id'])
        )

    s = result.get('summary', {})
    nodes = result.get('nodes', [])

    print()
    print(f"  Cloud provider:  {s.get('cloud_provider', 'unknown')}")
    print(f"  GPU model:       {s.get('gpu_model', 'unknown')}")
    print(f"  Total GPUs:      {s.get('total_gpus', 0)} ({s.get('gpus_available', '?')} available)")
    print(f"  GPU nodes:       {s.get('gpu_node_count', 0)} / {s.get('node_count', 0)} total")
    print(f"  GPU VRAM:        {round(s.get('gpu_memory_per_gpu_mb', 0) / 1024, 1)} GB")
    print(f"  RDMA:            {'yes' if s.get('has_rdma') else 'no'}")
    print(f"  Total CPU:       {s.get('total_cpu_cores', 0)} cores")
    print(f"  Total Memory:    {s.get('total_memory_gb', 0)} GB")

    if nodes:
        print()
        fmt = '  {:<40} {:<6} {:<15} {:<8}'
        print(fmt.format('Node', 'GPUs', 'GPU Model', 'RDMA'))
        print('  ' + '-' * 69)
        for n in nodes:
            print(fmt.format(
                n['name'][:40],
                n['gpus'],
                (n.get('gpu_model') or '-')[:15],
                'yes' if n.get('has_rdma') else 'no',
            ))
    return 0


# ── Cluster kubeconfig resolution for run ────────────────────────────────────

def _get_cluster_kubeconfig(cluster):
    """Extract kubeconfig from a cluster's K8s Secret. Returns temp file path or None."""
    secret_name = cluster.get('kubeconfig_secret')
    if not secret_name:
        return None

    import base64
    import subprocess
    import tempfile

    namespace = cluster.get('namespace', 'inftune')
    # Use the launcher's own kubectl to read the secret from the launcher cluster
    cmd = 'oc' if _is_oc() else 'kubectl'
    r = subprocess.run(
        [cmd, 'get', 'secret', secret_name, '-n', namespace,
         '-o', 'jsonpath={.data.kubeconfig}'],
        capture_output=True, text=True, timeout=15
    )
    if r.returncode != 0 or not r.stdout.strip():
        print(f"Error: could not read kubeconfig Secret '{secret_name}'")
        sys.exit(1)

    kubeconfig_data = base64.b64decode(r.stdout.strip()).decode()
    tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.kubeconfig', delete=False)
    tmp.write(kubeconfig_data)
    tmp.close()
    return tmp.name


def _is_oc():
    import subprocess
    try:
        r = subprocess.run(['kubectl', 'api-resources', '--api-group=route.openshift.io'],
                          capture_output=True, text=True, timeout=15)
        return r.returncode == 0 and 'Route' in r.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


# ── Run command ──────────────────────────────────────────────────────────────

def cmd_run(args):
    """Run or resume an optimization."""
    _setup_paths()

    if not args.resume and not args.model:
        print('Error: --model is required (or use --resume RUN_ID)')
        return 1

    from core.recipe_optimizer import RecipeOptimizer, RecipeOptimizerConfig
    from core.database_manager import DatabaseManager

    db = DatabaseManager(db_path=args.db)

    # Resolve cluster kubeconfig
    kubeconfig_path = None
    cluster_namespace = None
    if args.cluster:
        _ensure_launcher_db()
        cluster = _resolve_cluster(args.cluster)
        kubeconfig_path = _get_cluster_kubeconfig(cluster)
        cluster_namespace = cluster.get('namespace') or 'inftune'

    if args.resume:
        return resume_run(args, db, kubeconfig_path)

    tp_options = [int(x) for x in args.tp_options.split(',')]

    # Build advanced_vllm dict from flags
    advanced_vllm = {}
    adv_value_map = {
        'max_model_len': args.max_model_len,
        'gpu_memory_utilization': args.gpu_mem_util,
        'block_size': args.block_size,
        'dtype': args.dtype,
        'kv_cache_dtype': args.kv_cache_dtype,
        'pipeline_parallel_size': args.pipeline_parallel,
        'max_num_seqs': args.max_num_seqs,
        'max_num_batched_tokens': args.max_num_batched_tokens,
        'tool_call_parser': args.tool_call_parser,
    }
    for key, val in adv_value_map.items():
        if val is not None:
            advanced_vllm[key] = {'mode': 'custom', 'value': val}

    # Toggle flags
    toggle_map = {
        'enable_prefix_caching': (args.enable_prefix_caching, args.no_prefix_caching),
        'disable_custom_all_reduce': (args.disable_custom_all_reduce, None),
        'trust_remote_code': (args.trust_remote_code, args.no_trust_remote_code),
        'disable_log_requests': (args.disable_log_requests, None),
        'enable_auto_tool_choice': (args.enable_auto_tool_choice, None),
        'vllm_debug_logs': (args.vllm_debug_logs, None),
        'nccl_debug_logs': (args.nccl_debug_logs, None),
    }
    for key, (on_flag, off_flag) in toggle_map.items():
        if on_flag:
            advanced_vllm[key] = {'mode': 'on'}
        elif off_flag:
            advanced_vllm[key] = {'mode': 'off'}

    if not advanced_vllm:
        advanced_vllm = None

    # EPP config
    epp_config = None
    epp_preset = args.epp_preset
    if args.epp_weights:
        parts = args.epp_weights.split(':')
        if len(parts) != 3:
            print('Error: --epp-weights must be C:K:Q (e.g., 5:1:1)')
            return 1
        epp_preset = 'custom'
        epp_config = {
            'preset': 'custom',
            'plugins': {
                'prefix_cache': {'enabled': True, 'weight': float(parts[0])},
                'kv_cache': {'enabled': True, 'weight': float(parts[1])},
                'queue': {'enabled': True, 'weight': float(parts[2])},
                'slo': {'enabled': False},
            }
        }
    if args.epp_max_prefix_blocks or args.epp_lru_capacity or args.epp_non_cached_tokens:
        if epp_config is None:
            epp_config = {}
        if args.epp_max_prefix_blocks:
            epp_config['maxPrefixBlocksToMatch'] = args.epp_max_prefix_blocks
        if args.epp_lru_capacity:
            epp_config['lruCapacityPerServer'] = args.epp_lru_capacity
        if args.epp_non_cached_tokens:
            epp_config['nonCachedTokens'] = args.epp_non_cached_tokens

    selected_nodes = [n.strip() for n in args.nodes.split(',')] if args.nodes else []

    namespace = args.namespace or cluster_namespace or 'inftune'

    config_params = {
        'model_name': args.model,
        'namespace': namespace,
        'isl': args.isl,
        'osl': args.osl,
        'qps': float(args.users),
        'rate_type': args.rate_type,
        'total_gpus': args.gpus,
        'test_duration': args.duration,
        'stop_mode': args.stop_mode,
        'max_requests': args.max_requests,
        'isl_stdev': args.isl_stdev,
        'osl_stdev': args.osl_stdev,
        'turns': args.turns,
        'tp_options': tp_options,
        'tp_pair_top_n': args.tp_pair_depth,
        'pd_search_mode': args.pd_search,
        'headroom': args.headroom,
        'objective': args.objective,
        'use_achievable_qps': args.use_achievable_qps,
        'latency_constraint_enabled': args.latency_sla is not None,
        'latency_constraint_ms': args.latency_sla or 500,
        'latency_constraint_percentile': args.latency_percentile,
        'image': args.image,
        'pvc_name': args.pvc,
        'nccl_ib_hca': args.nccl_ib_hca,
        'hf_token': args.hf_token or os.environ.get('HF_TOKEN'),
        'selected_nodes': selected_nodes,
        'workload_mode': args.workload_mode,
        'dataset_source': args.dataset,
        'dataset_column': args.dataset_column,
        'dataset_max_output': args.dataset_max_output,
        'prefix_cache_hit_pct': args.prefix_cache_pct,
        'prefix_cache_mode': args.prefix_cache_mode,
        'prefix_cache_groups': args.prefix_cache_groups,
        'epp_preset': epp_preset,
        'epp_benchmark': args.epp_benchmark,
        'epp_config': epp_config,
        'advanced_vllm': advanced_vllm,
        'kubeconfig': kubeconfig_path,
        'single_test_architecture': args.single_test_arch,
        'single_test_tp': args.single_test_tp,
        'single_test_replicas': args.single_test_replicas,
        'single_test_prefill_tp': args.single_test_prefill_tp,
        'single_test_decode_tp': args.single_test_decode_tp,
        'single_test_prefill_pods': args.single_test_prefill_pods,
        'single_test_decode_pods': args.single_test_decode_pods,
    }

    # Create DB entry
    run_name = f"cli-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    run_id = db.create_optimization_run(
        run_name=run_name,
        model=args.model,
        isl=args.isl,
        osl=args.osl,
        num_users=args.users,
        notes=args.description or f"CLI run: {args.model}",
        config_dict=config_params,
    )

    if not args.quiet:
        print(f"{'=' * 70}")
        print(f"  Inftune Studio Optimization — Run #{run_id}")
        print(f"{'=' * 70}")
        print(f"  Model:      {args.model}")
        print(f"  Workload:   ISL={args.isl} OSL={args.osl} x {args.users} users")
        print(f"  GPUs:       {args.gpus}")
        print(f"  Objective:  {args.objective}")
        print(f"  TP options: {tp_options}")
        if args.cluster:
            print(f"  Cluster:    {args.cluster}")
        if args.latency_sla:
            print(f"  Latency SLA: {args.latency_sla}ms @ {args.latency_percentile}")
        if args.epp_benchmark:
            print(f"  EPP tuning: enabled (preset: {epp_preset})")
        if args.prefix_cache_pct:
            print(f"  Prefix cache: {args.prefix_cache_pct}% ({args.prefix_cache_mode})")
        print(f"  DB: {args.db} (run #{run_id})")
        print(f"{'=' * 70}")
        print()

    return run_optimization(config_params, run_id, args.db, args.quiet,
                            html_report=args.html_report, kubeconfig=kubeconfig_path)


def resume_run(args, db, kubeconfig_path=None):
    """Resume a previous optimization run."""
    import json as _json

    run_id = args.resume
    with db.get_connection() as conn:
        conn.row_factory = __import__('sqlite3').Row
        row = conn.execute('SELECT * FROM optimization_runs WHERE id = ?', (run_id,)).fetchone()
        if not row:
            print(f"Error: run #{run_id} not found")
            return 1

    row = dict(row)
    saved_config = {}
    if row.get('config_json'):
        saved_config = _json.loads(row['config_json'])

    config_params = {
        'model_name': row['model'],
        'namespace': saved_config.get('namespace', 'inftune'),
        'isl': row['isl'],
        'osl': row['osl'],
        'qps': float(row['num_users']),
        'rate_type': row.get('rate_type', 'concurrent'),
        'total_gpus': row.get('max_gpus', 16),
        'test_duration': row.get('test_duration', 300),
        'stop_mode': saved_config.get('stop_mode', 'duration'),
        'max_requests': saved_config.get('max_requests'),
        'isl_stdev': row.get('isl_stdev'),
        'osl_stdev': row.get('osl_stdev'),
        'turns': row.get('turns', 1),
        'tp_options': saved_config.get('tp_options', [1, 2, 4, 8]),
        'tp_pair_top_n': saved_config.get('tp_pair_top_n', 2),
        'pd_search_mode': saved_config.get('pd_search_mode', 'smart'),
        'headroom': saved_config.get('headroom', 1.3),
        'objective': row.get('goal', 'ttft'),
        'use_achievable_qps': bool(row.get('use_achievable_qps', 0)),
        'latency_constraint_enabled': bool(row.get('latency_constraint_enabled', 0)),
        'latency_constraint_ms': row.get('latency_constraint_ms', 500),
        'latency_constraint_percentile': row.get('latency_constraint_percentile', 'p99'),
        'image': saved_config.get('image', 'ghcr.io/llm-d/llm-d-cuda:v0.5.1'),
        'pvc_name': saved_config.get('pvc_name', 'inftune-model-cache'),
        'nccl_ib_hca': saved_config.get('nccl_ib_hca', 'mlx'),
        'hf_token': saved_config.get('hf_token') or os.environ.get('HF_TOKEN'),
        'selected_nodes': saved_config.get('selected_nodes', []),
        'workload_mode': row.get('workload_mode', 'synthetic'),
        'dataset_source': row.get('dataset_source'),
        'dataset_column': row.get('dataset_column'),
        'dataset_max_output': row.get('dataset_max_output', 256),
        'prefix_cache_hit_pct': row.get('prefix_cache_hit_pct', 0),
        'prefix_cache_mode': saved_config.get('prefix_cache_mode', 'identical'),
        'prefix_cache_groups': saved_config.get('prefix_cache_groups', 5),
        'prefix_cache_seed': row.get('prefix_cache_seed'),
        'epp_preset': saved_config.get('epp_preset', 'balanced'),
        'epp_benchmark': saved_config.get('epp_benchmark', False),
        'epp_config': saved_config.get('epp_config'),
        'advanced_vllm': saved_config.get('advanced_vllm'),
        'kubeconfig': kubeconfig_path,
        'single_test_architecture': saved_config.get('single_test_architecture'),
        'single_test_tp': saved_config.get('single_test_tp'),
        'single_test_replicas': saved_config.get('single_test_replicas'),
        'single_test_prefill_tp': saved_config.get('single_test_prefill_tp'),
        'single_test_decode_tp': saved_config.get('single_test_decode_tp'),
        'single_test_prefill_pods': saved_config.get('single_test_prefill_pods'),
        'single_test_decode_pods': saved_config.get('single_test_decode_pods'),
    }

    if not args.quiet:
        print(f"Resuming run #{run_id}: {row['model']} ({row['status']})")

    return run_optimization(config_params, run_id, args.db, args.quiet,
                            resume=True, html_report=args.html_report,
                            kubeconfig=kubeconfig_path)


def ensure_model_ready(config, log_fn):
    """Ensure model cache PVC exists and model is downloaded."""
    import subprocess as _sp
    from core.template_manager import TemplateManager

    pvc_name = config.pvc_name or 'inftune-model-cache'
    namespace = config.namespace
    model = config.model_name

    r = _sp.run(['kubectl', 'get', 'pvc', pvc_name, '-n', namespace],
                capture_output=True, timeout=10)
    if r.returncode != 0:
        log_fn(f'Creating model cache PVC: {pvc_name}', 'info')
        tmgr = TemplateManager()

        sc_r = _sp.run(['kubectl', 'get', 'sc', '-o', 'jsonpath={.items[0].metadata.name}'],
                       capture_output=True, timeout=10)
        storage_class = sc_r.stdout.decode().strip() if sc_r.returncode == 0 else 'shared-vast'

        pvc_yaml = tmgr.render_template('prereq/model-cache-pvc.yaml.j2',
            pvc_name=pvc_name, namespace=namespace,
            test_id=f'cli-setup', model_name=model,
            storage_class=storage_class, storage_size=512)

        r = _sp.run(['kubectl', 'apply', '-f', '-'], input=pvc_yaml.encode(),
                    capture_output=True, timeout=30)
        if r.returncode != 0:
            log_fn(f'Failed to create PVC: {r.stderr.decode()}', 'error')
            return False
        log_fn(f'PVC {pvc_name} created', 'success')
    else:
        log_fn(f'PVC {pvc_name} exists', 'success')

    log_fn(f'Downloading model: {model}', 'info')
    tmgr = TemplateManager()
    ts = datetime.now().strftime('%Y%m%d-%H%M%S')
    job_name = f'inftune-model-download-{ts}'

    job_yaml = tmgr.render_template('prereq/model-download-job.yaml.j2',
        job_name=job_name, namespace=namespace,
        test_id=f'cli-download-{ts}', model_name=model,
        pvc_name=pvc_name, hf_token=config.hf_token or os.environ.get('HF_TOKEN', ''))

    r = _sp.run(['kubectl', 'apply', '-f', '-'], input=job_yaml.encode(),
                capture_output=True, timeout=30)
    if r.returncode != 0:
        log_fn(f'Failed to create download job: {r.stderr.decode()}', 'error')
        return False

    log_fn(f'Waiting for model download (this may take several minutes)...', 'info')
    import time as _time
    for i in range(360):  # 30 min max
        r = _sp.run(['kubectl', 'get', 'job', job_name, '-n', namespace,
                     '-o', 'jsonpath={.status.conditions[0].type}'],
                    capture_output=True, timeout=10)
        status = r.stdout.decode().strip() if r.returncode == 0 else ''
        if status == 'Complete':
            log_fn(f'Model download complete', 'success')
            return True
        if status == 'Failed':
            log_fn(f'Model download failed', 'error')
            return False
        if r.returncode != 0 and i > 5:
            pod_r = _sp.run(['kubectl', 'get', 'pods', '-l', f'job-name={job_name}', '-n', namespace,
                            '-o', 'jsonpath={.items[0].status.phase}'],
                           capture_output=True, timeout=10)
            pod_phase = pod_r.stdout.decode().strip() if pod_r.returncode == 0 else ''
            if not pod_phase:
                log_fn(f'Model download complete (Job auto-cleaned)', 'success')
                return True
            if pod_phase == 'Succeeded':
                log_fn(f'Model download complete', 'success')
                return True
            if pod_phase == 'Failed':
                log_fn(f'Model download failed', 'error')
                return False
        if i > 0 and i % 12 == 0:
            log_fn(f'   Still downloading... ({i * 5}s)', 'info')
        _time.sleep(5)

    log_fn('Model download timed out after 30 minutes', 'error')
    return False


def run_optimization(config_params, run_id, db_path, quiet, resume=False,
                     html_report=None, kubeconfig=None):
    """Run the optimization pipeline."""
    from core.recipe_optimizer import RecipeOptimizer, RecipeOptimizerConfig
    from core.database_manager import DatabaseManager
    import json

    def log_fn(message, level='info'):
        if quiet:
            return
        ts = datetime.now().strftime('%H:%M:%S')
        icons = {'info': ' ', 'success': '+', 'warning': '!', 'error': 'X', 'decision': '*'}
        print(f"[{ts}] {icons.get(level, ' ')} {message}")

    try:
        config = RecipeOptimizerConfig(**config_params)
    except Exception as e:
        print(f"Error creating config: {e}")
        return 1

    if not resume:
        if not ensure_model_ready(config, log_fn):
            return 1

    try:
        optimizer = RecipeOptimizer(
            config=config,
            log_callback=log_fn,
            run_id=run_id,
            db_path=db_path,
        )
    except Exception as e:
        print(f"Error creating optimizer: {e}")
        return 1

    try:
        results = optimizer.optimize(resume=resume)

        db = DatabaseManager(db_path=db_path)
        with db.get_connection() as conn:
            conn.execute(
                'UPDATE optimization_runs SET status = ?, completed_at = ?, config_json = ? WHERE id = ?',
                ('completed', datetime.now().isoformat(), json.dumps(config.to_dict()), run_id)
            )

        if not quiet:
            print()
            print(f"{'=' * 70}")
            print(f"  Run #{run_id} completed")
            print(f"  Tests run: {results.get('total_tests_run', 0)}")
            print(f"  Pareto configs: {results.get('pareto_front_count', 0)}")
            if results.get('optimal_decode_tp'):
                print(f"  Optimal decode TP: {results['optimal_decode_tp']}")
            if results.get('optimal_prefill_tp'):
                print(f"  Optimal prefill TP: {results['optimal_prefill_tp']}")
            print(f"{'=' * 70}")
            print(f"  View report: open the web UI and click run #{run_id}")

        if html_report:
            generate_html_report(run_id, db_path, html_report, quiet)

        return 0

    except KeyboardInterrupt:
        print(f"\nInterrupted. Run #{run_id} can be resumed with: inftune run --resume {run_id}")
        db = DatabaseManager(db_path=db_path)
        with db.get_connection() as conn:
            conn.execute("UPDATE optimization_runs SET status = 'stopped' WHERE id = ?", (run_id,))
        return 130

    except Exception as e:
        print(f"\nError: {e}")
        import traceback
        traceback.print_exc()
        db = DatabaseManager(db_path=db_path)
        with db.get_connection() as conn:
            conn.execute(
                "UPDATE optimization_runs SET status = 'failed', completed_at = ? WHERE id = ?",
                (datetime.now().isoformat(), run_id)
            )
        return 1
    finally:
        if kubeconfig and os.path.exists(kubeconfig):
            os.unlink(kubeconfig)


def generate_html_report(run_id, db_path, output_path, quiet):
    """Generate a self-contained HTML report from the DB."""
    import json

    try:
        from core.report_data import ReportDataLoader
        from core.report_analysis import ReportAnalyzer

        analyzer = ReportAnalyzer()
        with ReportDataLoader(db_path) as loader:
            data = analyzer.build_full_report_data(run_id, loader)

        if not data:
            print(f"  No report data found for run #{run_id}")
            return

        app_root = os.environ.get('INFTUNE_PATH', '/mnt/storage/app')
        js_path = os.path.join(app_root, 'web', 'static', 'js', 'report-download.js')

        if not os.path.exists(js_path):
            print(f"  report-download.js not found at {js_path}")
            return

        with open(js_path) as f:
            js_code = f.read()

        import subprocess
        node_script = f"""
const data = {json.dumps(data)};
{js_code}
const html = buildFullReport({run_id}, data, data.charts, data.recommendation || {{}}, data.summary, data.summary.best_configs || {{}}, data.all_results || [], (data.all_results || []).some(r => r.architecture === 'PD'), !!(data.charts.vllm && data.charts.vllm.configs.length));
process.stdout.write(html);
"""
        result = subprocess.run(
            ['node', '-e', node_script],
            capture_output=True, text=True, timeout=30
        )

        if result.returncode != 0:
            html = build_python_html_report(run_id, data)
        else:
            html = result.stdout

        with open(output_path, 'w') as f:
            f.write(html)

        if not quiet:
            print(f"  HTML report saved to: {output_path}")

    except Exception as e:
        print(f"  Failed to generate HTML report: {e}")
        import traceback
        traceback.print_exc()


def build_python_html_report(run_id, data):
    """Fallback: build a minimal HTML report in Python (no Node.js needed)."""
    import json

    charts = data.get('charts', {})
    rec = data.get('recommendation', {})
    summary = data.get('summary', {})
    all_res = data.get('all_results', [])

    html = f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<title>Inftune Studio Report - Run {run_id}</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;width:95%;margin:0 auto;padding:20px;background:#f8fafc;color:#1e293b}}
h1{{color:#1e293b;border-bottom:3px solid #10b981;padding-bottom:10px}}
table{{width:100%;border-collapse:collapse}}
th{{background:#1e293b;color:white;padding:10px;text-align:left}}
td{{padding:8px 10px;border-bottom:1px solid #f1f5f9}}
.stat{{display:inline-block;background:white;padding:20px;border-radius:10px;box-shadow:0 2px 8px rgba(0,0,0,0.08);text-align:center;margin:8px;min-width:180px}}
.stat .val{{font-size:2em;font-weight:800}}
.stat .lbl{{color:#64748b;font-size:0.85em}}
.pareto{{background:#f0fdf4;font-weight:600}}
</style></head><body>
<h1>Inftune Studio Optimization Report &mdash; Run #{run_id}</h1>
<p style="color:#64748b;">Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
"""

    if rec.get('goal_info'):
        html += f"<h2>{rec['goal_info'].get('name', '')}</h2>"
        html += f"<p>{rec['goal_info'].get('description', '')}</p>"
        wl = rec.get('workload', {})
        html += f"<p>Model: <strong>{rec.get('model', '')}</strong> | ISL: {wl.get('isl', '')} | OSL: {wl.get('osl', '')} | Users: {wl.get('users', '')}</p>"

    best = summary.get('best_configs', {})
    html += '<div>'
    html += f'<div class="stat"><div class="val">{summary.get("successful_tests", 0)}</div><div class="lbl">Tests ({summary.get("total_tests", 0)} total)</div></div>'
    if best.get('lowest_latency'):
        html += f'<div class="stat"><div class="val">{best["lowest_latency"]["ttft_p90"]:.1f} ms</div><div class="lbl">Best TTFT P90</div></div>'
    if best.get('highest_throughput'):
        html += f'<div class="stat"><div class="val">{best["highest_throughput"]["throughput_p90"]:.2f} req/s</div><div class="lbl">Best Throughput P90</div></div>'
    html += '</div>'

    recs = rec.get('recommendations', {})
    if recs:
        html += '<h2>Deployment Recommendation</h2>'
        for key, r in recs.items():
            c = r.get('config', {})
            html += f'<div style="background:white;border:2px solid #10b981;border-radius:10px;padding:16px;margin:12px 0;">'
            html += f'<div style="font-weight:800;color:#1e293b;font-size:1.2em;">{r.get("deploy", "")}</div>'
            html += f'<div style="color:#475569;">TTFT P90: <strong>{c.get("ttft_p90", "N/A")} ms</strong> | '
            html += f'Throughput Mean: <strong>{c.get("throughput_mean", "N/A")} req/s</strong> | '
            html += f'Throughput P90: <strong>{c.get("throughput_p90", "N/A")} req/s</strong> | {c.get("gpus", "?")} GPUs</div>'
            html += '</div>'

    if all_res:
        html += '<h2>All Results</h2>'
        html += '<table><tr><th>Config</th><th>Arch</th><th>TTFT P90</th><th>Tput Mean</th><th>Tput P90</th><th>GPUs</th><th>Efficiency</th></tr>'
        pareto_names = set(p.get('config_name') for p in charts.get('pareto', {}).get('pareto_table', []))
        for r in all_res:
            cls = ' class="pareto"' if r.get('config_name') in pareto_names else ''
            html += f'<tr{cls}><td>{r.get("config_name", "")}</td><td>{r.get("architecture", "")}</td>'
            html += f'<td>{r.get("ttft_p90", "N/A")}</td><td>{r.get("throughput_mean", "N/A")}</td>'
            html += f'<td>{r.get("throughput_p90", "N/A")}</td><td>{r.get("gpus", "")}</td>'
            html += f'<td>{r.get("efficiency", "")}</td></tr>'
        html += '</table>'

    html += '<h2>Charts</h2>'
    html += '<div id="scatter" style="height:500px;background:white;border-radius:10px;margin:20px 0;padding:16px;"></div>'
    html += '<div id="efficiency" style="height:500px;background:white;border-radius:10px;margin:20px 0;padding:16px;"></div>'
    html += '<script>var cd=' + json.dumps(charts) + ';'
    html += 'var lo={margin:{t:30,b:80,l:60,r:20},height:480,font:{family:"sans-serif"}};'
    html += 'var co={responsive:true};'
    html += 'if(cd.scatter&&cd.scatter.traces&&cd.scatter.traces.length){'
    html += 'Plotly.newPlot("scatter",cd.scatter.traces.map(function(t){return{x:t.x,y:t.y,text:t.text,name:t.name,mode:"markers",marker:{size:t.sizes,color:t.color,opacity:0.7},hovertemplate:"<b>%{text}</b><extra></extra>"}}),{...lo,xaxis:{title:"TTFT P90 (ms)"},yaxis:{title:"Throughput P90 (req/s)"},showlegend:true},co);}'
    html += 'if(cd.efficiency&&cd.efficiency.configs&&cd.efficiency.configs.length){'
    html += 'Plotly.newPlot("efficiency",[{x:cd.efficiency.configs,y:cd.efficiency.values,type:"bar",marker:{color:cd.efficiency.colors},text:cd.efficiency.values.map(function(v){return v!=null?v.toFixed(3):""}),textposition:"outside"}],{...lo,xaxis:{tickangle:-45},yaxis:{title:"req/s per GPU"}},co);}'
    html += '</script></body></html>'

    return html


# ── Argument parsing ─────────────────────────────────────────────────────────

def build_run_parser(parser):
    """Add all run-related arguments to a parser."""
    parser.add_argument('--resume', type=int, metavar='RUN_ID',
                        help='Resume a previous run by ID instead of starting a new one')
    parser.add_argument('--model', type=str,
                        help='Model name or HuggingFace path (e.g., RedHatAI/gpt-oss-20b)')
    parser.add_argument('--cluster', type=str, metavar='NAME',
                        help='Run against a registered cluster (see: inftune cluster list)')

    wl = parser.add_argument_group('Workload')
    wl.add_argument('--isl', type=int, default=3000, help='Input sequence length (default: 3000)')
    wl.add_argument('--isl-stdev', type=int, default=None, help='ISL standard deviation')
    wl.add_argument('--osl', type=int, default=256, help='Output sequence length (default: 256)')
    wl.add_argument('--osl-stdev', type=int, default=None, help='OSL standard deviation')
    wl.add_argument('--users', type=int, default=100, help='Concurrent users (default: 100)')
    wl.add_argument('--rate-type', choices=['concurrent', 'constant', 'poisson'],
                    default='concurrent', help='Load profile (default: concurrent)')
    wl.add_argument('--turns', type=int, default=1, help='Conversation turns (default: 1)')
    wl.add_argument('--workload-mode', choices=['synthetic', 'dataset'],
                    default='synthetic', help='Workload mode (default: synthetic)')
    wl.add_argument('--dataset', type=str, default=None,
                    help='Dataset path or HuggingFace ID (requires --workload-mode dataset)')
    wl.add_argument('--dataset-column', type=str, default=None,
                    help='Column name in dataset to use as prompts')
    wl.add_argument('--dataset-max-output', type=int, default=256,
                    help='Max output tokens for dataset mode (default: 256)')

    pc = parser.add_argument_group('Prefix Cache Simulation')
    pc.add_argument('--prefix-cache-pct', type=int, default=0,
                    help='Prefix cache hit ratio 0-100%% (0=disabled, default: 0)')
    pc.add_argument('--prefix-cache-mode', choices=['identical', 'shared_prefix', 'multi_group'],
                    default='identical', help='Cache simulation mode (default: identical)')
    pc.add_argument('--prefix-cache-groups', type=int, default=5,
                    help='Number of distinct prompt groups for multi_group mode (default: 5)')

    hw = parser.add_argument_group('Hardware')
    hw.add_argument('--gpus', type=int, default=16, help='Total GPUs to use (default: 16)')
    hw.add_argument('--tp-options', type=str, default='1,2,4,8',
                    help='Comma-separated TP values to explore (default: 1,2,4,8)')
    hw.add_argument('--image', type=str, default='ghcr.io/llm-d/llm-d-cuda:v0.5.1',
                    help='vLLM container image')
    hw.add_argument('--namespace', type=str, default=None,
                    help='Kubernetes namespace (default: from cluster or inftune)')
    hw.add_argument('--pvc', type=str, default='inftune-model-cache',
                    help='PVC name for model cache (default: inftune-model-cache)')
    hw.add_argument('--nccl-ib-hca', type=str, default='mlx',
                    help='NCCL IB HCA device prefix (default: mlx)')
    hw.add_argument('--hf-token', type=str, default=None,
                    help='HuggingFace token for gated models')
    hw.add_argument('--nodes', type=str, default=None,
                    help='Comma-separated node names to pin tests to')

    ss = parser.add_argument_group('Search Strategy')
    ss.add_argument('--objective', choices=['ttft', 'throughput', 'balanced',
                    'aggregated_only', 'pd_only', 'ep_only', 'single_test'],
                    default='ttft', help='Optimization goal (default: ttft)')
    ss.add_argument('--tp-pair-depth', type=int, default=2, choices=[1, 2, 3, 4],
                    help='TP pair breadth: 1=fast, 2=default, 3=deep, 4=full (default: 2)')
    ss.add_argument('--pd-search', choices=['smart', 'exhaustive'],
                    default='smart', help='P/D ratio search mode (default: smart)')
    ss.add_argument('--headroom', type=float, default=1.3,
                    help='Sustainable load headroom multiplier (default: 1.3)')
    ss.add_argument('--use-achievable-qps', action='store_true',
                    help='Auto-scale concurrency to sustainable level')
    ss.add_argument('--duration', type=int, default=300,
                    help='Test duration in seconds (default: 300)')
    ss.add_argument('--stop-mode', choices=['duration', 'max_requests'],
                    default='duration', help='Stop mode (default: duration)')
    ss.add_argument('--max-requests', type=int, default=None,
                    help='Max requests per test (requires --stop-mode max_requests)')

    la = parser.add_argument_group('Latency SLA')
    la.add_argument('--latency-sla', type=int, default=None, metavar='MS',
                    help='Enable latency SLA with target in ms (e.g., 2000)')
    la.add_argument('--latency-percentile', choices=['p50', 'p90', 'p95', 'p99'],
                    default='p99', help='SLA percentile (default: p99)')

    ep = parser.add_argument_group('EPP Configuration')
    ep.add_argument('--epp-preset', choices=['balanced', 'cache_optimized',
                    'queue_balanced', 'latency_aware', 'custom'],
                    default='balanced', help='EPP scoring preset (default: balanced)')
    ep.add_argument('--epp-benchmark', action='store_true',
                    help='Benchmark EPP strategies (Step 9)')
    ep.add_argument('--epp-weights', type=str, default=None, metavar='C:K:Q',
                    help='Custom EPP weights as cache:kv:queue (e.g., 5:1:1). Implies --epp-preset custom')
    ep.add_argument('--epp-max-prefix-blocks', type=int, default=None,
                    help='Override maxPrefixBlocksToMatch (auto-calculated from ISL)')
    ep.add_argument('--epp-lru-capacity', type=int, default=None,
                    help='Override lruCapacityPerServer (auto-calculated from VRAM)')
    ep.add_argument('--epp-non-cached-tokens', type=int, default=None,
                    help='Override nonCachedTokens for PD routing threshold (default: 16)')

    st = parser.add_argument_group('Single Test (requires --objective single_test)')
    st.add_argument('--single-test-arch', choices=['aggregated', 'pd', 'ep'],
                    default=None, help='Architecture for single test')
    st.add_argument('--single-test-tp', type=int, default=None,
                    help='TP size for single test (aggregated/EP)')
    st.add_argument('--single-test-replicas', type=int, default=None,
                    help='Number of pods for single test (aggregated/EP)')
    st.add_argument('--single-test-prefill-tp', type=int, default=None,
                    help='Prefill TP for PD single test')
    st.add_argument('--single-test-decode-tp', type=int, default=None,
                    help='Decode TP for PD single test')
    st.add_argument('--single-test-prefill-pods', type=int, default=None,
                    help='Prefill pod count for PD single test')
    st.add_argument('--single-test-decode-pods', type=int, default=None,
                    help='Decode pod count for PD single test')

    av = parser.add_argument_group('Advanced vLLM Settings')
    av.add_argument('--max-model-len', type=int, default=None,
                    help='Override max_model_len (auto-calculated if omitted)')
    av.add_argument('--gpu-mem-util', type=float, default=None,
                    help='Override gpu_memory_utilization (auto-calculated if omitted)')
    av.add_argument('--block-size', type=int, default=None,
                    help='Override vLLM block size (auto = next-power-of-2 of sqrt(ISL+OSL), min 128 for PD)')
    av.add_argument('--dtype', type=str, default=None,
                    help='Model dtype (e.g., auto, float16, bfloat16)')
    av.add_argument('--kv-cache-dtype', type=str, default=None,
                    help='KV cache dtype (e.g., auto, fp8)')
    av.add_argument('--pipeline-parallel', type=int, default=None,
                    help='Pipeline parallel size (default: 1)')
    av.add_argument('--max-num-seqs', type=int, default=None,
                    help='Override max_num_seqs')
    av.add_argument('--max-num-batched-tokens', type=int, default=None,
                    help='Override max_num_batched_tokens')
    av.add_argument('--tool-call-parser', type=str, default=None,
                    help='Tool call parser (e.g., hermes, mistral)')

    tf = parser.add_argument_group('Toggle Flags (auto by default, use --flag/--no-flag to override)')
    tf.add_argument('--enable-prefix-caching', action='store_true', default=None,
                    help='Enable prefix caching (default: auto/on)')
    tf.add_argument('--no-prefix-caching', action='store_true', default=None,
                    help='Disable prefix caching')
    tf.add_argument('--disable-custom-all-reduce', action='store_true', default=None,
                    help='Disable custom all-reduce (default: auto/off)')
    tf.add_argument('--trust-remote-code', action='store_true', default=None,
                    help='Trust remote code (default: auto/on)')
    tf.add_argument('--no-trust-remote-code', action='store_true', default=None,
                    help='Disable trust remote code')
    tf.add_argument('--disable-log-requests', action='store_true', default=None,
                    help='Disable request logging (default: auto/on)')
    tf.add_argument('--enable-auto-tool-choice', action='store_true', default=None,
                    help='Enable auto tool choice (default: auto/off)')
    tf.add_argument('--vllm-debug-logs', action='store_true', default=None,
                    help='Enable vLLM debug logs (default: off)')
    tf.add_argument('--nccl-debug-logs', action='store_true', default=None,
                    help='Enable NCCL debug logs (default: off)')

    ou = parser.add_argument_group('Output')
    ou.add_argument('--html-report', type=str, default=None, metavar='PATH',
                    help='Generate HTML report to file after completion (e.g., report.html)')
    ou.add_argument('--description', type=str, default=None,
                    help='Run description (stored in DB)')
    ou.add_argument('--db', type=str, default='/mnt/storage/inftune.db',
                    help='Database path (default: /mnt/storage/inftune.db)')
    ou.add_argument('--quiet', action='store_true',
                    help='Suppress progress output')


def main():
    top = argparse.ArgumentParser(
        prog='inftune',
        description='Inftune Studio — LLM inference optimization CLI',
    )
    sub = top.add_subparsers(dest='command')

    # ── cluster ──────────────────────────────────────────────────────────
    cluster_parser = sub.add_parser('cluster', help='Manage registered clusters')
    cluster_sub = cluster_parser.add_subparsers(dest='cluster_action')

    add_p = cluster_sub.add_parser('add', help='Register a cluster')
    add_p.add_argument('--name', required=True, help='Cluster name')
    add_p.add_argument('--kubeconfig', type=str, default=None,
                       help='Path to kubeconfig file (omit for current context)')
    add_p.add_argument('--namespace', type=str, default='inftune',
                       help='Namespace for Inftune resources (default: inftune)')
    add_p.add_argument('--storage-class', type=str, default=None,
                       help='Storage class for PVCs')

    cluster_sub.add_parser('list', help='List registered clusters')

    rm_p = cluster_sub.add_parser('remove', help='Remove a registered cluster')
    rm_p.add_argument('name', help='Cluster name to remove')

    scan_p = cluster_sub.add_parser('scan', help='Scan cluster resources')
    scan_p.add_argument('name', help='Cluster name to scan')
    scan_p.add_argument('--namespace', type=str, default='inftune',
                        help='Namespace (default: inftune)')

    # ── run ──────────────────────────────────────────────────────────────
    run_parser = sub.add_parser('run', help='Run an optimization',
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    build_run_parser(run_parser)

    # ── Parse ────────────────────────────────────────────────────────────
    args = top.parse_args()

    if not args.command:
        top.print_help()
        return 1

    if args.command == 'cluster':
        if not args.cluster_action:
            cluster_parser.print_help()
            return 1
        if args.cluster_action == 'add':
            return cmd_cluster_add(args)
        elif args.cluster_action == 'list':
            return cmd_cluster_list(args)
        elif args.cluster_action == 'remove':
            return cmd_cluster_remove(args)
        elif args.cluster_action == 'scan':
            return cmd_cluster_scan(args)

    elif args.command == 'run':
        return cmd_run(args)

    return 0


if __name__ == '__main__':
    sys.exit(main())
