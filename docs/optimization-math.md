# Inftune Studio Optimization Pipeline — Math & Formulas

Complete reference for every calculation, heuristic, and formula used in the optimization pipeline.

---

## Overview

Inftune Studio runs an 11-step optimization pipeline that deploys vLLM on Kubernetes, benchmarks it with guidellm, and finds optimal configurations. The pipeline tests different Tensor Parallelism (TP) values, Prefill/Decode (P/D) splits, aggregated configurations, EPP routing weights, and latency-bounded concurrency levels.

**Key metric: TPSG (Tokens Per Second per GPU)** — normalizes throughput by GPU count so configurations with different TP values can be compared fairly.

---

## Step 1: Initialization & Resource Detection

### GPU VRAM Detection
```
Source: core/optimizer/pipeline.py:122-126

gpu_vram_gb = cluster_resources.gpu_memory_per_gpu_mb / 1024
Fallback: 80 GB (A100/H100 default)
```

### Model Size Estimation
```
Source: core/optimizer/pipeline.py:803-856

Dense model:
  per_layer = attn_params + hidden × intermediate × 3
  attn_params = hidden × (num_heads × head_dim)           # Q projection
              + hidden × (num_kv_heads × head_dim) × 2    # K + V projections
              + (num_heads × head_dim) × hidden            # output projection
  embed_params = vocab_size × hidden_size × 2              # input + output embeddings
  total = layers × per_layer + embed_params

MoE model (Mixtral):
  per_layer = attn_params + (hidden × intermediate × 3) × num_experts + hidden × num_experts
  The last term is the router/gating network.

MoE model (Qwen/DeepSeek with moe_intermediate_size):
  per_layer = attn_params
            + (hidden × moe_intermediate × 3) × num_routed_experts     # expert FFN
            + (hidden × intermediate × 3) × num_shared_experts          # shared FFN
            + hidden × num_experts                                       # router

Model weight size in GB:
  FP8:  model_size_b × 1.0 GB
  FP16: model_size_b × 2.0 GB
```

### Max Model Length (stdev-adjusted)
```
Source: core/test_planner.py via pipeline.py:164-180

max_model_len = (ISL + 2×ISL_stdev) + (OSL + 2×OSL_stdev)

The 2-sigma rule covers 97.7% of the Gaussian distribution.
guidellm generates sequences with mean=ISL/OSL and stdev=ISL_stdev/OSL_stdev,
so max_model_len must accommodate the longest likely sequence.
```

### Valid TP Options
```
Source: core/optimizer/pipeline.py:783-801

Valid TP = powers of 2 up to max GPUs per node, filtered by:
  min_tp = estimate_model_gpu_requirement(model_size_gb, dtype)
  
  model_size_gb for FP8 = params_B × 1.0
  model_size_gb for FP16 = params_B × 2.0
  
  min_tp = ceil(model_size_gb / (gpu_vram_gb × 0.7))
  # 0.7 factor: model weights can use ~70% of VRAM, rest is KV cache + overhead

Example: 70B FP8 model on 80GB GPUs
  model_size_gb = 70 × 1.0 = 70 GB
  min_tp = ceil(70 / (80 × 0.7)) = ceil(70 / 56) = 2
  Valid TPs: [2, 4, 8]
```

---

## Steps 2-3: TP Calibration (Decode & Prefill Sweeps)

### TPSG Calculation
```
Source: core/optimizer/tp_calibration.py:69-72 (decode), 182-185 (prefill)

Step 2 (Decode): TPSG = (throughput_p90 × OSL) / TP
  Workload: ISL=1, OSL=target (decode-focused, minimal prefill)
  
Step 3 (Prefill): TPSG = (throughput_p90 × ISL) / TP
  Workload: ISL=target, OSL=1 (prefill-focused, minimal decode)

TPSG = Tokens Per Second per GPU
  Numerator: total tokens generated per second (throughput × sequence length)
  Denominator: GPUs used (= TP, since calibration uses 1 replica)
```

### Selection Criteria
```
When objective = 'ttft':  select TP with lowest TTFT_p90
When objective = 'throughput' or 'balanced':  select TP with highest TPSG

Fallback: if all TTFT values are infinity (normal for ISL=1 decode tests
where there's no meaningful "first token"), fall back to highest TPSG.
```

### Profiled Memory Data
```
During Steps 2-3, vLLM pod logs are parsed for:
  - vllm_available_kv_gb: Available KV cache memory after model loading
  - vllm_fixed_overhead_gb: GPU memory used by model weights + CUDA graphs + workspace
  - vllm_gpu_blocks: Number of KV cache blocks allocated

These measured values are used in later steps for precise memory calculations.
```

---

## Step 4: Cluster Capacity Analysis

### GPU Cost Per Request
```
Source: core/optimizer/pd_search.py:198-211

prefill_cost = ISL / prefill_TPSG    [GPU-seconds]
decode_cost  = OSL / decode_TPSG     [GPU-seconds]
total_cost   = prefill_cost + decode_cost

Example: ISL=2048, OSL=512, prefill_TPSG=50000, decode_TPSG=8000
  prefill_cost = 2048 / 50000 = 0.041 GPU-sec
  decode_cost  = 512 / 8000  = 0.064 GPU-sec
  total_cost   = 0.105 GPU-sec/request
```

### Sustainable Throughput
```
Source: core/optimizer/pd_search.py:215-216

sustainable_qps = total_gpus / total_cost / headroom

headroom = 1.3 (default) — 30% safety margin for load spikes

Example: 32 GPUs, total_cost=0.105, headroom=1.3
  sustainable_qps = 32 / 0.105 / 1.3 = 234 req/s
```

### Max-Throughput Prefill Ratio
```
Source: core/optimizer/pd_search.py:212

max_throughput_pct = (prefill_cost / total_cost) × 100

This is the percentage of GPUs that should be prefill nodes
to maximize throughput (balance prefill and decode bottlenecks).
```

### Latency-Optimal Prefill Ratio (TTFT objective)
```
Source: core/optimizer/pd_search.py:226-234

ideal_decode_gpus = min(concurrency × decode_tp, total_gpus - prefill_tp)
ideal_prefill_pct = ((total_gpus - ideal_decode_gpus) / total_gpus) × 100

Logic: allocate enough decode GPUs to handle all concurrent users
(1 decode pod per user × decode_tp GPUs per pod), then give the rest to prefill.
```

### Sustainable Concurrency (Overload Detection)
```
Source: core/optimizer/pd_search.py:266

sustainable_concurrency = max(1, floor(total_gpus / headroom))

If requested concurrency > sustainable_concurrency:
  - If use_achievable_qps=True: scale down to sustainable level for Steps 7-8
  - If use_achievable_qps=False: use original concurrency (expect overload),
    Step 10 (latency search) or Step 11 (calibrated load) will find the right level
```

---

## Step 5: Feasible P/D Split Generation

### TP Pair Selection
```
Source: core/optimizer/pd_search.py:13-79

1. Rank prefill TPs: by TTFT (ttft objective) or TPSG (throughput objective)
2. Rank decode TPs: always by TPSG (decode throughput efficiency)
3. Take top-N from each ranking (N = config.tp_pair_top_n, default 1)
4. Cross-product: all (prefill_tp, decode_tp) combinations

NIXL constraint: skip pairs where prefill_tp >= num_kv_heads AND prefill_tp > decode_tp
  Reason: when KV heads are fully sharded across prefill TP workers (1 head per GPU),
  NIXL cannot transfer this fragmented data to fewer decode GPUs because the
  many-to-few mapping logic fails with an assertion error.
```

### Smart PD Search Formula
```
Source: core/optimizer/pd_search.py:113-186

For each (prefill_tp, decode_tp) pair:

  r = decode_throughput_p90 / prefill_throughput_p90     # throughput ratio
  d_ideal = usable_gpus / (r × prefill_tp + decode_tp)  # ideal decode pod count

Derivation:
  Let P = prefill pods, D = decode pods
  Total GPUs: P × prefill_tp + D × decode_tp = usable_gpus
  
  Balanced utilization requires:
    prefill_throughput_per_pod × P = decode_throughput_per_pod × D
    → P/D = decode_thr / prefill_thr = r
    → P = r × D
  
  Substituting: r × D × prefill_tp + D × decode_tp = usable_gpus
  Solving:      D = usable_gpus / (r × prefill_tp + decode_tp)

Candidates: floor(d_ideal)-1, floor(d_ideal), ceil(d_ideal), ceil(d_ideal)+1
  → ~3 splits per TP pair (vs exhaustive which tests ALL valid splits)
```

### Exhaustive Split Enumeration
```
Source: core/optimizer/pd_search.py:91-111

For each (prefill_tp, decode_tp):
  for prefill_gpus in range(prefill_tp, usable_gpus, prefill_tp):
    decode_gpus = usable_gpus - prefill_gpus
    if decode_gpus >= decode_tp AND decode_gpus % decode_tp == 0:
      → valid split

When max_pd_splits is set, splits nearest the ideal_prefill_pct are prioritized.
```

---

## Step 6: Aggregated Configuration Search

Tests every valid TP value with the full ISL+OSL workload using all available GPUs.

```
Source: core/optimizer/pd_search.py:414-504

For each TP in valid_tp_options:
  replicas = total_gpus // TP
  Run guidellm benchmark with ISL=target, OSL=target, concurrent users

Selection:
  throughput objective → highest throughput_p90
  ttft objective      → lowest ttft_p90
```

---

## Step 7: P/D Split Testing & Pareto Front

### Pareto Dominance
```
Source: core/optimizer/config_builder.py:15-43

Configuration j DOMINATES configuration i if:
  ttft_j <= ttft_i  AND  throughput_j >= throughput_i
  AND at least one inequality is strict

A configuration is PARETO OPTIMAL if no other configuration dominates it.
The Pareto front = set of all Pareto optimal configurations.
```

---

## Step 8: Architecture Comparison

No new tests. Compares best PD (from Step 7) vs best Aggregated (from Step 6).

```
Source: core/optimizer/pd_search.py:576-632

ttft_diff = pd_ttft - agg_ttft
tput_diff = pd_tput - agg_tput

Decision:
  If agg_ttft < pd_ttft AND agg_tput >= pd_tput → AGGREGATED IS BETTER
  If agg_ttft < pd_ttft AND agg_tput < pd_tput  → AGGREGATED HAS BETTER TTFT (trade-off)
  Otherwise → PD CONFIRMED
```

---

## Step 9: EPP (Endpoint Picker) Tuning

```
Source: core/optimizer/epp_tuning.py

Tests different EPP scoring weight combinations to find optimal request routing.
EPP scores each endpoint by: prefix_cache_weight × prefix_score + kv_cache_weight × kv_score + queue_weight × queue_score

Weight combos tested:
  Default:     prefix=3, kv=2, queue=2
  If ISL/OSL ratio > 10 (long prompts, short outputs):
    cache-heavy: prefix=5, kv=1, queue=1  (prioritize prefix cache hits)
    queue-heavy: prefix=1, kv=1, queue=5  (prioritize shortest queue)
    kv-heavy:    prefix=2, kv=5, queue=1  (prioritize KV cache availability)
    equal:       prefix=2, kv=2, queue=2

Winner selected by lowest TTFT_p90.
```

### EPP Config Parameters
```
Source: core/optimizer/pipeline.py:603-613

maxPrefixBlocksToMatch = ceil(ISL / block_size)
  How many prefix blocks to compare for cache hit detection.

lruCapacityPerServer = 31250
  LRU cache capacity for prefix matching (fixed).

nonCachedTokens = min(16, max(1, ISL // 100))
  Number of unique (non-cached) tokens at the end of each prompt.
  Shorter prompts → fewer unique tokens.
```

---

## Step 10: Latency-Bounded Throughput Search

### Starting Concurrency Estimation
```
Source: core/optimizer/latency_search.py:43-57

ceiling = observed_throughput_p90 × (target_ms / observed_latency)
starting_concurrency = max(1, floor(ceiling × 0.6))

The 0.6 factor starts conservatively (60% of estimated max) to avoid
starting above the SLA boundary and wasting tests.
```

### Binary Search Algorithm
```
Source: core/user_defined_tuning.py (LatencyBinarySearch)

Phase 1: Exponential ramp-up
  Start at starting_concurrency
  If latency < target: multiply by 2.0 (or 1.2 if approaching)
  If latency > target: stop, we found the upper bound

Phase 2: Binary search between last-good and first-bad
  mid = (low + high) // 2
  Converge when: (high - low) / low < 0.05 (5% threshold)

Result: optimal concurrency where latency ≈ target SLA
```

---

## Step 11: Calibrated Load Validation

```
Source: core/optimizer/latency_search.py:253-396

Runs only when:
  - Concurrency exceeds sustainable capacity
  - User did NOT enable use_achievable_qps
  - Latency search (Step 10) did NOT run

Re-tests best PD and Aggregated configs at sustainable_concurrency
to show realistic (non-overloaded) performance numbers.
```

---

## Auto-Computed vLLM Parameters

### gpu_memory_utilization
```
Source: core/optimizer/config_builder.py:132-164

With profiled data (from Steps 2-3):
  safe_budget = gpu_vram_gb - measured_overhead - 2.0    # 2 GB fragmentation buffer
  U = safe_budget / gpu_vram_gb
  U = min(U, 0.95)

Without profiled data (fallback):
  pods_per_node = max_gpus_per_node // TP
  reserve_pct = 0.05 + (pods_per_node - 1) × 0.008     # more pods → more overhead
  reserve_gb = max(gpu_vram_gb × reserve_pct, 5.0)      # at least 5 GB reserved
  U = (gpu_vram_gb - reserve_gb) / gpu_vram_gb

Example: 80GB GPU, measured_overhead=45GB
  safe_budget = 80 - 45 - 2 = 33 GB
  U = 33/80 = 0.41 → vLLM gets 41% of VRAM for KV cache
```

### max_num_seqs
```
Source: core/optimizer/config_builder.py:166-206

KV cache per sequence:
  kv_per_seq_gb = (2 × num_layers × kv_heads_per_gpu × head_dim × max_model_len × 2) / 1024³

  2 = K + V tensors
  kv_heads_per_gpu = max(num_kv_heads // TP, 1)
  head_dim = hidden_size // num_attention_heads
  × 2 bytes (FP16 default KV cache dtype)

max_num_seqs = floor(measured_available_kv_gb / kv_per_seq_gb)

Example: Llama-70B at TP=4, max_model_len=4096
  num_layers=80, num_kv_heads=8, head_dim=128
  kv_heads_per_gpu = 8/4 = 2
  kv_per_seq_gb = (2 × 80 × 2 × 128 × 4096 × 2) / 1024³ = 0.31 GB
  If available_kv = 20 GB → max_num_seqs = 64
```

### max_num_batched_tokens
```
Source: core/optimizer/config_builder.py:208-227

target_batch_latency = 0.2 seconds (200ms budget per batch)
batch_budget = prefill_TPSG × TP × target_batch_latency
max_num_batched_tokens = clamp(batch_budget, [2048, max_model_len])

This limits how many tokens vLLM processes in a single forward pass.
Prevents large batches from causing latency spikes.

Example: prefill_TPSG=50000, TP=4
  batch_budget = 50000 × 4 × 0.2 = 40000
  max_num_batched_tokens = min(40000, max_model_len)
```

### block_size (KV cache block size)
```
Source: core/optimizer/pipeline.py:587-601

block_size = next_power_of_2(sqrt(ISL + OSL))
Clamped to [8, 512]

For PD objectives (ttft, balanced, pd_only): floor = 128
  Reason: NIXL transfers KV cache in blocks — larger blocks reduce transfer count

Example: ISL=2048, OSL=512
  sqrt(2560) ≈ 50.6
  next_power_of_2(50) = 64
  For PD: max(128, 64) = 128
```

### Pod Resources (memory + CPU)
```
Source: core/optimizer/pipeline.py:633-705

pods_per_node = max(
  ceil(total_pods / num_gpu_nodes),    # from deployment density
  max_gpus_per_node // TP              # from GPU density
)

memory_per_pod = floor((avg_node_memory_gb × 0.85) / pods_per_node)  [Gi]
cpu_per_pod    = floor((avg_node_cpus × 0.80) / pods_per_node)

0.85 / 0.80 factors reserve headroom for OS, kubelet, and system pods.
```

---

## Constants Reference

| Constant | Value | Purpose |
|----------|-------|---------|
| Headroom | 1.3 | Safety margin for sustainable throughput |
| GPU VRAM fallback | 80 GB | Default when cluster scan fails |
| Memory reserve minimum | 5.0 GB | Minimum per-GPU overhead buffer |
| Fragmentation buffer | 2.0 GB | Added to profiled overhead |
| Batch latency target | 0.2 s | Max time for a single batch forward pass |
| Min batched tokens | 2048 | Floor for max_num_batched_tokens |
| Max block size | 512 | Ceiling for KV cache block size |
| PD block size floor | 128 | Minimum for NIXL efficiency |
| Binary search threshold | 5% | Convergence criterion for latency search |
| Starting C factor | 0.6 | Conservative start for latency search |
| Node memory fraction | 0.85 | Usable fraction for pods |
| Node CPU fraction | 0.80 | Usable fraction for pods |
| Max gpu_memory_util | 0.95 | Safety cap |
| Reserve scaling | 0.008 | Per additional pod per node |
| Base reserve | 5% | Minimum overhead percentage |
