# Inftune Studio CLI

Command-line interface for running LLM inference optimization without the web UI.
Results are saved to the same database and can be viewed in the web UI report.

## Quick Start

```bash
# Run from inside the optimizer pod
kubectl exec -it -n llm-d deploy/inftune-optimizer -- bash
cd /mnt/storage/app

# Minimal run — only --model is required
python3 scripts/inftune.py --model RedHatAI/gpt-oss-20b
```

## Usage

```
python3 scripts/inftune.py --model MODEL [options]
python3 scripts/inftune.py --resume RUN_ID [options]
```

Only `--model` is required for a new run. Everything else has sensible defaults
matching the web UI wizard.

---

## Examples

### Basic Optimization

```bash
# Optimize with default settings (16 GPUs, ISL=3000, OSL=256, 100 users)
python3 scripts/inftune.py --model RedHatAI/gpt-oss-20b

# Specify workload parameters
python3 scripts/inftune.py --model RedHatAI/gpt-oss-20b \
    --isl 9000 --osl 50 --users 100 --gpus 16

# With ISL/OSL standard deviation (realistic variable-length prompts)
python3 scripts/inftune.py --model RedHatAI/gpt-oss-20b \
    --isl 9000 --isl-stdev 4000 --osl 50 --osl-stdev 20 --users 100
```

### Optimization Goals

```bash
# Minimize response time (TTFT) — default
python3 scripts/inftune.py --model RedHatAI/gpt-oss-20b --objective ttft

# Maximize throughput
python3 scripts/inftune.py --model RedHatAI/gpt-oss-20b --objective throughput

# Balanced — test PD, EP, and Aggregated architectures
python3 scripts/inftune.py --model RedHatAI/gpt-oss-20b --objective balanced

# Only test aggregated (no PD disaggregation)
python3 scripts/inftune.py --model RedHatAI/gpt-oss-20b --objective aggregated_only

# Only test PD disaggregation
python3 scripts/inftune.py --model RedHatAI/gpt-oss-20b --objective pd_only
```

### Latency SLA

```bash
# Find max throughput under 2000ms TTFT at P99
python3 scripts/inftune.py --model RedHatAI/gpt-oss-20b \
    --latency-sla 2000 --latency-percentile p99

# Strict SLA: 500ms at P95
python3 scripts/inftune.py --model RedHatAI/gpt-oss-20b \
    --latency-sla 500 --latency-percentile p95

# Auto-scale concurrency to sustainable level
python3 scripts/inftune.py --model RedHatAI/gpt-oss-20b \
    --use-achievable-qps
```

### EPP Configuration

```bash
# Use cache-optimized EPP preset
python3 scripts/inftune.py --model RedHatAI/gpt-oss-20b \
    --epp-preset cache_optimized

# Benchmark EPP strategies to find optimal routing
python3 scripts/inftune.py --model RedHatAI/gpt-oss-20b \
    --epp-benchmark

# Custom EPP weights (cache:kv:queue)
python3 scripts/inftune.py --model RedHatAI/gpt-oss-20b \
    --epp-weights 5:1:1

# Override EPP auto-calculated parameters
python3 scripts/inftune.py --model RedHatAI/gpt-oss-20b \
    --epp-max-prefix-blocks 512 \
    --epp-lru-capacity 50000 \
    --epp-non-cached-tokens 32
```

### Prefix Cache Simulation

```bash
# 80% identical prompts (FAQ/popular query pattern)
python3 scripts/inftune.py --model RedHatAI/gpt-oss-20b \
    --prefix-cache-pct 80 --prefix-cache-mode identical

# Shared prefix — all prompts share 80% common prefix (system prompt pattern)
python3 scripts/inftune.py --model RedHatAI/gpt-oss-20b \
    --prefix-cache-pct 80 --prefix-cache-mode shared_prefix

# Multi-group — 10 distinct tenant groups with 80% cache hit rate
python3 scripts/inftune.py --model RedHatAI/gpt-oss-20b \
    --prefix-cache-pct 80 --prefix-cache-mode multi_group --prefix-cache-groups 10
```

### Search Strategy

```bash
# Fast search — 1 TP pair, smart PD splits
python3 scripts/inftune.py --model RedHatAI/gpt-oss-20b \
    --tp-pair-depth 1 --pd-search smart

# Thorough search — all TP pairs, exhaustive PD splits
python3 scripts/inftune.py --model RedHatAI/gpt-oss-20b \
    --tp-pair-depth 4 --pd-search exhaustive

# Specific TP values only
python3 scripts/inftune.py --model RedHatAI/gpt-oss-20b \
    --tp-options 1,2,4

# Short test duration (quick validation)
python3 scripts/inftune.py --model RedHatAI/gpt-oss-20b \
    --duration 120

# Stop after N requests instead of duration
python3 scripts/inftune.py --model RedHatAI/gpt-oss-20b \
    --stop-mode max_requests --max-requests 1000
```

### Load Profile

```bash
# Concurrent users (default)
python3 scripts/inftune.py --model RedHatAI/gpt-oss-20b \
    --users 100 --rate-type concurrent

# Constant requests per second
python3 scripts/inftune.py --model RedHatAI/gpt-oss-20b \
    --users 50 --rate-type constant

# Poisson-distributed arrivals
python3 scripts/inftune.py --model RedHatAI/gpt-oss-20b \
    --users 50 --rate-type poisson
```

### Custom Dataset

```bash
# HuggingFace dataset
python3 scripts/inftune.py --model RedHatAI/gpt-oss-20b \
    --workload-mode dataset \
    --dataset openai/gsm8k \
    --dataset-column question

# Local JSONL file
python3 scripts/inftune.py --model RedHatAI/gpt-oss-20b \
    --workload-mode dataset \
    --dataset /mnt/storage/my-prompts.jsonl \
    --dataset-column prompt \
    --dataset-max-output 512
```

### Multi-Turn Conversations

```bash
# 3-turn conversation simulation
python3 scripts/inftune.py --model RedHatAI/gpt-oss-20b \
    --turns 3 --isl 2000 --osl 200
```

### Infrastructure

```bash
# Custom vLLM image
python3 scripts/inftune.py --model RedHatAI/gpt-oss-20b \
    --image ghcr.io/llm-d/llm-d-cuda:v0.6.0

# Different namespace and PVC
python3 scripts/inftune.py --model RedHatAI/gpt-oss-20b \
    --namespace my-namespace --pvc my-model-cache

# Pin to specific nodes
python3 scripts/inftune.py --model RedHatAI/gpt-oss-20b \
    --nodes worker-gpu-01,worker-gpu-02

# Gated model with HuggingFace token
python3 scripts/inftune.py --model meta-llama/Llama-3-70b \
    --hf-token hf_xxxxxxxxxxxxx
# Or set HF_TOKEN environment variable
export HF_TOKEN=hf_xxxxxxxxxxxxx
python3 scripts/inftune.py --model meta-llama/Llama-3-70b
```

### Advanced vLLM Settings

```bash
# Override engine parameters
python3 scripts/inftune.py --model RedHatAI/gpt-oss-20b \
    --max-model-len 16384 \
    --gpu-mem-util 0.92 \
    --block-size 256 \
    --dtype float16 \
    --kv-cache-dtype fp8

# Enable debug logs for troubleshooting
python3 scripts/inftune.py --model RedHatAI/gpt-oss-20b \
    --vllm-debug-logs --nccl-debug-logs

# Disable prefix caching
python3 scripts/inftune.py --model RedHatAI/gpt-oss-20b \
    --no-prefix-caching

# Tool calling support
python3 scripts/inftune.py --model RedHatAI/gpt-oss-20b \
    --tool-call-parser hermes --enable-auto-tool-choice
```

### Resume & Reports

```bash
# Resume a stopped/failed run
python3 scripts/inftune.py --resume 7

# Generate HTML report after optimization
python3 scripts/inftune.py --model RedHatAI/gpt-oss-20b \
    --html-report results.html

# Generate report from an existing completed run
python3 scripts/inftune.py --resume 7 --html-report run7-report.html

# Run with description
python3 scripts/inftune.py --model RedHatAI/gpt-oss-20b \
    --description "Production baseline test with 80% cache hit"

# Quiet mode (no progress output)
python3 scripts/inftune.py --model RedHatAI/gpt-oss-20b --quiet
```

### Full Production Example

```bash
# Complete production optimization run
python3 scripts/inftune.py \
    --model RedHatAI/gpt-oss-20b \
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

| Flag | Default | Description |
|------|---------|-------------|
| `--model` | *required* | Model name or HuggingFace path |
| `--resume` | — | Resume a previous run by ID |
| **Workload** | | |
| `--isl` | 3000 | Input sequence length |
| `--isl-stdev` | — | ISL standard deviation |
| `--osl` | 256 | Output sequence length |
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
| `--image` | ghcr.io/llm-d/llm-d-cuda:v0.5.1 | vLLM container image |
| `--namespace` | llm-d | Kubernetes namespace |
| `--pvc` | inftune-model-cache | PVC name |
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
| `--db` | /mnt/storage/inftune.db | Database path |
| `--quiet` | off | Suppress progress output |
