# DRA-NET Setup — GPU + RDMA NIC Pairing

Hi Mark,

This doc walks you through setting up DRA-NET on a Kubernetes cluster so that each GPU gets paired with its closest RDMA NIC via PCIe affinity. By the end you'll be able to deploy pods that request `dra.llm-d.io/gpu-nic-pair: "8"` and get 8 GPUs + 8 RDMA NICs automatically paired by hardware topology.

I know you ran into TLS/caBundle issues before — Step 2 covers that specifically with the exact commands to fix it.

---

## Why This Matters

When NCCL does AllReduce across nodes, data goes GPU → PCIe → NIC → network. If the GPU and NIC are on different PCIe root complexes, the data has to cross the CPU socket — that adds latency and kills bandwidth. DRA-NET makes sure each GPU always gets the NIC that's physically closest to it on the PCIe bus.

---

## What You Need Before Starting

- Kubernetes 1.31+ with the DRA feature gate enabled
- NVIDIA DRA GPU driver installed on the cluster
- DRA-NET driver installed on the cluster
- GPU nodes with RDMA-capable NICs (InfiniBand or RoCE)
- `openssl` on the machine where you'll run deployment commands

---

## Step 1: Verify DRA Drivers Are Installed

Before anything else, confirm both DRA drivers are running.

```bash
kubectl get deviceclass
```

You need to see at least:
```
NAME             AGE
gpu.nvidia.com   ...
dranet           ...
```

If either is missing, get the corresponding driver installed first. This guide assumes both are already running.

Now verify devices expose PCIe topology (this is what enables the pairing):

```bash
kubectl get resourceslice -o json | python3 -c "
import json, sys
data = json.load(sys.stdin)
for item in data.get('items', []):
    driver = item.get('spec', {}).get('driver', '')
    node = item.get('spec', {}).get('nodeName', '')
    devices = item.get('spec', {}).get('devices') or []
    for d in devices:
        attrs = d.get('attributes', {})
        rdma = attrs.get('dra.net/rdma', {}).get('bool', False)
        pcie = attrs.get('resource.kubernetes.io/pcieRoot', {}).get('string', '')
        if rdma or driver == 'gpu.nvidia.com':
            ifname = attrs.get('dra.net/ifName', {}).get('string', '')
            print(f'{node}  {driver:20s}  {d[\"name\"]:25s}  pcie={pcie:15s}  rdma={rdma}  if={ifname}')
"
```

You want to see GPUs and RDMA NICs sharing the same `pcie=` value:
```
worker-1  gpu.nvidia.com        gpu-0                      pcie=pci0000:e6      rdma=False  if=
worker-1  dra.net               pci-0000-e9-00-0           pcie=pci0000:e6      rdma=True   if=enp233s0
```

Both have `pcie=pci0000:e6` → same PCIe root. If the `pcie=` column is empty, your DRA drivers aren't exposing PCIe topology and pairing won't work.

---

## Step 2: Deploy the Admission Webhook (with TLS Fix)

This is where the TLS issue you hit before comes in. The webhook needs a valid TLS certificate, and the `MutatingWebhookConfiguration` needs the matching CA bundle. Here's how to do it properly.

### Clone the Repo

```bash
git clone https://github.com/openshift-psap/dra-rail-admission-webhook.git
cd dra-rail-admission-webhook
```

### Build

```bash
make build
```

### Generate TLS Certificates

This is the critical part. The certificate's CN and SAN must match the webhook service name exactly:

```bash
NAMESPACE=dra-webhook-system

# Create the namespace
kubectl create namespace $NAMESPACE --dry-run=client -o yaml | kubectl apply -f -

# Generate self-signed cert with the correct service DNS name
openssl req -x509 -newkey rsa:4096 \
  -keyout certs/tls.key -out certs/tls.crt \
  -days 365 -nodes \
  -subj "/CN=dra-gpu-nic-webhook.${NAMESPACE}.svc" \
  -addext "subjectAltName=DNS:dra-gpu-nic-webhook.${NAMESPACE}.svc,DNS:dra-gpu-nic-webhook.${NAMESPACE}.svc.cluster.local"
```

### Create the TLS Secret

```bash
kubectl create secret tls dra-gpu-nic-webhook-tls \
  --cert=certs/tls.crt --key=certs/tls.key \
  -n $NAMESPACE --dry-run=client -o yaml | kubectl apply -f -
```

### Update the caBundle in the Webhook Config

**This is the step that was causing your TLS issue.** The `deploy/base/webhook-config.yaml` file has a hardcoded `caBundle` from a previous deployment. You MUST replace it with your newly generated certificate:

```bash
# Get the base64-encoded CA certificate
CABUNDLE=$(cat certs/tls.crt | base64 | tr -d '\n')
echo "Your caBundle:"
echo $CABUNDLE
```

Now edit `deploy/base/webhook-config.yaml` and replace BOTH `caBundle` values (there are two — one for `/mutate` and one for `/mutate-ext`):

```bash
# Option A: sed replacement (if on Linux/Mac)
sed -i "s|caBundle:.*|caBundle: ${CABUNDLE}|g" deploy/base/webhook-config.yaml

# Option B: manual edit
# Open deploy/base/webhook-config.yaml
# Find the two "caBundle:" lines
# Replace the base64 string with the value from $CABUNDLE
```

### Deploy Everything

**Important:** Do NOT use `make deploy` by itself — it generates new certs but applies the YAML with the OLD caBundle baked into `webhook-config.yaml`. This is the exact bug you hit before. Instead, do it in the right order:

```bash
# Apply everything EXCEPT the webhook config first
kubectl apply -f deploy/base/webhook-deployment.yaml -n $NAMESPACE
kubectl apply -f deploy/base/reconciler-deployment.yaml -n $NAMESPACE
kubectl apply -f deploy/base/configmap.yaml -n $NAMESPACE
kubectl apply -f deploy/base/rbac.yaml -n $NAMESPACE
kubectl apply -f deploy/base/service.yaml -n $NAMESPACE

# Now apply the webhook config with the CORRECT caBundle
CABUNDLE=$(cat certs/tls.crt | base64 | tr -d '\n')
sed "s|caBundle:.*|caBundle: ${CABUNDLE}|g" deploy/base/webhook-config.yaml | kubectl apply -f -
```

Or as a single all-in-one script you can save and re-run:

```bash
#!/bin/bash
set -e
NAMESPACE=dra-webhook-system

# Create namespace
kubectl create namespace $NAMESPACE --dry-run=client -o yaml | kubectl apply -f -

# Generate cert
mkdir -p certs
openssl req -x509 -newkey rsa:4096 \
  -keyout certs/tls.key -out certs/tls.crt \
  -days 365 -nodes \
  -subj "/CN=dra-gpu-nic-webhook.${NAMESPACE}.svc" \
  -addext "subjectAltName=DNS:dra-gpu-nic-webhook.${NAMESPACE}.svc,DNS:dra-gpu-nic-webhook.${NAMESPACE}.svc.cluster.local"

# Create TLS secret
kubectl create secret tls dra-gpu-nic-webhook-tls \
  --cert=certs/tls.crt --key=certs/tls.key \
  -n $NAMESPACE --dry-run=client -o yaml | kubectl apply -f -

# Apply all manifests (except webhook-config)
for f in deploy/base/*.yaml; do
  [[ "$f" == *webhook-config* ]] && continue
  kubectl apply -f "$f" -n $NAMESPACE 2>/dev/null || kubectl apply -f "$f" 2>/dev/null
done

# Apply webhook-config with correct caBundle injected
CABUNDLE=$(cat certs/tls.crt | base64 | tr -d '\n')
sed "s|caBundle:.*|caBundle: ${CABUNDLE}|g" deploy/base/webhook-config.yaml | kubectl apply -f -

echo "Done! Webhook deployed with matching TLS."
kubectl get pods -n $NAMESPACE
```

### Verify the Webhook Is Running

```bash
kubectl get pods -n dra-webhook-system
```

Expected:
```
NAME                                      READY   STATUS    RESTARTS   AGE
dra-gpu-nic-webhook-xxxxx                 1/1     Running   0          1m
dra-gpu-nic-reconciler-xxxxx              1/1     Running   0          1m
```

### Verify TLS Is Working

If the webhook pod is running but pods aren't getting mutated, check the webhook logs:

```bash
kubectl logs -n dra-webhook-system deploy/dra-gpu-nic-webhook --tail=20
```

And verify the API server can reach the webhook:

```bash
# This should NOT show any TLS errors
kubectl get mutatingwebhookconfiguration dra-gpu-nic-webhook -o yaml | grep -A2 failurePolicy
```

If you see `x509: certificate signed by unknown authority` in the API server logs, the caBundle doesn't match the cert. Re-run the caBundle update step above.

### If You Need to Regenerate Certs Later

If you ever need to rotate or regenerate certs (e.g., after expiry):

```bash
# Regenerate cert
openssl req -x509 -newkey rsa:4096 \
  -keyout certs/tls.key -out certs/tls.crt \
  -days 365 -nodes \
  -subj "/CN=dra-gpu-nic-webhook.dra-webhook-system.svc" \
  -addext "subjectAltName=DNS:dra-gpu-nic-webhook.dra-webhook-system.svc,DNS:dra-gpu-nic-webhook.dra-webhook-system.svc.cluster.local"

# Update the secret
kubectl create secret tls dra-gpu-nic-webhook-tls \
  --cert=certs/tls.crt --key=certs/tls.key \
  -n dra-webhook-system --dry-run=client -o yaml | kubectl apply -f -

# Update caBundle
CABUNDLE=$(cat certs/tls.crt | base64 | tr -d '\n')
kubectl patch mutatingwebhookconfiguration dra-gpu-nic-webhook --type=json -p "[
  {\"op\":\"replace\",\"path\":\"/webhooks/0/clientConfig/caBundle\",\"value\":\"$CABUNDLE\"},
  {\"op\":\"replace\",\"path\":\"/webhooks/1/clientConfig/caBundle\",\"value\":\"$CABUNDLE\"}
]"

# Restart the webhook to pick up the new cert
kubectl rollout restart deployment/dra-gpu-nic-webhook -n dra-webhook-system
```

---

## Step 3: Configure the Network

Edit the ConfigMap to match your cluster's network. The webhook reads this at startup.

```bash
kubectl edit configmap dra-gpu-nic-webhook-config -n dra-webhook-system
```

### For RoCE / Ethernet Clusters

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: dra-gpu-nic-webhook-config
  namespace: dra-webhook-system
data:
  config.yaml: |
    gpuDeviceClassName: gpu.nvidia.com
    nicDeviceClassName: dranet
    maxPairsPerNUMA: 4
    maxPairsPerNode: 8
    preflightCheck: false
    nicConfig:
      mtu: 9000
      rdmaRequired: true
      interfacePrefix: "net"
      startingTableId: 100
      crossRailCIDR: "10.0.0.0/13"
      rails:
        - subnet: "10.0.0.0/16"
          gateway: "10.0.0.1"
          ipv4Prefix: "10.0."
        - subnet: "10.1.0.0/16"
          gateway: "10.1.0.1"
          ipv4Prefix: "10.1."
        - subnet: "10.2.0.0/16"
          gateway: "10.2.0.1"
          ipv4Prefix: "10.2."
        - subnet: "10.3.0.0/16"
          gateway: "10.3.0.1"
          ipv4Prefix: "10.3."
        - subnet: "10.4.0.0/16"
          gateway: "10.4.0.1"
          ipv4Prefix: "10.4."
        - subnet: "10.5.0.0/16"
          gateway: "10.5.0.1"
          ipv4Prefix: "10.5."
        - subnet: "10.6.0.0/16"
          gateway: "10.6.0.1"
          ipv4Prefix: "10.6."
        - subnet: "10.7.0.0/16"
          gateway: "10.7.0.1"
          ipv4Prefix: "10.7."
  reconciler.yaml: |
    interval: "5m"
    autoReap: false
    gracePeriod: "10m"
    statePath: "/data/reconciler-state.json"
```

Adjust `rails` to match your actual RDMA network subnets. One rail per NIC port — 8 entries for an 8-GPU-per-node cluster.

### For InfiniBand Clusters

Replace `rails` with `ibRails`:

```yaml
    nicConfig:
      mtu: 2044
      rdmaRequired: true
      ibRails:
        - gpu: "0001:00:00.0"
          nic: "0101:00:00.0"
        - gpu: "0002:00:00.0"
          nic: "0102:00:00.0"
        # one entry per GPU-NIC pair
```

To find your PCIe addresses:
```bash
# GPU PCIe bus IDs
kubectl get resourceslice -o json | jq -r '
  .items[] | select(.spec.driver=="gpu.nvidia.com") |
  .spec.devices[] |
  .attributes["resource.kubernetes.io/pciBusID"].string'

# NIC PCI addresses
kubectl get resourceslice -o json | jq -r '
  .items[] | select(.spec.driver=="dra.net") |
  .spec.devices[] |
  select(.attributes["dra.net/rdma"].bool==true) |
  .attributes["dra.net/pciAddress"].string'
```

### After Editing the Config

Restart the webhook — it only reads the ConfigMap at startup:

```bash
kubectl rollout restart deployment/dra-gpu-nic-webhook -n dra-webhook-system
```

---

## Step 4: Fix the CRI-O NRI Timeout

Easy to miss, but critical. Without this, pods with multiple RDMA NICs crash randomly during startup.

On **every GPU worker node**:

```bash
cat > /etc/crio/crio.conf.d/10-nri-timeout.conf << 'EOF'
[crio.nri]
enable_nri = true
nri_plugin_request_timeout = "60s"
nri_plugin_registration_timeout = "10s"
EOF

systemctl restart crio
```

Why: CRI-O's default NRI timeout is 2 seconds. Setting up 8 RDMA NICs takes longer. Without the increased timeout, CRI-O kills the DRA-NET NRI plugin mid-setup.

---

## Step 5: Label Your Namespaces

The webhook only operates on namespaces you explicitly enable:

```bash
kubectl label namespace my-namespace dra.llm-d.io/webhook-enabled=true
```

If you skip this, pods won't get mutated and will fail because `dra.llm-d.io/gpu-nic-pair` isn't a real Kubernetes resource.

---

## Step 6: Deploy a Workload

### Pod with 8 GPUs + 8 NICs

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: my-inference-server
  namespace: my-namespace
spec:
  containers:
  - name: vllm
    image: vllm/vllm-openai:latest
    command:
    - python3
    - -m
    - vllm.entrypoints.openai.api_server
    - --model=meta-llama/Llama-3.1-70B-Instruct
    - --tensor-parallel-size=8
    - --port=8000
    env:
    - name: NCCL_SOCKET_IFNAME
      value: "net0"
    - name: GLOO_SOCKET_IFNAME
      value: "net0"
    resources:
      requests:
        cpu: "16"
        memory: "128Gi"
        dra.llm-d.io/gpu-nic-pair: "8"
      limits:
        cpu: "32"
        memory: "256Gi"
        dra.llm-d.io/gpu-nic-pair: "8"
    ports:
    - containerPort: 8000
```

The `dra.llm-d.io/gpu-nic-pair: "8"` is a synthetic resource. The webhook intercepts it and:
1. Creates a ResourceClaimTemplate with 8 GPU+NIC pairs constrained by PCIe root
2. Injects `resourceClaims` into the pod spec
3. Strips the synthetic resource
4. Pins the pod to a node where all 8 pairs can be satisfied
5. Annotates the pod with `dra.llm-d.io/mutated: "true"`

### Deployment with 4 GPUs per Replica

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: inference
  namespace: my-namespace
spec:
  replicas: 2
  selector:
    matchLabels:
      app: inference
  template:
    metadata:
      labels:
        app: inference
    spec:
      containers:
      - name: model
        image: vllm/vllm-openai:latest
        resources:
          requests:
            cpu: "16"
            memory: "128Gi"
            dra.llm-d.io/gpu-nic-pair: "4"
          limits:
            cpu: "32"
            memory: "256Gi"
            dra.llm-d.io/gpu-nic-pair: "4"
```

### Valid Counts

| Count | What happens |
|-------|-------------|
| 1-4 | Single NUMA zone — all pairs on one socket |
| 5-7 | Rejected. Add annotation `dra.llm-d.io/allow-cross-numa: "true"` to allow |
| 8 | Full node, cross-NUMA automatically |
| >8 | Rejected — exceeds one node |

---

## Step 7: Verify

```bash
# Check pod was mutated
kubectl get pod my-inference-server -o jsonpath='{.metadata.annotations.dra\.llm-d\.io/mutated}'
# Should print: true

# Inside the pod
kubectl exec -it my-inference-server -- bash

nvidia-smi -L              # 8 GPUs
ip link show | grep net     # net0 through net7
rdma link show              # 8 RDMA devices
nvidia-smi topo -m          # GPU-NIC PCIe affinity
```

---

## Troubleshooting

### TLS / Certificate Issues

If you see `x509: certificate signed by unknown authority` or webhook admission failures:

```bash
# Check the caBundle matches the cert
CABUNDLE_IN_CONFIG=$(kubectl get mutatingwebhookconfiguration dra-gpu-nic-webhook -o jsonpath='{.webhooks[0].clientConfig.caBundle}')
CABUNDLE_FROM_CERT=$(cat certs/tls.crt | base64 | tr -d '\n')

if [ "$CABUNDLE_IN_CONFIG" = "$CABUNDLE_FROM_CERT" ]; then
  echo "caBundle matches ✅"
else
  echo "caBundle MISMATCH ❌ — run the caBundle update step from Step 2"
fi
```

Fix: re-run the `kubectl patch mutatingwebhookconfiguration` command from Step 2.

### Pod Stuck in Pending

```bash
kubectl describe pod <pod-name>
```

- **"not enough devices"** — not enough free GPUs or RDMA NICs
- **"constraint not satisfiable"** — PCIe pairing can't be fulfilled
- **No events** — namespace not labeled (Step 5)

### Check Available GPUs/NICs

```bash
kubectl get resourceslice -o json | python3 -c "
import json, sys, collections
data = json.load(sys.stdin)
per_node = collections.defaultdict(lambda: {'gpus': 0, 'rdma_nics': 0})
for item in data.get('items', []):
    driver = item.get('spec', {}).get('driver', '')
    node = item.get('spec', {}).get('nodeName', '')
    devices = item.get('spec', {}).get('devices') or []
    for d in devices:
        attrs = d.get('attributes', {})
        if driver == 'gpu.nvidia.com':
            per_node[node]['gpus'] += 1
        if attrs.get('dra.net/rdma', {}).get('bool', False):
            per_node[node]['rdma_nics'] += 1
for node, c in sorted(per_node.items()):
    if c['gpus'] > 0:
        print(f'{node}: {c[\"gpus\"]} GPUs, {c[\"rdma_nics\"]} RDMA NICs')
"
```

### Webhook Logs

```bash
kubectl logs -n dra-webhook-system deploy/dra-gpu-nic-webhook --tail=50
```

---

## Quick Reference

| Item | Value |
|------|-------|
| Resource to request | `dra.llm-d.io/gpu-nic-pair: "N"` |
| Namespace label | `dra.llm-d.io/webhook-enabled: "true"` |
| Cross-NUMA annotation | `dra.llm-d.io/allow-cross-numa: "true"` |
| Max per NUMA zone | 4 (default) |
| Max per node | 8 (default) |
| Webhook namespace | `dra-webhook-system` |
| ConfigMap | `dra-gpu-nic-webhook-config` |
| TLS secret | `dra-gpu-nic-webhook-tls` |
| Webhook repo | https://github.com/openshift-psap/dra-rail-admission-webhook |

Let me know if you hit any issues, Mark.
