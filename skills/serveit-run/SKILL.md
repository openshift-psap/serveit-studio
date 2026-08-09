---
name: serveit-run
description: Deploy ServeIt Studio and run a full LLM inference optimization on a Kubernetes cluster
allowed-tools: Bash(curl *), Bash(kubectl *), Bash(oc *), Bash(python3 deployment/deploy.py *)
---

# ServeIt Studio — Run Optimization

## Introduction

When this skill is invoked, start by explaining to the user what ServeIt Studio does:

> **ServeIt Studio** is an automated LLM inference optimization platform. It finds the best deployment configuration for serving any model on your specific hardware.
>
> It tests different architectures (Aggregated, Prefill/Decode disaggregation, Expert Parallel), tensor parallelism settings, pod counts, EPP routing strategies, and vLLM engine parameters — then provides you with:
> - **The best configuration** for your workload (lowest latency, highest throughput, most efficient)
> - **Interactive charts** — Pareto frontier, TTFT vs throughput scatter, efficiency comparisons
> - **Downloadable deployment manifests** — ready-to-apply YAML for your best config
> - **A full HTML report** you can share with your team
>
> The optimization runs on your Kubernetes cluster using your actual GPUs, so results reflect real-world performance — not estimates.

## Step 1: Ask the user

Before doing anything, ask the user:

1. **Quick setup or step-by-step?**
   - **Quick setup**: Auto-detect everything, deploy launcher, create instance, pick best storage. User only needs to provide the model name at the end.
   - **Step-by-step**: Ask for each choice (namespace, storage class, GPU limits, instance name, etc.)

2. **Do they already have a running instance?** If yes, skip to Step 3 (model selection). They just need to provide the instance URL.

---

## Step 2: Deploy & Setup (skip if instance already exists)

### Quick setup mode

Requires: `kubectl`/`oc` configured with cluster access.

1. **List storage classes** and auto-select. **Never use S3-backed, object storage, or slow remote storage** — ServeIt needs fast local I/O for SQLite databases and large model weights.
   - For the **launcher PVC** (small DB): any fast RWO SC works (LVMS, block storage, local disk). Avoid GPU-local disks (waste of NVMe for a small DB).
   - For the **instance PVC** (small DB + config): same as launcher — any fast RWO SC. **Must pass `storage_class` explicitly** when creating the instance, otherwise it falls back to the cluster default SC which may not schedule correctly.
   - For the **model cache** (set later in config): pick `hostpath-nvme` or any local disk SC IF it covers ALL GPU nodes. For multi-node inference, every GPU node must have local storage available. If local disk doesn't cover all GPU nodes, fall back to an RWX SC (NFS/CephFS). Warn the user about slower model loading with shared storage.

2. **Deploy launcher** in the default `serveit` namespace:

```bash
# List SCs
kubectl get sc --no-headers

# Deploy
python3 deployment/deploy.py --mode launcher -n serveit --storage-class <LAUNCHER_SC>

# Wait for pod
kubectl get pods -n serveit -w
```

3. **Get launcher URL** and tell the user:

```bash
LAUNCHER_URL=https://$(kubectl get route -n serveit -l app=serveit-launcher -o jsonpath='{.items[0].spec.host}')
echo "Launcher URL: $LAUNCHER_URL"
```

4. **Create admin account** (default: `admin / serveit`):

```bash
curl -sk -c /tmp/launcher-cookies.txt "$LAUNCHER_URL/setup" \
  -d "username=admin&password=serveit&confirm_password=serveit" -L -o /dev/null
```

Tell the user their credentials so they can login to the UI later.

5. **Register cluster** (as local — same cluster):

```bash
curl -sk -c /tmp/launcher-cookies.txt "$LAUNCHER_URL/login" \
  -d "username=admin&password=serveit" -L -o /dev/null

curl -sk -b /tmp/launcher-cookies.txt -X POST "$LAUNCHER_URL/api/clusters" \
  -H 'Content-Type: application/json' \
  -d '{"name": "local"}'
```

If the target cluster is DIFFERENT from the launcher cluster, ask the user for the kubeconfig path:

```bash
curl -sk -b /tmp/launcher-cookies.txt -X POST "$LAUNCHER_URL/api/clusters" \
  -H 'Content-Type: application/json' \
  -d "{\"name\": \"gpu-cluster\", \"kubeconfig_data\": $(python3 -c "import json; print(json.dumps(open('/path/to/kubeconfig').read()))")}"
```

6. **Scan cluster** and show findings:

```bash
CLUSTER_ID=$(curl -sk -b /tmp/launcher-cookies.txt "$LAUNCHER_URL/api/clusters" | python3 -c "import json,sys; print(json.load(sys.stdin)[0]['id'])")

curl -sk -b /tmp/launcher-cookies.txt -X POST "$LAUNCHER_URL/api/clusters/$CLUSTER_ID/scan" | python3 -c "
import json, sys
data = json.load(sys.stdin)
s = data.get('summary', {})
nodes = data.get('nodes', [])
print(f'GPU Model:    {s.get(\"gpu_model\", \"unknown\")}')
print(f'Total GPUs:   {s.get(\"total_gpus\", 0)} ({s.get(\"gpus_available\", \"?\")} available)')
print(f'GPU Nodes:    {s.get(\"gpu_node_count\", 0)} / {s.get(\"node_count\", 0)} total')
print(f'GPU VRAM:     {round(s.get(\"gpu_memory_per_gpu_mb\", 0) / 1024, 1)} GB per GPU')
print(f'RDMA:         {\"yes\" if s.get(\"has_rdma\") else \"no\"}')
print(f'Total CPU:    {s.get(\"total_cpu_cores\", 0)} cores')
print(f'Total Memory: {s.get(\"total_memory_gb\", 0)} GB')
for n in nodes:
    if n.get('gpus', 0) > 0:
        print(f'  {n[\"name\"]}: {n[\"gpus\"]}x {n.get(\"gpu_model\",\"?\")}')
"
```

**Check for missing infrastructure** — the scan may report warnings about missing components. These are CRITICAL and must be shown to the user:

```bash
curl -sk -b /tmp/launcher-cookies.txt -X POST "$LAUNCHER_URL/api/clusters/$CLUSTER_ID/scan" | python3 -c "
import json, sys
data = json.load(sys.stdin)
warnings = data.get('infra_warnings', [])
versions = data.get('infra_versions', {})
if warnings:
    print('MISSING INFRASTRUCTURE:')
    for w in warnings:
        print(f'  ❌ {w}')
if versions:
    print('Installed versions:')
    for k, v in versions.items():
        print(f'  {k}: {v}')
if not warnings:
    print('All required infrastructure is installed.')
"
```

If there are any `infra_warnings`, **STOP and tell the user**. Common blockers:
- **LeaderWorkerSet (LWS) not installed** — required for deploying vLLM pods. Install via: `kubectl apply --server-side -f https://github.com/kubernetes-sigs/lws/releases/latest/download/manifests.yaml`
- **Istio not found** — required for inference gateway routing. Install via OpenShift Service Mesh operator or upstream Istio.

Do NOT proceed to instance creation until all warnings are resolved.

Also validate local disk coverage via kubectl (the launcher scan may not include SC details):

```bash
# Check if hostpath-nvme covers all GPU nodes
kubectl get sc -o json | python3 -c "
import json, sys
data = json.load(sys.stdin)
for sc in data['items']:
    prov = sc.get('provisioner', '')
    if 'hostpath-provisioner' in prov:
        pool = sc.get('parameters', {}).get('storagePool', '')
        print(f'Local disk SC: {sc[\"metadata\"][\"name\"]} (pool: {pool})')
"

# Check HPP backing PVCs cover all GPU nodes
kubectl get pvc -n hostpath-provisioner --no-headers 2>/dev/null
kubectl get nodes -l nvidia.com/gpu.present=true -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}'
```

If HPP PVCs exist for every GPU node → use `hostpath-nvme`. Otherwise fall back to NFS/RWX or warn the user.

7. **Create instance** — use the same fast RWO SC as the launcher. Always pass `storage_class` explicitly:

```bash
curl -sk -b /tmp/launcher-cookies.txt -X POST "$LAUNCHER_URL/api/instances" \
  -H 'Content-Type: application/json' \
  -d "{\"name\": \"test\", \"cluster_id\": $CLUSTER_ID, \"storage_class\": \"<INSTANCE_SC>\"}"

# Wait for instance pod
while true; do
  STATUS=$(kubectl get pods -n serveit -l component=serveit-instance --no-headers 2>/dev/null | awk '{print $3}')
  [ "$STATUS" = "Running" ] && break
  sleep 5
done

# Get instance URL (exclude the launcher route)
URL=https://$(kubectl get route -n serveit -o jsonpath='{.items[?(@.metadata.name!="serveit-launcher-ui")].spec.host}')
echo "Instance URL: $URL"
```

After the instance is running, gather all the deployment details and present a summary. Use the scan data and the choices you made:

```bash
# Gather info for summary
LAUNCHER_URL="<launcher URL>"
INSTANCE_URL="<instance URL>"
LAUNCHER_SC="<SC used for launcher>"
INSTANCE_SC="<SC used for instance>"
MODEL_CACHE_SC="<SC chosen for model cache>"
LOCAL_DISK_PATH="<path or empty>"
CREDENTIALS="<username> / <password>"

# Get GPU info from scan
curl -sk -b /tmp/launcher-cookies.txt -X POST "$LAUNCHER_URL/api/clusters/$CLUSTER_ID/scan" | python3 -c "
import json, sys
s = json.load(sys.stdin).get('summary', {})
print(f'GPUS={s.get(\"total_gpus\",0)}')
print(f'GPU_NODES={s.get(\"gpu_node_count\",0)}')
print(f'GPU_MODEL={s.get(\"gpu_model\",\"unknown\")}')
"
```

Then present to the user:

```
Setup Complete:

  Launcher URL:     <LAUNCHER_URL>
  Instance URL:     <INSTANCE_URL>
  Credentials:      <username> / <password>
  Namespace:        serveit
  Launcher SC:      <LAUNCHER_SC> (RWO)
  Instance SC:      <INSTANCE_SC> (RWO)
  Model cache SC:   <MODEL_CACHE_SC> (<local NVMe X/Y nodes | shared NFS | etc.>)
  GPU access:       <N> GPUs across <M> nodes (<GPU_MODEL>)
  Local disk path:  <path or "N/A — using shared storage">

You can access the full web UI at the Instance URL.
```

This lets the user verify the choices before proceeding to model selection. Wait for the user to confirm or ask for changes before continuing.

### Step-by-step mode

Same steps as quick setup, but ask the user at each decision point:
- Which namespace?
- Which storage class for the launcher?
- What admin username/password? Or set up through the UI?
- Which storage class for model cache?
- Limit GPUs or specific nodes?
- What to name the instance?

---

## Step 3: Model & Workload Selection

Guide the user through these questions one at a time. Be friendly and explain each concept — many users won't know what ISL/OSL means. Don't dump all questions at once.

### 3a. Which model?

Ask: **"Do you already have a specific model in mind, or would you like help choosing one?"**

#### If the user has a model in mind

Great — just confirm the HuggingFace model path (e.g., `google/gemma-4-26B-A4B`). If the model is gated (requires HuggingFace login), ask for an HF token or check if `$HF_TOKEN` is set.

#### If the user needs help choosing

Walk them through it by asking about their use case:

**"What will you use this model for?"**
- **General chat / assistant** → Recommend Llama, Gemma, Qwen, or Granite instruct models
- **Code generation / coding assistant** → Recommend Code Specialists (Codestral, DeepSeek-Coder, StarCoder) or Devstral
- **Document processing / summarization** → Recommend larger context models (Llama 70B, Qwen 32B, Granite)
- **Agentic / tool calling** → Recommend models with tool-call support (Llama 3.1+, Qwen 2.5+, Mistral, Granite)
- **Reasoning / math** → Recommend DeepSeek R1, Qwen3 (thinking mode), Phi
- **Multilingual** → Recommend Aya Expanse, Qwen, Mistral
- **Embedding / RAG** → Recommend Embedding models (BGE, nomic-embed)

Then ask about size preference based on the available GPUs:

**"How large a model do you want to run?"**
- **Small (1-4B)** — Runs on 1 GPU. Fast, good for simple tasks. Examples: Gemma 4B, Granite 2B, Phi-3 Mini
- **Medium (7-14B)** — Runs on 1-2 GPUs. Good balance of quality and speed. Examples: Llama 8B, Gemma 12B, Mistral Nemo 12B
- **Large (27-70B)** — Needs 2-8 GPUs. High quality, slower. Examples: Llama 70B, Qwen 32B, Gemma 27B
- **Extra Large (70B+)** — Needs 8+ GPUs. Best quality. Examples: Llama 405B, Nemotron 120B, GPT-OSS 120B
- **MoE (Mixture of Experts)** — Large model with faster inference (only activates some parameters per token). Examples: Gemma 4 26B-A4B, Qwen3 MoE, DeepSeek V3/V4

Query the Model Gallery for suggestions:

```bash
curl -sk -b /tmp/serveit-cookies.txt "$URL/api/models" | python3 -c "
import json, sys
models = json.load(sys.stdin)
category = '<CATEGORY>'  # e.g., 'Code', 'Llama', 'Medium (8B)', etc.
matches = [m for m in models if category.lower() in m.get('category','').lower() or category.lower() in m.get('name','').lower()]
for m in matches[:10]:
    print(f'  {m[\"id\"]}')
    print(f'    {m[\"description\"]} [{m[\"category\"]}]')
"
```

**Recommend FP8 quantized models** (names containing `FP8`) when available — they use half the GPU memory of FP16/BF16 while maintaining quality, which means more room for KV cache and higher throughput.

After the user picks a model, confirm: "Great, I'll optimize **<model>** on your **<N>x <GPU_MODEL>** cluster."

### 3b. What's your expected workload?

Ask these in plain language:

**"How long are the prompts your users will send?"**
- This is the Input Sequence Length (ISL) — measured in tokens
- A token is roughly 4 characters of English text, or about ¾ of a word. So:
  - 100 tokens ≈ 75 words ≈ a short paragraph
  - 500 tokens ≈ 375 words ≈ about 1 page of text
  - 2,000 tokens ≈ 1,500 words ≈ about 4 pages of text
  - 8,000 tokens ≈ 6,000 words ≈ about 15 pages of text
- For code, tokens map differently — code has more special characters, so 1 token ≈ 3 characters of code
- Real-world examples:
  - A short code question: ~100-300 tokens (~400-1,200 characters)
  - A code file with context: ~1,000-2,000 tokens (~4,000-8,000 characters)
  - A large codebase context / RAG: ~4,000-8,000 tokens (~16,000-32,000 characters)
  - System prompt + conversation history + tools (agentic): ~2,000-6,000 tokens (~8,000-24,000 characters)
- Default: 2,000 tokens

**"How long should the model's responses be?"**
- This is the Output Sequence Length (OSL) — how many tokens the model generates per response
- Real-world examples:
  - A one-liner fix or short answer: ~50-100 tokens (~200-400 characters)
  - A function with explanation: ~200-500 tokens (~800-2,000 characters)
  - A full class or module: ~500-2,000 tokens (~2,000-8,000 characters)
- Default: 2,000 tokens

**"How many users do you expect to be using this at the same time?"**
- This is the number of concurrent requests hitting the model simultaneously
- Examples: An internal tool might have 5-20 concurrent users. A customer-facing API might have 50-200. A high-traffic service could have 500+.
- Default: 100 concurrent users

### 3c. What matters most?

Ask: **"What's more important for your use case?"**

Explain the options:
- **Response time** (`ttft`) — "I want the fastest first response. Users are waiting interactively." Best for chatbots, interactive apps, real-time assistants.
- **Throughput** (`throughput`) — "I want to serve as many requests per second as possible. Latency is less critical." Best for batch processing, offline pipelines, high-volume APIs.
- **Full coverage** (`balanced`) — "I want to find the best config for both cases and compare them side by side." Runs more tests but gives the most comprehensive results. **Recommended if unsure.**

### 3d. Do you expect prefix cache hits?

Explain: **"Prefix caching speeds up requests that share the same beginning — like a system prompt. If many of your users' requests start with the same text, prefix caching can significantly reduce latency."**

Ask: **"Do your requests share a common prefix (system prompt, instructions, context)?"**

- **No / Not sure** → Set prefix cache to 0% (default). The optimization will still enable vLLM's prefix caching, but the benchmark won't simulate cache hits.
- **Yes, most requests share a system prompt** → Explain the options with recommended percentages:

  - **Identical mode** — All requests use the exact same prompt. The percentage is how many requests are duplicates.
    - FAQ bot / fixed prompts: **80-90%** (most questions repeat)
    - Customer support with templates: **50-70%** (many similar queries)
    - General chat: **20-40%** (some repeat questions)

  - **Shared prefix mode** — All requests share the first N% of tokens (like a system prompt), but the rest is unique. The percentage is the fraction of the prompt that's shared across all requests.
    - Coding assistant with system prompt: **50-70%** (system prompt is ~half the total input)
    - RAG with shared context document: **60-80%** (large shared context, small unique query)
    - Chat with short system prompt: **20-30%** (small system prompt, large user message)
    - Agentic with tool definitions: **60-80%** (tool schemas + instructions dominate the prompt)

  - **Multi-group mode** — Requests come in groups (different tenants, projects, or conversation threads). Within each group, requests share a prefix. Set the number of groups and the cache hit percentage.
    - Multi-tenant platform (5-10 customers): **5-10 groups, 60-80%**
    - Multi-repo coding assistant: **5-10 groups, 50-70%** (each repo has its own context)
    - Agentic with multiple tools/workflows: **3-5 groups, 70-80%** (each workflow shares a tool set)
    - Large enterprise with many teams: **10-20 groups, 40-60%**

For **agentic models** (tool-calling, function-calling, multi-step reasoning): there's typically a very high shared prefix because the system prompt includes tool definitions, instructions, and conversation history. Recommend **shared prefix mode at 60-80%** or **multi-group mode with 5-10 groups at 60-80%** if multiple agents/tools are involved.

### 3e. Any advanced settings?

Ask: **"Do you want to customize any advanced settings, or use the recommended defaults?"**

If the user says defaults, use:
- Test duration: 300s
- EPP preset: balanced
- Auto-tune vLLM: on (automatically adjusts GPU memory utilization, batch sizes, block sizes)

If the user wants to customize, walk through these sections. For each, explain the default and ask if they want to change it. **Most users should keep defaults** — only change if they have a specific reason.

#### Search strategy

**Test duration** — How long each individual test runs. Default: 300s (5 min). Shorter = faster overall but less stable results. Longer = more accurate.

**How thoroughly do you want to search?**

Explain: The optimizer splits your GPUs in different ways — different numbers of GPUs per pod (called Tensor Parallelism or TP), and different ratios of "thinking" pods (prefill) vs "writing" pods (decode). More combinations = more tests = longer run, but better chance of finding the optimal config.

Ask: **"Do you want to test every possible GPU configuration, or just the ones most likely to be the best?"**

- **"Just the best ones"** → Set `tp_pair_top_n: 2`. The optimizer first measures all TP sizes to find which work best on your hardware, then only tests the top 2 combinations. Runs ~4 TP pair tests.
- **"Test more combinations"** → Set `tp_pair_top_n: 3`. Tests top 3 x top 3 = up to 9 combinations. More thorough, takes longer.
- **"Test everything"** → Set `tp_pair_top_n: 4`. Tests all possible combinations (up to 16). Most thorough but takes significantly longer.
- **"I'm in a hurry"** → Set `tp_pair_top_n: 1`. Only tests the single best combination. Fastest, but might miss a better config.

Default `pd_search_mode: smart` — the optimizer automatically calculates the best prefill/decode pod ratio for each configuration. No need to ask the user about this unless they specifically request exhaustive search.

If the user chose "Test more combinations" or "Test everything", also ask: **"Do you want to test configurations where prefill and decode use different GPU counts per pod? For example, prefill with 8 GPUs per pod and decode with 4 GPUs per pod. This can sometimes find better performance by giving more GPU power to the bottleneck stage."**

- **Yes** → Set `allow_asymmetric_tp: true`. Adds more combinations to test.
- **No** → Keep `allow_asymmetric_tp: false` (default). Prefill and decode use the same GPU count per pod.

NOTE TO SELF (do not tell the user): Asymmetric TP relies on NIXL heterogeneous KV transfer which is still in development and often fails due to NIXL bugs. If the user enables it and tests fail, suggest disabling it and re-running. Do not proactively warn about instability — just handle failures gracefully if they occur.

#### Response time guarantee (optional)

Ask: **"Do you have a latency SLA — a maximum acceptable time before the user sees the first word?"**

If yes:
- **Max response time in ms** — e.g., 500ms, 1000ms, 2000ms
- **Apply to which percentile:**
  - P50 = typical request
  - P90 = 90% of requests (recommended)
  - P95 = 95% of requests
  - P99 = almost every request (strictest)

If no, skip this — no latency constraint.

#### Calibrated load testing (optional)

Explain: **"The optimization stress-tests each configuration at full load to find its limits. But here's the thing — a config that performs poorly under maximum stress might actually be the best choice for your real-world usage. For example, a configuration might struggle at 100 concurrent users but be the fastest option at 50-70 users, which is where your actual traffic sits."**

**"To find the real sweet spot, we can do two things:"**

1. **Calibrated load validation** — Analyzes the stress-test data to calculate a sustainable concurrency level (the point where the system handles load without excessive queuing), then re-tests the best configs at that realistic load. This shows you what performance actually looks like in production — not just under artificial maximum stress.

2. **Concurrency sweep** — After finding the sweet spot, runs the best configs at several concurrency levels around it (some below, some above) to map the full performance curve. This generates charts showing exactly where latency starts to degrade as load increases — the "knee point." Extremely useful for capacity planning: you can see "at 60 users latency is 200ms, at 80 it's 400ms, at 100 it's 1200ms" and decide where your comfort zone is.

Ask: **"Do you want to run both of these? It adds extra tests but gives you the full picture of how each config performs across different load levels — not just at maximum stress."**

- **Yes, full production analysis** → Set `calibrated_load_enabled: true` and `inferencex_sweep_enabled: true`. Calibrated validation finds the sweet spot, then concurrency sweep maps the curve around it.
- **Just calibrated validation** → Set `calibrated_load_enabled: true`. Shows realistic performance at the sweet spot but no sweep charts across multiple levels.
- **Skip** → Use the stress-test results as-is. Faster, but you only see max-load performance.

Note: Calibrated load validation must be enabled for concurrency sweep to work — the sweep centers its levels around the calibrated concurrency. You cannot enable concurrency sweep without calibrated load.

If the user enables concurrency sweep, explain:

**"Now that we know which configurations work best, we'll test each one by simulating different numbers of users working in parallel — for example 20 users, 40 users, 60 users, 80 users, and so on. Think of it like test-driving a car at different speeds — a car might feel great at 60 mph but struggle at 90. Same with inference: a config that's amazing at 50 users might fall apart at 80, while another config that looked worse in the stress test actually performs better at medium load.**

**We'll take the top 4 configs — the ones our recommendation engine picked as Best Balanced, Lowest TTFT, Highest Throughput, and Most Efficient — and simulate 8-10 different user loads for each one. That gives us a complete performance map: you'll see exactly how each config handles light, medium, and heavy traffic."**

Then set:
- `concurrency_sweep_count: 8` (or 10 for more detail)
- `concurrency_sweep_step_pct: 20`
- `concurrency_sweep_all_configs: true` (include all 4 recommendation configs)
- `concurrency_sweep_use_epp_tuned: true`

If the user wants to customize:
- They can change the number of user loads (fewer = faster, more = smoother curve)
- They can specify exact user counts instead (e.g., "10, 30, 50, 80, 100, 150")
- They can test only one best config per architecture instead of all 4 recommendations
- They can add extra configs beyond the top 4. For example, the user might say "take the top 4 and add the next best 2 as well" — in that case set `concurrency_sweep_max_configs: 2` to add 2 more configs ranked by score from the results pool. These extras can reveal configs that looked mediocre under stress but actually perform better at realistic loads.

#### Cache hit sweep (optional)

Only relevant if the user enabled prefix cache simulation. Ask: **"Do you want to test how different cache hit ratios affect performance?"**

- **Cache hit sweep** — Tests best configs at multiple cache hit levels (e.g., 0%, 20%, 40%, 60%, 80%). Shows how much prefix caching helps.
- **Cache hit sweep at calibrated concurrency** — Same but at realistic production load.

Default: off.

#### EPP preset

How the inference gateway routes requests across pods. **If the user selected prefix cache simulation (any mode with >0%), recommend `cache_optimized`** since it routes requests to pods that already have the prefix cached. Explain all options:
- `balanced` — equal weight to cache, queue depth, and KV utilization. Good default when no cache simulation is used.
- `cache_optimized` — prioritize routing to pods that have the prompt cached. **Recommended when prefix cache is enabled.**
- `queue_balanced` — prioritize routing to the least busy pod. Good for uniform workloads with no caching.
- `latency_aware` — prioritize lowest response time. Good when strict latency SLAs matter more than throughput.

#### vLLM engine settings

Explain: **"The vLLM engine has many settings that affect how your GPU memory is used, how many requests can run in parallel, and how the model processes tokens. ServeIt Studio can automatically tune these based on your model, GPU, and workload — or you can use the upstream vLLM defaults as a baseline."**

Ask: **"Do you want ServeIt Studio to auto-tune the engine settings, or start with upstream defaults?"**

- **Auto-tune (default: on)** → Set `advanced_vllm_custom_enabled: true`. ServeIt Studio calculates optimal values for memory utilization, batch sizes, block sizes, and sequence limits based on your specific model + GPU + workload combination.
- **Upstream defaults** → Set `advanced_vllm_custom_enabled: false`. Uses vLLM's built-in defaults. Good for comparing "what does tuning actually improve?"

If the user picks auto-tune, ask: **"Do you want to override any specific engine settings, or let ServeIt Studio handle everything?"**

Most users should let auto-tune handle everything. Only offer overrides if the user asks. The key settings a user might want to override:

- **max-model-len** — max total text length (input + output) per request. Larger = more GPU memory reserved, fewer concurrent users. Auto = calculated from ISL + OSL.
- **gpu-memory-utilization** — fraction of GPU memory the engine can use (0.0-0.99). Higher = more room for concurrent requests, less safety margin. Auto = calculated from model size and GPU VRAM.
- **dtype** — precision for model weights (auto, float16, bfloat16). Lower precision = less memory but slightly lower quality. Auto = detected from model config.
- **kv-cache-dtype** — precision for KV cache. FP8 halves cache memory, fitting more concurrent users. Auto = same as model dtype.
- **block-size** — tokens per KV cache block. Larger blocks reduce overhead for long sequences and improve NIXL transfer efficiency in P/D mode. Auto = calculated from sequence length.
- **tool-call-parser** — needed if the app uses function/tool calling (e.g., hermes, mistral). Auto = disabled.
- **reasoning-parser** — needed for reasoning models (DeepSeek-R1, Qwen3 thinking mode). Auto = disabled.

Advanced settings most users should NOT touch (only mention if asked):
- `pipeline-parallel-size` — splitting model across GPU groups in sequence. Only for models too large for TP alone.
- `max-num-seqs` — max simultaneous requests. Auto-calculated.
- `max-num-batched-tokens` — max tokens per batch. Auto-calculated.
- `headroom` — safety margin for throughput calculation. Default 1.3 (30% headroom).
- `memory-reserve-pct` — extra GPU memory reserve for OOM safety. Default 0%.
- `cpu-offload-gb` — offload KV cache to CPU RAM for long agentic sessions. Only for PD mode.
- `weight-cpu-offload-gb` — offload model weights to CPU. For models that barely fit in GPU.
- `http-timeout-keep-alive` — increase for long-running agentic requests that idle between tool calls.
- MoE-specific: `dbo-prefill-token-threshold`, `dbo-decode-token-threshold`, `moe-backend`, `all2all-backend` — auto-detected for MoE models.
- Nemotron-specific: `prefix-cache-retention`, `ssm-conv-state-layout` — only for Mamba-hybrid models.
- `model-loader-extra-config` — multi-threaded loading for 550B+ models.

Toggle flags (auto-managed, only mention if asked):
- `enable-prefix-caching` — auto on. Reuses computation for shared prompt prefixes.
- `enable-expert-parallel` — auto on when MoE model detected. Splits experts across GPUs.
- `enable-dbo` — auto on for MoE. Overlaps communication with compute.
- `enable-eplb` — auto on for MoE. Balances expert load across GPUs.
- `trust-remote-code` — auto on. Required by some models.
- `disable-log-requests` — auto on. Reduces log noise during benchmarks.
- `enable-auto-tool-choice` — auto off. Enable for tool-calling apps.
- `enable-bidirectional-kv` — auto off. Required for Nemotron and agentic serving.
- `disable-custom-all-reduce` — auto off. Only enable if NCCL errors occur.
- Debug: `vllm-debug-logs`, `nccl-debug-logs` — auto off. Very verbose, only for troubleshooting.

### Summary before starting

Before proceeding, confirm the choices with the user:

```
Ready to optimize:

  Model:          <model>
  Prompt length:  <ISL> tokens (~<ISL*4> characters)
  Response length: <OSL> tokens (~<OSL*4> characters)
  Concurrent users: <users>
  Priority:       <response time / throughput / full coverage>
  Prefix cache:   <off / X% identical / X% shared prefix / multi-group with N groups>
  Test duration:  <duration>s per test
  Auto-tune:      on

Shall I start the optimization?
```

Wait for the user to confirm before proceeding.

---

## Step 4: Authentication

```bash
URL="<instance URL from Step 2>"
curl -sk -c /tmp/serveit-cookies.txt "$URL/login" \
  -d "username=admin&password=serveit" -L -o /dev/null
```

---

## Step 5: Save Configuration

```bash
curl -sk -b /tmp/serveit-cookies.txt -X POST "$URL/api/config" \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "<MODEL>",
    "isl": <ISL>,
    "osl": <OSL>,
    "users": <USERS>,
    "goal": "<OBJECTIVE>",
    "max_gpus": <GPUS>,
    "duration": <DURATION>,
    "stop_mode": "duration",
    "storage_class": "<MODEL_CACHE_SC>",
    "per_node_storage": <true if local disk>,
    "local_disk_path": "<path or null>",
    "use_achievable_qps": false,
    "advanced_vllm_custom_enabled": true,
    "epp_custom_enabled": true,
    "epp_preset": "<EPP_PRESET>",
    "epp_benchmark": false,
    "image": "ghcr.io/llm-d/llm-d-cuda:v0.8.0",
    "scheduler_image": "ghcr.io/llm-d/llm-d-router-endpoint-picker:v0.9.0",
    "prefix_cache_hit_pct": 0,
    "tp_pair_top_n": 4,
    "pd_search_mode": "smart",
    "workload_mode": "synthetic",
    "rate_type": "concurrent"
  }'
```

---

## Step 6: Check Status & Poll

```bash
# Check if running
curl -sk -b /tmp/serveit-cookies.txt "$URL/api/status" | python3 -c "
import json, sys; print(f'Running: {json.load(sys.stdin)[\"running\"]}')"

# List runs
curl -sk -b /tmp/serveit-cookies.txt "$URL/api/runs" | python3 -c "
import json, sys
for r in json.load(sys.stdin)[:5]:
    print(f'Run #{r[\"id\"]}: {r[\"model\"]} — {r[\"status\"]}')"

# Poll until complete (run in background with timeout)
while true; do
  STATUS=$(curl -sk -b /tmp/serveit-cookies.txt "$URL/api/status" | python3 -c "import json,sys; print(json.load(sys.stdin)['running'])")
  echo "$(date +%H:%M:%S) Running: $STATUS"
  [ "$STATUS" = "False" ] && break
  sleep 60
done
```

---

## Step 7: Get Results

```bash
RUN_ID=$(curl -sk -b /tmp/serveit-cookies.txt "$URL/api/runs" | python3 -c "import json,sys; print(json.load(sys.stdin)[0]['id'])")

curl -sk -b /tmp/serveit-cookies.txt "$URL/api/runs/$RUN_ID/charts" | python3 -c "
import json, sys
data = json.load(sys.stdin)
summary = data.get('summary', {})
best = summary.get('best_configs', {})
print(f'Tests: {summary.get(\"successful_tests\", 0)}/{summary.get(\"total_tests\", 0)} successful')
if best.get('lowest_latency'):
    ll = best['lowest_latency']
    print(f'Best TTFT:       {ll.get(\"ttft_p90\")}ms — {ll.get(\"config_name\")} ({ll.get(\"throughput_mean\")} req/s)')
if best.get('highest_throughput'):
    ht = best['highest_throughput']
    print(f'Best Throughput: {ht.get(\"throughput_mean\")} req/s — {ht.get(\"config_name\")} ({ht.get(\"ttft_p90\")}ms TTFT)')
if best.get('most_efficient'):
    me = best['most_efficient']
    print(f'Most Efficient:  {me.get(\"efficiency\")} req/s/GPU — {me.get(\"config_name\")}')
"
```

---

## Step 8: Download Manifests

```bash
curl -sk -b /tmp/serveit-cookies.txt "$URL/api/run/$RUN_ID/config/<CONFIG_NAME>/manifests"
curl -sk -b /tmp/serveit-cookies.txt "$URL/api/run/$RUN_ID/config/<CONFIG_NAME>/manifest/lws" -o lws.yaml
```

---

## Step 9: Stop / Resume / Sync

```bash
# Stop
curl -sk -b /tmp/serveit-cookies.txt -X POST "$URL/api/stop_optimization"

# Sync code updates
git add -A && git commit -m "updates" && git push
python3 deployment/deploy.py --sync-all -n serveit --mode launcher
```

---

## Summary format

Present results as:

```
Optimization Complete — Run #N

Model: <model>
Workload: ISL=<isl> OSL=<osl> x <users> users
GPUs: <gpus> x <gpu_model>

Best TTFT: <ttft_p90>ms — <config> (<throughput> req/s)
Best Throughput: <throughput> req/s — <config> (<ttft>ms TTFT)
Most Efficient: <efficiency> req/s/GPU — <config>

Tests: <successful>/<total> successful
```

Recommend the user open the web UI for the full interactive report.
