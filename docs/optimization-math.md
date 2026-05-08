# Inftune Studio Optimization Pipeline — Math & Formulas

Complete reference for every calculation, heuristic, and formula used in the optimization pipeline. Each section explains not just the formula but **why** each value was chosen.

---

## Overview

Inftune Studio runs an 11-step optimization pipeline that deploys vLLM on Kubernetes, benchmarks it with guidellm, and finds optimal configurations. The pipeline tests different Tensor Parallelism (TP) values, Prefill/Decode (P/D) splits, aggregated configurations, EPP routing weights, and latency-bounded concurrency levels.

**Key metric: TPSG (Tokens Per Second per GPU)** — normalizes throughput by GPU count so configurations with different TP values can be compared fairly. Without normalization, TP=8 would always look faster than TP=2 simply because it uses 4× more GPUs.

---

## Step 1: Initialization & Resource Detection

### GPU VRAM Detection
```
gpu_vram_gb = cluster_resources.gpu_memory_per_gpu_mb / 1024
Fallback: 80 GB
```
**Why 80 GB fallback?** A100-80GB and H100-80GB are the most common GPU accelerators for LLM inference. If the cluster scanner can't detect VRAM (e.g., driver not reporting), 80 GB is a safe assumption that won't over-allocate.

### Model Size Estimation

**Dense model (e.g., Llama, Mistral):**
```
attn_params = hidden × (num_heads × head_dim)           # Q projection
            + hidden × (num_kv_heads × head_dim) × 2    # K + V projections (×2 because two separate projections)
            + (num_heads × head_dim) × hidden            # output projection

per_layer = attn_params + hidden × intermediate × 3
```
**Why × 3 for FFN?** The feed-forward network has 3 weight matrices: gate projection, up projection, and down projection (SwiGLU architecture used by most modern LLMs).

```
embed_params = vocab_size × hidden_size × 2
```
**Why × 2 for embeddings?** Input embedding + output (LM head) projection. Most models tie these weights, but we count the full parameter budget for VRAM planning.

```
total_params = layers × per_layer + embed_params
```

**MoE model (Mixtral):**
```
per_layer = attn_params + (hidden × intermediate × 3) × num_experts + hidden × num_experts
```
The last term (`hidden × num_experts`) is the router/gating network — a small linear layer that decides which expert processes each token.

**Model weight size in GB:**
```
FP8:  params_B × 1.0 GB     (1 byte per parameter)
FP16: params_B × 2.0 GB     (2 bytes per parameter)
```
**Why these ratios?** FP8 stores each parameter in 8 bits (1 byte). FP16 uses 16 bits (2 bytes). A 70B parameter model at FP8 = 70 GB, at FP16 = 140 GB.

### Max Model Length (stdev-adjusted)
```
max_model_len = (ISL + 2 × ISL_stdev) + (OSL + 2 × OSL_stdev)
```
**Why 2-sigma?** The 2-sigma rule covers 97.7% of a Gaussian distribution. guidellm generates sequences with normally distributed lengths (mean=ISL/OSL, stdev=ISL_stdev/OSL_stdev). Setting max_model_len at mean + 2σ ensures vLLM can handle virtually all generated sequences without truncation. Using 3σ (99.7%) would waste GPU memory on rarely-needed headroom.

### Valid TP Options
```
Valid TP = powers of 2 up to max GPUs per node
min_tp = ceil(model_size_gb / (gpu_vram_gb × 0.7))
```
**Why powers of 2?** GPU-to-GPU communication (NCCL AllReduce) is most efficient when the number of participants is a power of 2. Non-power-of-2 TP values cause uneven data splits and slower communication.

**Why × 0.7?** Model weights typically use ~70% of GPU VRAM. The remaining ~30% is needed for:
- KV cache (~20%): working memory for each concurrent request
- CUDA kernels/graphs (~5%): execution overhead
- Activation memory (~5%): intermediate computation tensors

### Example
```
70B FP8 model on 80GB GPUs:
  model_size_gb = 70 × 1.0 = 70 GB
  min_tp = ceil(70 / (80 × 0.7)) = ceil(70 / 56) = 2
  Valid TPs: [2, 4, 8]   (TP=1 excluded: 70GB > 56GB available)
```

---

## Steps 2-3: TP Calibration (Decode & Prefill Sweeps)

### TPSG Calculation
```
Step 2 (Decode): TPSG = (throughput_p90 × OSL) / TP
Step 3 (Prefill): TPSG = (throughput_p90 × ISL) / TP
```

**Why throughput_p90?** P90 is a robust measure of sustained throughput — it excludes the top 10% of results which may be inflated by caching or measurement artifacts, while being less conservative than the median (P50).

**Why multiply by sequence length?** Raw throughput is in requests/second, but different sequence lengths produce different amounts of work. A request with OSL=512 generates 512 tokens, while OSL=128 generates 128. Multiplying by sequence length converts to tokens/second — a fair unit of work.

**Why divide by TP?** This normalizes per GPU. A TP=8 config uses 8 GPUs, so its raw throughput should be 8× higher. Dividing by TP gives tokens/second/GPU, making TP=2 and TP=8 directly comparable.

### Workload Design
- **Step 2 uses ISL=1**: Minimizes prefill time so the benchmark measures pure decode throughput. ISL=1 means a single input token — essentially no prefill work.
- **Step 3 uses OSL=1**: Minimizes decode time so the benchmark measures pure prefill throughput. OSL=1 means generating only one output token.

**Why isolate prefill and decode?** In P/D disaggregated mode, prefill and decode run on separate GPU pools. Measuring them independently lets us calculate the optimal ratio between prefill and decode GPUs.

### Selection Criteria
```
TTFT objective:  select TP with lowest TTFT_p90
Throughput objective:  select TP with highest TPSG
```
**Why different criteria?** TTFT-optimized workloads (chatbots, interactive apps) care about how quickly the first token appears — lower TP often wins because less inter-GPU communication is needed before the first token. Throughput-optimized workloads (batch processing) care about total tokens/second — higher TP can win by processing larger batches.

### Profiled Memory Data
```
During Steps 2-3, vLLM pod logs are parsed for:
  vllm_available_kv_gb: Available KV cache memory after model loading
  vllm_fixed_overhead_gb: GPU memory consumed by model weights + CUDA graphs + workspace
  vllm_gpu_blocks: Number of KV cache blocks allocated
```
**Why profile from logs?** vLLM's actual memory consumption is the most accurate source — it accounts for CUDA graph memory, activation buffers, and internal fragmentation that theoretical calculations miss. These profiled values replace heuristic estimates in later steps.

---

## Step 4: Cluster Capacity Analysis

### GPU Cost Per Request
```
prefill_cost = ISL / prefill_TPSG    [GPU-seconds]
decode_cost  = OSL / decode_TPSG     [GPU-seconds]
total_cost   = prefill_cost + decode_cost
```
**What is GPU-seconds?** The amount of time one GPU spends processing one request. A cost of 0.05 means one GPU is busy for 50ms per request. This unit makes it possible to calculate how many requests a cluster can handle.

**Example:** ISL=2048, OSL=512, prefill_TPSG=50000, decode_TPSG=8000
```
prefill_cost = 2048 / 50000 = 0.041 GPU-sec  (41ms of GPU time for prefill)
decode_cost  = 512 / 8000   = 0.064 GPU-sec  (64ms of GPU time for decode)
total_cost   = 0.105 GPU-sec/request
```

### Sustainable Throughput
```
sustainable_qps = total_gpus / total_cost / headroom
```
**Why divide by headroom (1.3)?** Real workloads have variable arrival rates — requests come in bursts, not at a constant rate. The 30% headroom ensures the system can absorb load spikes without queuing. Without headroom, even small traffic bursts cause latency spikes because every GPU is already fully utilized.

**Why 1.3 specifically?** Empirically, LLM inference workloads show ~20-30% variance in request arrival rates. 1.3× provides enough buffer for typical variance while not wasting too many GPUs on idle capacity. For production systems with strict SLAs, users can increase this to 1.5 or 2.0.

### Max-Throughput Prefill Ratio
```
max_throughput_pct = (prefill_cost / total_cost) × 100
```
**Why?** To maximize throughput, the prefill and decode GPU pools must be balanced — neither should be bottlenecked while the other is idle. The ratio of GPU time spent on prefill vs total gives the optimal split. If prefill takes 40% of GPU time, allocate 40% of GPUs to prefill.

### Latency-Optimal Prefill Ratio (TTFT objective)
```
ideal_decode_gpus = min(concurrency × decode_tp, total_gpus - prefill_tp)
ideal_prefill_pct = ((total_gpus - ideal_decode_gpus) / total_gpus) × 100
```
**Why different from throughput-optimal?** For low TTFT, you want enough decode pods to handle all concurrent users without queuing. Each concurrent user needs one decode "slot" (one decode pod can handle one user's decode stream). So you allocate `concurrency × decode_tp` GPUs to decode, and give everything else to prefill. This minimizes the time a request waits for a decode slot.

### Sustainable Concurrency
```
sustainable_concurrency = max(1, floor(total_gpus / headroom))
```
**Why this formula?** Each concurrent request occupies approximately 1 GPU's worth of compute (averaged over prefill + decode). So the number of GPUs (divided by headroom) approximates how many simultaneous requests the cluster can handle without overloading.

---

## Step 5: Feasible P/D Split Generation

### NIXL KV Transfer Constraint
```
Skip pairs where: prefill_tp >= num_kv_heads AND prefill_tp > decode_tp
```
**Why?** NIXL (the KV cache transfer library in llm-d) transfers KV cache data from prefill GPUs to decode GPUs after prefill completes. When prefill TP >= number of KV heads, each prefill GPU holds only 1 KV head (or a fraction). Transferring this highly fragmented data to fewer decode GPUs (where each GPU needs multiple KV heads) causes a mapping failure — the NIXL handshake asserts that the source and destination layouts are compatible, and this many-to-few mapping violates that assertion.

### Smart PD Search Formula
```
r = decode_throughput_p90 / prefill_throughput_p90
d_ideal = usable_gpus / (r × prefill_tp + decode_tp)
```

**Derivation:**
Let P = prefill pods, D = decode pods.

1. **GPU constraint:** `P × prefill_tp + D × decode_tp = usable_gpus`
2. **Balance constraint:** `prefill_throughput × P = decode_throughput × D`
   This means the prefill pool processes requests at the same rate the decode pool consumes them — no bottleneck.

From (2): `P = D × (decode_thr / prefill_thr) = D × r`

Substituting into (1): `D × r × prefill_tp + D × decode_tp = usable_gpus`

Solving: `D = usable_gpus / (r × prefill_tp + decode_tp)`

**Why test floor/ceil ± 1?** The ideal D is usually not an integer. We test the 3-4 closest valid integer values to find the actual optimum, accounting for rounding effects and real-world performance variations.

**Why is this "smart"?** Exhaustive search tests ALL valid splits (could be 30+ for a 32-GPU cluster). Smart search tests ~3 per TP pair — the mathematically optimal point and its neighbors. This reduces test time by 10× while finding configurations within 1-2% of the exhaustive optimum.

---

## Step 6: Aggregated Configuration Search

Tests every valid TP value at full workload using all GPUs.

```
For each TP in valid_tp_options:
  replicas = total_gpus // TP
  Run guidellm benchmark with ISL=target, OSL=target, concurrent users
```
**Why test all TPs?** Aggregated mode (no P/D split) may outperform P/D for some workloads — especially when ISL is small (little prefill work to separate) or when the cluster has few GPUs (P/D split overhead exceeds the benefit). Testing all TPs here means Step 8 can compare the best aggregated config against the best P/D config without running additional tests.

---

## Step 7: P/D Split Testing & Pareto Front

### Pareto Dominance
```
Configuration j DOMINATES configuration i if:
  ttft_j <= ttft_i  AND  throughput_j >= throughput_i
  AND at least one strict inequality
```
**Why Pareto front?** TTFT and throughput are competing objectives — improving one often degrades the other. The Pareto front is the set of configurations where you can't improve one metric without sacrificing the other. This gives the user a menu of trade-offs rather than a single "best" answer.

---

## Step 8: Architecture Comparison

No new tests. Compares best PD (Step 7) vs best Aggregated (Step 6).

**Why no new tests?** Step 6 already tested all aggregated configs. Comparing against P/D results from Step 7 is a pure data analysis step — no additional benchmarks needed.

---

## Step 9: EPP (Endpoint Picker) Tuning

### EPP Scoring Formula
```
score = prefix_cache_weight × prefix_score
      + kv_cache_weight × kv_score
      + queue_weight × queue_score
```

The EPP routes each incoming request to the vLLM server that will handle it fastest. Each server is scored by three factors:

- **prefix_score**: How much of the request's prompt is already cached on this server. Higher = less prefill work needed.
- **kv_score**: How much free KV cache memory this server has. Higher = can accept more concurrent requests.
- **queue_score**: How many requests are already queued on this server. Lower queue = faster processing.

### Weight Selection Strategy
```
Default:     prefix=3, kv=2, queue=2
If ISL/OSL > 10 (long prompts, short outputs):
  cache-heavy: prefix=5, kv=1, queue=1
  queue-heavy: prefix=1, kv=1, queue=5
  kv-heavy:    prefix=2, kv=5, queue=1
  equal:       prefix=2, kv=2, queue=2
```
**Why ISL/OSL > 10 triggers more combos?** When prompts are much longer than outputs, prefix cache hits have a dramatic effect — a cache hit can skip thousands of tokens of prefill computation. This makes the routing decision much more impactful, so testing different weight strategies is worthwhile. For balanced ISL/OSL, the default weights work well because no single factor dominates.

### EPP Config Parameters

**maxPrefixBlocksToMatch:**
```
maxPrefixBlocksToMatch = ceil(ISL / block_size)
```
This is the number of KV cache blocks the EPP compares when checking for prefix cache hits. It matches the maximum number of blocks a single prompt could occupy, ensuring the EPP checks the full prompt for cache matches.

**lruCapacityPerServer:**
```
lruCapacity = (gpu_vram_gb × kv_cache_fraction × 1024³) /
              (block_size × 2 × num_layers × kv_heads_per_gpu × head_dim × 2)
```

Breaking down each term:

| Term | Meaning | Why |
|------|---------|-----|
| `gpu_vram_gb` | GPU memory in GB | Total memory available |
| `kv_cache_fraction` (0.5) | 50% of VRAM for KV cache | Typical split: ~50% model weights, ~50% KV cache. Conservative — actual fraction depends on model size vs GPU VRAM |
| `× 1024³` | Convert GB → bytes | 1 GB = 1,073,741,824 bytes (1024 × 1024 × 1024) |
| `block_size` | Tokens per KV cache block | From `_compute_block_size()` — how many tokens are grouped into one cache block |
| `× 2` (first) | K + V tensors | Each token position stores both a Key tensor and a Value tensor — two separate data structures |
| `num_layers` | Transformer layers | Each layer has its own independent KV cache (attention is computed per-layer) |
| `kv_heads_per_gpu` | `num_kv_heads // TP` | With tensor parallelism, KV heads are split across GPUs. Each GPU stores only its shard |
| `head_dim` | `hidden_size // num_heads` | Dimension of each attention head (typically 128 for most LLMs) |
| `× 2` (second) | FP16 = 2 bytes per value | KV cache uses float16 by default. Each number takes 2 bytes of memory |

The result is the number of KV cache blocks that fit in one GPU's available memory — this is how many prefix entries the EPP's LRU cache should track per server.

**Example:** Llama-70B on H100-80GB at TP=4, block_size=128
```
kv_heads_per_gpu = 8 / 4 = 2
bytes_per_block = 128 × 2 × 80 × 2 × 128 × 2 = 10,485,760 bytes (10 MB)
available_bytes = 80 × 0.5 × 1024³ = 42,949,672,960 bytes (40 GB)
lru_capacity = 42,949,672,960 / 10,485,760 = 4,096 blocks
```

**Example:** Llama-8B on H200-140GB at TP=1, block_size=128
```
kv_heads_per_gpu = 8 / 1 = 8
bytes_per_block = 128 × 2 × 32 × 8 × 128 × 2 = 16,777,216 bytes (16 MB)
available_bytes = 140 × 0.5 × 1024³ = 75,161,927,680 bytes (70 GB)
lru_capacity = 75,161,927,680 / 16,777,216 = 4,480 blocks
```

**nonCachedTokens:**
```
nonCachedTokens = min(16, max(1, ISL // 100))
```
**Why?** This is the number of unique (non-shared) tokens at the end of each prompt that the EPP considers "uncacheable." In prefix caching, the system prompt and shared instructions are cached, but the user-specific part at the end varies per request. For a 2000-token prompt, ~20 tokens are unique; for a 100-token prompt, ~1 token. The min/max bounds keep this in a sensible range.

---

## Step 10: Latency-Bounded Throughput Search

### Starting Concurrency Estimation
```
ceiling = observed_throughput_p90 × (target_ms / observed_latency)
starting_concurrency = max(1, floor(ceiling × 0.6))
```

**Why this formula?** Uses Little's Law: if the system handles `T` requests/second at latency `L`, and we want latency `target`, we can serve approximately `T × (target / L)` concurrent users. This gives the theoretical ceiling.

**Why × 0.6?** Start at 60% of the estimated ceiling to be conservative. Starting too high wastes benchmark time on overloaded tests that all fail the SLA. Starting too low wastes time ramping up. 60% empirically gives a good starting point that usually passes the SLA on the first try, then the exponential ramp-up quickly finds the actual limit.

### Binary Search Algorithm
```
Phase 1: Exponential ramp-up
  If latency < target: multiply concurrency by 2.0 (far from limit) or 1.2 (approaching)
Phase 2: Binary search between last-good and first-bad
  Converge when: (high - low) / low < 0.05
```
**Why 2.0 then 1.2?** The ramp-up needs to quickly find the region where latency exceeds the SLA. Doubling (2.0×) is fast when far from the limit. Once we're within 50% of the estimated ceiling, we switch to 1.2× to avoid overshooting and wasting a test.

**Why 5% convergence?** Below 5% difference in concurrency, the throughput difference is negligible (typically <2% in practice). Further refinement would require more benchmark runs with diminishing returns.

---

## Step 11: Calibrated Load Validation

```
Runs when:
  - Concurrency exceeds sustainable capacity (cluster would be overloaded)
  - User did NOT enable use_achievable_qps (didn't ask us to auto-scale down)
  - Latency search (Step 10) did NOT run (it already finds the right level)
```
**Why re-test?** Steps 7-8 ran at the user's requested concurrency, which overloads the cluster. Those results show degraded performance (high TTFT, potentially lower throughput due to queuing). Re-testing at the sustainable concurrency level shows what the system actually delivers under realistic load — this is what users should plan capacity around.

---

## Auto-Computed vLLM Parameters

### gpu_memory_utilization
```
With profiled data (from Steps 2-3):
  safe_budget = gpu_vram_gb - measured_overhead - 2.0
  U = min(safe_budget / gpu_vram_gb, 0.95)
```
**Why subtract 2.0 GB?** This is a fragmentation buffer. CUDA memory allocation doesn't perfectly pack — small gaps accumulate between allocations. 2 GB covers typical fragmentation on 40-140 GB GPUs. Without this buffer, vLLM may fail to allocate KV cache blocks even though "enough" memory appears free.

**Why cap at 0.95?** Even with perfect profiling, reserving 5% of VRAM prevents out-of-memory crashes from unexpected allocation spikes (e.g., CUDA graph capture, dynamic batch sizes, or NCCL communication buffers that aren't accounted for in the profiled overhead).

```
Without profiled data (fallback):
  pods_per_node = max_gpus_per_node // TP
  reserve_pct = 0.05 + (pods_per_node - 1) × 0.008
  reserve_gb = max(gpu_vram_gb × reserve_pct, 5.0)
  U = (gpu_vram_gb - reserve_gb) / gpu_vram_gb
```
**Why scale with pods_per_node?** Multiple vLLM pods on the same node share system resources (CPU memory, PCIe bandwidth, network buffers). Each additional pod adds ~0.8% overhead from shared CUDA context, NCCL communicator setup, and OS-level memory pressure. The base 5% covers single-pod overhead.

**Why minimum 5.0 GB?** On smaller GPUs (e.g., A10 24GB), percentage-based reserves can be too small. 5 GB ensures enough room for CUDA context (~2 GB), NCCL buffers (~1 GB), and activation memory (~2 GB) regardless of GPU size.

### max_num_seqs
```
kv_per_seq_gb = (2 × num_layers × kv_heads_per_gpu × head_dim × max_model_len × 2) / 1024³
max_num_seqs = floor(measured_available_kv_gb / kv_per_seq_gb)
```

Breaking down `kv_per_seq_gb`:
| Term | Value | Why |
|------|-------|-----|
| `2` (first) | K + V | Two tensors stored per attention layer per token |
| `num_layers` | e.g., 80 | Each transformer layer has its own KV cache |
| `kv_heads_per_gpu` | `num_kv_heads // TP` | GQA shards KV heads across GPUs |
| `head_dim` | e.g., 128 | Dimension per attention head |
| `max_model_len` | e.g., 4096 | Maximum sequence length — worst case memory per sequence |
| `2` (second) | FP16 bytes | Each KV cache value is float16 (2 bytes) |
| `/ 1024³` | → GB | Convert bytes to gigabytes |

**Why use max_model_len (worst case)?** vLLM pre-allocates KV cache slots at the maximum sequence length to avoid runtime reallocation. Even if most sequences are shorter, each slot reserves max_model_len tokens worth of memory.

### max_num_batched_tokens
```
target_batch_latency = 0.2 seconds
batch_budget = prefill_TPSG × TP × target_batch_latency
max_num_batched_tokens = clamp(batch_budget, [2048, max_model_len])
```
**Why 200ms?** This is the target latency for a single batch forward pass. At 200ms, the system can process ~5 batches per second per GPU. Shorter batches (< 100ms) waste GPU cycles on batch overhead. Longer batches (> 500ms) cause latency spikes for requests that arrive mid-batch and must wait.

**Why clamp to [2048, max_model_len]?** The floor of 2048 ensures enough tokens per batch for GPU utilization — small batches underutilize the GPU's parallel compute. The ceiling of max_model_len prevents vLLM from trying to batch more tokens than a single sequence could contain.

### block_size (KV cache block size)
```
block_size = next_power_of_2(sqrt(ISL + OSL))
Clamped to [8, 512]
For PD objectives: floor = 128
```
**Why sqrt(ISL + OSL)?** Block size is a trade-off:
- **Too small** (e.g., 8): Many tiny blocks → high management overhead, more metadata per sequence, inefficient memory access patterns
- **Too large** (e.g., 512): Few large blocks → wasted memory when sequences don't fill complete blocks (internal fragmentation)
- **sqrt(total_seq_len)** balances these — for a 4096-token sequence, sqrt(4096) = 64, giving ~64 blocks per sequence. This is a sweet spot for cache management efficiency.

**Why power of 2?** GPU memory operations are aligned to powers of 2 for optimal throughput. Non-power-of-2 block sizes cause unaligned memory accesses that reduce bandwidth.

**Why floor = 128 for PD?** In P/D mode, NIXL transfers KV cache from prefill GPUs to decode GPUs in blocks. Each transfer has fixed overhead (connection setup, header, acknowledgment). Larger blocks (128+ tokens) amortize this overhead — transferring 128 tokens in one block is much faster than 16 blocks of 8 tokens each, even though the total data is the same.

### Pod Resources (memory + CPU)
```
pods_per_node = max(
  ceil(total_pods / num_gpu_nodes),
  max_gpus_per_node // TP
)
memory_per_pod = floor((avg_node_memory_gb × 0.85) / pods_per_node)
cpu_per_pod    = floor((avg_node_cpus × 0.80) / pods_per_node)
```
**Why 0.85 for memory?** Reserve 15% of node memory for:
- Kubelet and container runtime (~2-4 GB)
- OS kernel and filesystem cache (~2-4 GB)
- System pods (kube-proxy, CNI, monitoring) (~1-2 GB)
- Safety margin for memory spikes

**Why 0.80 for CPU?** Reserve 20% of node CPUs for:
- Kubelet and container runtime overhead
- NCCL communication threads (run on CPU alongside GPU work)
- System pods and OS processes
- CPU is less critical than memory (GPU-bound workloads), so a bit more aggressive

---

## Constants Reference

| Constant | Value | Why |
|----------|-------|-----|
| Headroom | 1.3 | 30% buffer absorbs typical load spikes without queuing |
| GPU VRAM fallback | 80 GB | Most common GPU VRAM (A100/H100) |
| Memory reserve min | 5.0 GB | Covers CUDA context + NCCL + activations on any GPU |
| Fragmentation buffer | 2.0 GB | Covers CUDA memory fragmentation gaps |
| Batch latency target | 0.2 s | ~5 batches/sec/GPU — balances throughput and latency |
| Min batched tokens | 2048 | Floor for GPU utilization efficiency |
| Max block size | 512 | Ceiling to limit internal fragmentation |
| PD block size floor | 128 | Minimum for NIXL transfer efficiency |
| Binary search threshold | 5% | Diminishing returns below this precision |
| Starting C factor | 0.6 | 60% of ceiling — conservative but not too slow |
| Node memory fraction | 0.85 | 15% for kubelet, OS, system pods |
| Node CPU fraction | 0.80 | 20% for system overhead and NCCL threads |
| Max gpu_memory_util | 0.95 | 5% safety for unexpected allocation spikes |
| Reserve scaling | 0.008 | 0.8% per additional pod for shared overhead |
| Base reserve | 5% | Minimum overhead for single pod |
| KV cache fraction | 0.5 | Conservative split: ~50% model, ~50% KV cache |
