# ServeIt Studio API Reference

ServeIt Studio exposes three API surfaces:

| Surface | Transport | Use case |
|---------|-----------|----------|
| **REST API** | HTTP | Status checks, run management, data export, reports |
| **Socket.IO** | WebSocket | Real-time operations: cluster scan, storage setup, optimization lifecycle |
| **CLI** | Shell | Headless optimization runs, cluster management |

**Base URL:** `https://<instance-route>` (OpenShift Route) or `http://localhost:5000` (local)

**Authentication:** Session-based login via `/login` (username + password). REST and Socket.IO share the same Flask session cookie.

---

## Table of Contents

- [REST API](#rest-api)
  - [Status & Config](#status--config)
  - [Optimization Runs](#optimization-runs)
  - [Reports & Charts](#reports--charts)
  - [Models & Templates](#models--templates)
  - [Logs](#logs)
  - [Data Management](#data-management)
  - [Manifests](#manifests)
  - [MLflow Integration](#mlflow-integration)
- [Socket.IO Events](#socketio-events)
  - [Session Management](#session-management)
  - [Configuration](#configuration)
  - [Cluster Scanning](#cluster-scanning)
  - [Storage & Model Download](#storage--model-download)
  - [Optimization Lifecycle](#optimization-lifecycle)
  - [Utilities](#utilities)
- [CLI Reference](#cli-reference)
  - [Cluster Commands](#cluster-commands)
  - [Run Command](#run-command)
- [End-to-End Examples](#end-to-end-examples)
  - [Python: Full Optimization Flow](#python-full-optimization-flow)
  - [curl: REST-Only Workflow](#curl-rest-only-workflow)
  - [CLI: Headless Run](#cli-headless-run)

---

## REST API

### Status & Config

#### `GET /api/status`

Current optimization state.

```bash
curl -s $BASE_URL/api/status | jq
```

```json
{
  "running": false,
  "config": { "model": "google/gemma-4-26B-A4B", "isl": 2000, ... }
}
```

#### `GET /api/config`

Full saved configuration including cluster resources.

```bash
curl -s $BASE_URL/api/config | jq '.storage_class, .model'
```

#### `POST /api/config`

Update configuration.

```bash
curl -s -X POST $BASE_URL/api/config \
  -H 'Content-Type: application/json' \
  -d '{"model": "google/gemma-4-26B-A4B", "isl": 2000, "osl": 100}'
```

**Response:** `{"success": true, "config": {...}}`

#### `POST /api/stop_optimization`

Stop a running optimization. Idempotent.

```bash
curl -s -X POST $BASE_URL/api/stop_optimization
```

**Response:** `{"success": true, "message": "Optimization stopped"}`

#### `POST /api/clear_console`

Clear the UI console display (logs are preserved in DB).

```bash
curl -s -X POST $BASE_URL/api/clear_console
```

---

### Optimization Runs

#### `GET /api/runs`

List all optimization runs.

```bash
curl -s $BASE_URL/api/runs | jq '.[0]'
```

```json
{
  "id": 42,
  "run_name": "Run #42",
  "model": "google/gemma-4-26B-A4B",
  "isl": 2000,
  "osl": 100,
  "num_users": 100,
  "max_gpus": 16,
  "goal": "ttft",
  "status": "completed",
  "created_at": "2026-08-09T12:00:00",
  "completed_at": "2026-08-09T14:30:00",
  "notes": "Production baseline"
}
```

#### `GET /api/runs_for_resume`

Runs with step-level progress for resume UI.

```bash
curl -s $BASE_URL/api/runs_for_resume | jq '.[0] | {id, model, status, completed_steps, last_step}'
```

```json
{
  "id": 42,
  "model": "google/gemma-4-26B-A4B",
  "status": "stopped",
  "completed_steps": [2, 3, 6, 7],
  "last_step": 7
}
```

#### `GET /api/resumable_run`

Check if there's a run that can be resumed.

```bash
curl -s $BASE_URL/api/resumable_run | jq
```

```json
{
  "resumable": true,
  "run": { "id": 42, "run_name": "Run #42", "model": "google/gemma-4-26B-A4B", "status": "stopped" },
  "completed_tests": 12
}
```

#### `DELETE /api/delete_run/<run_id>`

Delete a run and all its test results. Fails if the run is currently running (409).

```bash
curl -s -X DELETE $BASE_URL/api/delete_run/42
```

**Response:** `{"success": true, "deleted_tests": 15}`

#### `POST /api/restart_run/<run_id>`

Reset a run — clears all test results so it can be re-run from scratch.

```bash
curl -s -X POST $BASE_URL/api/restart_run/42
```

**Response:** `{"success": true, "deleted_tests": 15}`

#### `PUT /api/runs/<run_id>/notes`

Update run description/notes.

```bash
curl -s -X PUT $BASE_URL/api/runs/42/notes \
  -H 'Content-Type: application/json' \
  -d '{"notes": "Production baseline with 80% cache hit"}'
```

---

### Reports & Charts

#### `GET /api/runs/<run_id>/charts`

Full report data including charts, summary statistics, and deployment recommendations.

```bash
curl -s $BASE_URL/api/runs/42/charts | jq '.summary'
```

```json
{
  "total_tests": 15,
  "successful_tests": 12,
  "best_configs": {
    "lowest_latency": { "config_name": "step6-agg-tp8-2x", "ttft_p90": 287, "throughput_mean": 30.12 },
    "highest_throughput": { "config_name": "step7-pd-3p1d-tp4", "ttft_p90": 675, "throughput_mean": 28.79 }
  }
}
```

#### `GET /api/runs/<run_id>/configurations`

All test configurations for a run.

```bash
curl -s $BASE_URL/api/runs/42/configurations | jq '.[0] | {config_name, architecture, status, ttft_p90, throughput_mean}'
```

#### `GET /api/runs/<run_id>/pod_errors`

Pod error logs for a run.

```bash
curl -s $BASE_URL/api/runs/42/pod_errors | jq
```

---

### Models & Templates

#### `GET /api/models`

Available Red Hat AI models catalog.

```bash
curl -s $BASE_URL/api/models | jq '.[0]'
```

#### `GET /api/deployment_templates`

List deployment templates. Optional query filters.

```bash
# All templates
curl -s $BASE_URL/api/deployment_templates | jq '.count'

# Filter by model and architecture
curl -s "$BASE_URL/api/deployment_templates?model_name=google/gemma-4-26B-A4B&architecture=aggregated"
```

#### `GET /api/deployment_templates/<model_name>/<architecture>`

Get a specific deployment template.

```bash
curl -s "$BASE_URL/api/deployment_templates/google%2Fgemma-4-26B-A4B/aggregated?role=prefill"
```

#### `PUT /api/deployment_templates`

Create or update a deployment template.

```bash
curl -s -X PUT $BASE_URL/api/deployment_templates \
  -H 'Content-Type: application/json' \
  -d '{
    "model_name": "google/gemma-4-26B-A4B",
    "architecture": "aggregated",
    "tensor_parallelism": 8,
    "replicas": 2,
    "gpu_memory_utilization": 0.92,
    "image": "ghcr.io/llm-d/llm-d-cuda:v0.8.0"
  }'
```

---

### Logs

#### `GET /api/logs`

Console logs with filtering.

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `run_id` | int | — | Filter by run ID |
| `job_name` | str | — | Filter by job name |
| `since` | ISO timestamp | — | Only logs after this time |
| `limit` | int | 100 | Max entries (up to 100000) |

```bash
# Last 50 logs for run #42
curl -s "$BASE_URL/api/logs?run_id=42&limit=50" | jq '.logs[-1]'

# Logs since a timestamp
curl -s "$BASE_URL/api/logs?since=2026-08-09T12:00:00&limit=1000"
```

---

### Data Management

#### `GET /api/backup/database`

Download the SQLite database (gzip compressed).

```bash
curl -s $BASE_URL/api/backup/database -o serveit.db.gz
```

#### `GET /api/backup/artifacts`

Download test artifacts archive (tar.gz).

```bash
curl -s $BASE_URL/api/backup/artifacts -o artifacts.tar.gz
```

#### `POST /api/restore/artifacts`

Restore artifacts from a tar.gz archive.

```bash
curl -s -X POST $BASE_URL/api/restore/artifacts \
  -F "artifacts=@artifacts.tar.gz"
```

**Response:** `{"success": true, "files_restored": 42}`

#### `POST /api/upload-dataset`

Upload a custom dataset file (.csv, .json, .jsonl, .txt).

```bash
curl -s -X POST $BASE_URL/api/upload-dataset \
  -F "file=@my-prompts.jsonl"
```

**Response:** `{"success": true, "path": "/mnt/storage/datasets/my-prompts.jsonl", "filename": "my-prompts.jsonl"}`

#### `POST /api/upload_database`

Import runs from another ServeIt Studio database.

```bash
curl -s -X POST $BASE_URL/api/upload_database \
  -F "database=@other-instance.db"
```

**Response:** `{"success": true, "imported_runs": 5, "imported_tests": 42, "skipped_runs": 2}`

#### `GET /api/download_database`

Download compressed database (alternative to backup endpoint).

#### `GET /api/download_raw_data`

Download raw test data archive.

---

### Manifests

#### `GET /api/run/<run_id>/config/<config_name>/manifests`

List available manifest types for a test configuration.

```bash
curl -s $BASE_URL/api/run/42/config/step6-agg-tp8-2x/manifests
```

**Response:** `{"available": ["lws", "epp-configmap", "service", "httproute"]}`

#### `GET /api/run/<run_id>/config/<config_name>/manifest/<manifest_type>`

Download a specific manifest as YAML.

```bash
curl -s $BASE_URL/api/run/42/config/step6-agg-tp8-2x/manifest/lws -o lws.yaml
```

---

### MLflow Integration

#### `GET /api/mlflow/config`

Get MLflow tracking configuration.

```bash
curl -s $BASE_URL/api/mlflow/config | jq '.config'
```

#### `POST /api/mlflow/config`

Configure MLflow tracking.

```bash
curl -s -X POST $BASE_URL/api/mlflow/config \
  -H 'Content-Type: application/json' \
  -d '{
    "tracking_uri": "https://mlflow.example.com",
    "username": "admin",
    "password": "secret",
    "experiment_name": "llm-optimization"
  }'
```

#### `GET /api/mlflow/runs`

List runs available for MLflow export.

#### `GET /api/mlflow/tests/<run_id>`

Get test results for a run in MLflow-exportable format.

#### `POST /api/mlflow/export`

Export run data to MLflow.

```bash
curl -s -X POST $BASE_URL/api/mlflow/export \
  -H 'Content-Type: application/json' \
  -d '{"run_id": 42}'
```

---

### Analysis

#### `GET /api/optuna_trials/<run_id>`

Get Optuna hyperparameter search trials.

```bash
curl -s "$BASE_URL/api/optuna_trials/42?step=step9_latency_bounded" | jq '.trials | length'
```

#### `GET /api/latency_search/<run_id>`

Get latency search data grouped by architecture.

```bash
curl -s "$BASE_URL/api/latency_search/42?architecture=aggregated" | jq
```

---

## Socket.IO Events

Connect to the Socket.IO server at the base URL. All events use JSON payloads.

### Session Management

#### `connect`

Automatic on connection. The server enforces single active UI session.

**Emits back:**
- `session_granted {}` — you are the active session
- `session_locked {username, connected_at}` — another session is active

After `session_granted`, the server replays:
- `status_update {running, config}` — current state
- `console_log {type, message, replayed}` — recent log entries (up to 100)

#### `take_over`

Force take the active session from another tab/user.

```python
sio.emit('take_over')
# Receives: session_granted
# Old session receives: session_kicked {taken_by}
```

#### `heartbeat`

Send periodically to prevent session timeout. No response.

```python
sio.emit('heartbeat')
```

---

### Configuration

#### `save_config`

Persist UI configuration to database.

```python
sio.emit('save_config', {
    'config': {
        'model': 'google/gemma-4-26B-A4B',
        'isl': 2000,
        'osl': 100,
        'users': 100,
        'goal': 'ttft',
        'max_gpus': 16,
        'storage_class': 'hostpath-nvme',
        'per_node_storage': True,
        'local_disk_path': '/var/hpvolumes/local-nvme',
        # ... all wizard config fields
    },
    'current_step': 3
})
# Receives: save_config_result {success: true}
```

#### `load_config`

Load saved configuration from database.

```python
sio.emit('load_config')
# Receives: load_config_result {
#   success: true,
#   config: {...},
#   current_step: 3,
#   optimization_running: false,
#   namespace: 'serveit-admin-nemotron-janus'
# }
```

---

### Cluster Scanning

#### `scan_cluster`

Scan the Kubernetes cluster for GPUs, storage, network, and infrastructure.

```python
sio.emit('scan_cluster', {})
```

**Response event: `cluster_scan_result`**

```json
{
  "total_gpus": 16,
  "gpus_in_use": 0,
  "gpus_available": 16,
  "gpus_per_node": [{"node": "gpu-node-1", "gpus": 8}, {"node": "gpu-node-2", "gpus": 8}],
  "max_gpus_per_node": 8,
  "gpu_node_count": 2,
  "gpu_model": "H200",
  "gpu_memory_per_gpu_mb": 143360,
  "has_rdma": true,
  "tp_options": [1, 2, 4, 8],
  "storage_classes": [
    {
      "name": "hostpath-nvme",
      "provisioner": "kubevirt.io.hostpath-provisioner",
      "is_local": true,
      "gpu_nodes_covered": 2,
      "access_mode": "ReadWriteOnce",
      "local_path": "/var/hpvolumes/local-nvme"
    },
    {
      "name": "nfs",
      "provisioner": "example.com/nfs",
      "is_local": false,
      "access_mode": "ReadWriteMany",
      "local_path": ""
    }
  ],
  "nodes_detail": [
    {
      "name": "gpu-node-1",
      "gpus": 8,
      "gpu_model": "H200",
      "gpu_memory_mb": 143360,
      "cpu_cores": 96,
      "memory_gb": 1024,
      "has_rdma": true,
      "nics": [{"name": "mlx5_0", "type": "infiniband", "speed_gbps": 400}]
    }
  ],
  "provider": "ibm-cloud",
  "network_type": "sriov",
  "gateway_class": "istio",
  "lws_supports_vct": true
}
```

---

### Storage & Model Download

#### `setup_storage`

Create PVCs and start model download. Three modes:

**Mode 1: Existing PVC** (skip download, start optimization immediately)
```python
sio.emit('setup_storage', {
    'existing_pvc': 'my-model-cache',
    'model': 'google/gemma-4-26B-A4B',
    'hf_token': 'hf_xxx',
    # + all optimization params (triggers auto-start)
    'isl': 2000, 'osl': 100, 'num_users': 100,
    'optimization_goal': 'ttft', 'max_gpus': 16,
    'duration': 300, 'stop_mode': 'duration',
    # ...
})
```

**Mode 2: Local disk (hostPath)** — per-node NVMe download
```python
sio.emit('setup_storage', {
    'model': 'google/gemma-4-26B-A4B',
    'storage_class': 'hostpath-nvme',
    'pvc_size': 256,
    'hf_token': 'hf_xxx',
    'per_node_storage': True,
    'local_disk_path': '/var/hpvolumes/local-nvme'
})
```

**Mode 3: Shared PVC** — single PVC with model download
```python
sio.emit('setup_storage', {
    'model': 'google/gemma-4-26B-A4B',
    'storage_class': 'nfs',
    'pvc_size': 256,
    'hf_token': 'hf_xxx'
})
```

**Response event: `storage_setup_result`**

```json
{
  "success": true,
  "pvc_name": "2x local-disk",
  "pvc_size": "local",
  "storage_class": "hostpath-nvme",
  "model": "google/gemma-4-26B-A4B",
  "existing": false,
  "per_node": true,
  "local_disk_path": "/var/hpvolumes/local-nvme",
  "job_name": "serveit-download-22mkm-20260809-134952"
}
```

**Progress events:** `console_log {type, message}` — streamed download progress

**Completion event:** `storage_download_complete {success, job_name}`

#### `list_pvcs`

List PVCs in the target namespace.

```python
sio.emit('list_pvcs', {})
# Receives: pvc_list_result {success, pvcs: [{name, size, storage_class, status}]}
```

#### `recreate_storage`

Recreate storage and re-download model for an existing run.

```python
sio.emit('recreate_storage', {
    'run_id': 42,
    'hf_token': 'hf_xxx',
    'storage_class': 'nfs'  # optional override
})
# Receives: recreate_storage_done {run_id} or {run_id, error}
```

---

### Optimization Lifecycle

#### `generate_test_plan`

Generate a test plan based on model requirements and cluster resources.

```python
sio.emit('generate_test_plan', {
    'model': 'google/gemma-4-26B-A4B',
    'optimization_goal': 'ttft',
    'max_gpus': 16,
    'isl': 2000,
    'osl': 100,
    'num_users': 100,
    'hf_token': 'hf_xxx'
})
```

**Response event: `test_plan_result`**

```json
{
  "model_name": "google/gemma-4-26B-A4B",
  "total_gpus_available": 16,
  "max_gpus_to_use": 16,
  "optimization_goal": "ttft",
  "can_proceed": true,
  "model_requirements": {
    "estimated_vram_gb": 26.5,
    "min_gpus": 1,
    "min_tp": 1,
    "recommended_tp_options": [1, 2, 4, 8],
    "gpu_memory_utilization": 0.92
  },
  "tests": [
    {
      "test_name": "step2-calibrate-agg-tp1",
      "architecture": "aggregated",
      "gpus_required": 1,
      "tp": 1,
      "description": "Calibration: TP1 aggregated"
    },
    {
      "test_name": "step6-agg-tp8-2x",
      "architecture": "aggregated",
      "gpus_required": 16,
      "tp": 8,
      "description": "Aggregated TP8 x 2 replicas"
    }
  ]
}
```

#### `start_optimization`

Start a new optimization run. Requires a valid test plan.

```python
sio.emit('start_optimization', {
    'model': 'google/gemma-4-26B-A4B',
    'isl': 2000,
    'osl': 100,
    'num_users': 100,
    'optimization_metric': 'ttft',
    'max_test_duration': 300,
    'stop_mode': 'duration',
    'hf_token': 'hf_xxx',
    'max_gpus': 16,
    'use_achievable_qps': False,
    'selected_nodes': [],

    # Search strategy
    'tp_pair_top_n': 4,
    'pd_search_mode': 'smart',

    # Workload
    'workload_mode': 'synthetic',
    'rate_type': 'concurrent',
    'prefix_cache_hit_pct': 0,

    # EPP
    'epp_custom_enabled': True,
    'epp_preset': 'balanced',
    'epp_benchmark': False,

    # Auto-tune
    'advanced_vllm_custom_enabled': True,
    'advanced_vllm': None,

    # Images
    'image': 'ghcr.io/llm-d/llm-d-cuda:v0.8.0',
    'scheduler_image': 'ghcr.io/llm-d/llm-d-router-endpoint-picker:v0.9.0',
})
```

**Response events:**
- `status_update {running: true, message}` — broadcast
- `console_log {type, message}` — continuous progress updates
- `status_update {running: false}` — on completion/failure

#### `resume_optimization`

Resume a stopped/failed run.

```python
sio.emit('resume_optimization', {
    'run_id': 42,
    'hf_token': 'hf_xxx'  # not stored in DB
})
# Receives: status_update {running: true, message: 'Optimization resumed'}
```

#### `stop_optimization`

Stop the running optimization. Cleans up deployed pods.

```python
sio.emit('stop_optimization')
# Receives: status_update {running: false, message: 'Optimization stopped'}
```

#### `cleanup_deployment`

Delete test pods and LWS resources from the last run.

```python
sio.emit('cleanup_deployment', {})
# Receives: cleanup_result {success: true, message: '...'}
```

---

### Utilities

#### `fetch_image_tags`

Fetch container image tags from a registry.

```python
sio.emit('fetch_image_tags', {
    'repo': 'ghcr.io/llm-d/llm-d-cuda',
    'target': 'image'  # 'image' or 'scheduler'
})
# Receives: image_tags_result {tags: ['v0.8.0', 'v0.7.1', ...], repo: '...'}
```

#### `reset_database`

Delete all runs, tests, logs, and templates. **Destructive.**

```python
sio.emit('reset_database', {})
# Receives: reset_complete {success: true}
```

#### `compress_database`

Compress database for download.

```python
sio.emit('compress_database')
# Receives: compression_progress {percent, status, original_size} (multiple)
# Receives: compression_complete {original_size, compressed_size, ratio}
# Then download via: GET /api/download_database
```

#### `compress_raw_data`

Compress test artifacts for download.

```python
sio.emit('compress_raw_data')
# Receives: raw_compression_complete {original_size, compressed_size, ratio}
# Then download via: GET /api/download_raw_data
```

---

## CLI Reference

The CLI runs inside the optimizer pod at `/mnt/storage/app/cli/inftune.py`.

```bash
# From inside the pod
cd /mnt/storage/app
python3 cli/inftune.py <command>

# Or with the serveit alias (if configured)
serveit <command>
```

### Cluster Commands

```bash
# Register the current kubectl context
serveit cluster add --name local

# Register a remote cluster
serveit cluster add --name prod \
    --kubeconfig ~/.kube/prod.yaml \
    --namespace my-namespace \
    --storage-class hostpath-nvme

# List clusters
serveit cluster list

# Scan cluster resources
serveit cluster scan prod

# Remove a cluster
serveit cluster remove prod
```

### Run Command

```bash
# Minimal run
serveit run --model google/gemma-4-26B-A4B --cluster local

# Full production run
serveit run \
    --model google/gemma-4-26B-A4B \
    --cluster prod \
    --isl 2000 --isl-stdev 1000 \
    --osl 100 --osl-stdev 50 \
    --users 100 \
    --gpus 16 \
    --objective ttft \
    --tp-pair-depth 4 \
    --pd-search smart \
    --epp-preset cache_optimized \
    --epp-benchmark \
    --prefix-cache-pct 50 \
    --prefix-cache-mode multi_group \
    --prefix-cache-groups 10 \
    --auto-tune \
    --duration 300 \
    --image ghcr.io/llm-d/llm-d-cuda:v0.8.0 \
    --hf-token $HF_TOKEN \
    --html-report report.html \
    --description "Production baseline"

# Resume a run
serveit run --resume 42 --cluster prod

# Quick single test
serveit run \
    --model google/gemma-4-26B-A4B \
    --cluster local \
    --objective single_test \
    --single-test-arch aggregated \
    --single-test-tp 8 \
    --single-test-replicas 2 \
    --duration 120
```

#### All Run Flags

| Flag | Default | Description |
|------|---------|-------------|
| **Required** | | |
| `--model` | — | HuggingFace model path |
| `--cluster` | — | Registered cluster name |
| **Workload** | | |
| `--isl` | 3000 | Input sequence length |
| `--isl-stdev` | — | ISL standard deviation |
| `--osl` | 256 | Output sequence length |
| `--osl-stdev` | — | OSL standard deviation |
| `--users` | 100 | Concurrent users |
| `--rate-type` | concurrent | concurrent, constant, poisson |
| `--turns` | 1 | Conversation turns |
| `--workload-mode` | synthetic | synthetic or dataset |
| `--dataset` | — | Dataset path or HuggingFace ID |
| `--dataset-column` | — | Column for prompts |
| `--dataset-max-output` | 256 | Max output tokens |
| **Prefix Cache** | | |
| `--prefix-cache-pct` | 0 | Cache hit ratio 0-100% |
| `--prefix-cache-mode` | identical | identical, shared_prefix, multi_group |
| `--prefix-cache-groups` | 5 | Groups for multi_group mode |
| `--prefix-cache-seed` | — | Random seed |
| **Hardware** | | |
| `--gpus` | 16 | Total GPUs |
| `--tp-options` | 1,2,4,8 | TP values to explore |
| `--image` | ghcr.io/llm-d/llm-d-cuda:v0.6.0 | vLLM image |
| `--namespace` | from cluster | K8s namespace |
| `--pvc` | serveit-cache | PVC name |
| `--nccl-ib-hca` | mlx | NCCL IB HCA prefix |
| `--hf-token` | $HF_TOKEN | HuggingFace token |
| `--nodes` | — | Node names (comma-separated) |
| `--scheduler-image` | — | EPP scheduler image |
| `--thanos-url` | auto | Prometheus/Thanos URL |
| `--extra-env-vars` | — | Extra env vars (KEY=VAL,...) |
| **Search** | | |
| `--objective` | ttft | ttft, throughput, balanced, aggregated_only, pd_only, ep_only, single_test |
| `--tp-pair-depth` | 4 | 1=fast, 4=full |
| `--pd-search` | smart | smart or exhaustive |
| `--headroom` | 1.3 | Load headroom multiplier |
| `--allow-asymmetric-tp` | off | Allow prefill TP > decode TP |
| `--max-pd-splits` | 0 | Limit PD splits (0=unlimited) |
| `--use-achievable-qps` | off | Auto-scale concurrency |
| `--duration` | 300 | Test duration (seconds) |
| `--stop-mode` | duration | duration or max_requests |
| `--max-requests` | — | Max requests per test |
| **Latency SLA** | | |
| `--latency-sla` | — | Target latency (ms) |
| `--latency-percentile` | p99 | p50, p90, p95, p99 |
| **EPP** | | |
| `--epp-preset` | balanced | balanced, cache_optimized, queue_balanced, latency_aware, custom |
| `--epp-custom` | off | Enable EPP customization |
| `--epp-benchmark` | off | Benchmark EPP strategies |
| `--epp-weights` | — | Cache:KV:Queue (e.g., 5:1:1) |
| `--epp-max-prefix-blocks` | auto | maxPrefixBlocksToMatch |
| `--epp-lru-capacity` | auto | lruCapacityPerServer |
| `--epp-non-cached-tokens` | auto | nonCachedTokens |
| **Single Test** | | |
| `--single-test-arch` | — | aggregated, pd, ep |
| `--single-test-tp` | — | TP size |
| `--single-test-replicas` | — | Pod count |
| `--single-test-prefill-tp` | — | Prefill TP (PD) |
| `--single-test-decode-tp` | — | Decode TP (PD) |
| `--single-test-prefill-pods` | — | Prefill pods (PD) |
| `--single-test-decode-pods` | — | Decode pods (PD) |
| **Advanced vLLM** | | |
| `--auto-tune` | off | Enable auto-tuning |
| `--memory-reserve-pct` | 0 | Extra GPU memory reserve % |
| `--max-model-len` | auto | Max model length |
| `--gpu-mem-util` | auto | GPU memory utilization |
| `--block-size` | auto | KV cache block size |
| `--dtype` | auto | Model dtype |
| `--kv-cache-dtype` | auto | KV cache dtype |
| `--pipeline-parallel` | auto | Pipeline parallel size |
| `--max-num-seqs` | auto | Max concurrent sequences |
| `--max-num-batched-tokens` | auto | Max tokens per batch |
| `--tool-call-parser` | auto | Tool call parser |
| **Toggles** | | |
| `--enable-prefix-caching` / `--no-prefix-caching` | auto | Prefix caching |
| `--disable-custom-all-reduce` | auto | Custom all-reduce |
| `--trust-remote-code` / `--no-trust-remote-code` | auto | Trust remote code |
| `--disable-log-requests` | auto | Request logging |
| `--enable-auto-tool-choice` | auto | Auto tool choice |
| `--vllm-debug-logs` | off | vLLM debug logs |
| `--nccl-debug-logs` | off | NCCL debug logs |
| **Output** | | |
| `--html-report` | — | Save HTML report to file |
| `--description` | — | Run description |
| `--db` | /mnt/storage/serveit.db | Database path |
| `--quiet` | off | Suppress output |

---

## End-to-End Examples

### Python: Full Optimization Flow

Complete automation using `python-socketio`:

```python
import socketio
import time
import requests

BASE_URL = 'https://serveit-admin-nemotron-janus-ui-inftune.apps.example.com'

# Login
session = requests.Session()
session.post(f'{BASE_URL}/login', data={'username': 'admin', 'password': 'admin'})
cookies = session.cookies.get_dict()

# Connect Socket.IO
sio = socketio.Client()
results = {}

@sio.on('session_granted')
def on_granted():
    print('Session granted')

@sio.on('cluster_scan_result')
def on_scan(data):
    print(f"Cluster: {data['gpu_node_count']} GPU nodes, {data['total_gpus']} GPUs ({data['gpu_model']})")
    results['scan'] = data

@sio.on('storage_setup_result')
def on_storage(data):
    print(f"Storage: {data['pvc_name']} ({data['storage_class']})")
    results['storage'] = data

@sio.on('test_plan_result')
def on_plan(data):
    print(f"Test plan: {len(data['tests'])} tests, can_proceed={data['can_proceed']}")
    results['plan'] = data

@sio.on('status_update')
def on_status(data):
    print(f"Status: running={data['running']}")
    results['running'] = data['running']

@sio.on('console_log')
def on_log(data):
    if not data.get('replayed'):
        print(f"  [{data['type']}] {data['message']}")

sio.connect(BASE_URL, headers={'Cookie': f'session={cookies["session"]}'})

# Step 1: Scan cluster
sio.emit('scan_cluster', {})
time.sleep(10)

# Step 2: Setup storage + download model
sio.emit('setup_storage', {
    'model': 'google/gemma-4-26B-A4B',
    'storage_class': 'hostpath-nvme',
    'pvc_size': 256,
    'hf_token': 'hf_xxx',
    'per_node_storage': True,
    'local_disk_path': '/var/hpvolumes/local-nvme'
})

# Wait for download to complete
while not results.get('storage'):
    time.sleep(5)

# Step 3: Generate test plan
sio.emit('generate_test_plan', {
    'model': 'google/gemma-4-26B-A4B',
    'optimization_goal': 'ttft',
    'max_gpus': 16,
    'isl': 2000,
    'osl': 100,
    'num_users': 100,
    'hf_token': 'hf_xxx'
})
time.sleep(5)

# Step 4: Start optimization
sio.emit('start_optimization', {
    'model': 'google/gemma-4-26B-A4B',
    'isl': 2000, 'osl': 100, 'num_users': 100,
    'optimization_metric': 'ttft',
    'max_test_duration': 300,
    'stop_mode': 'duration',
    'max_gpus': 16,
    'hf_token': 'hf_xxx',
    'epp_custom_enabled': True,
    'epp_preset': 'balanced',
    'advanced_vllm_custom_enabled': True,
    'image': 'ghcr.io/llm-d/llm-d-cuda:v0.8.0',
    'scheduler_image': 'ghcr.io/llm-d/llm-d-router-endpoint-picker:v0.9.0',
})

# Wait for completion
while results.get('running', True):
    time.sleep(30)

# Step 5: Get results
resp = session.get(f'{BASE_URL}/api/runs')
runs = resp.json()
latest = runs[0]
print(f"\nRun #{latest['id']}: {latest['status']}")

charts = session.get(f'{BASE_URL}/api/runs/{latest["id"]}/charts').json()
summary = charts.get('summary', {})
best = summary.get('best_configs', {})
if best.get('lowest_latency'):
    ll = best['lowest_latency']
    print(f"Best TTFT: {ll['ttft_p90']}ms ({ll['config_name']})")
if best.get('highest_throughput'):
    ht = best['highest_throughput']
    print(f"Best Throughput: {ht['throughput_mean']} req/s ({ht['config_name']})")

sio.disconnect()
```

### curl: REST-Only Workflow

Check status, list runs, and download reports without Socket.IO:

```bash
BASE=https://serveit-instance.apps.example.com

# Login (get session cookie)
curl -sk -c cookies.txt $BASE/login \
  -d "username=admin&password=admin" -L -o /dev/null

# Check status
curl -sk -b cookies.txt $BASE/api/status | jq '.running'

# List runs
curl -sk -b cookies.txt $BASE/api/runs | jq '.[] | {id, model, status}'

# Get report for run #42
curl -sk -b cookies.txt $BASE/api/runs/42/charts | jq '.summary.best_configs'

# Download manifests
curl -sk -b cookies.txt $BASE/api/run/42/config/step6-agg-tp8-2x/manifests | jq
curl -sk -b cookies.txt $BASE/api/run/42/config/step6-agg-tp8-2x/manifest/lws -o lws.yaml

# Download database backup
curl -sk -b cookies.txt $BASE/api/backup/database -o backup.db.gz

# Stop optimization
curl -sk -b cookies.txt -X POST $BASE/api/stop_optimization | jq
```

### CLI: Headless Run

Run optimization from inside the pod without the web UI:

```bash
# SSH into the pod
kubectl exec -it -n inftune deploy/serveit-optimizer -- bash
cd /mnt/storage/app

# Register cluster
python3 cli/inftune.py cluster add --name janus

# Scan resources
python3 cli/inftune.py cluster scan janus

# Run optimization
python3 cli/inftune.py run \
    --model google/gemma-4-26B-A4B \
    --cluster janus \
    --isl 2000 --osl 100 \
    --users 100 \
    --gpus 16 \
    --objective ttft \
    --auto-tune \
    --epp-preset cache_optimized \
    --epp-benchmark \
    --duration 300 \
    --html-report /mnt/storage/report.html

# Resume if interrupted
python3 cli/inftune.py run --resume 42 --cluster janus
```
