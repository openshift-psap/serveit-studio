# ServeIt Studio CLI

Command-line interface for running LLM inference optimization without the web UI.
Supports multi-cluster management and results are saved to the same database
and can be viewed in the web UI report.

## Quick Start

```bash
# Run from inside the optimizer pod
kubectl exec -it -n serveit deploy/serveit-launcher -- bash
cd /mnt/storage/app

# Register the current cluster
serveit cluster add --name local

# Minimal run — only --model and --cluster are required
serveit run --model RedHatAI/Qwen2.5-Coder-7B-Instruct-FP8-dynamic --cluster local
```

## Commands

```
serveit cluster add       Register a cluster
serveit cluster list      List registered clusters
serveit cluster remove    Remove a registered cluster
serveit cluster scan      Scan cluster resources (GPUs, nodes, RDMA)
serveit run               Run or resume an optimization
```

---

## Cluster Management

### Register Clusters

```bash
# Register the current kubectl context as a local cluster
serveit cluster add --name local

# Register a remote cluster with a kubeconfig file
serveit cluster add --name prod --kubeconfig ~/.kube/prod.yaml

# With custom namespace and storage class
serveit cluster add --name staging \
    --kubeconfig ~/.kube/staging.yaml \
    --namespace my-namespace \
    --storage-class gp3
```

### List and Inspect

```bash
# List all registered clusters
serveit cluster list

# Scan a cluster's resources
serveit cluster scan prod
# Output: GPU model, count, RDMA, nodes, cloud provider

# Remove a cluster
serveit cluster remove staging
```

---

## Running Optimizations

### Basic Optimization

```bash
# Optimize with default settings (16 GPUs, ISL=2000, OSL=2000, 100 users)
serveit run --model RedHatAI/Qwen2.5-Coder-7B-Instruct-FP8-dynamic --cluster local

# Specify workload parameters
serveit run --model RedHatAI/Qwen2.5-Coder-7B-Instruct-FP8-dynamic --cluster prod \
    --isl 9000 --osl 50 --users 100 --gpus 16

# With ISL/OSL standard deviation (realistic variable-length prompts)
serveit run --model RedHatAI/Qwen2.5-Coder-7B-Instruct-FP8-dynamic --cluster local \
    --isl 9000 --isl-stdev 4000 --osl 50 --osl-stdev 20 --users 100
```

### Optimization Goals

```bash
# Minimize response time (TTFT) — default
serveit run --model RedHatAI/Qwen2.5-Coder-7B-Instruct-FP8-dynamic --cluster local --objective ttft

# Maximize throughput
serveit run --model RedHatAI/Qwen2.5-Coder-7B-Instruct-FP8-dynamic --cluster local --objective throughput

# Balanced — test PD, EP, and Aggregated architectures
serveit run --model RedHatAI/Qwen2.5-Coder-7B-Instruct-FP8-dynamic --cluster local --objective balanced

# Only test aggregated (no PD disaggregation)
serveit run --model RedHatAI/Qwen2.5-Coder-7B-Instruct-FP8-dynamic --cluster local --objective aggregated_only

# Only test PD disaggregation
serveit run --model RedHatAI/Qwen2.5-Coder-7B-Instruct-FP8-dynamic --cluster local --objective pd_only
```

### Latency SLA

```bash
# Find max throughput under 2000ms TTFT at P99
serveit run --model RedHatAI/Qwen2.5-Coder-7B-Instruct-FP8-dynamic --cluster local \
    --latency-sla 2000 --latency-percentile p99

# Strict SLA: 500ms at P95
serveit run --model RedHatAI/Qwen2.5-Coder-7B-Instruct-FP8-dynamic --cluster local \
    --latency-sla 500 --latency-percentile p95

# Auto-scale concurrency to sustainable level
serveit run --model RedHatAI/Qwen2.5-Coder-7B-Instruct-FP8-dynamic --cluster local \
    --use-achievable-qps
```

### EPP Configuration

```bash
# Use cache-optimized EPP preset
serveit run --model RedHatAI/Qwen2.5-Coder-7B-Instruct-FP8-dynamic --cluster local \
    --epp-preset cache_optimized

# Benchmark EPP strategies to find optimal routing
serveit run --model RedHatAI/Qwen2.5-Coder-7B-Instruct-FP8-dynamic --cluster local \
    --epp-benchmark

# Custom EPP weights (cache:kv:queue)
serveit run --model RedHatAI/Qwen2.5-Coder-7B-Instruct-FP8-dynamic --cluster local \
    --epp-weights 5:1:1

# Override EPP auto-calculated parameters
serveit run --model RedHatAI/Qwen2.5-Coder-7B-Instruct-FP8-dynamic --cluster local \
    --epp-max-prefix-blocks 512 \
    --epp-lru-capacity 50000 \
    --epp-non-cached-tokens 32
```

### Prefix Cache Simulation

```bash
# 80% identical prompts (FAQ/popular query pattern)
serveit run --model RedHatAI/Qwen2.5-Coder-7B-Instruct-FP8-dynamic --cluster local \
    --prefix-cache-pct 80 --prefix-cache-mode identical

# Shared prefix — all prompts share 80% common prefix (system prompt pattern)
serveit run --model RedHatAI/Qwen2.5-Coder-7B-Instruct-FP8-dynamic --cluster local \
    --prefix-cache-pct 80 --prefix-cache-mode shared_prefix

# Multi-group — 10 distinct tenant groups with 80% cache hit rate
serveit run --model RedHatAI/Qwen2.5-Coder-7B-Instruct-FP8-dynamic --cluster local \
    --prefix-cache-pct 80 --prefix-cache-mode multi_group --prefix-cache-groups 10
```

### Search Strategy

```bash
# Fast search — 1 TP pair, smart PD splits
serveit run --model RedHatAI/Qwen2.5-Coder-7B-Instruct-FP8-dynamic --cluster local \
    --tp-pair-depth 1 --pd-search smart

# Thorough search — all TP pairs, exhaustive PD splits
serveit run --model RedHatAI/Qwen2.5-Coder-7B-Instruct-FP8-dynamic --cluster local \
    --tp-pair-depth 4 --pd-search exhaustive

# Specific TP values only
serveit run --model RedHatAI/Qwen2.5-Coder-7B-Instruct-FP8-dynamic --cluster local \
    --tp-options 1,2,4

# Short test duration (quick validation)
serveit run --model RedHatAI/Qwen2.5-Coder-7B-Instruct-FP8-dynamic --cluster local \
    --duration 120

# Stop after N requests instead of duration
serveit run --model RedHatAI/Qwen2.5-Coder-7B-Instruct-FP8-dynamic --cluster local \
    --stop-mode max_requests --max-requests 1000
```

### Load Profile

```bash
# Concurrent users (default)
serveit run --model RedHatAI/Qwen2.5-Coder-7B-Instruct-FP8-dynamic --cluster local \
    --users 100 --rate-type concurrent

# Constant requests per second
serveit run --model RedHatAI/Qwen2.5-Coder-7B-Instruct-FP8-dynamic --cluster local \
    --users 50 --rate-type constant

# Poisson-distributed arrivals
serveit run --model RedHatAI/Qwen2.5-Coder-7B-Instruct-FP8-dynamic --cluster local \
    --users 50 --rate-type poisson
```

### Custom Dataset

```bash
# HuggingFace dataset
serveit run --model RedHatAI/Qwen2.5-Coder-7B-Instruct-FP8-dynamic --cluster local \
    --workload-mode dataset \
    --dataset openai/gsm8k \
    --dataset-column question

# Local JSONL file
serveit run --model RedHatAI/Qwen2.5-Coder-7B-Instruct-FP8-dynamic --cluster local \
    --workload-mode dataset \
    --dataset /mnt/storage/my-prompts.jsonl \
    --dataset-column prompt \
    --dataset-max-output 512
```

### Multi-Turn Conversations

```bash
# 3-turn conversation simulation
serveit run --model RedHatAI/Qwen2.5-Coder-7B-Instruct-FP8-dynamic --cluster local \
    --turns 3 --isl 2000 --osl 200
```

### Infrastructure

```bash
# Custom vLLM image
serveit run --model RedHatAI/Qwen2.5-Coder-7B-Instruct-FP8-dynamic --cluster local \
    --image ghcr.io/llm-d/llm-d-cuda:v0.8.0

# Different namespace and PVC
serveit run --model RedHatAI/Qwen2.5-Coder-7B-Instruct-FP8-dynamic --cluster local \
    --namespace my-namespace --pvc my-model-cache

# Pin to specific nodes
serveit run --model RedHatAI/Qwen2.5-Coder-7B-Instruct-FP8-dynamic --cluster local \
    --nodes worker-gpu-01,worker-gpu-02

# Gated model with HuggingFace token
serveit run --model meta-llama/Llama-3.1-70B-Instruct --cluster local \
    --hf-token hf_xxxxxxxxxxxxx
# Or set HF_TOKEN environment variable
export HF_TOKEN=hf_xxxxxxxxxxxxx
serveit run --model meta-llama/Llama-3.1-70B-Instruct --cluster local
```

### Advanced vLLM Settings

```bash
# Override engine parameters
serveit run --model RedHatAI/Qwen2.5-Coder-7B-Instruct-FP8-dynamic --cluster local \
    --max-model-len 16384 \
    --gpu-mem-util 0.92 \
    --block-size 256 \
    --dtype float16 \
    --kv-cache-dtype fp8

# Enable debug logs for troubleshooting
serveit run --model RedHatAI/Qwen2.5-Coder-7B-Instruct-FP8-dynamic --cluster local \
    --vllm-debug-logs --nccl-debug-logs

# Disable prefix caching
serveit run --model RedHatAI/Qwen2.5-Coder-7B-Instruct-FP8-dynamic --cluster local \
    --no-prefix-caching

# Tool calling support
serveit run --model RedHatAI/Qwen2.5-Coder-7B-Instruct-FP8-dynamic --cluster local \
    --tool-call-parser hermes --enable-auto-tool-choice
```

### Resume & Reports

```bash
# Resume a stopped/failed run
serveit run --resume 7 --cluster local

# Generate HTML report after optimization
serveit run --model RedHatAI/Qwen2.5-Coder-7B-Instruct-FP8-dynamic --cluster local \
    --html-report results.html

# Generate report from an existing completed run
serveit run --resume 7 --cluster local --html-report run7-report.html

# Run with description
serveit run --model RedHatAI/Qwen2.5-Coder-7B-Instruct-FP8-dynamic --cluster local \
    --description "Production baseline test with 80% cache hit"

# Quiet mode (no progress output)
serveit run --model RedHatAI/Qwen2.5-Coder-7B-Instruct-FP8-dynamic --cluster local --quiet
```

### Full Production Example

```bash
# Complete production optimization run
serveit run \
    --model RedHatAI/Qwen2.5-Coder-7B-Instruct-FP8-dynamic \
    --cluster prod \
    --isl 9000 --isl-stdev 4000 \
    --osl 50 --osl-stdev 20 \
    --users 100 \
    --gpus 16 \
    --objective ttft \
    --tp-pair-depth 2 \
    --pd-search smart \
    --latency-sla 2000 --latency-percentile p99 \
    --epp-preset balanced \
    --epp-benchmark \
    --prefix-cache-pct 80 --prefix-cache-mode identical \
    --duration 300 \
    --description "Production optimization — gpt-oss-20b with latency SLA" \
    --html-report /mnt/storage/report-gpt-oss-20b.html
```

---

## All Options Reference

### Cluster Commands

| Command | Description |
|---------|-------------|
| `serveit cluster add --name NAME` | Register current kubectl context |
| `serveit cluster add --name NAME --kubeconfig PATH` | Register remote cluster |
| `serveit cluster list` | List registered clusters |
| `serveit cluster remove NAME` | Remove a cluster |
| `serveit cluster scan NAME` | Scan cluster resources |

### Run Options

| Flag | Default | Description |
|------|---------|-------------|
| `--model` | *required* | Model name or HuggingFace path |
| `--cluster` | — | Registered cluster name |
| `--resume` | — | Resume a previous run by ID |
| **Workload** | | |
| `--isl` | 2000 | Input sequence length |
| `--isl-stdev` | — | ISL standard deviation |
| `--osl` | 2000 | Output sequence length |
| `--osl-stdev` | — | OSL standard deviation |
| `--users` | 100 | Concurrent users |
| `--rate-type` | concurrent | Load profile: concurrent, constant, poisson |
| `--turns` | 1 | Conversation turns |
| `--workload-mode` | synthetic | synthetic or dataset |
| `--dataset` | — | Dataset path or HuggingFace ID |
| `--dataset-column` | — | Column name for prompts |
| `--dataset-max-output` | 256 | Max output tokens for dataset mode |
| **Prefix Cache** | | |
| `--prefix-cache-pct` | 0 | Cache hit ratio 0-100% |
| `--prefix-cache-mode` | identical | identical, shared_prefix, multi_group |
| `--prefix-cache-groups` | 5 | Prompt groups for multi_group mode |
| **Hardware** | | |
| `--gpus` | 16 | Total GPUs |
| `--tp-options` | 1,2,4,8 | TP values to explore |
| `--image` | ghcr.io/llm-d/llm-d-cuda:v0.8.0 | vLLM container image |
| `--namespace` | from cluster | Kubernetes namespace |
| `--pvc` | serveit-cache | PVC name |
| `--nccl-ib-hca` | mlx | NCCL IB HCA prefix |
| `--hf-token` | — | HuggingFace token (or HF_TOKEN env) |
| `--nodes` | — | Comma-separated node names |
| **Search Strategy** | | |
| `--objective` | ttft | ttft, throughput, balanced, aggregated_only, pd_only, ep_only |
| `--tp-pair-depth` | 2 | TP pair breadth: 1-4 |
| `--pd-search` | smart | smart or exhaustive |
| `--headroom` | 1.3 | Load headroom multiplier |
| `--use-achievable-qps` | off | Auto-scale concurrency |
| `--duration` | 300 | Test duration (seconds) |
| `--stop-mode` | duration | duration or max_requests |
| `--max-requests` | — | Max requests per test |
| **Latency SLA** | | |
| `--latency-sla` | — | Target latency in ms |
| `--latency-percentile` | p99 | p50, p90, p95, p99 |
| **EPP** | | |
| `--epp-preset` | balanced | balanced, cache_optimized, queue_balanced, latency_aware, custom |
| `--epp-benchmark` | off | Benchmark EPP strategies |
| `--epp-weights` | — | Custom weights C:K:Q |
| `--epp-max-prefix-blocks` | auto | maxPrefixBlocksToMatch |
| `--epp-lru-capacity` | auto | lruCapacityPerServer |
| `--epp-non-cached-tokens` | auto | nonCachedTokens (PD routing threshold) |
| **Advanced vLLM** | | |
| `--max-model-len` | auto | Max model length |
| `--gpu-mem-util` | auto | GPU memory utilization |
| `--block-size` | auto | KV cache block size |
| `--dtype` | auto | Model dtype |
| `--kv-cache-dtype` | auto | KV cache dtype |
| `--pipeline-parallel` | auto | Pipeline parallel size |
| `--max-num-seqs` | auto | Max concurrent sequences |
| `--max-num-batched-tokens` | auto | Max tokens per batch |
| `--tool-call-parser` | auto | Tool call parser |
| **Toggle Flags** | | |
| `--enable-prefix-caching` | auto (on) | Enable prefix caching |
| `--no-prefix-caching` | — | Disable prefix caching |
| `--disable-custom-all-reduce` | auto (off) | Disable custom all-reduce |
| `--trust-remote-code` | auto (on) | Trust remote code |
| `--no-trust-remote-code` | — | Disable trust remote code |
| `--disable-log-requests` | auto (on) | Disable request logging |
| `--enable-auto-tool-choice` | auto (off) | Enable auto tool choice |
| `--vllm-debug-logs` | off | Enable vLLM debug logs |
| `--nccl-debug-logs` | off | Enable NCCL debug logs |
| **Output** | | |
| `--html-report` | — | Generate HTML report to file |
| `--description` | — | Run description |
| `--db` | /mnt/storage/serveit.db | Database path |
| `--quiet` | off | Suppress progress output |
