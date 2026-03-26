#!/usr/bin/env python3.11
"""
Resume the latest optimization run from the database.

Usage:
    python3.11 resume_latest.py [--run-id N] [--hf-token TOKEN]

Runs from /mnt/storage/app/ on the optimizer pod.
"""
import os
import sys
import sqlite3
import signal

sys.path.insert(0, '/mnt/storage/app')

DB_PATH = '/mnt/storage/in-s8.db'


def get_latest_run(run_id=None):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    if run_id:
        row = conn.execute('SELECT * FROM optimization_runs WHERE id = ?', (run_id,)).fetchone()
    else:
        row = conn.execute('SELECT * FROM optimization_runs ORDER BY id DESC LIMIT 1').fetchone()
    conn.close()
    if not row:
        print("No runs found in database")
        sys.exit(1)
    return dict(row)


def mark_failed_as_incomplete(run_id):
    """Delete failed test entries so they get re-run."""
    conn = sqlite3.connect(DB_PATH)
    deleted = conn.execute(
        "DELETE FROM test_configurations WHERE run_id = ? AND status = 'failed'",
        (run_id,)
    ).rowcount
    conn.execute(
        "UPDATE optimization_runs SET status = 'running' WHERE id = ?",
        (run_id,)
    )
    conn.commit()
    conn.close()
    return deleted


def log_callback(message, level='info'):
    print(f"[{level.upper():7s}] {message}")


def main():
    import argparse
    parser = argparse.ArgumentParser(description='Resume latest optimization run')
    parser.add_argument('--run-id', type=int, help='Specific run ID to resume (default: latest)')
    parser.add_argument('--hf-token', help='HuggingFace token (default: from env)')
    parser.add_argument('--latency-ms', type=int, help='Latency constraint in ms (default: from original run or 500)')
    parser.add_argument('--latency-percentile', help='Latency percentile (default: from original run or p99)')
    args = parser.parse_args()

    run = get_latest_run(args.run_id)
    run_id = run['id']

    goal = run.get('goal') or 'ttft'
    isl = run['isl']
    osl = run['osl']
    num_users = run['num_users']
    max_gpus = run.get('max_gpus') or 16
    test_duration = run.get('test_duration') or 300
    isl_stdev = run.get('isl_stdev')
    osl_stdev = run.get('osl_stdev')
    turns = run.get('turns') or 1

    print("=" * 60)
    print(f"Resuming Run #{run_id}: {run['run_name']}")
    print(f"  Model:    {run['model']}")
    print(f"  ISL:      {isl}, OSL: {osl}")
    if isl_stdev or osl_stdev:
        print(f"  StdDev:   ISL={isl_stdev or 'none'}, OSL={osl_stdev or 'none'}")
    if turns > 1:
        print(f"  Turns:    {turns}")
    print(f"  Users:    {num_users}")
    print(f"  Goal:     {goal}")
    print(f"  Max GPUs: {max_gpus}")
    print(f"  Duration: {test_duration}s")
    print(f"  Status:   {run['status']}")
    print("=" * 60)

    # Show completed tests
    conn = sqlite3.connect(DB_PATH)
    tests = conn.execute(
        'SELECT config_name, status FROM test_configurations WHERE run_id = ? ORDER BY id',
        (run_id,)
    ).fetchall()
    conn.close()

    if tests:
        print("\nExisting tests:")
        for name, status in tests:
            icon = "✅" if status == 'completed' else "❌"
            print(f"  {icon} {status:10s} {name}")

    # Delete failed entries so they get retried
    deleted = mark_failed_as_incomplete(run_id)
    if deleted:
        print(f"\n🗑️  Removed {deleted} failed test(s) — will be retried")

    # Build config
    hf_token = args.hf_token or os.environ.get('HF_TOKEN')

    from core.recipe_optimizer import RecipeOptimizer, RecipeOptimizerConfig
    from core.system_scanner import SystemScanner

    namespace = os.environ.get('TARGET_NAMESPACE', 'llm-d')
    scanner = SystemScanner(namespace=namespace)
    cluster_resources = scanner.scan_cluster()
    tp_options = cluster_resources.get_tp_options()
    print(f"\nCluster TP options: {tp_options}")

    # Reconstruct config: prefer config_json (exact original), fall back to columns
    config_json_str = run.get('config_json')
    if config_json_str:
        import json
        saved = json.loads(config_json_str)
        saved['hf_token'] = hf_token
        saved['tp_options'] = tp_options
        if args.latency_ms:
            saved['latency_constraint_enabled'] = True
            saved['latency_constraint_ms'] = args.latency_ms
        if args.latency_percentile:
            saved['latency_constraint_percentile'] = args.latency_percentile
        recipe_config = RecipeOptimizerConfig.from_dict(saved)
        print(f"  ✅ Restored exact config from config_json")
    else:
        from core.test_planner import calculate_engine_memory_config
        max_model_len, gpu_memory_utilization = calculate_engine_memory_config(
            isl=isl, osl=osl, num_users=num_users,
            model_size_b=20.0, dtype='mxfp4',
            gpu_vram_gb=cluster_resources.gpu_memory_per_gpu_mb / 1024 if cluster_resources.gpu_memory_per_gpu_mb else 80.0,
            tensor_parallelism=1
        )
        latency_enabled = bool(run.get('latency_constraint_enabled'))
        latency_ms = args.latency_ms or run.get('latency_constraint_ms') or 500
        latency_pct = args.latency_percentile or run.get('latency_constraint_percentile') or 'p99'
        if args.latency_ms:
            latency_enabled = True

        recipe_config = RecipeOptimizerConfig(
            model_name=run['model'], namespace=namespace,
            isl=isl, osl=osl, isl_stdev=isl_stdev, osl_stdev=osl_stdev, turns=turns,
            qps=float(num_users), total_gpus=max_gpus,
            max_model_len=max_model_len, gpu_memory_utilization=gpu_memory_utilization,
            test_duration=test_duration, max_pd_splits=8,
            image='ghcr.io/llm-d/llm-d-cuda:v0.5.1', pvc_name='in-s8-model-cache',
            nccl_ib_hca='mlx', hf_token=hf_token, tp_options=tp_options,
            objective=goal, use_achievable_qps=bool(run.get('use_achievable_qps', 0)),
            latency_constraint_enabled=latency_enabled,
            latency_constraint_ms=latency_ms, latency_constraint_percentile=latency_pct,
        )
        print(f"  ⚠️  No config_json found, reconstructed from DB columns")

    if recipe_config.latency_constraint_enabled:
        print(f"  Latency SLA: {recipe_config.latency_constraint_percentile} ≤ {recipe_config.latency_constraint_ms}ms")

    stopped = False

    def handle_signal(signum, frame):
        nonlocal stopped
        stopped = True
        print("\n🛑 Stopping after current test completes...")

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    optimizer = RecipeOptimizer(
        config=recipe_config,
        log_callback=log_callback,
        run_id=run_id,
        db_path=DB_PATH,
        stop_check=lambda: stopped,
    )

    print("\n🚀 Starting optimization...\n")

    try:
        results = optimizer.optimize(resume=True)
        print("\n" + "=" * 60)
        print("🎯 OPTIMIZATION COMPLETE")
        print(f"  Total tests: {results['total_tests_run']}")
        print(f"  Decode TP:   {results['optimal_decode_tp']}")
        print(f"  Prefill TP:  {results['optimal_prefill_tp']}")
        print("=" * 60)
    except Exception as e:
        print(f"\n❌ Optimization failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    main()
