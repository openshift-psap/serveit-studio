# Inftune Studio — Supporting Technical Material

Supplementary documentation covering system components not included in `optimization-math.md`. Covers model architecture detection, cloud/network auto-discovery, deployment lifecycle, metrics collection, and prefix cache simulation.

---

## Model Architecture Detection

### Config Fetching

The system automatically retrieves model architecture parameters from three sources, in priority order:

1. **Local PVC** — If the model is pre-downloaded to a PVC, reads `config.json` directly from the volume. Used in air-gapped/offline environments.
2. **HuggingFace API** — Fetches `https://huggingface.co/{model_name}/raw/main/config.json` with optional auth token for gated models. Retries with exponential backoff (max 3 attempts). Handles 401/403 (auth), 404 (not found), timeouts gracefully.
3. **Hardcoded lookup table** — For common model families (Llama, Qwen, Mixtral, Mistral, DeepSeek, GLM, Yi), a built-in table provides `layers`, `kv_heads`, and `head_dim` when remote fetch fails.

**Source:** `core/test_planner.py` lines 291–393, 172–216

### Architecture Parameter Extraction

From `config.json`, the system extracts:

| Parameter | Config Key | Fallback |
|-----------|-----------|----------|
| Hidden size | `hidden_size` | None (required) |
| Layers | `num_hidden_layers` | None (required) |
| KV heads | `num_key_value_heads` | `num_attention_heads` (assumes no GQA) |
| Attention heads | `num_attention_heads` | 32 |
| Head dim | computed: `hidden_size // num_attention_heads` | 128 |

**GQA (Grouped Query Attention):** When `num_kv_heads < num_attention_heads`, the model uses GQA — fewer KV heads are shared across attention heads. This directly affects KV cache size per GPU: `kv_heads_per_gpu = num_kv_heads // TP`.

If all fields are missing, the system falls back to conservative defaults: 40 layers, 8 KV heads, 128 head_dim (covers 7B–70B models safely).

**Source:** `core/test_planner.py` lines 460–513

### MoE Model Detection

Mixture-of-Experts models (e.g., Mixtral) are detected by:

1. Parsing the model name for the `NxMB` pattern (e.g., "8x7B")
2. Calculating effective VRAM: `(num_experts × expert_size) / 1.2`

The `/1.2` factor accounts for expert sparsity — only 2 of 8 experts are active per token in Mixtral, but all expert weights must be loaded in VRAM. The 1.2 divisor is conservative, reflecting that model weights dominate VRAM but not all experts contribute to per-token compute.

**Source:** `core/test_planner.py` lines 395–434

### VRAM Requirements Calculation

Full VRAM breakdown per GPU:

```
Model weights:   model_size_B × bytes_per_param / 1024³ / TP
KV cache:        2 × layers × kv_heads_per_gpu × head_dim × max_seq_len × 2 bytes
Activations:     15% of model weight size
CUDA overhead:   5% of model weight size
Safety buffer:   5% of GPU VRAM
```

Bytes per parameter by dtype: FP32=4, FP16=2, FP8=1, INT8=1, INT4=0.5.

Minimum TP is the smallest power of 2 where all components fit in GPU VRAM.

**Source:** `core/test_planner.py` lines 515–675

---

## Cloud Provider Detection

### Detection Method

The system auto-detects the cloud provider to apply provider-specific constraints:

| Provider | Detection Method |
|----------|-----------------|
| IBM Cloud | OpenShift `infrastructure.config.openshift.io/cluster` → `status.platform = "IBMCloud"` |
| AWS | Same infrastructure resource → `status.platform = "AWS"` |
| GCP | Same → `"GCP"` |
| Azure | Same → `"Azure"` |
| CoreWeave | Node label: `gpu.coreweave.cloud/vendor=NVIDIA` |
| Bare Metal / On-Prem | Fallback when no provider signature found |

**Source:** `core/cloud_constraints.py` lines 68–117

### Provider-Specific Constraints

**IBM Cloud DRA constraint:** Maximum 1 PD pod (prefill OR decode) per node. Prefill and decode cannot coexist on the same node. This is bypassed when using DRANET (DRA networking), which handles GPU+NIC pairing internally.

Other providers have no special pod placement constraints.

The `validate_pd_config()` function checks whether a proposed PD split is legal for the detected provider by calculating pods-per-node and comparing against provider limits.

**Source:** `core/cloud_constraints.py` lines 40–65, 125–171

---

## Network Type Detection & Configuration

### Three Networking Modes

The system supports three RDMA networking modes for GPU-to-GPU communication:

| Mode | Technology | Use Case |
|------|-----------|----------|
| **DRA** | Dynamic Resource Allocation (DRANET) | IBM Cloud — GPU+NIC paired via PCIe affinity |
| **NAD** | NetworkAttachmentDefinition (Multus CNI) | Bare metal — explicit NIC assignment per pod |
| **Shared Device** | Shared RDMA device plugin | CoreWeave, cloud — pods request `rdma/ib` directly |

### RDMA Device Discovery

`detect_rdma_device_resources()` scans node allocatable resources for RDMA-capable devices. Returns the intersection of RDMA devices across all GPU nodes, ensuring any device listed is available cluster-wide.

- DRA mode returns empty (GPU+NIC pairing handled internally by the webhook)
- NAD/SharedDevice returns resource keys like `['rdma/ib']` or `['rdma/ib-1', 'rdma/ib-2']`

**Source:** `core/networking/__init__.py` lines 22–55

### DRA Network Configuration

In DRA mode, the system supports two sub-modes:

- **Webhook mode** (default): No ResourceClaimTemplates created. A mutating webhook intercepts pod creation and auto-generates GPU+NIC pair claims using `dra.llm-d.io/gpu-nic-pair` requests. Multi-rail support up to 8 rails for H100.
- **Legacy mode**: Creates explicit ResourceClaimTemplates with GPU+NIC requests and PCIe affinity constraints.

**Source:** `core/networking/dra.py` lines 39–70

### NAD Network Configuration

Creates NetworkAttachmentDefinitions with:
- Host-device plugin for RDMA NIC passthrough
- DHCP or static IPAM configuration
- Source-based routing (sbr-custom) for multi-homed pods
- Tuning plugins for sysctl configuration
- Device name mapping for multi-port NICs (port 1 = enp233s0, port 2 = enp234s0, etc.)

**Source:** `core/networking/nad.py` lines 40–178

---

## Cluster Resource Scanning

### GPU Detection

Scans node allocatable resources for GPU types:
- NVIDIA: `nvidia.com/gpu`
- AMD: `amd.com/gpu`
- Intel: `gpu.intel.com/i915`

GPU model determined from node labels (`nvidia.com/gpu.product`) or inferred from label patterns (H100, H200, A100, V100, T4, L4, L40, MI300).

**Source:** `core/system_scanner.py` lines 141–208

### Physical NIC Count Detection

The number of physical RDMA NICs per node is determined through four strategies, tried in order:

1. **Port labels** — Regex match against node labels for `.ibp\d+.` or `.sriov.device-\d+` patterns (CoreWeave, OpenShift)
2. **Speed labels** — `ib.coreweave.cloud/speed` divided by per-port speed (400Gbps for NDR/ConnectX-7, 200Gbps for HDR/ConnectX-6)
3. **Device plugin ConfigMap** — Parse `k8s-rdma-shared-dev-plugin` ConfigMap for device resource names
4. **Fallback** — Assume 1:1 GPU:NIC ratio (standard for modern GPU servers)

**Source:** `core/system_scanner.py` lines 395–479

### GPU VRAM Estimation

Priority order:
1. `nvidia.com/gpu.memory` node label (in MB)
2. CoreWeave `gpu.nvidia.com/vram` label (in GB)
3. Model-based lookup: H200=141GB, H100/A100=80GB, L40/L40S=48GB, etc.

**Source:** `core/system_scanner.py` lines 593–650

### CPU Model Detection

- CoreWeave: `cpu.coreweave.cloud/family` and `cpu.coreweave.cloud/cores` labels
- NFD (Node Feature Discovery): `feature.node.kubernetes.io/cpu-model.*` labels
- Intel CPU family mapping: family=6 + model_id → Xeon generation (Sapphire Rapids=143, Ice Lake=106/108, etc.)

**Source:** `core/system_scanner.py` lines 210–251

---

## Deployment Lifecycle

### LeaderWorkerSet (LWS) Deployment

All vLLM inference pods are deployed as LeaderWorkerSets — a Kubernetes extension for multi-pod workloads where a "leader" pod coordinates with "worker" pods. LWS ensures all pods in a group are co-scheduled and share lifecycle.

### PD Sequential Deployment Order

For Prefill/Decode architecture, pods are deployed in a specific order to prevent scheduling deadlocks:

1. **Higher GPU requirement first** — If decode uses TP=8 and prefill uses TP=4, deploy decode pods first. This prevents small pods from spreading across nodes and starving large pod allocation.
2. **Wait for running** before deploying next component — Polls every 5 seconds until the LWS reports `replicas >= pods_expected`.
3. **Services deployed last** — After all pods are running.

**Source:** `core/deployment_manager.py` lines 148–217, `core/template_manager.py` lines 169–217

### Stuck Pod Recovery

Pods stuck in Pending state (common with DRA GPU-NIC pair allocation) are automatically recovered:

1. Polls every 30 seconds for pods Pending > 180 seconds
2. Deletes stuck pods to trigger LWS re-allocation attempt
3. Allows maximum 3 restart attempts per deployment
4. Logs pod readiness conditions for debugging

This handles transient DRA allocation failures where the GPU+NIC pairing webhook fails on first attempt but succeeds on retry.

**Source:** `core/deployment_manager.py` lines 394–499

### vLLM Model Loading Detection

After pods reach Running state, the system waits for vLLM to finish loading the model:

- Streams pod logs looking for vLLM's "Application startup complete" or "Uvicorn running" messages
- Extracts profiled memory data from logs: `vllm_available_kv_gb`, `vllm_fixed_overhead_gb`, `vllm_gpu_blocks`
- These profiled values replace heuristic estimates in subsequent optimization steps

**Source:** `core/deployment_manager.py` lines 316–392

---

## Metrics Collection & Analysis

### Prometheus Query Categories

The system collects metrics from two sources during each benchmark:

**GPU Metrics (DCGM Exporter):**
- `DCGM_FI_DEV_GPU_UTIL` — GPU compute utilization percentage
- `DCGM_FI_DEV_MEM_COPY_UTIL` — Memory copy utilization
- `DCGM_FI_DEV_FB_USED` — Framebuffer used (MB)
- `DCGM_FI_DEV_POWER_USAGE` — Power consumption (watts)

**vLLM Metrics:**
- TTFT: `time_to_first_token_seconds` (P50/P90/P95/P99 via histogram_quantile)
- ITL: `inter_token_latency_seconds` (same percentiles)
- E2E latency: `e2e_request_latency_seconds`
- Scheduler: `num_requests_running`, `num_requests_waiting`, `kv_cache_usage_perc`
- Throughput: `prompt_tokens_total`, `generation_tokens_total` (rate)
- Prefix cache: `prefix_cache_hits_total`, `prefix_cache_queries_total`
- Timing breakdown: `request_prefill_time`, `decode_time`, `queue_time`

**Inference Gateway Metrics:**
- Request duration buckets, count, sum
- Output tokens, request errors
- Running requests, queue size, KV cache utilization

**Source:** `core/metrics_collector.py` lines 117–317

### Rate Window Calculation

The Prometheus rate() window is derived from benchmark duration: `duration / 8`, with a minimum of 120 seconds. Since PodMonitor scrape interval is 30 seconds and `rate()` needs at least 2 data points (60s minimum), a 4× buffer ensures reliable rate calculations.

**Source:** `core/metrics_collector.py` lines 99–115

### Per-Pod Analysis

Metrics are grouped by `exported_pod` label to provide per-pod GPU utilization, memory, and throughput breakdowns. Total throughput is distributed equally across pods when guidellm reports aggregate numbers.

**Source:** `core/metrics_analyzer.py` lines 253–328

---

## Prefix Cache Simulation

### Purpose

Tests how well the inference setup handles repeated or similar prompts — a common pattern in production (system prompts, FAQ responses, RAG pipelines). Generates synthetic datasets that simulate specific cache hit patterns.

### Three Simulation Modes

**1. Identical Mode** (default)
- `hit_pct`% of requests use an identical shared prompt
- Remaining requests use unique prompts
- Simulates: FAQ bots, repeated queries

**2. Shared Prefix Mode**
- All prompts share the first `hit_pct% × ISL` tokens as a common prefix
- Each prompt has a unique suffix
- Simulates: System prompt + user message pattern (chatbots, RAG)

**3. Multi-Group Mode**
- Divides dataset into `N` distinct prompt groups (default 5)
- Each group contains `(hit_pct% × pool_size) / N` identical prompts
- Remaining prompts are unique
- Simulates: Multi-tenant platforms where requests cluster around tenant-specific prompts

### Dataset Generation

**Pool sizing:** Estimates available prefix cache capacity from GPU VRAM:
```
available_cache = (total_gpus × gpu_vram × 0.9 - model_size) GB
cacheable_sequences = available_cache / (ISL × 0.5KB per token)
pool_size = 1.5 × cacheable_sequences  (ensures cache eviction)
```
Capped at 10,000 rows.

**Prompt generation:** Uses model tokenizer (`AutoTokenizer`) when available, falls back to random ASCII words. Word count = `target_tokens × 1.3` to account for subword tokenization.

**Variance support:** ISL/OSL standard deviation generates per-request length from normal distribution. Each row stores its actual `output_tokens_count`.

**Deterministic seeding:** All randomness seeded from a hash of: `model_name:isl:osl:hit_pct:isl_stdev:osl_stdev:cache_mode:groups`. Seed is stored in the database for reproducibility across resumed runs.

**Storage:** JSONL format at `{storage}/prefix-cache-datasets/prefix-cache-{mode}-{seed}.jsonl`. Reused if already exists.

**Source:** `core/optimizer/dataset.py` lines 13–201

---

## Template System

### Manifest Templates

Kubernetes manifests are generated from Jinja2 templates for three architectures:

| Architecture | Templates |
|-------------|-----------|
| Aggregated | `aggregated/lws.yaml.j2`, `aggregated/service.yaml.j2` |
| Prefill/Decode | `pd/prefill-lws.yaml.j2`, `pd/decode-lws.yaml.j2`, `pd/prefill-service.yaml.j2`, `pd/decode-service.yaml.j2` |
| Expert Parallelism | `ep/lws.yaml.j2`, `ep/service.yaml.j2` |
| Prerequisites | `prereq/` — Gateway, EPP, RBAC, model download, PVC |

### Role-Specific Configuration

For PD architecture, templates receive role-specific values:
- `prefill_tp` and `decode_tp` can differ (asymmetric TP)
- `prefill_gpu_memory_utilization` and `decode_gpu_memory_utilization` can differ
- `prefill_max_num_seqs` and `decode_max_num_seqs` calculated independently

### Configurable Parameters

All templates support:
- `block_size`, `gpu_memory_utilization`, `max_model_len`, `max_num_seqs`
- `enable_prefix_caching`, `disable_custom_all_reduce`, `dtype`, `kv_cache_dtype`
- `vllm_debug_logs` (VLLM_LOGGING_LEVEL), `nccl_debug_logs` (NCCL_DEBUG)
- Node pinning via `selected_nodes`
- RDMA device resources (auto-detected per cloud provider)
- EPP configuration injection

**Source:** `core/template_manager.py` lines 88–217

---

## System Design Patterns

### Fallback Chains

The system uses cascading fallback chains throughout to ensure robustness:

| Component | Primary | Fallback 1 | Fallback 2 |
|-----------|---------|------------|------------|
| Model config | HuggingFace API | Local PVC | Hardcoded lookup table |
| GPU VRAM | Node label | Model-based lookup | 80GB default |
| NIC count | Port labels | Speed labels | GPU count (1:1) |
| vLLM memory | Profiled from logs | Heuristic calculation | Conservative defaults |
| Cloud provider | OpenShift infrastructure | Node labels | Bare metal |

### Deterministic Reproducibility

- Prefix cache datasets seeded from config hash
- Seeds stored in database
- Resumed runs reconstruct identical datasets
- Test IDs encode full configuration for traceability

### Auto-Adaptation

The system adapts to the deployment environment without manual configuration:
- Cloud provider → network type → RDMA resources
- GPU model → VRAM → minimum TP → valid TP options
- Model architecture → KV cache size → memory utilization → max sequences
- Workload profile → block size → batch tokens → EPP weights
