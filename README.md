# Inftune Studio

Automated benchmarking tool that finds the optimal vLLM inference configuration for your hardware, model, and workload.

Given a model, a target QPS, and an optimization goal (response time or throughput), Inftune Studio deploys real vLLM instances on your cluster, runs benchmarks, and returns a Pareto-optimal set of configurations — ranked by TTFT, throughput, or both.

## How It Works

Inftune Studio supports three inference architectures:

| Architecture | Description | Optimizes For |
|---|---|---|
| **Aggregated** | Single vLLM deployment (baseline) | Simplicity |
| **Prefill/Decode (PD)** | Separate prefill and decode pods with KV cache transfer via NIXL/RDMA | Response time (TTFT) |
| **Expert Parallelism (EP)** | Distributed MoE inference with expert-parallel routing | Throughput |

The optimizer selects which architectures to test based on the goal:

- **Response Time** — Aggregated vs PD
- **Throughput** — Aggregated vs EP
- **Balanced** — All three
- **Aggregated Only / PD Only / EP Only** — Test a single architecture

### Optimization Steps

The recipe-based optimizer runs up to 10 steps:

1. **Prerequisite Infrastructure** — Deploy gateway, EPP, RBAC, RDMA discovery
2. **Decode TP Sweep** — Deploy aggregated vLLM at each TP (1, 2, 4, 8), measure decode TPSG
3. **Prefill TP Sweep** — Same for prefill workloads (often different optimal TP)
4. **Cluster Capacity Analysis** — Calculate GPU cost per request, sustainable throughput
5. **GPU Sizing & Feasible Splits** — Enumerate valid P/D GPU divisions respecting TP constraints
6. **Aggregated Configuration Search** — Test all aggregated configs (TP1×16R, TP2×8R, etc.)
7. **P/D Split Optimization** — Test selected P/D splits, find Pareto front
8. **Architecture Comparison** — Compare best PD vs best Aggregated (no new tests)
9. **Latency-Bounded Throughput** — Binary search for max throughput under latency SLA (optional)
10. **Calibrated Load Validation** — Re-test at sustainable QPS if overloaded (optional)

Steps 2-3 and 6-10 deploy real workloads. Steps 4-5 are pure math.

## Configuration Options

### Workload Settings

| Setting | Description | Default |
|---|---|---|
| **Model** | HuggingFace model name | Required |
| **ISL** | Input Sequence Length (prompt tokens) | Required |
| **ISL Stdev** | Standard deviation for ISL distribution | None (fixed) |
| **OSL** | Output Sequence Length (generated tokens) | Required |
| **OSL Stdev** | Standard deviation for OSL distribution | None (fixed) |
| **Concurrent Users** | Number of simultaneous requests | 100 |
| **Rate Type** | Load profile: Concurrent, Constant RPS, or Poisson RPS | Concurrent |
| **Test Duration** | How long each benchmark runs (seconds) | 300 |
| **Stop Mode** | Stop by duration or max requests | Duration |
| **Conversation Turns** | Multi-turn conversation support (1 = single-turn) | 1 |
| **Workload Mode** | Synthetic (generated prompts) or Dataset (HuggingFace/file) | Synthetic |
| **Dataset Source** | HuggingFace dataset ID or local file path | None |
| **Prefix Cache Hit %** | Simulate prefix cache hits (0-100%). Generates a dataset where N% of prompts share an identical prefix | 0 |
| **Run Description** | Free-text note to identify the run later | None |

### Search Strategy

| Setting | Description | Default |
|---|---|---|
| **Optimization Goal** | TTFT, Throughput, Balanced, Aggregated Only, PD Only, EP Only | TTFT |
| **GPU Split Combinations** | How many Prefill TP × Decode TP combinations to explore (1-4) | 2 |
| **Prefill/Decode Pod Balance** | Smart (calculated ~3 splits/pair) or Exhaustive (all valid splits) | Smart |
| **Auto-Scale Load** | Scale down to sustainable concurrency if cluster is overloaded | Off |
| **Headroom** | Safety margin for sustainable throughput calculation | 1.3× |

### Response Time Guarantee (Step 9)

| Setting | Description | Default |
|---|---|---|
| **Latency SLA** | Enable latency-bounded throughput search | Off |
| **Target Latency** | Maximum acceptable TTFT (ms) | 500 |
| **Target Percentile** | Which percentile must meet the target (p50, p90, p95, p99) | p90 |

### Advanced vLLM Settings

All settings default to "Auto" — Inftune Studio calculates optimal values. Override manually if needed.

#### Value Settings

| Setting | Description | Auto Behavior |
|---|---|---|
| **max-model-len** | Max total tokens (input + output) per request | Calculated from ISL + OSL |
| **gpu-memory-utilization** | Fraction of GPU memory to use (0.0-0.99) | Calculated from model size + GPU VRAM |
| **max-num-seqs** | Max concurrent requests per pod | Calculated from users + GPU count |
| **max-num-batched-tokens** | Max tokens per forward pass | Calculated from workload |
| **dtype** | Model weight precision (bfloat16, float16, float32) | vLLM auto-detects |
| **kv-cache-dtype** | KV cache precision (auto, fp8, fp8_e5m2, fp8_e4m3) | Same as model dtype |
| **pipeline-parallel-size** | Split model across GPU groups in sequence | 1 |
| **block-size** | KV cache block size (8-512) | `next_power_of_2(sqrt(ISL+OSL))`, min 128 for PD (NIXL) |
| **tool-call-parser** | Function/tool call parser (openai, hermes, mistral, llama3_json) | Disabled |

#### Toggle Flags

| Flag | Description | Auto Default |
|---|---|---|
| **enable-prefix-caching** | Reuse computation for shared prompt prefixes | On |
| **disable-custom-all-reduce** | Turn off optimized GPU-to-GPU communication | Off |
| **enable-auto-tool-choice** | Auto-detect when to invoke tools | Off |
| **trust-remote-code** | Allow custom model code from HuggingFace | On |
| **disable-log-requests** | Suppress per-request logging during benchmarks | On |
| **vllm-debug-logs** | Enable verbose vLLM engine logs (DEBUG level) | Off |
| **nccl-debug-logs** | Enable verbose NCCL communication logs (INFO level) | Off |

## Prerequisites

- Kubernetes or OpenShift cluster with NVIDIA GPUs
- [LeaderWorkerSet](https://github.com/kubernetes-sigs/lws) CRD installed
- `kubectl` (or `oc`) CLI configured
- A HuggingFace token stored as Secret `llm-d-hf-token` in the target namespace

## Deployment

```bash
# Deploy with defaults (auto-detects DRA vs NAD networking)
./deployment/deploy.sh --storage-class <your-class>

# Deploy with an existing PVC
./deployment/deploy.sh --pvc-name my-existing-pvc

# Force NAD (Multus) mode instead of DRA
./deployment/deploy.sh --force-nad
```

### Dev Mode

```bash
# Deploy in dev mode (syncs local code to pod, auto-restarts on crash)
./deployment/deploy.sh --dev --pvc-name my-pvc

# Re-sync code to a running dev pod
./deployment/deploy.sh --sync

# Port-forward the UI to localhost:8080
./deployment/deploy.sh --port-forward

# Restart the server process in the pod
./deployment/deploy.sh --restart-server
```

### Access the UI

```bash
# Kubernetes
./deployment/deploy.sh --port-forward
# Opens http://localhost:8080

# OpenShift (auto-creates Route)
oc get route inftune-optimizer-ui -n llm-d
```

## Project Structure

```
core/
├── recipe_optimizer.py        # Recipe-based optimization engine (steps 1-10)
├── optimization_strategies.py # TTFT, Throughput, Balanced, PD-only, EP-only strategies
├── test_orchestrator.py       # Deploy → benchmark → collect → cleanup pipeline
├── deployment_manager.py      # Kubernetes deployment lifecycle (LWS, sequential PD deploy)
├── prereq_manager.py          # Prerequisite infrastructure (RBAC, gateway, EPP, RDMA)
├── system_scanner.py          # Cluster resource discovery (GPUs, RDMA, nodes, storage)
├── config_generator.py        # Test configuration generation (TestConfig dataclass)
├── template_manager.py        # Jinja2 template rendering for K8s manifests
├── metrics_collector.py       # Prometheus/Thanos metrics collection
├── metrics_analyzer.py        # Per-pod GPU/memory/throughput analysis
├── database_manager.py        # SQLite persistence for runs and results
├── resource_calculator.py     # CPU/memory/GPU sizing math
├── report_analysis.py         # Report data builder (charts, Pareto front, recommendations)
├── report_data.py             # Data models and DB loader for reports
├── cloud_constraints.py       # Cloud provider detection and constraints
├── networking/                # Network plugins (DRA, NAD, shared device)
├── providers/                 # Cloud provider adapters (AWS, Azure, GCP, IBM, CoreWeave, bare metal)
└── templates/                 # Jinja2 K8s manifest templates
    ├── aggregated/            #   Aggregated LWS + Service
    ├── pd/                    #   Prefill/Decode LWS + Services
    ├── ep/                    #   Expert Parallelism LWS + Service
    └── prereq/                #   RBAC, gateway, GAIE, RDMA ConfigMap, model download

web/
├── server.py                  # Flask + SocketIO + gevent web server
├── static/
│   ├── css/style.css          # Red Hat branded UI styles
│   ├── js/app.js              # Single-page app logic (wizard, charts, reports)
│   └── img/logo.png           # Inftune Studio logo
└── templates/                 # Jinja2 HTML templates (wizard steps, overlays)

deployment/
└── deploy.sh                  # Deployment script (YAML gen, deploy, dev mode, port-forward)

scripts/
├── backfill_test_config.py    # Backfill test_config_json for existing runs
├── resume_latest.py           # CLI resume for the latest stopped run
└── run_optimization_cli.py    # CLI-based optimization runner
```

## Multi-Cloud Support

Inftune Studio auto-detects the cloud provider and configures networking accordingly:

| Provider | GPU Resource | Networking | RDMA |
|---|---|---|---|
| IBM Cloud | DRA (`dra.llm-d.io/gpu-nic-pair`) | DRANet | InfiniBand via DRA |
| CoreWeave | `nvidia.com/gpu` + `rdma/ib` | Shared device plugin | InfiniBand via device plugin |
| Bare Metal | `nvidia.com/gpu` | NAD (Multus) | InfiniBand via Multus |
| AWS / Azure / GCP | `nvidia.com/gpu` | Standard | Provider-specific |

## Smart PD Search

Instead of exhaustively testing every valid P/D split (50+ tests), Smart mode calculates the mathematically optimal split from calibration data:

```
D_ideal = total_gpus / (r × prefill_tp + decode_tp)
where r = decode_throughput / prefill_throughput
```

Tests ~3 configurations around the calculated optimum per TP pair, reducing Step 7 from O(all_valid_splits) to O(3 × num_tp_pairs).

## Block Size Auto-Tuning

KV cache block size is auto-tuned from the workload:

```
block_size = next_power_of_2(sqrt(ISL + OSL))
```

For PD goals (TTFT, Balanced, PD Only), minimum is 128 because NIXL transfers KV cache in blocks — larger blocks reduce transfer count and network overhead.

## Metrics

**Client-side** (via [guidellm](https://github.com/vllm-project/guidellm)):
- TTFT (p50, p90, p95, p99)
- Inter-token latency (ITL)
- Throughput (requests/sec)
- TPOT (time per output token)
- E2E request latency
- Request counts (total, successful, incomplete, errored)

**Server-side** (via Prometheus/Thanos):
- GPU utilization and memory per pod
- vLLM queue depth, batch size, KV cache usage
- InfiniBand RDMA throughput

Results are stored in SQLite at `/mnt/storage/inftune.db`.

## Optimization Report

The report UI includes these tabs:

| Tab | Content |
|---|---|
| **Recommendation** | Deployment recommendation cards, percentile breakdown |
| **TP Calibration** | Steps 2-3 Pareto chart (TTFT vs GPU count) |
| **Configurations** | Scatter plots, efficiency bars, Pareto table, all results |
| **Comparison** | PD vs Aggregated and EP vs Aggregated head-to-head |
| **Latency Search** | Step 9 binary search trials (if latency SLA enabled) |
| **Calibrated Load** | Step 10 re-test at sustainable QPS |
| **vLLM Metrics** | Prometheus metrics tables |
| **Estimator** | Scale results to different workloads without re-testing |
| **Test Settings** | Full run configuration (workload, strategy, infrastructure, vLLM settings) |

## Troubleshooting

```bash
# Check pod status
kubectl get pods -n llm-d -l app=inftune-optimizer

# View server logs
kubectl exec -n llm-d <pod> -- cat /tmp/server.log

# Check active test deployments
kubectl get leaderworkerset -n llm-d

# Check GPU usage across all namespaces
kubectl get pods --all-namespaces -o json | python3 -c "
import sys, json
for pod in json.load(sys.stdin)['items']:
    if pod['status'].get('phase') not in ('Running','Pending'): continue
    for c in pod['spec'].get('containers',[]):
        gpus = c.get('resources',{}).get('requests',{}).get('nvidia.com/gpu')
        if gpus and int(gpus) > 0:
            print(f\"{pod['metadata']['namespace']}/{pod['metadata']['name']}: {gpus} GPU(s)\")
"

# Clean up stuck test pods
kubectl delete lws -n llm-d -l component=inftune-test
```

## License

[Apache 2.0](LICENSE)
