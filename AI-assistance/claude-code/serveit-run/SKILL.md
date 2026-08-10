---
name: serveit-run
description: Deploy ServeIt Studio and run a full LLM inference optimization on a Kubernetes cluster
allowed-tools: Bash(curl *), Bash(kubectl *), Bash(oc *), Bash(python3 deployment/deploy.py *)
---

# ServeIt Studio — Run Optimization

## Introduction

When this skill is invoked, start by explaining to the user what ServeIt Studio does:

> **ServeIt Studio** is an automated LLM inference optimization platform. It finds the best deployment configuration for serving any model on your specific hardware — extracting the maximum performance out of your cluster.
>
> It tests different architectures (Aggregated, Prefill/Decode disaggregation, Expert Parallel), tensor parallelism settings, pod counts, EPP routing strategies, and vLLM engine parameters — then provides you with:
> - **The best configuration** for your workload (lowest latency, highest throughput, most efficient)
> - **Interactive charts** — Pareto frontier, TTFT vs throughput scatter, efficiency comparisons
> - **Downloadable deployment manifests** — ready-to-apply YAML for your best config
> - **A full HTML report** you can share with your team
>
> The optimization runs on your Kubernetes cluster using your actual GPUs, so results reflect real-world performance — not estimates.

**The launcher is a multi-tenant control plane** — it can manage multiple instances across multiple clusters from a single dashboard. Each instance:
- Connects to a different cluster (or the same cluster with different resource allocations)
- Can be limited to specific GPUs, nodes, or namespaces
- Runs independently — multiple optimizations can run in parallel on different clusters
- Can be backed up and restored — download the database and artifacts, then restore on another instance or after a redeployment
- Supports **HTTPS proxy** for clusters behind a corporate proxy
- Supports **API server IP override** for clusters whose API server isn't resolvable via DNS (e.g., private clusters with IP-only access)

## General rules for interacting with the user

- **Always number options** when presenting choices (1, 2, 3, etc.) so the user can reply with just a number.
- **Always include an "Other" option** as the last choice so the user can provide custom input.
- **One question at a time** — don't dump multiple questions. Wait for the answer before moving on.
- **Explain in plain language** — avoid jargon. When using technical terms, explain what they mean.
- **Show examples** — concrete numbers, real-world scenarios, character counts alongside tokens.

---

## Step 1: Ask the user

Before doing anything, ask the user:

1. **Quick setup or step-by-step?**
   - **Quick setup**: Auto-detect everything, deploy and configure. User only needs to pick the model at the end.
   - **Step-by-step**: Ask for each choice (namespace, storage class, GPU limits, instance name, etc.)

2. **Do they already have a running instance?** If yes, skip to Step 3 (model selection). They just need to provide the instance URL.

3. **Deployment mode** — explain the two options:

   - **Local mode (single instance)** — deploys ServeIt Studio directly as a standalone instance. Simplest setup — one command, no launcher overhead. Best for: single user, single cluster, quick testing.
     ```bash
     python3 deployment/deploy.py --mode local -n serveit --storage-class <SC>
     ```

   - **Launcher mode (multi-tenant control plane)** — deploys a launcher that manages multiple instances across clusters. Each instance can be:
     - Allocated specific GPUs or nodes (resource allocation system)
     - Connected to a different cluster via kubeconfig
     - Backed up and restored — download the database and test artifacts from any instance and restore them on a different instance or cluster. This means you can run tests on cluster A, then restore the results on cluster B to compare, or migrate between environments.
     - Managed from a single dashboard
     Best for: teams sharing GPU resources, multi-cluster setups, production environments where you need backup/restore.

   If the user is unsure, ask: **"Are you the only one using this cluster, or do multiple people/teams need separate optimization environments?"**
   - Single user → suggest **local mode**
   - Multiple users or clusters → suggest **launcher mode**

---

## Step 2: Deploy & Setup (skip if instance already exists)

### Local mode (single instance)

Requires: `kubectl`/`oc` configured with cluster access.

1. **List storage classes** and pick one that has capacity on a Ready node. The instance PVC only needs RWO — any fast block or LVM storage works. Verify the SC has available capacity before deploying (same verification as launcher mode).

```bash
kubectl get sc --no-headers
# Verify SC has capacity on ready nodes
kubectl get pv --no-headers | grep <SC_NAME>

# Deploy directly
python3 deployment/deploy.py --mode local -n serveit --storage-class <SC>

# Get URL
URL=https://$(kubectl get route -n serveit -o jsonpath='{.items[0].spec.host}')
echo "Instance URL: $URL"
```

2. **Create admin account** — same as launcher mode (see below).

3. Skip to Step 3 (model selection) — no cluster registration or instance creation needed.

### Launcher mode (multi-tenant)

Requires: `kubectl`/`oc` configured with cluster access.

1. **List storage classes** and auto-select. **Never use S3-backed, object storage, or slow remote storage** — ServeIt needs fast local I/O for SQLite databases and large model weights.
   - For the **launcher PVC** (small DB): any fast RWO SC works (LVMS, block storage, local disk). Avoid GPU-local disks (waste of NVMe for a small DB).
   - For the **instance PVC** (small DB + config): same as launcher — any fast RWO SC. **Must pass `storage_class` explicitly** when creating the instance, otherwise it falls back to the cluster default SC which may not schedule correctly.
   - For the **model cache** (set later in config): pick `hostpath-nvme` or any local disk SC IF it covers ALL GPU nodes. For multi-node inference, every GPU node must have local storage available. If local disk doesn't cover all GPU nodes, fall back to an RWX SC (NFS/CephFS). Warn the user about slower model loading with shared storage.

Before deploying, verify the chosen storage class actually has capacity and nodes that can use it:

```bash
# Check if the SC has available PVs or can provision on available nodes
kubectl get pv --no-headers | grep <SC_NAME>
kubectl get nodes --no-headers | grep Ready
```

For LVMS/topolvm SCs, verify the volume group exists on at least one Ready node. For hostpath, verify HPP backing PVCs exist on Ready nodes. If the SC has no capacity (e.g., its backing node is down), pick a different one.

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

6. **Scan cluster** — tell the user: **"Scanning your cluster to detect GPUs, networking, storage, and installed infrastructure. This may take a moment..."**

Then show findings:

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

### 3a. HuggingFace token

Before selecting a model, ask: **"Some models on HuggingFace require an access token (gated models like Llama, Gemma, etc.). Do you have a HuggingFace token, or is `$HF_TOKEN` already set in your environment?"**

1. **Yes, here's my token** — the user provides a token string
2. **It's set in my environment** — check `$HF_TOKEN`
3. **No, I'll only use open models** — skip token, only ungated models will work
4. **I don't know** — explain that they can get a token at https://huggingface.co/settings/tokens and that RedHatAI models are generally open (no token needed)

### 3b. Which model?

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

After the user picks a model, **validate it exists on HuggingFace** before proceeding:

```bash
curl -sk -o /dev/null -w "%{http_code}" "https://huggingface.co/api/models/<MODEL_ID>"
```

- **200** — model exists, proceed
- **401** — model is gated, needs HF token. Retry with token: `curl -sk -H "Authorization: Bearer <TOKEN>" ...`
- **404** — model doesn't exist. Check for typos, suggest the closest match from the gallery.

Then confirm: "Great, I'll optimize **<model>** on your **<N>x <GPU_MODEL>** cluster."

### 3c. What's your expected workload?

Ask these in plain language:

**"How long are the prompts your users will send?"**
- This is the Input Sequence Length (ISL) — the length of text a user types, pastes, or sends to the model (including system prompts, code snippets, context). Measured in tokens
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
- Default: 2,000 tokens (input), 2,000 tokens (output)

After the user picks ISL/OSL, ask about variation:

**"Do you want variable-length inputs and outputs? In real life, not every user sends the same amount of text and the model doesn't always respond with the same length. Adding variation makes the benchmark more realistic — some requests will be short, some long, centered around your chosen ISL/OSL."**

1. **Moderate variation** — stdev = 50% of ISL/OSL (e.g., ISL=2000±1000, prompts range ~1000-3000)
2. **High variation** — stdev = 100% of ISL/OSL (e.g., ISL=2000±2000, prompts range ~0-4000)
3. **Light variation** — stdev = 25% of ISL/OSL (e.g., ISL=2000±500, prompts range ~1500-2500)
4. **Custom** — enter specific stdev values
5. **No variation** — fixed length (stdev=0)

**"How long should the model's responses be?"**
- This is the Output Sequence Length (OSL) — the length of text the model generates back to the user
- Real-world examples:
  - A one-liner fix or short answer: ~50-100 tokens (~200-400 characters)
  - A function with explanation: ~200-500 tokens (~800-2,000 characters)
  - A full class or module: ~500-2,000 tokens (~2,000-8,000 characters)
- Default: 2,000 tokens

**"How many users do you expect to be using this at the same time?"**
- This is the number of concurrent requests hitting the model simultaneously
- Examples:
  1. **Internal dev team**: ~5-20 concurrent users
  2. **Company-wide coding assistant**: ~50-100 concurrent users
  3. **Platform API / high-traffic service**: ~200+ concurrent users
  4. **Custom** — enter a specific number
- Default: 100 concurrent users

### 3d. What matters most?

Ask: **"What's more important for your use case?"**

1. **Response time** (`ttft`) — how fast does the first word appear after a user hits send? If your users are staring at their screen waiting for a response (chat, IDE, real-time apps), this is what matters. We'll optimize for the shortest wait time.
2. **Throughput** (`throughput`) — how many requests can the system handle per second? If you care more about serving the most users possible and latency is secondary (batch processing, background pipelines, high-volume APIs), this is your priority.
3. **Full coverage** (`balanced`) — not sure which matters more? We'll test for both and show you the best config for each — so you can compare and decide based on real data. Runs more tests but gives the most complete picture.
4. **Other** — describe your priority.

### 3e. Do you expect prefix cache hits?

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

### 3f. Any advanced settings?

Ask: **"Do you want to customize any advanced settings, or use defaults?"**

List the defaults and all customizable options with numbers:

1. **Use all defaults** — start with the settings listed below
2. **Test duration** — how long each individual test runs (default: 300s / 5 min), or stop after a specific number of requests instead of time
3. **Search depth** — how many GPU configurations to test (default: top 2 per architecture)
4. **Latency SLA** — set a maximum acceptable response time (default: none)
5. **Production load analysis** — re-test at realistic loads + sweep multiple user counts (default: on, top 4 configs at 8 user loads)
6. **Cache hit sweep** — test different cache hit ratios to see impact (default: off)
7. **vLLM auto-tune** — automatically optimize engine settings vs upstream defaults (default: on)
8. **EPP routing** — how the gateway routes requests across pods (default: cache_optimized when prefix cache enabled, balanced otherwise)
9. **Container images** — which vLLM and EPP versions to use (default: latest GA)
10. **Network** — how pods communicate, RDMA options (default: auto-detected from cluster)
11. **Storage** — where model weights are stored (default: auto-detected, prefers local NVMe)
12. **Other** — describe what you want to change

If the user picks a number, jump to that specific section. If they pick 1, use defaults:

If the user says defaults, use:
- Test duration: 300s
- Search depth: top 2 configs per architecture
- P/D search: smart
- Latency SLA: none
- Calibrated load: yes
- Concurrency sweep: yes — after finding the optimal configs, the tool calculates the ideal load for your setup, then tests the top 4 recommendations (Best Balanced, Lowest TTFT, Highest Throughput, Most Efficient) across 8 stress scenarios from light to heavy traffic to show exactly where each config starts to struggle
- EPP tuned sweep: yes
- Cache sweep: no
- EPP preset: `cache_optimized` if prefix cache > 0%, otherwise `balanced`
- Auto-tune vLLM: on (automatically adjusts GPU memory utilization, batch sizes, block sizes)

If the user wants to customize, walk through these sections. For each, explain the default and ask if they want to change it. **Most users should keep defaults** — only change if they have a specific reason.

#### Search strategy

**Test duration** — How long each individual test runs. Default: 300s (5 min). Shorter = faster overall but less stable results. Longer = more accurate.

**How thoroughly do you want to search?**

Explain: The optimizer splits your GPUs in different ways — different numbers of GPUs per pod (called Tensor Parallelism or TP), and different ratios of "thinking" pods (prefill) vs "writing" pods (decode). More combinations = more tests = longer run, but better chance of finding the optimal config.

Ask: **"Do you want to test every possible GPU configuration, or just the ones most likely to be the best?"**

- **"Just the best ones"** → Set `tp_pair_top_n: 2`. The optimizer first measures all GPU-per-pod sizes (TP) to find which work best on your hardware, then picks the top 2 for deeper testing. Each "config" can be a different architecture — Aggregated (all pods do everything), PD (separate prefill and decode pods), or EP (expert parallel for MoE models). So the top 2 might be "Aggregated with 8 GPUs/pod" and "PD with 4 GPUs/pod" for example.
- **"Test more combinations"** → Set `tp_pair_top_n: 3`. Picks the top 3 GPU sizes and crosses them = up to 9 configs across architectures. More thorough, takes longer.
- **"Test everything"** → Set `tp_pair_top_n: 4`. Tests all GPU sizes across all architectures (up to 16 configs). Most thorough but takes significantly longer.
- **"I'm in a hurry"** → Set `tp_pair_top_n: 1`. Only tests the single best GPU size per architecture. Fastest, but might miss a better config.

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

**We'll take the top 4 configs — the ones our recommendation engine picked as Best Balanced, Lowest TTFT, Highest Throughput, and Most Efficient — and run each through 8-10 stress scenarios, from light traffic all the way up to heavy load. That gives us a complete performance map: you'll see exactly where each config starts to struggle and where it shines."**

Then set:
- `concurrency_sweep_count: 8` (8 stress scenarios from light to heavy load)
- `concurrency_sweep_step_pct: 20`
- `concurrency_sweep_all_configs: true` (include the 4 recommendation configs: Best Balanced, Lowest TTFT, Highest Throughput, Most Efficient)
- `concurrency_sweep_max_configs: 0` (default — no extra configs beyond the 4 recommendations. User can increase to add more.)
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

#### EPP (Endpoint Picker) — request routing

Explain: **"When you have multiple pods serving your model, the EPP (Endpoint Picker) decides which pod handles each request. This matters a lot — especially with prefix caching, because sending a request to a pod that already has the prompt prefix cached means it doesn't need to reprocess it, saving time and GPU compute."**

If the user selected prefix cache > 0%, suggest: **"Since you're using shared prefix caching, I'd suggest `cache_optimized` — it routes requests to pods that already have the prefix cached, which maximizes the benefit of caching."**

Presets:
- **`cache_optimized`** — prioritizes routing requests to pods that already have the prompt prefix cached. Best when prefix cache is enabled.
- **`balanced`** — equal weight across cache, memory, queue depth, and active requests. Good default for most workloads.
- **`queue_balanced`** — prioritizes routing to the least busy pod. Best for diverse prompts with low cache hits.
- **`latency_aware`** — uses latency prediction to route to pods that can meet the SLO target. Requires a latency SLA to be set.
- **`custom`** — build your own scoring weights from individual plugins (see below).

If the user wants custom, or wants to understand what the presets actually do under the hood, show the scoring plugins:

**EPP Scoring Plugins:**
| Plugin | Description | Default Weight |
|--------|-------------|---------------|
| `prefix-cache-scorer` | Routes to pods that already have parts of the prompt cached in GPU memory. | 3 |
| `kv-cache-utilization-scorer` | Favors pods with more free GPU memory for KV cache (can handle longer sequences). | 2 |
| `queue-scorer` | Favors pods with shorter request queues. Prevents overloading a single pod. | 2 |
| `latency-scorer` | Uses predicted latency to route to pods that can meet the SLO target. Requires latency SLA to be enabled. | 3 |
| `precise-prefix-cache-scorer` | Tracks KV cache state in real time for exact prefix match scoring. More accurate than approximate prefix-cache-scorer but higher overhead. | 3 |
| `active-request-scorer` | Scores by number of in-flight requests. Load-aware alternative to queue-scorer. | 2 |
| `no-hit-lru-scorer` | For cold requests with no cache hits, ranks pods by least-recently-used to spread load across underutilized pods. | 1 |
| `session-affinity-scorer` | Routes requests from the same session/user to the same pod for better cache reuse across a conversation. | 2 |

Each plugin can be enabled/disabled and given a weight (higher = more influence on routing decisions). For example, a user might say "I want cache-optimized but also add session affinity for my chat app" — in that case set `prefix-cache-scorer: 3, session-affinity-scorer: 2, kv-cache-utilization-scorer: 1`.

#### vLLM engine settings

Explain: **"The vLLM engine has many settings that affect how your GPU memory is used, how many requests can run in parallel, how tokens are batched, and much more. ServeIt Studio can automatically tune all of these based on your model, GPU, and workload — or you can use the upstream vLLM defaults as a baseline."**

Before asking, check if the model is very large and might benefit from offloading. A model needs offloading when it can barely fit in GPU memory:
- On H100 (80GB): models requiring TP8 or higher (roughly 400B+ parameters)
- On H200 (140GB): models requiring TP8 or higher (roughly 550B+ parameters)
- On A100 (80GB): models requiring TP4 or higher (roughly 200B+ parameters)

If the model is in this range, mention: **"This is a very large model. If it's tight on GPU memory, we can offload some model weights or KV cache to CPU RAM to free up space for more concurrent users. Auto-tune will handle this if needed, but let me know if you want to configure it explicitly."**

Ask: **"Do you want ServeIt Studio to auto-tune the engine settings, or start with upstream defaults?"**

- **Auto-tune (default: on)** → Set `advanced_vllm_custom_enabled: true`. ServeIt Studio calculates optimal values for memory utilization, batch sizes, block sizes, sequence limits, and more based on your specific model + GPU + workload combination.
- **Upstream defaults** → Set `advanced_vllm_custom_enabled: false`. Uses vLLM's built-in defaults. Good for comparing "what does tuning actually improve?"

If the user picks auto-tune, ask: **"Do you want to see all available engine settings, or let ServeIt Studio handle everything?"**

If the user wants to see all options, print these tables:

**Core memory & batching:**
| Setting | Description | Auto value |
|---------|-------------|------------|
| `max-model-len` | Max total text length (input + output) per request. Larger = more GPU memory reserved, fewer concurrent users. | Calculated from ISL + OSL |
| `gpu-memory-utilization` | Fraction of GPU memory the engine can use (0.5-0.99). Higher = more room for concurrent requests, less safety margin. | Calculated from model size + GPU VRAM |
| `max-num-seqs` | Max requests processed simultaneously. Too high = OOM, too low = wasted GPU. | Calculated from concurrent users + GPU count |
| `max-num-batched-tokens` | Max tokens in a single batch. Larger batches = better throughput, more memory. | Calculated from workload |
| `block-size` | Tokens per KV cache block. Larger blocks reduce overhead. Must be power of 2. Min 128 for P/D mode. | Calculated from sequence length |

**Precision:**
| Setting | Description | Auto value |
|---------|-------------|------------|
| `dtype` | Model weight precision (auto, float16, bfloat16). Lower = less memory, slightly lower quality. | Detected from model config |
| `kv-cache-dtype` | KV cache precision. FP8 halves cache memory, fitting more concurrent users. | Same as model dtype |

**Parallelism:**
| Setting | Description | Auto value |
|---------|-------------|------------|
| `pipeline-parallel-size` | Split model across GPU groups in sequence. Only for models too large for TP alone. | 1 |

**Tool calling & reasoning:**
| Setting | Description | Auto value |
|---------|-------------|------------|
| `tool-call-parser` | How the engine parses function/tool calls (hermes, mistral, etc.). Only if your app uses tool calling. | Disabled |
| `reasoning-parser` | Chain-of-thought extraction. For reasoning models (DeepSeek-R1, Qwen3). | Disabled |
| `chat-template-content-format` | How chat content is formatted. Some models (GLM) need 'string'. | Auto |

**Performance tuning:**
| Setting | Description | Auto value |
|---------|-------------|------------|
| `headroom` | Safety margin for throughput calculation. Higher = more conservative. | 1.3 (30%) |
| `memory-reserve-pct` | Extra GPU memory reserve for OOM safety. | 0% |
| `http-timeout-keep-alive` | Keep-alive timeout. Increase for agentic requests that idle between tool calls. | 5s |

**Offloading (for very large models):**
| Setting | Description | Auto value |
|---------|-------------|------------|
| `cpu-offload-gb` | Offload KV cache to CPU RAM. Extends prefix cache for long sessions. PD mode only. | Disabled |
| `weight-cpu-offload-gb` | Offload model weights to CPU. Frees GPU for KV cache, slower inference. | Disabled |
| `model-loader-extra-config` | Multi-threaded model loading for 550B+ models. | Disabled |

**MoE-specific (auto-detected for MoE models):**
| Setting | Description | Auto value |
|---------|-------------|------------|
| `dbo-prefill-token-threshold` | Min tokens to trigger Dual Batch Overlap on prefill. | 32 |
| `dbo-decode-token-threshold` | Min tokens to trigger Dual Batch Overlap on decode. | 32 |
| `moe-backend` | MoE expert computation backend (deep_gemm for DeepSeek-style). | deep_gemm when MoE detected |
| `all2all-backend` | MoE all-to-all communication backend. High-throughput for prefill, low-latency for decode. | Auto per role |

**Nemotron / Mamba-hybrid specific:**
| Setting | Description | Auto value |
|---------|-------------|------------|
| `prefix-cache-retention` | Prefix cache retention interval. Set to 0 for Mamba-hybrid models to improve multi-turn cache hit rates. | vLLM default (set 0 for Nemotron) |
| `ssm-conv-state-layout` | SSM convolution state layout. Required for Mamba-hybrid models like Nemotron. | vLLM default (set DS for Nemotron) |

**Toggle flags:**
| Flag | Description | Auto |
|------|-------------|------|
| `enable-prefix-caching` | Reuse computation for shared prompt prefixes. | On |
| `enable-expert-parallel` | Split MoE experts across GPUs. | On when MoE detected |
| `enable-dbo` | Overlap MoE communication with compute. | On when MoE detected |
| `enable-eplb` | Balance expert load across GPUs. | On when MoE detected |
| `trust-remote-code` | Allow custom Python from HuggingFace. Required by some models. | On |
| `disable-log-requests` | Reduce log noise during benchmarks. | On |
| `enable-auto-tool-choice` | Auto tool/function calling. | Off |
| `enable-bidirectional-kv` | Bidirectional KV transfer. For Nemotron and agentic serving. | Off |
| `disable-custom-all-reduce` | Disable optimized GPU-to-GPU comms. Only if NCCL errors. | Off |
| `vllm-debug-logs` | Verbose vLLM engine logs. Very noisy, only for troubleshooting. | Off |
| `nccl-debug-logs` | Verbose NCCL communication logs. Very noisy, only for multi-GPU networking issues. | Off |

Let the user override specific values. **Validate overrides before applying:**
- `gpu-memory-utilization` must be between 0.5 and 0.99. Below 0.5 wastes GPU. Above 0.99 will OOM.
- `max-model-len` must be at least ISL + OSL. Setting it too high wastes memory.
- `max-num-seqs` should not exceed concurrent users by more than 2x — oversized values cause OOM.
- `pipeline-parallel-size` must evenly divide the number of GPUs.
- `block-size` must be a power of 2 (8, 16, 32, 64, 128, 256).
- `dtype` must match what the model supports — FP8 models can't run in float32.

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

#### Infrastructure & Deployment

This section configures how pods are deployed and communicate. Most of this is auto-detected from the cluster scan — only ask the user if something needs changing.

**Container images:**

The cluster scan detects installed infrastructure versions. Show what was detected and ask:

**"Which container images would you like to use?"**
- **Latest GA release** — show the user the latest stable versions available and mention them by version number:
  - Inference engine: `ghcr.io/llm-d/llm-d-cuda:<latest_tag>`
  - EPP scheduler: `ghcr.io/llm-d/llm-d-router-endpoint-picker:<latest_tag>`
- **Custom images** — the user provides their own image URLs (e.g., for testing a pre-release, internal build, or a different registry).

To find the latest available tags, check the container registry. Show the user the top 3-5 stable tags (exclude `latest`, `dev`, `nightly` — prefer versioned tags like `v0.8.0`, `v0.9.0`).

**Network type:**

The cluster scan detects which networking options are actually available on this cluster. Only show what was found — do not list options that don't exist on the cluster.

Ask: **"How would you like the pods to communicate? Based on the cluster scan, these options are available:"**

Then list only the detected options. The scan result contains `network_type` (the auto-detected best option) and `available_networks` / `dranet_available` fields. Common detected options:
- **DRA** — GPU+NIC affinity via device classes. Fastest, modern approach.
- **SR-IOV** — dedicated virtual NIC per pod for RDMA.
- **Host Network** — pods share the host's network stack.
- **NAD** — Multus secondary networks.
- **TCP** — standard Kubernetes networking. Always available as a fallback.

Tell the user which one was auto-detected as the best fit and why. For multi-node inference, RDMA-capable networking (DRA, SR-IOV) makes a significant performance difference.

**When using DRA:** The scan returns `device_classes` for each DRA option. Look for a device class with `kind: gpu_nic_pair` — this ensures each pod gets a GPU and NIC that are on the same PCIe bus, giving the best RDMA performance. If a `gpu_nic_pair` class exists (e.g., `composite-gpu-nic-pair`), use it. If only `gpu` classes exist (no NIC pairing), the pod will get a GPU but the NIC assignment won't be topology-aware — still works but may have lower RDMA throughput. Set the chosen device class in `selected_dra_classes` config field.

**Storage class for model cache:**

The scan returns all available storage classes. Show them to the user and explain the trade-offs:

Ask: **"Where should the model weights be stored? This matters a lot for large models — a 70B model is ~140GB, and downloading it over the network to each pod can take 30+ minutes on NFS but only 2-3 minutes on local NVMe."**

Explain the options based on what the scan found:

- **Local NVMe / hostPath** (e.g., `hostpath-nvme`) — the model is stored directly on each GPU node's local NVMe disk. Fastest option (~2-3 GB/s read speed). Each node has its own copy. **Best for large models (30B+) and multi-node setups.** Requires the HostPath Provisioner (HPP) or similar operator to be installed, and local disks must be available on ALL GPU nodes the user plans to use. If local disk doesn't cover all GPU nodes, warn the user.
- **NFS / shared filesystem** (e.g., `nfs`, CephFS, EFS) — the model is stored once on a shared volume. All pods read from the same copy. Two options:
  1. **Single shared PVC** — simplest, but creates contention when multiple pods load the model simultaneously. Fine for small models or few pods.
  2. **Per-node PVCs** — one RWX PVC per GPU node. Reduces contention, model is downloaded once per node. More setup but better for large models.
  For large models on NFS, initial model loading can take 20-40 minutes per pod as each pod reads the full model over the network.
- **Block storage** (e.g., `ibmc-vpc-block`, `gp3`, LVMS) — cloud or local block devices. Usually RWO (one pod at a time). **Can only support single-node deployments** — each PVC can only be mounted on one node, so multi-node inference won't work with RWO alone.

**If no local disk is available and the model is large (30B+)**, suggest: "For the best performance with large models, I'd recommend setting up local NVMe storage using the HostPath Provisioner (HPP). This gives each node direct access to its NVMe drives — a 70B model loads in ~2 minutes vs 30+ minutes on NFS. See `docs/local-nvme-hostpath-setup.md` for setup instructions."

If the scan found a local disk SC with `is_local: true` and `gpu_nodes_covered >= gpu_node_count`, suggest it:

**"I found local NVMe storage (`hostpath-nvme`) available on all your GPU nodes. This gives the fastest model loading — the model is read directly from each node's NVMe drive instead of over the network. I'd suggest using this."**

If local disk is available but doesn't cover all GPU nodes, warn:

**"Local NVMe storage is only available on X of Y GPU nodes. For multi-node inference, every GPU node needs local storage. You can either limit your tests to the X nodes with local disk, or use shared storage (NFS) for all nodes."**

If no local disk is available and the model is large (30B+), suggest:

**"This is a large model (~XGB). With network-based storage (NFS), each pod will need to download the full model over the network, which can take 20-40 minutes. If you're able to set up local disk storage using the HostPath Provisioner (HPP) or a similar solution, it would significantly speed up model loading. See `docs/local-nvme-hostpath-setup.md` for setup instructions."**

Set the chosen SC in the config:
- If local disk: set `storage_class`, `per_node_storage: true`, `local_disk_path` from the SC's `local_path` field
- If shared/NFS: set `storage_class`, `per_node_storage: false`

**GPU allocation:**

Already determined during setup. Confirm:
- Total GPUs available and how many will be used
- Which nodes are selected (all or specific)

**Gateway class:**

Auto-detected (usually Istio). Only mention if the user needs to know or if there's an issue.

**Namespace:**

The instance namespace is set during instance creation. No need to ask again.

### Summary before starting

Before proceeding, show a complete summary of ALL choices and ask the user to confirm. Include every setting — do not skip any. This is the user's last chance to change something before a potentially multi-hour optimization run.

```
Ready to optimize:

  Model:            <model>
  Prompt length:    <ISL> tokens (~<ISL*4> characters)
  Response length:  <OSL> tokens (~<OSL*4> characters)
  Concurrent users: <users>
  Priority:         <response time / throughput / full coverage>
  Prefix cache:     <off / X% identical / X% shared prefix / multi-group with N groups>
  Search depth:     <top 1/2/3/all combinations>
  P/D search:       <smart / exhaustive>
  Asymmetric TP:    <yes / no>
  Latency SLA:      <none / Xms at PYY>
  Calibrated load:  <yes / no>
  Concurrency sweep: <yes — top N configs + M extra, K user loads / no>
  EPP tuned sweep:  <yes / no>
  Cache sweep:      <yes / no>
  vLLM auto-tune:   <on / off + any overrides listed>
  EPP preset:       <preset name — Plugin(W) + Plugin(W) + ...>

  Infrastructure:
    Engine image:    <vLLM image:tag>
    Scheduler image: <EPP image:tag>
    Network:         <type> — <device class or details>
    Storage:         <SC name> (<local NVMe X/Y nodes / shared NFS / block>)
    Local disk:      <path or N/A — using shared storage>
    GPUs:            <N>x <GPU_MODEL> across <M> nodes
    Nodes:           <all / pinned to specific nodes>
    Gateway:         <Istio / other>

Shall I start the optimization?
```

Wait for the user to confirm before proceeding. If they want to change anything, go back to that specific section.

---

## Step 4: Authentication

```bash
URL="<instance URL from Step 2>"
curl -sk -c /tmp/serveit-cookies.txt "$URL/login" \
  -d "username=admin&password=serveit" -L -o /dev/null
```

---

## Step 5: Save Configuration

**First, lock the config** to prevent the browser UI from overwriting our settings. The UI auto-saves on every page load and navigation — without locking, it will clobber the config we set via REST.

```bash
curl -sk -b /tmp/serveit-cookies.txt -X POST "$URL/api/config/lock"
```

Then save ALL the settings the user confirmed. Include every field — missing fields will use server defaults which may not match what was agreed.

```bash
curl -sk -b /tmp/serveit-cookies.txt -X POST "$URL/api/config" \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "<MODEL>",
    "isl": <ISL>,
    "osl": <OSL>,
    "isl_stdev": <ISL_STDEV or null>,
    "osl_stdev": <OSL_STDEV or null>,
    "users": <USERS>,
    "goal": "<OBJECTIVE>",
    "max_gpus": <GPUS>,
    "duration": <DURATION>,
    "stop_mode": "duration",
    "storage_class": "<MODEL_CACHE_SC>",
    "per_node_storage": <true/false>,
    "local_disk_path": "<path or null>",
    "use_achievable_qps": false,
    "advanced_vllm_custom_enabled": <true/false>,
    "epp_custom_enabled": true,
    "epp_preset": "<EPP_PRESET>",
    "epp_benchmark": false,
    "image": "<ENGINE_IMAGE>",
    "scheduler_image": "<SCHEDULER_IMAGE>",
    "network_type": "<dra/nad/eth0/etc>",
    "selected_dra_classes": [{"name": "<DEVICE_CLASS>"}],
    "prefix_cache_hit_pct": <0-100>,
    "prefix_cache_mode": "<identical/shared_prefix/multi_group>",
    "prefix_cache_groups": <N>,
    "tp_pair_top_n": <1-4>,
    "pd_search_mode": "<smart/exhaustive>",
    "allow_asymmetric_tp": <true/false>,
    "latency_constraint_enabled": <true/false>,
    "latency_constraint_ms": <MS or 500>,
    "latency_constraint_percentile": "<p50/p90/p95/p99>",
    "calibrated_load_enabled": <true/false>,
    "inferencex_sweep_enabled": <true/false>,
    "concurrency_sweep_count": <N or null>,
    "concurrency_sweep_step_pct": <20>,
    "concurrency_sweep_all_configs": <true/false>,
    "concurrency_sweep_max_configs": <N or null>,
    "concurrency_sweep_use_epp_tuned": <true/false>,
    "cache_sweep_enabled": <true/false>,
    "workload_mode": "synthetic",
    "rate_type": "concurrent"
  }'
```

### Verify configuration was saved

After saving, read back the config and verify the critical fields match what was agreed:

```bash
curl -sk -b /tmp/serveit-cookies.txt "$URL/api/config" | python3 -c "
import json, sys
cfg = json.load(sys.stdin)
checks = {
    'model': '<MODEL>',
    'isl': <ISL>,
    'osl': <OSL>,
    'users': <USERS>,
    'goal': '<OBJECTIVE>',
    'max_gpus': <GPUS>,
    'storage_class': '<SC>',
    'per_node_storage': <true/false>,
    'local_disk_path': '<PATH or None>',
    'network_type': '<NETWORK>',
    'epp_preset': '<EPP_PRESET>',
    'prefix_cache_hit_pct': <PCT>,
    'tp_pair_top_n': <DEPTH>,
    'calibrated_load_enabled': <true/false>,
    'inferencex_sweep_enabled': <true/false>,
}
ok = True
for k, expected in checks.items():
    actual = cfg.get(k)
    match = str(actual) == str(expected)
    status = '✅' if match else '❌'
    if not match:
        ok = False
        print(f'{status} {k}: expected {expected}, got {actual}')
if ok:
    print('✅ All settings verified — ready to start')
else:
    print('❌ Some settings did not save correctly — fix before starting')
"
```

If any field doesn't match, re-save before proceeding. Do NOT start the optimization with incorrect settings.

After verification passes, remind the user:

**"Everything is set. Here are your URLs:**
- **Launcher**: <LAUNCHER_URL>
- **Instance (wizard UI)**: <INSTANCE_URL>

**You can track the optimization progress in two ways:**
1. **Open the wizard UI** in your browser — it shows live console output, progress bars, and will display interactive charts and results as tests complete.
2. **Stream the console here** — I can poll the logs and show you progress updates right in this terminal.

**Important: if the optimization stops or fails at any point, it can always be resumed from where it left off. There is never a need to restart from scratch — all completed tests are saved and the optimizer picks up from the last successful step.**

**You can download an HTML report at any time** — even while the optimization is still running. The report includes all results collected so far. In the wizard UI, click the 💾 **Download** button in the sidebar to get a self-contained HTML file you can open offline or share with your team.

**Would you like me to start the optimization and stream the console output here?"**

---

## Step 6: Set UI to Running State

Before polling, set the UI to step 7 (Review & Run) and mark optimization as running. This prevents users from changing settings in the wizard while tests are executing. Use `/api/set_state` which persists to the database — not `/api/config` which only updates in-memory state.

```bash
curl -sk -b /tmp/serveit-cookies.txt -X POST "$URL/api/set_state" \
  -H 'Content-Type: application/json' \
  -d '{"current_step": 7, "running": true}'
```

---

## Step 7: Check Status & Poll

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

## Step 8: Get Results

Unlock the config so the UI can save normally again:

```bash
curl -sk -b /tmp/serveit-cookies.txt -X POST "$URL/api/config/unlock"
```

Then fetch results:

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

## Step 9: Download Manifests

```bash
curl -sk -b /tmp/serveit-cookies.txt "$URL/api/run/$RUN_ID/config/<CONFIG_NAME>/manifests"
curl -sk -b /tmp/serveit-cookies.txt "$URL/api/run/$RUN_ID/config/<CONFIG_NAME>/manifest/lws" -o lws.yaml
```

---

**Important: syncing code (`deploy.py --sync-all`) restarts the server, which clears the in-memory config lock and running state. The optimization continues running in the background, but the UI will show it as stopped. After any sync while optimization is running, re-apply the lock and state:**

```bash
curl -sk -b /tmp/serveit-cookies.txt -X POST "$URL/api/config/lock"
curl -sk -b /tmp/serveit-cookies.txt -X POST "$URL/api/set_state" \
  -H 'Content-Type: application/json' -d '{"current_step":7,"running":true}'
```

---

## Step 10: Stop / Resume / Sync

```bash
# Stop optimization
curl -sk -b /tmp/serveit-cookies.txt -X POST "$URL/api/stop_optimization"
# Also update UI state
curl -sk -b /tmp/serveit-cookies.txt -X POST "$URL/api/set_state" \
  -H 'Content-Type: application/json' -d '{"running": false}'

# Resume a stopped run (provide run_id)
curl -sk -b /tmp/serveit-cookies.txt -X POST "$URL/api/resume_optimization" \
  -H 'Content-Type: application/json' \
  -d '{"run_id": <RUN_ID>, "hf_token": ""}'
# Set UI back to running
curl -sk -b /tmp/serveit-cookies.txt -X POST "$URL/api/set_state" \
  -H 'Content-Type: application/json' -d '{"current_step": 7, "running": true}'

# Sync code updates
git add -A && git commit -m "updates" && git push
python3 deployment/deploy.py --sync-all -n serveit --mode launcher
```

---

## Troubleshooting

Common issues and how to handle them:

**TopologyAffinityError** — Pod can't be scheduled because GPUs span multiple NUMA nodes. Fix: patch the kubelet config to `best-effort` topology manager: `kubectl patch kubeletconfig <name> --type merge -p '{"spec":{"kubeletConfig":{"topologyManagerPolicy":"best-effort"}}}'`. GPU nodes will reboot to apply.

**Permission denied on hostPath** — vLLM pod can't write to `/model-cache`. Fix: ensure the pod's `securityContext` has `privileged: true` and the `llm-d-modelserver` SA has an SCC that allows hostPath with `RunAsAny`.

**Model download fails (404)** — Model ID doesn't exist on HuggingFace. Validate with `curl -sk -o /dev/null -w "%{http_code}" "https://huggingface.co/api/models/<MODEL_ID>"`. Check for typos, try the `-dynamic` suffix variant.

**Config overwritten by UI** — The browser UI auto-saves config on every page load. Use `POST /api/config/lock` before saving config via REST to prevent this. Auto-unlocks on stop/complete/failure.

**Server restart clears state** — Syncing code (`deploy.py --sync-all`) restarts the server, clearing in-memory config lock and running state. Re-apply with `POST /api/config/lock` and `POST /api/set_state`.

**Pod stuck in ContainerCreating** — Usually pulling the vLLM image (~1.7GB). First pull takes 3-5 minutes. Check with `kubectl describe pod <name>`.

**FailedUpdate on Machine** — The VM was created but is stuck booting. Monitor events with `kubectl get events -A | grep "Update machine"`. If `FailedUpdate` persists for more than 15-20 minutes with no `Normal Update`, delete the machine and let the MachineSet recreate it.

**NFS volume mount errors with local disk** — Template is generating NFS volume mounts despite `local_disk_path` being set. Ensure `per_node_storage: true` AND `local_disk_path` are both set in the config. The prereq manager skips NFS PVC creation when `local_disk_path` is set.

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
