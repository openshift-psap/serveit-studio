#!/bin/bash
set -e

# In-S8 Optimizer YAML Generator
# Generates Kubernetes manifests for In-S8 optimizer deployment

# Default configuration
NAMESPACE="llm-d"
IMAGE="quay.io/bbenshab/vllm:in-s8"
PVC_NAME=""
STORAGE_CLASS=""
STORAGE_SIZE="100Gi"
AUTO_DEPLOY=true
FORCE_NAD="false"  # Default to letting provider auto-detect (DRA on IBM Cloud)
DEV_MODE="false"   # Auto-sync code to PVC and auto-restart server
PORT_FORWARD_ONLY=false
STOP_PORT_FORWARD=false
RESTART_SERVER=false
SYNC_CODE=false
LOCAL_PORT=8080

# Show help
show_help() {
    cat << 'EOF'
In-S8 Optimizer Deployment Script

Usage: ./deployment/deploy.sh [options]

By default, this script deploys In-S8 optimizer to your cluster automatically.
Use --just-yaml to only generate YAML without deploying.

Options:
  -n, --namespace NAME        Kubernetes namespace (default: llm-d)
  -i, --image IMAGE           Container image (default: quay.io/bbenshab/vllm:in-s8)

  Storage Options (choose one):
  -p, --pvc-name NAME         Use existing PVC (skips PVC creation)
  -s, --storage-class CLASS   Create new PVC with this storage class
      --storage-size SIZE     PVC size when creating new (default: 100Gi)

  Network Options:
      --force-nad             Force NAD (Multus) mode instead of DRA (default: auto-detect)

  Dev Options:
      --dev                   Deploy in dev mode (auto-sync code to PVC, auto-restart server)
      --sync                  Re-sync local code to running dev pod (no redeploy)

  Port Forward & Server:
      --port-forward          Start port-forward in background (no deploy)
      --stop-port-forward     Stop background port-forward
      --restart-server        Restart the server in the pod and re-establish port-forward
      --local-port PORT       Local port for port-forward (default: 8080)

      --just-yaml             Only output YAML, do not deploy
  -h, --help                  Show this help message

Examples:
  # Deploy automatically with defaults (DRA auto-detected on IBM Cloud)
  ./deployment/deploy.sh

  # Deploy with specific storage class
  ./deployment/deploy.sh --storage-class nfs-csi --storage-size 200Gi

  # Deploy with existing PVC
  ./deployment/deploy.sh --pvc-name my-existing-pvc

  # Force NAD (Multus) mode instead of DRA
  ./deployment/deploy.sh --force-nad

  # Deploy in dev mode (auto-sync code, auto-restart server)
  ./deployment/deploy.sh --dev

  # Re-sync code to running dev pod (no redeploy)
  ./deployment/deploy.sh --sync

  # Start port-forward to access the UI (no deploy)
  ./deployment/deploy.sh --port-forward

  # Restart server in the pod (kills old, starts new, re-establishes port-forward)
  ./deployment/deploy.sh --restart-server

  # Stop background port-forward
  ./deployment/deploy.sh --stop-port-forward

  # Only generate YAML (for manual review/deployment)
  ./deployment/deploy.sh --just-yaml > in-s8-optimizer.yaml
  less in-s8-optimizer.yaml
  kubectl apply -f in-s8-optimizer.yaml

  # Update the deployment with a new image
  kubectl set image deployment/in-s8-optimizer runner=quay.io/bbenshab/vllm:new-tag -n <namespace>

EOF
}

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        -h|--help)
            show_help
            exit 0
            ;;
        -n|--namespace)
            NAMESPACE="$2"
            shift 2
            ;;
        -i|--image)
            IMAGE="$2"
            shift 2
            ;;
        -p|--pvc-name)
            PVC_NAME="$2"
            shift 2
            ;;
        -s|--storage-class)
            STORAGE_CLASS="$2"
            shift 2
            ;;
        --storage-size)
            STORAGE_SIZE="$2"
            shift 2
            ;;
        --force-nad)
            FORCE_NAD="true"
            shift
            ;;
        --dev)
            DEV_MODE="true"
            shift
            ;;
        --port-forward)
            PORT_FORWARD_ONLY=true
            shift
            ;;
        --stop-port-forward)
            STOP_PORT_FORWARD=true
            shift
            ;;
        --restart-server)
            RESTART_SERVER=true
            shift
            ;;
        --sync)
            SYNC_CODE=true
            shift
            ;;
        --local-port)
            LOCAL_PORT="$2"
            shift 2
            ;;
        --just-yaml)
            AUTO_DEPLOY=false
            shift
            ;;
        *)
            echo "Error: Unknown option: $1" >&2
            echo "Run with --help for usage information" >&2
            exit 1
            ;;
    esac
done

# Detect cluster type (OpenShift vs vanilla Kubernetes)
IS_OPENSHIFT="false"
if kubectl api-resources --api-group=route.openshift.io 2>/dev/null | grep -q Route; then
    IS_OPENSHIFT="true"
fi
# grep -q returns 1 on no match, safe to continue

# Detect CoreWeave (GPU nodes have gpu.coreweave.cloud labels)
IS_COREWEAVE="false"
KUBECTL_DETECT=$(command -v oc 2>/dev/null || command -v kubectl 2>/dev/null || true)
if [[ -n "$KUBECTL_DETECT" ]]; then
    if $KUBECTL_DETECT get nodes -l backend.coreweave.cloud/enabled --no-headers 2>/dev/null | grep -q .; then
        IS_COREWEAVE="true"
    fi
fi

# Determine PVC strategy — storage class is mandatory
if [[ -n "$PVC_NAME" ]]; then
    CREATE_PVC="false"
    ACTUAL_PVC_NAME="$PVC_NAME"
elif [[ -n "$STORAGE_CLASS" ]]; then
    CREATE_PVC="true"
    ACTUAL_PVC_NAME="in-s8-storage"
else
    # Not needed for port-forward/stop commands
    if [[ "$PORT_FORWARD_ONLY" != "true" && "$STOP_PORT_FORWARD" != "true" && "$RESTART_SERVER" != "true" && "$SYNC_CODE" != "true" ]]; then
        echo "Error: Storage is required. Use one of:" >&2
        echo "  -p, --pvc-name NAME         Use an existing PVC" >&2
        echo "  -s, --storage-class CLASS   Create a new PVC with this storage class" >&2
        exit 1
    fi
    ACTUAL_PVC_NAME=""
fi

# Function to generate YAML
generate_yaml() {
cat << EOF
# Generated by In-S8 deployment/deploy.sh
# Namespace: ${NAMESPACE}
# Image: ${IMAGE}
# PVC: ${ACTUAL_PVC_NAME}
# Storage Class: ${STORAGE_CLASS:-default}
# Storage Size: ${STORAGE_SIZE}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: in-s8-optimizer-prometheus-access
subjects:
- kind: ServiceAccount
  name: default
  namespace: ${NAMESPACE}
roleRef:
  kind: ClusterRole
  name: prometheus-k8s
  apiGroup: rbac.authorization.k8s.io
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: in-s8-optimizer-pod-manager
  namespace: ${NAMESPACE}
rules:
# Core resource management
- apiGroups: [""]
  resources: ["pods", "pods/log", "pods/exec"]
  verbs: ["get", "list", "create", "delete", "patch", "watch"]
- apiGroups: [""]
  resources: ["persistentvolumeclaims"]
  verbs: ["get", "list", "create", "delete", "patch"]
- apiGroups: ["apps"]
  resources: ["deployments", "statefulsets"]
  verbs: ["get", "list", "create", "delete", "patch", "update"]
- apiGroups: ["batch"]
  resources: ["jobs"]
  verbs: ["get", "list", "create", "delete", "patch", "watch"]
- apiGroups: ["leaderworkerset.x-k8s.io"]
  resources: ["leaderworkersets"]
  verbs: ["get", "list", "create", "delete", "patch", "update"]
- apiGroups: [""]
  resources: ["services"]
  verbs: ["get", "list", "create", "delete", "patch"]
# Prerequisite infrastructure (GAIE, Gateway, InferencePool)
- apiGroups: [""]
  resources: ["serviceaccounts", "configmaps", "secrets"]
  verbs: ["get", "list", "create", "delete", "patch"]
- apiGroups: ["rbac.authorization.k8s.io"]
  resources: ["roles", "rolebindings"]
  verbs: ["get", "list", "create", "delete", "patch"]
- apiGroups: ["inference.networking.k8s.io"]
  resources: ["inferencepools"]
  verbs: ["get", "list", "create", "delete", "patch", "watch"]
- apiGroups: ["inference.networking.x-k8s.io"]
  resources: ["inferencemodelrewrites", "inferenceobjectives"]
  verbs: ["get", "list", "watch"]
- apiGroups: ["gateway.networking.k8s.io"]
  resources: ["gateways", "httproutes"]
  verbs: ["get", "list", "create", "delete", "patch"]
- apiGroups: ["networking.istio.io"]
  resources: ["destinationrules"]
  verbs: ["get", "list", "create", "delete", "patch"]
# Network integration (DRA and NAD)
- apiGroups: ["resource.k8s.io"]
  resources: ["resourceclaimtemplates", "resourceclaims"]
  verbs: ["get", "list", "create", "delete", "patch", "update"]
- apiGroups: ["k8s.cni.cncf.io"]
  resources: ["network-attachment-definitions"]
  verbs: ["get", "list", "create", "delete", "patch", "update"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: in-s8-optimizer-pod-manager-binding
  namespace: ${NAMESPACE}
subjects:
- kind: ServiceAccount
  name: default
  namespace: ${NAMESPACE}
roleRef:
  kind: Role
  name: in-s8-optimizer-pod-manager
  apiGroup: rbac.authorization.k8s.io
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: in-s8-optimizer-cluster-manager
rules:
# Cluster-scoped RBAC for prerequisite deployment (GAIE needs ClusterRole/ClusterRoleBinding)
- apiGroups: ["rbac.authorization.k8s.io"]
  resources: ["clusterroles", "clusterrolebindings"]
  verbs: ["get", "list", "create", "delete", "patch"]
# GAIE ClusterRole permissions (needed to create GAIE's ClusterRole)
- apiGroups: ["authentication.k8s.io"]
  resources: ["tokenreviews"]
  verbs: ["create"]
- apiGroups: ["authorization.k8s.io"]
  resources: ["subjectaccessreviews"]
  verbs: ["create"]
- nonResourceURLs: ["/metrics"]
  verbs: ["get"]
# Thanos/Prometheus metrics collection
- apiGroups: ["route.openshift.io"]
  resources: ["routes"]
  verbs: ["get", "list"]
  resourceNames: ["thanos-querier"]
- apiGroups: [""]
  resources: ["services", "endpoints"]
  verbs: ["get", "list"]
# GPU usage monitoring (read pods across all namespaces)
- apiGroups: [""]
  resources: ["pods"]
  verbs: ["get", "list"]
# Cloud provider detection (OpenShift infrastructure resource)
- apiGroups: ["config.openshift.io"]
  resources: ["infrastructures"]
  verbs: ["get", "list"]
# DRANET detection (DRA DeviceClass)
- apiGroups: ["resource.k8s.io"]
  resources: ["deviceclasses"]
  verbs: ["get", "list"]
# Node resource scanning
- apiGroups: [""]
  resources: ["nodes"]
  verbs: ["get", "list"]
# Storage class scanning
- apiGroups: ["storage.k8s.io"]
  resources: ["storageclasses"]
  verbs: ["get", "list"]
# Namespace labeling (for OpenShift User Workload Monitoring)
- apiGroups: [""]
  resources: ["namespaces"]
  verbs: ["get", "list", "patch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: in-s8-optimizer-cluster-manager-binding
subjects:
- kind: ServiceAccount
  name: default
  namespace: ${NAMESPACE}
roleRef:
  kind: ClusterRole
  name: in-s8-optimizer-cluster-manager
  apiGroup: rbac.authorization.k8s.io
EOF

# Conditionally create PVC
if [[ "$CREATE_PVC" == "true" ]]; then
    cat << EOF
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: ${ACTUAL_PVC_NAME}
  namespace: ${NAMESPACE}
  labels:
    app: in-s8-optimizer
    component: storage
spec:
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: ${STORAGE_SIZE}
EOF

    # Add storageClassName only if specified
    if [[ -n "$STORAGE_CLASS" ]]; then
        cat << EOF
  storageClassName: ${STORAGE_CLASS}
EOF
    fi
fi

# Pod definition
cat << EOF
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: in-s8-optimizer
  namespace: ${NAMESPACE}
  labels:
    app: in-s8-optimizer
spec:
  replicas: 1
  strategy:
    type: Recreate  # Use Recreate to avoid two pods mounting same PVC
  selector:
    matchLabels:
      app: in-s8-optimizer
  template:
    metadata:
      labels:
        app: in-s8-optimizer
    spec:
      serviceAccountName: default
      volumes:
        - name: storage-volume
          persistentVolumeClaim:
            claimName: ${ACTUAL_PVC_NAME}
        - name: tmp-mem
          emptyDir:
            medium: Memory
            sizeLimit: 4Gi
      containers:
        - name: runner
          image: ${IMAGE}
          imagePullPolicy: Always
          command:
            - "/bin/sh"
            - "-c"
            - |
              echo "--- Starting In-S8 Optimizer ---"
              if [ "\${DEV_MODE}" = "true" ]; then
                CODE_DIR="/mnt/storage/app"
                echo "Dev mode: waiting for code at \${CODE_DIR}/web/server.py ..."
                while [ ! -f "\${CODE_DIR}/web/server.py" ]; do sleep 2; done
                echo "Code found. Starting server (auto-restart on crash)..."
                export IN_S8_PATH="\${CODE_DIR}"
                while true; do
                  cd "\${CODE_DIR}/web"
                  find "\${CODE_DIR}" -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
                  python3.11 server.py || true
                  echo "Server exited, restarting in 3s..."
                  sleep 3
                done
              else
                echo "Starting web server from image..."
                cd /app/web
                exec python3.11 server.py
              fi
          env:
            - name: IN_S8_PATH
              value: "/app"
            - name: OPTIMIZATION_OUTPUT_DIR
              value: "/mnt/storage/optimization-runs"
            - name: HOME_STORAGE_DIR
              value: "/mnt/storage"
            - name: TARGET_NAMESPACE
              value: "${NAMESPACE}"
            - name: METRIC_STEP_SECONDS
              value: "5"
            - name: POD_NAME_PATTERN
              value: "wide-ep-"
            - name: MODEL
              value: "RedHatAI/Qwen3-235B-A22B-FP8-dynamic"
            - name: INFERENCE_GATEWAY
              value: "http://infra-ep-inference-gateway-istio.${NAMESPACE}.svc.cluster.local:80"
            - name: DB_PATH
              value: "/mnt/storage/in-s8.db"
            - name: IN_S8_FORCE_NAD
              value: "${FORCE_NAD}"
            - name: DEV_MODE
              value: "${DEV_MODE}"
            - name: HF_TOKEN
              valueFrom:
                secretKeyRef:
                  name: in-s8-optimizer-hf-token
                  key: HF_TOKEN
                  optional: true
          volumeMounts:
            - name: storage-volume
              mountPath: /mnt/storage
            - name: tmp-mem
              mountPath: /tmp
          ports:
            - containerPort: 5000
          resources:
            requests:
              memory: "2Gi"
              cpu: "1000m"
---
apiVersion: v1
kind: Service
metadata:
  name: in-s8-optimizer-ui
  namespace: ${NAMESPACE}
  labels:
    app: in-s8-optimizer
spec:
  ports:
  - name: http
    port: 5000
    protocol: TCP
    targetPort: 5000
  selector:
    app: in-s8-optimizer
EOF

if [[ "$IS_OPENSHIFT" == "true" ]]; then
    # OpenShift: ClusterIP + Route
    cat << EOF
  type: ClusterIP
---
apiVersion: route.openshift.io/v1
kind: Route
metadata:
  name: in-s8-optimizer-ui
  namespace: ${NAMESPACE}
  labels:
    app: in-s8-optimizer
spec:
  port:
    targetPort: 5000
  to:
    kind: Service
    name: in-s8-optimizer-ui
    weight: 100
  tls:
    termination: edge
    insecureEdgeTerminationPolicy: Redirect
EOF
else
    # Vanilla Kubernetes: LoadBalancer
    cat << EOF
  type: LoadBalancer
EOF
fi
}

PF_PID_FILE="/tmp/in-s8-port-forward.pid"

# Start port-forward in background with auto-reconnect
start_port_forward() {
    local kubectl_cmd="$1"
    local namespace="$2"
    local local_port="$3"

    # Kill existing port-forward if running
    stop_port_forward quiet

    echo "🔌 Starting port-forward (localhost:${local_port} -> svc/in-s8-optimizer-ui:5000)..." >&2

    # Wrapper loop: reconnects when kubectl port-forward exits (e.g. server restart)
    (
        set +e
        while true; do
            start_time=$(date +%s)
            $kubectl_cmd port-forward -n "$namespace" svc/in-s8-optimizer-ui "${local_port}:5000" &>/dev/null
            elapsed=$(( $(date +%s) - start_time ))
            if [[ $elapsed -lt 3 ]]; then
                sleep 2
            fi
        done
    ) &
    local pid=$!
    echo "$pid" > "$PF_PID_FILE"

    sleep 2 || true

    if kill -0 "$pid" 2>/dev/null; then
        echo "✅ Port-forward running (PID: $pid)" >&2
        echo "   Web UI: http://localhost:${local_port}" >&2
        echo "   Auto-reconnect is enabled — port-forward will restart" >&2
        echo "   automatically if the server restarts or the connection drops." >&2
        echo "   Stop with: ./deployment/deploy.sh --stop-port-forward" >&2
    else
        echo "⚠️  Port-forward failed to start" >&2
        rm -f "$PF_PID_FILE"
    fi
    return 0
}

# Stop port-forward
stop_port_forward() {
    local quiet="${1:-}"
    if [[ -f "$PF_PID_FILE" ]]; then
        local pid=$(cat "$PF_PID_FILE")
        if kill -0 "$pid" 2>/dev/null; then
            pkill -P "$pid" 2>/dev/null || true
            kill "$pid" 2>/dev/null
            [[ -z "$quiet" ]] && echo "✅ Port-forward stopped (PID: $pid)" >&2 || true
        else
            [[ -z "$quiet" ]] && echo "ℹ️  Port-forward was not running" >&2 || true
        fi
        rm -f "$PF_PID_FILE"
    else
        [[ -z "$quiet" ]] && echo "ℹ️  No port-forward running" >&2 || true
    fi
}

# Restart server in the pod: kill old process, start new one, reconnect port-forward
restart_server() {
    local kubectl_cmd="$1"
    local namespace="$2"
    local local_port="$3"

    # Find the pod
    local pod_name=$($kubectl_cmd get pod -n "$namespace" -l app=in-s8-optimizer -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
    if [[ -z "$pod_name" ]]; then
        echo "❌ No in-s8-optimizer pod found in namespace $namespace" >&2
        exit 1
    fi
    echo "🔄 Restarting server in pod: $pod_name" >&2

    # Kill existing port-forward first (it will die anyway when server stops)
    stop_port_forward quiet

    # Kill existing server process in the pod
    echo "   Stopping old server..." >&2
    $kubectl_cmd exec -n "$namespace" "$pod_name" -- bash -c "pkill -f 'python.*server.py' || true" 2>/dev/null || true
    sleep 1

    # Detect code location: PVC (/mnt/storage/app) takes priority over image (/app)
    local code_path="/app"
    if $kubectl_cmd exec -n "$namespace" "$pod_name" -- test -f /mnt/storage/app/web/server.py 2>/dev/null; then
        code_path="/mnt/storage/app"
        echo "   Using code from PVC: $code_path" >&2
    else
        echo "   Using code from image: $code_path" >&2
    fi

    # Start server in background (close stdin/stdout/stderr so kubectl exec returns immediately)
    echo "   Starting server..." >&2
    $kubectl_cmd exec -n "$namespace" "$pod_name" -- bash -c \
        "cd $code_path && IN_S8_PATH=$code_path nohup python3 web/server.py > /tmp/server.log 2>&1 < /dev/null &" 2>/dev/null

    # Wait for server to bind to port (use Python socket check — works on any platform)
    echo "   Waiting for server to start..." >&2
    local retries=0
    local max_retries=15
    while [[ $retries -lt $max_retries ]]; do
        if $kubectl_cmd exec -n "$namespace" "$pod_name" -- python3 -c "import socket; s=socket.socket(); s.settimeout(1); s.connect(('127.0.0.1',5000)); s.close()" 2>/dev/null; then
            break
        fi
        sleep 1
        retries=$((retries + 1))
    done

    if [[ $retries -ge $max_retries ]]; then
        echo "⚠️  Server may not have started. Check logs:" >&2
        echo "   kubectl exec -n $namespace $pod_name -- tail -20 /tmp/server.log" >&2
        return 1
    fi

    echo "   ✅ Server started" >&2

    # Re-establish port-forward (skip on OpenShift — Routes handle access)
    if [[ "$kubectl_cmd" != "oc" ]]; then
        start_port_forward "$kubectl_cmd" "$namespace" "$local_port"
    else
        local route_host=$($kubectl_cmd get route -n "$namespace" in-s8-optimizer -o jsonpath='{.spec.host}' 2>/dev/null)
        if [[ -n "$route_host" ]]; then
            echo "🌐 Web UI: https://$route_host" >&2
        fi
    fi
}

# Handle --stop-port-forward, --restart-server, and --port-forward early exits
if [[ "$STOP_PORT_FORWARD" == "true" ]]; then
    stop_port_forward
    exit 0
fi

# Detect kubectl/oc command
detect_kubectl() {
    if command -v oc &> /dev/null; then
        echo "oc"
    elif command -v kubectl &> /dev/null; then
        echo "kubectl"
    else
        echo "Error: Neither kubectl nor oc found. Please install Kubernetes CLI." >&2
        exit 1
    fi
}

sync_code_to_pod() {
    local kubectl_cmd="$1"
    local namespace="$2"
    local pod_name="$3"
    local repo_root="$4"
    local local_manifest=$(mktemp)
    local remote_manifest=$(mktemp)
    trap "rm -f $local_manifest $remote_manifest" RETURN

    $kubectl_cmd exec -n ${namespace} "$pod_name" -- mkdir -p /mnt/storage/app 2>/dev/null || true

    # Build local manifest: "md5 path" sorted by path
    (cd "$repo_root" && find . -type f \
        -not -path './.git/*' \
        -not -path './.claude/*' \
        -not -path '*/__pycache__/*' \
        -not -name '.DS_Store' \
        -exec md5 -r {} \; 2>/dev/null || \
     find . -type f \
        -not -path './.git/*' \
        -not -path './.claude/*' \
        -not -path '*/__pycache__/*' \
        -not -name '.DS_Store' \
        -exec md5sum {} \;
    ) | awk '{print $1, $2}' | sort -k2 > "$local_manifest"

    # Build remote manifest in a single exec call
    $kubectl_cmd exec -n ${namespace} "$pod_name" -- bash -c '
        cd /mnt/storage/app 2>/dev/null || exit 0
        find . -type f -exec md5sum {} \;
    ' 2>/dev/null | awk '{print $1, $2}' | sort -k2 > "$remote_manifest"

    # Compare: find files to copy (new or changed hash)
    local to_copy=()
    while IFS=' ' read -r local_hash local_path; do
        [[ -z "$local_path" ]] && continue
        remote_hash=$(awk -v p="$local_path" '$2==p {print $1; exit}' "$remote_manifest")
        if [[ "$local_hash" != "$remote_hash" ]]; then
            to_copy+=("$local_path")
        fi
    done < "$local_manifest"

    # Find files to delete (on remote but not local)
    local to_delete=()
    while IFS=' ' read -r _ remote_path; do
        [[ -z "$remote_path" ]] && continue
        if ! grep -q " ${remote_path}$" "$local_manifest"; then
            to_delete+=("$remote_path")
        fi
    done < "$remote_manifest"

    if [[ ${#to_copy[@]} -eq 0 && ${#to_delete[@]} -eq 0 ]]; then
        echo "   Already up to date." >&2
        return 0
    fi

    # Create all needed directories in one exec call
    if [[ ${#to_copy[@]} -gt 0 ]]; then
        local dirs=$(printf '%s\n' "${to_copy[@]}" | sed 's|^\./||' | xargs -I{} dirname {} | sort -u | grep -v '^\.$')
        if [[ -n "$dirs" ]]; then
            $kubectl_cmd exec -n ${namespace} "$pod_name" -- bash -c "cd /mnt/storage/app && echo '$dirs' | xargs -I{} mkdir -p {}" 2>/dev/null || true
        fi
    fi

    # Copy changed files
    local copied=0
    for f in "${to_copy[@]}"; do
        local rel="${f#./}"
        $kubectl_cmd cp "$repo_root/$rel" "${namespace}/${pod_name}:/mnt/storage/app/$rel" 2>/dev/null || true
        copied=$((copied + 1))
    done

    # Delete stale remote files
    local deleted=0
    if [[ ${#to_delete[@]} -gt 0 ]]; then
        local del_list=$(printf '%s\n' "${to_delete[@]}" | sed 's|^\./|/mnt/storage/app/|')
        $kubectl_cmd exec -n ${namespace} "$pod_name" -- bash -c "echo '$del_list' | xargs rm -f" 2>/dev/null || true
        deleted=${#to_delete[@]}
    fi

    echo "   ${copied} file(s) updated, ${deleted} file(s) removed." >&2
}

if [[ "$PORT_FORWARD_ONLY" == "true" ]]; then
    KUBECTL_CMD=$(detect_kubectl)
    start_port_forward "$KUBECTL_CMD" "$NAMESPACE" "$LOCAL_PORT"
    exit $?
fi

if [[ "$SYNC_CODE" == "true" || "$RESTART_SERVER" == "true" ]]; then
    KUBECTL_CMD=$(detect_kubectl)

    if [[ "$SYNC_CODE" == "true" ]]; then
        SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
        REPO_ROOT="$SCRIPT_DIR/.."

        POD_NAME=$($KUBECTL_CMD get pod -n ${NAMESPACE} -l app=in-s8-optimizer -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
        if [[ -z "$POD_NAME" ]]; then
            echo "❌ No optimizer pod found in namespace $NAMESPACE" >&2
            exit 1
        fi

        echo "📦 Syncing code to pod $POD_NAME..." >&2
        sync_code_to_pod "$KUBECTL_CMD" "$NAMESPACE" "$POD_NAME" "$REPO_ROOT"
        echo "" >&2
    fi

    if [[ "$RESTART_SERVER" == "true" ]]; then
        restart_server "$KUBECTL_CMD" "$NAMESPACE" "$LOCAL_PORT"
    else
        echo "   Code synced. Server will auto-restart and pick up changes." >&2
        echo "   To force restart: ./deployment/deploy.sh --restart-server" >&2
    fi
    exit 0
fi

# Check if PVC exists and handle storage class conflicts
check_pvc_conflict() {
    local kubectl_cmd="$1"
    local namespace="$2"
    local pvc_name="$3"
    local requested_storage_class="$4"

    # Check if PVC exists
    if ! $kubectl_cmd get pvc "$pvc_name" -n "$namespace" &>/dev/null; then
        return 0  # PVC doesn't exist, no conflict
    fi

    # Get existing storage class
    local existing_storage_class=$($kubectl_cmd get pvc "$pvc_name" -n "$namespace" -o jsonpath='{.spec.storageClassName}' 2>/dev/null)

    # If no storage class was requested, use existing PVC
    if [[ -z "$requested_storage_class" ]]; then
        echo "ℹ️  Using existing PVC: $pvc_name (storage class: ${existing_storage_class:-default})" >&2
        return 0
    fi

    # Check if storage classes match
    if [[ "$existing_storage_class" != "$requested_storage_class" ]]; then
        echo "" >&2
        echo "⚠️  PVC STORAGE CLASS CONFLICT DETECTED" >&2
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" >&2
        echo "   PVC Name: $pvc_name" >&2
        echo "   Existing Storage Class: ${existing_storage_class:-default}" >&2
        echo "   Requested Storage Class: $requested_storage_class" >&2
        echo "" >&2
        echo "   PVC storage class cannot be changed after creation." >&2
        echo "" >&2
        echo "   Options:" >&2
        echo "   1. Use existing PVC (ignore -s flag)" >&2
        echo "   2. Delete and recreate PVC (⚠️  DATA LOSS)" >&2
        echo "   3. Abort deployment" >&2
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" >&2
        echo "" >&2
        read -p "Choose [1/2/3]: " choice

        case $choice in
            1)
                echo "✅ Using existing PVC with storage class: ${existing_storage_class:-default}" >&2
                # Switch to using existing PVC (don't create new one)
                CREATE_PVC="false"
                PVC_NAME="$pvc_name"
                ACTUAL_PVC_NAME="$pvc_name"
                return 0
                ;;
            2)
                echo "⚠️  WARNING: Deleting PVC will permanently delete all data!" >&2
                read -p "Type 'DELETE' to confirm: " confirm
                if [[ "$confirm" == "DELETE" ]]; then
                    echo "🗑️  Deleting PVC: $pvc_name" >&2
                    $kubectl_cmd delete pvc "$pvc_name" -n "$namespace"
                    echo "✅ PVC deleted. Will create new PVC with storage class: $requested_storage_class" >&2
                    return 0
                else
                    echo "❌ Deletion cancelled. Aborting deployment." >&2
                    return 1
                fi
                ;;
            3|*)
                echo "❌ Deployment aborted." >&2
                return 1
                ;;
        esac
    fi

    return 0
}

# Main logic
if [[ "$AUTO_DEPLOY" == "false" ]]; then
    # Just output YAML to stdout
    generate_yaml
else
    # Deploy automatically
    KUBECTL_CMD=$(detect_kubectl)

    echo "🚀 Deploying In-S8 Optimizer to namespace: ${NAMESPACE}" >&2
    echo "" >&2

    # Check for LeaderWorkerSet CRD (required for all pod deployments)
    if ! $KUBECTL_CMD api-resources 2>/dev/null | grep -q leaderworkerset; then
        echo "❌ LeaderWorkerSet (LWS) CRD not found on this cluster." >&2
        echo "   In-S8 requires LeaderWorkerSet for all pod deployments." >&2
        echo "" >&2
        echo "   Install it with:" >&2
        echo "   kubectl apply --server-side -f https://github.com/kubernetes-sigs/lws/releases/latest/download/manifests.yaml" >&2
        echo "" >&2
        exit 1
    fi

    # Check for Istio (required for Gateway API inference routing)
    if ! $KUBECTL_CMD get namespace istio-system &>/dev/null; then
        echo "❌ istio-system namespace not found." >&2
        echo "   In-S8 requires Istio for Gateway API inference routing." >&2
        echo "" >&2
        echo "   Install Istio before deploying In-S8." >&2
        echo "" >&2
        exit 1
    fi

    # Create namespace if it doesn't exist
    if ! $KUBECTL_CMD get namespace "$NAMESPACE" &>/dev/null; then
        echo "📦 Creating namespace: ${NAMESPACE}" >&2
        $KUBECTL_CMD create namespace "$NAMESPACE"
    fi

    # On vanilla K8s (not OpenShift), copy Istio pull secret to target namespace
    # so Gateway pods can pull the proxy image (e.g., Red Hat Service Mesh on CoreWeave)
    if [[ "$IS_OPENSHIFT" != "true" ]]; then
        if $KUBECTL_CMD get secret rhaii-pull-secret -n istio-system &>/dev/null; then
            if ! $KUBECTL_CMD get secret rhaii-pull-secret -n "$NAMESPACE" &>/dev/null; then
                echo "🔑 Copying Istio pull secret to ${NAMESPACE}..." >&2
                $KUBECTL_CMD get secret rhaii-pull-secret -n istio-system -o json \
                    | python3 -c "
import sys, json
s = json.load(sys.stdin)
s['metadata'] = {'name': s['metadata']['name'], 'namespace': '$NAMESPACE'}
json.dump(s, sys.stdout)" \
                    | $KUBECTL_CMD apply -f - >&2
                echo "   ✅ Pull secret copied" >&2
            fi
        fi
    fi

    # Check for PVC conflicts before deploying
    if [[ "$CREATE_PVC" == "true" ]]; then
        if ! check_pvc_conflict "$KUBECTL_CMD" "$NAMESPACE" "$ACTUAL_PVC_NAME" "$STORAGE_CLASS"; then
            exit 1
        fi
    fi

    # Note: RDMA discovery ConfigMap is now deployed by prereq_manager.py during Step 1

    # Apply YAML
    if generate_yaml | $KUBECTL_CMD apply -f -; then
        echo "✅ Deployment successful!" >&2
        echo "" >&2

        # Wait for deployment to be ready
        echo "⏳ Waiting for deployment to be ready..." >&2
        $KUBECTL_CMD wait --for=condition=available deployment/in-s8-optimizer -n ${NAMESPACE} --timeout=300s 2>&1 | sed 's/^/   /' >&2

        echo "" >&2
        echo "============================================================" >&2
        echo "✅ In-S8 Optimizer deployment complete!" >&2
        echo "============================================================" >&2
        echo "" >&2

        if [[ "$IS_OPENSHIFT" == "true" ]]; then
            ROUTE_HOST=$($KUBECTL_CMD get route in-s8-optimizer-ui -n ${NAMESPACE} -o jsonpath='{.spec.host}' 2>/dev/null)
            if [[ -n "$ROUTE_HOST" ]]; then
                echo "   Web UI: https://${ROUTE_HOST}" >&2
            fi
        fi

        if [[ "$DEV_MODE" == "true" ]]; then
            POD_NAME=$($KUBECTL_CMD get pod -n ${NAMESPACE} -l app=in-s8-optimizer -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
            SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
            REPO_ROOT="$SCRIPT_DIR/.."

            echo "📦 Syncing code to pod..." >&2
            sync_code_to_pod "$KUBECTL_CMD" "$NAMESPACE" "$POD_NAME" "$REPO_ROOT"
            echo "" >&2
            echo "   Server will auto-start and auto-restart on crash." >&2
            echo "   To re-sync code: ./deployment/deploy.sh --sync" >&2
        fi

        echo "" >&2
        if [[ "$IS_OPENSHIFT" == "true" ]]; then
            ROUTE_HOST=$($KUBECTL_CMD get route in-s8-optimizer-ui -n ${NAMESPACE} -o jsonpath='{.spec.host}' 2>/dev/null)
            if [[ -n "$ROUTE_HOST" ]]; then
                echo "🌐 Web UI: https://${ROUTE_HOST}" >&2
            fi
        else
            # Vanilla Kubernetes: auto-start port-forward
            start_port_forward "$KUBECTL_CMD" "$NAMESPACE" "$LOCAL_PORT" || true
        fi
        echo "============================================================" >&2
    else
        echo "❌ Deployment failed!" >&2
        exit 1
    fi
fi
