# InfeRecipe

Automated benchmarking tool that finds the optimal vLLM inference configuration for your hardware, model, and workload.

Given a model, a target QPS, and an optimization goal (response time or throughput), InfeRecipe deploys real vLLM instances on your cluster, runs benchmarks, and returns a Pareto-optimal set of configurations — ranked by TTFT, throughput, or both.

## How It Works

InfeRecipe supports three inference architectures:

| Architecture | Description | Optimizes For |
|---|---|---|
| **Aggregated** | Single vLLM deployment (baseline) | Simplicity |
| **Prefill/Decode (PD)** | Separate prefill and decode pods with KV cache transfer via RDMA | Response time (TTFT) |
| **Expert Parallelism (EP)** | Distributed MoE inference with expert-parallel routing | Throughput |

The optimizer selects which architectures to test based on the goal:

- **Response Time** — Aggregated vs PD
- **Throughput** — Aggregated vs EP
- **Balanced** — All three

### Optimization Steps

The recipe-based optimizer runs up to 9 steps:

1. **Baseline Setup** — Scan cluster resources (GPUs, nodes, RDMA NICs), determine valid TP values
2. **Decode TP Sweep** — Deploy aggregated vLLM at each TP, measure decode TPSG (tokens/sec/GPU)
3. **Prefill TP Sweep** — Same for prefill workloads (compute-bound, often different optimal TP)
4. **Workload Calculation** — `prefill_workload = QPS × ISL`, `decode_workload = QPS × OSL`
5. **GPU Sizing** — Required GPUs from TPSG with 1.3× headroom, scale QPS if GPUs exceed capacity
6. **Feasible Splits** — Enumerate all valid prefill/decode GPU divisions respecting TP constraints
7. **PD Split Testing** — Deploy and benchmark selected splits as full PD configurations
8. **Validation** — Run final aggregated baseline, extract Pareto front of non-dominated configs
9. **Calibrated QPS** — Re-test top configurations at production QPS

Steps 2–3 and 7–9 deploy real workloads. Steps 4–6 are pure math.

## Prerequisites

- Kubernetes or OpenShift cluster with NVIDIA GPUs
- [LeaderWorkerSet](https://github.com/kubernetes-sigs/lws) CRD installed
- `kubectl` (or `oc`) CLI configured
- A HuggingFace token stored as Secret `llm-d-hf-token` in the target namespace

## Deployment

```bash
# Deploy with defaults (auto-detects DRA vs NAD networking)
./deployment/deploy.sh

# Deploy with a specific storage class
./deployment/deploy.sh --storage-class nfs-csi --storage-size 200Gi

# Deploy with an existing PVC
./deployment/deploy.sh --pvc-name my-existing-pvc

# Force NAD (Multus) mode instead of DRA
./deployment/deploy.sh --force-nad
```

### Dev Mode

```bash
# Deploy in dev mode (syncs local code to pod, restarts server on changes)
./deployment/deploy.sh --dev

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
kubectl port-forward svc/inferecipe-benchmark-svc 5000:5000 -n llm-d

# OpenShift
oc get route inferecipe-benchmark-ui -n llm-d
```

## Project Structure

```
core/
├── recipe_optimizer.py        # Recipe-based optimization engine (steps 1-9)
├── optimization_strategies.py # TTFT and throughput strategy implementations
├── test_orchestrator.py       # Deploy → benchmark → collect → cleanup pipeline
├── deployment_manager.py      # Kubernetes deployment lifecycle
├── prereq_manager.py          # Prerequisite infrastructure (RBAC, gateway, RDMA)
├── system_scanner.py          # Cluster resource discovery (GPUs, RDMA, nodes)
├── config_generator.py        # Test configuration generation
├── template_manager.py        # Jinja2 template rendering for K8s manifests
├── metrics_collector.py       # Prometheus/Thanos metrics collection
├── database_manager.py        # SQLite persistence for runs and results
├── resource_calculator.py     # CPU/memory/GPU sizing math
├── networking/                # Network plugins (DRA, NAD, shared device)
├── providers/                 # Cloud provider adapters (AWS, Azure, GCP, IBM, CoreWeave, bare metal)
└── templates/                 # Jinja2 K8s manifest templates
    ├── aggregated/            #   Aggregated LWS + Service
    ├── pd/                    #   Prefill/Decode LWS + Services
    ├── ep/                    #   Expert Parallelism LWS + Service
    └── prereq/                #   RBAC, gateway, GAIE, RDMA ConfigMap

web/
├── server.py                  # Flask + SocketIO web server
└── templates/index.html       # Single-page UI

deployment/
└── deploy.sh                  # Deployment script (generates YAML, deploys, dev mode)
```

## Multi-Cloud Support

InfeRecipe auto-detects the cloud provider and configures networking accordingly:

| Provider | GPU Resource | Networking | RDMA |
|---|---|---|---|
| IBM Cloud | DRA (`dra.llm-d.io/gpu-nic-pair`) | DRANet | InfiniBand via DRA |
| CoreWeave | `nvidia.com/gpu` + `rdma/ib` | Shared device plugin | InfiniBand via device plugin |
| Bare Metal | `nvidia.com/gpu` | NAD (Multus) | InfiniBand via Multus |
| AWS / Azure / GCP | `nvidia.com/gpu` | Standard | Provider-specific |

## Metrics

**Client-side** (via [guidellm](https://github.com/neuralmagic/guidellm)):
- TTFT (p50, p90, p99)
- Inter-token latency (ITL)
- Throughput (requests/sec, tokens/sec)

**Server-side** (via Prometheus/Thanos):
- GPU utilization and memory
- vLLM queue depth, batch size, KV cache usage
- InfiniBand RDMA throughput

Results are stored in SQLite at `/mnt/storage/inferecipe.db`.

## Troubleshooting

```bash
# Check pod status
kubectl get pod inferecipe-benchmark -n llm-d

# View logs
kubectl logs -f inferecipe-benchmark -n llm-d

# Check active test deployments
kubectl get leaderworkerset -n llm-d

# Inspect a stuck pod
kubectl describe pod inferecipe-benchmark -n llm-d
```

## License

[Apache 2.0](LICENSE)
