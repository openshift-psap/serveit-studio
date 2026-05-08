# Inftune Studio Core Modules

Core optimization engine for finding optimal LLM inference configurations.

## Module Overview

| Module | Description |
|---|---|
| `recipe_optimizer.py` | Recipe-based optimization engine (Steps 1-10), Smart PD Search, block size auto-tuning |
| `optimization_strategies.py` | Goal-specific strategies: TTFT, Throughput, Balanced, Aggregated-only, PD-only, EP-only |
| `test_orchestrator.py` | Deploy → benchmark (guidellm) → collect metrics → cleanup pipeline |
| `deployment_manager.py` | K8s deployment lifecycle (LWS creation, sequential PD deploy, readiness checks) |
| `prereq_manager.py` | Prerequisite infrastructure (RBAC, gateway, EPP, RDMA discovery, model download) |
| `system_scanner.py` | Cluster resource discovery (GPUs, RDMA NICs, CPU, memory, storage classes, cloud provider) |
| `config_generator.py` | Test configuration dataclass (`TestConfig`) with all vLLM and deployment parameters |
| `template_manager.py` | Jinja2 template rendering for K8s manifests |
| `metrics_collector.py` | Prometheus/Thanos metrics collection during benchmarks |
| `metrics_analyzer.py` | Per-pod GPU utilization, memory, throughput analysis from Prometheus data |
| `database_manager.py` | SQLite persistence for optimization runs, test results, Optuna trials |
| `resource_calculator.py` | CPU/memory/GPU sizing math (per-node resource allocation) |
| `report_analysis.py` | Report data builder (Pareto front, architecture comparison, charts) |
| `report_data.py` | Data models (`TestResult`, `ParetoPoint`) and DB loader |
| `cloud_constraints.py` | Cloud provider detection (IBM, CoreWeave, AWS, Azure, GCP, bare metal) |
| `k8s_utils.py` | Shared kubectl/oc detection (cached) and command runner |
| `cleanup_manager.py` | Test deployment cleanup (LWS, services, pods) |
| `test_planner.py` | Memory calculation, engine config estimation |

## Key Dataclasses

### RecipeOptimizerConfig
Full optimization run configuration. Key fields:

```python
model_name, namespace, isl, osl, qps, rate_type,
total_gpus, max_model_len, gpu_memory_utilization,
test_duration, stop_mode, max_requests,
isl_stdev, osl_stdev, turns,
tp_pair_top_n,           # GPU Split Combinations (1-4)
pd_search_mode,          # 'smart' or 'exhaustive'
objective,               # 'ttft', 'throughput', 'balanced', etc.
use_achievable_qps,      # Auto-scale load
latency_constraint_enabled, latency_constraint_ms, latency_constraint_percentile,
workload_mode,           # 'synthetic' or 'dataset'
dataset_source, dataset_column, dataset_max_output,
prefix_cache_hit_pct,    # 0-100
advanced_vllm,           # Dict of user overrides
```

### TestConfig
Per-test deployment configuration. Includes all vLLM flags, resource limits, networking, and workload parameters. Serialized to `test_config_json` in the DB for the report detail view.

### FeasibleSplit
A valid P/D GPU allocation: `prefill_pods`, `decode_pods`, `prefill_tp`, `decode_tp`, `prefill_pct`.

## Smart PD Search Algorithm

```python
# For each (prefill_tp, decode_tp) pair:
prefill_thr = calibration_step3[prefill_tp].throughput_p90  # req/s per pod
decode_thr  = calibration_step2[decode_tp].throughput_p90   # req/s per pod
r = decode_thr / prefill_thr                                # balanced ratio
D_ideal = total_gpus / (r * prefill_tp + decode_tp)         # ideal decode pods

# Test floor(D_ideal), ceil(D_ideal), ±1 → ~3 valid configs per pair
```

## Block Size Auto-Tuning

```python
block_size = next_power_of_2(sqrt(ISL + OSL))  # clamped to [8, 512]
# For PD goals: minimum 128 (NIXL transfers KV cache in blocks)
```

## Templates

```
templates/
├── aggregated/lws.yaml.j2        # Aggregated vLLM deployment
├── aggregated/service.yaml.j2
├── pd/prefill-lws.yaml.j2        # PD prefill pods (with NIXL KV transfer)
├── pd/decode-lws.yaml.j2         # PD decode pods
├── pd/prefill-service.yaml.j2
├── pd/decode-service.yaml.j2
├── ep/lws.yaml.j2                # Expert Parallelism deployment
├── ep/service.yaml.j2
└── prereq/                       # Gateway, EPP, RBAC, model download, PVC
```

All templates support configurable:
- `block_size`, `gpu_memory_utilization`, `max_model_len`, `max_num_seqs`
- `enable_prefix_caching`, `disable_custom_all_reduce`, `dtype`, `kv_cache_dtype`
- `vllm_debug_logs` (VLLM_LOGGING_LEVEL), `nccl_debug_logs` (NCCL_DEBUG)
- Node pinning via `selected_nodes`
- RDMA device resources (auto-detected per cloud provider)
