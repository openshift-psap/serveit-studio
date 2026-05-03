# InfeRecipe Deployment

Deployment script for InfeRecipe Optimizer.

## Quick Start

```bash
# Deploy with existing PVC (recommended)
./deployment/deploy.sh --pvc-name my-existing-pvc

# Deploy with new storage
./deployment/deploy.sh --storage-class nfs-csi --storage-size 200Gi
```

## Dev Mode

```bash
# Deploy in dev mode (syncs code from local repo to pod)
./deployment/deploy.sh --dev --pvc-name my-pvc

# Re-sync code after local changes
./deployment/deploy.sh --sync

# Restart the server process (picks up code changes)
./deployment/deploy.sh --restart-server

# Port-forward to localhost:8080
./deployment/deploy.sh --port-forward

# Stop port-forward
./deployment/deploy.sh --stop-port-forward
```

## Command Line Options

| Option | Short | Description | Default |
|---|---|---|---|
| `--namespace NAME` | `-n` | Kubernetes namespace | `llm-d` |
| `--image IMAGE` | `-i` | Container image | `quay.io/bbenshab/vllm:inferecipe` |
| `--pvc-name NAME` | `-p` | Use existing PVC (skips creation) | — |
| `--storage-class CLASS` | `-s` | Storage class for new PVC | — |
| `--storage-size SIZE` | | Size of new PVC | `100Gi` |
| `--force-nad` | | Force NAD (Multus) networking | Auto-detect |
| `--dev` | | Deploy in dev mode (code sync + auto-restart) | Off |
| `--sync` | | Re-sync local code to running dev pod | — |
| `--restart-server` | | Restart server in the pod | — |
| `--port-forward` | | Start port-forward (localhost:8080) | — |
| `--stop-port-forward` | | Stop background port-forward | — |
| `--local-port PORT` | | Local port for port-forward | `8080` |
| `--just-yaml` | | Only output YAML, do not deploy | — |
| `--help` | `-h` | Show help message | — |

## What Gets Created

1. **ClusterRoleBinding** — Prometheus metrics access
2. **Role + RoleBinding** — Pod/PVC/Job/LWS/Service management
3. **ClusterRole + ClusterRoleBinding** — Node/StorageClass read access
4. **Deployment** — InfeRecipe optimizer pod
5. **Service** — ClusterIP service on port 5000
6. **Route** (OpenShift only) — External access with TLS

## Persistent Storage

The PVC is mounted at `/mnt/storage` and contains:

| Path | Content |
|---|---|
| `/mnt/storage/inferecipe.db` | SQLite database (runs, tests, console logs, hardware scans) |
| `/mnt/storage/app/` | Synced application code (dev mode) |
| `/mnt/storage/.cache/huggingface/` | HuggingFace model/tokenizer cache |
| `/mnt/storage/prefix-cache-datasets/` | Generated prefix cache simulation datasets |
| `/mnt/storage/.flask_secret_key` | Flask session signing key |

Data persists across pod restarts and redeployments.

## Code Sync (Dev Mode)

In dev mode, `--sync` uses md5 checksums to efficiently sync only changed files:

1. Computes md5 of all local files (excluding `.git`, `__pycache__`, `.DS_Store`)
2. Computes md5 of all remote files on the pod via single `kubectl exec`
3. Copies only changed files (up to 4 in parallel via `xargs -P4`)
4. Deletes remote files that no longer exist locally

## Accessing the UI

### Kubernetes
```bash
./deployment/deploy.sh --port-forward
# Opens http://localhost:8080
```

### OpenShift
```bash
oc get route inferecipe-optimizer-ui -n llm-d -o jsonpath='{.spec.host}'
```
