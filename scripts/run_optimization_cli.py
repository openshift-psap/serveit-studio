#!/usr/bin/env python3
"""
CLI Optimization Runner
Simulates user selections from web console and runs optimization without the UI.
Useful for debugging and headless runs.

Usage:
    python3 scripts/run_optimization_cli.py

Edit the config_params dict below to match your desired workload parameters.
"""

import sys
import os
from datetime import datetime

# Add app root to path
app_root = '/mnt/storage/app'
sys.path.insert(0, app_root)

print("=" * 80)
print("🧪 RECIPE-BASED OPTIMIZATION TEST")
print("=" * 80)
print(f"Started at: {datetime.now().isoformat()}")
print()

# Import required modules
try:
    from core.recipe_optimizer import RecipeOptimizer, RecipeOptimizerConfig
    print("✅ Imported RecipeOptimizer")
except Exception as e:
    print(f"❌ Failed to import RecipeOptimizer: {e}")
    sys.exit(1)

try:
    from core.system_scanner import SystemScanner
    print("✅ Imported SystemScanner")
except Exception as e:
    print(f"❌ Failed to import SystemScanner: {e}")
    sys.exit(1)

try:
    from core.database_manager import DatabaseManager
    print("✅ Imported DatabaseManager")
except Exception as e:
    print(f"❌ Failed to import DatabaseManager: {e}")
    sys.exit(1)

print()
print("=" * 80)
print("📋 CONFIGURATION (from user selections)")
print("=" * 80)

# Configuration matching user selections
# Workload parameters
isl = 3000
osl = 256
num_users = 400  # Concurrent users

# Calculate max_model_len and gpu_memory_utilization together
# This ensures they're coordinated to avoid OOM errors
from core.test_planner import calculate_engine_memory_config

max_model_len, gpu_memory_utilization = calculate_engine_memory_config(
    isl=isl,
    osl=osl,
    num_users=num_users,
    model_size_b=20.0,  # gpt-oss-20b
    dtype='mxfp4',  # MXFP4 quantized model
    gpu_vram_gb=141.0,  # H200 141GB
    tensor_parallelism=1  # Will be optimized by Recipe, start with 1 for calc
)

config_params = {
    'model_name': 'RedHatAI/gpt-oss-20b',
    'namespace': 'inferecipe',
    'isl': isl,
    'osl': osl,
    'qps': 400.0,  # 400 users = 400 queries per second target
    'total_gpus': 16,
    'max_model_len': max_model_len,
    'gpu_memory_utilization': gpu_memory_utilization,
    'test_duration': 120,  # 120 seconds per test (must be > warmup of 60s)

    # Steps 2 & 3 exhaustively test ALL valid TP values
    # Step 7 exhaustively tests P/D splits near the ideal ratio
    'max_pd_splits': 8,

    # Infrastructure
    'thanos_url': None,
    'image': 'ghcr.io/llm-d/llm-d-cuda:v0.5.1',
    'pvc_name': 'inferecipe-model-cache',
    'nccl_ib_hca': 'mlx',
    'hf_token': None,

    # TP options to explore
    'tp_options': [1, 2, 4, 8],

    # Resource headroom (30% buffer)
    'headroom': 1.3,

    # Optimization objective
    'objective': 'ttft',

    # Latency SLA
    'latency_constraint_enabled': True,
    'latency_constraint_ms': 500,
    'latency_constraint_percentile': 'p99',

    # Dataset workload (openai/gsm8k instead of synthetic)
    'workload_mode': 'dataset',
    'dataset_source': 'openai/gsm8k',
    'dataset_column': 'question',
    'dataset_max_output': 256,
}

# Print configuration
for key, value in config_params.items():
    print(f"  {key}: {value}")

print()

# Scan cluster to get actual GPU count and valid TP options
print("=" * 80)
print("🔍 SCANNING CLUSTER")
print("=" * 80)

try:
    scanner = SystemScanner(namespace='inferecipe')
    cluster_resources = scanner.scan_cluster()

    total_gpus = cluster_resources.total_gpus
    gpu_nodes = cluster_resources.gpu_node_count
    max_gpus_per_node = cluster_resources.max_gpus_per_node
    tp_options = cluster_resources.get_tp_options()

    print(f"✅ Cluster scanned successfully")
    print(f"  Total GPUs: {total_gpus}")
    print(f"  GPU Nodes: {gpu_nodes}")
    print(f"  Max GPUs per Node: {max_gpus_per_node}")
    print(f"  Valid TP Options: {tp_options}")
    print()

    # Update config with actual cluster values
    config_params['total_gpus'] = total_gpus
    config_params['tp_options'] = tp_options

except Exception as e:
    print(f"❌ Failed to scan cluster: {e}")
    print(f"  Using defaults: {config_params['total_gpus']} GPUs, TP options {config_params['tp_options']}")
    print()

# Create database run entry
print("=" * 80)
print("💾 CREATING DATABASE RUN ENTRY")
print("=" * 80)

try:
    db_manager = DatabaseManager(db_path='/mnt/storage/inferecipe.db')
    run_id = db_manager.create_optimization_run(
        run_name=f"recipe-test-{datetime.now().strftime('%Y%m%d-%H%M%S')}",
        model=config_params['model_name'],
        isl=config_params['isl'],
        osl=config_params['osl'],
        num_users=int(config_params['qps']),
        notes="CLI optimization run",
        config_dict=config_params,
    )
    print(f"✅ Created database run entry (run_id={run_id})")
    print()
except Exception as e:
    print(f"❌ Failed to create database entry: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

# Create RecipeOptimizerConfig
print("=" * 80)
print("⚙️  CREATING OPTIMIZER CONFIG")
print("=" * 80)

try:
    recipe_config = RecipeOptimizerConfig(**config_params)
    print("✅ RecipeOptimizerConfig created successfully")
    print()
except Exception as e:
    print(f"❌ Failed to create config: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

# Create optimizer with logging callback
print("=" * 80)
print("🚀 CREATING OPTIMIZER")
print("=" * 80)

def log_callback(message: str, level: str = 'info'):
    """Log callback for optimizer."""
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    level_emoji = {
        'info': 'ℹ️',
        'success': '✅',
        'warning': '⚠️',
        'error': '❌',
        'decision': '📊'
    }.get(level, 'ℹ️')
    print(f"[{timestamp}] {level_emoji}  {message}")

try:
    optimizer = RecipeOptimizer(
        config=recipe_config,
        log_callback=log_callback,
        run_id=run_id,
        db_path='/mnt/storage/inferecipe.db'
    )
    print("✅ RecipeOptimizer created successfully")
    print(f"   Database persistence enabled (run_id={run_id})")
    print()
except Exception as e:
    print(f"❌ Failed to create optimizer: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

# Run optimization
print()
print("=" * 80)
print("🎯 STARTING OPTIMIZATION")
print("=" * 80)
print()
print("⏰ Expected duration: ~60-70 minutes")
print("   - Step 2: Decode TP (all valid TPs, ~20-25 min)")
print("   - Step 3: Prefill TP (all valid TPs, ~20-25 min)")
print("   - Step 7: P/D Splits (4 tests, ~20-25 min)")
print("   ⚡ Quick validation mode: 60s tests instead of 300s")
print()
print("💡 Press Ctrl+C to interrupt")
print()

try:
    start_time = datetime.now()

    # Run optimization
    results = optimizer.optimize()

    end_time = datetime.now()
    duration = end_time - start_time

    print()
    print("=" * 80)
    print("🎉 OPTIMIZATION COMPLETE")
    print("=" * 80)
    print(f"Duration: {duration}")
    print()

    # Print summary
    print("📊 RESULTS SUMMARY")
    print("-" * 80)
    print(f"Total tests run: {results['total_tests_run']}")
    print(f"Optimal decode TP: {results['optimal_decode_tp']}")
    print(f"Optimal prefill TP: {results['optimal_prefill_tp']}")
    print(f"Decode TPSG: {results['decode_tpsg']:.0f} tokens/s/GPU")
    print(f"Prefill TPSG: {results['prefill_tpsg']:.0f} tokens/s/GPU")
    print(f"Feasible splits: {results['feasible_splits_count']}")
    print(f"Pareto front: {results['pareto_front_count']} configurations")
    print()

    # Print Pareto configurations
    if results['pareto_configurations']:
        print("🏆 PARETO OPTIMAL CONFIGURATIONS")
        print("-" * 80)
        for i, config in enumerate(results['pareto_configurations'], 1):
            print(f"{i}. {config['prefill_pods']}P×TP{config['prefill_tp']} + "
                  f"{config['decode_pods']}D×TP{config['decode_tp']}")
            print(f"   TTFT p90: {config['ttft_p90']:.1f}ms")
            print(f"   Throughput p90: {config['throughput_p90']:.2f} req/s")
            print()

    # Print all test results summary
    print("📝 ALL TEST RESULTS")
    print("-" * 80)
    print(f"Total test results: {len(results['all_test_results'])}")
    print()

    for i, (test_config, test_result) in enumerate(results['all_test_results'], 1):
        status = "✅" if test_result.guidellm_success else "❌"
        print(f"{i}. {status} {test_config.test_id}")
        print(f"   Architecture: {test_config.architecture}")
        print(f"   TP: {test_config.tensor_parallelism}")
        if test_config.architecture == 'pd':
            print(f"   P/D: {test_config.prefill_replicas}:{test_config.decode_replicas}")
        if test_result.guidellm_success:
            if test_result.ttft_p90:
                print(f"   TTFT p90: {test_result.ttft_p90:.1f}ms")
            if test_result.throughput_p90:
                print(f"   Throughput p90: {test_result.throughput_p90:.2f} req/s")
        print()

    print("=" * 80)
    print("✅ TEST COMPLETED SUCCESSFULLY")
    print("=" * 80)

    # Update database run status
    try:
        db_manager.update_run_status(
            run_id=run_id,
            status='completed',
            notes=f"Completed {len(results['all_test_results'])} tests successfully"
        )
        print(f"✅ Updated database run status to 'completed'")
    except Exception as e:
        print(f"⚠️  Failed to update run status: {e}")

    # Exit with success
    sys.exit(0)

except KeyboardInterrupt:
    print()
    print()
    print("=" * 80)
    print("🛑 OPTIMIZATION INTERRUPTED BY USER")
    print("=" * 80)

    # Update database run status
    try:
        db_manager.update_run_status(
            run_id=run_id,
            status='failed',
            notes="Interrupted by user"
        )
    except Exception:
        pass

    sys.exit(130)

except Exception as e:
    print()
    print()
    print("=" * 80)
    print("❌ OPTIMIZATION FAILED")
    print("=" * 80)
    print(f"Error: {e}")
    print()
    print("Full traceback:")
    import traceback
    traceback.print_exc()
    print()
    print("=" * 80)

    # Update database run status
    try:
        db_manager.update_run_status(
            run_id=run_id,
            status='failed',
            notes=f"Error: {str(e)}"
        )
    except Exception:
        pass

    sys.exit(1)
