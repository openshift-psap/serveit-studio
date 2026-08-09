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

Ask: **"Which model do you want to optimize?"**

Give examples: `google/gemma-4-26B-A4B`, `RedHatAI/Meta-Llama-3.1-70B-Instruct-FP8-dynamic`, `meta-llama/Llama-3.1-8B-Instruct`

If the model is gated (requires HuggingFace login), ask for an HF token or check if `$HF_TOKEN` is set.

### 3b. What's your expected workload?

Ask these in plain language:

**"How long are the prompts your users will send?"**
- This is the Input Sequence Length (ISL) — measured in tokens (roughly 1 token = 4 characters of English text)
- Examples: A short chat message is ~50-200 tokens. A document summary prompt with context is ~2,000-4,000 tokens. RAG with large context windows can be 8,000-32,000 tokens.
- Default: 2,000 tokens

**"How long should the model's responses be?"**
- This is the Output Sequence Length (OSL) — how many tokens the model generates per response
- Examples: A short answer is ~50-100 tokens. A detailed explanation is ~200-500 tokens. Code generation can be 500-2,000 tokens.
- Default: 100 tokens

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
- **Yes, most requests share a system prompt** → Explain the options:
  - **Identical mode** — All requests use the exact same prompt. Good for: FAQ bots, fixed-instruction APIs. Set cache hit % to how many requests you expect to repeat (e.g., 50-80%).
  - **Shared prefix mode** — All requests share the first N% of tokens (like a system prompt), but the rest is unique. Good for: Chat apps with a system prompt, RAG with shared context. Set cache hit % to the fraction of the prompt that's shared.
  - **Multi-group mode** — Requests come in groups (like different tenants or conversation threads), and requests within a group share a prefix. Good for: Multi-tenant platforms, agentic workflows with multiple tools. Set the number of groups (e.g., 5-20 groups).

For **agentic models** (tool-calling, function-calling, multi-step reasoning): there's typically a very high shared prefix because the system prompt includes tool definitions, instructions, and conversation history. Recommend **shared prefix mode at 50-80%** or **multi-group mode with 5-10 groups** if multiple agents/tools are involved.

### 3e. Any advanced settings?

Ask: **"Do you want to customize any advanced settings, or use the recommended defaults?"**

If the user says defaults, use:
- Test duration: 300s
- EPP preset: balanced
- Auto-tune vLLM: on (automatically adjusts GPU memory utilization, batch sizes, block sizes)

If the user wants to customize, explain:
- **Test duration** — How long each individual test runs. 300s (5 min) is a good balance. Shorter = faster overall but less stable results. Longer = more accurate but takes longer.
- **EPP preset** — How the inference gateway routes requests across pods:
  - `balanced` — equal weight to cache, queue depth, and KV utilization
  - `cache_optimized` — prioritize routing to pods that have the prompt cached (best for high cache-hit workloads)
  - `queue_balanced` — prioritize routing to the least busy pod
  - `latency_aware` — prioritize lowest response time

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
