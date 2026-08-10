# GPU + RDMA NIC Pairing with PCIe Affinity (DRA)

Complete guide to pairing GPUs with RDMA NICs on the same PCIe root complex using Kubernetes Dynamic Resource Allocation (DRA). Covers both available approaches.

---

## Why PCIe Affinity Matters

When GPUs communicate across nodes (e.g., NCCL AllReduce during distributed inference), data flows from GPU → PCIe bus → NIC → network. If a GPU and NIC are on different PCIe root complexes, the data crosses the CPU socket, adding latency and reducing bandwidth.

**Without PCIe affinity:** The container runtime picks NICs arbitrarily — GPU 0 might get a NIC closest to GPU 7.

**With PCIe affinity:** Each GPU is guaranteed the physically closest RDMA NIC, giving the shortest PCIe path for GPUDirect RDMA transfers.

---

## Two Approaches

There are two ways to achieve GPU+NIC PCIe affinity with DRA. Both deliver the same result but work differently.

| | Webhook Approach | Composite DRA Driver |
|---|---|---|
| **How it works** | Admission webhook intercepts pods at creation, generates ResourceClaimTemplates on-the-fly | DaemonSet pre-discovers and pairs devices, publishes composite ResourceSlices |
| **Pod spec** | `dra.llm-d.io/gpu-nic-pair: "8"` | `composite.dra.io/gpu-nic-pair: "8"` |
| **Device pairing** | Done at admission time by the webhook | Done continuously by the DaemonSet — pairs are pre-computed |
| **Admission latency** | Adds webhook call per pod create | Minimal — webhook only converts resource to DRA claim |
| **Visibility** | Pairs created on-demand, not visible until pod is scheduled | Pairs visible as ResourceSlices — inspect anytime with `kubectl get resourceslice` |
| **InfiniBand support** | Yes — explicit IB rail configuration | Ethernet/RoCE only |
| **Maturity** | Original approach, battle-tested | Newer, simpler architecture |

### When to use the Webhook

- Your cluster uses InfiniBand (explicit IB rail support)
- You already have the `dra-rail-admission-webhook` deployed
- You want on-demand pairing without a persistent DaemonSet

### When to use the Composite DRA Driver

- New deployment from scratch
- Your cluster uses RoCE/Ethernet RDMA
- You want to see available GPU+NIC pairs before scheduling pods
- You prefer Helm-based deployment
- You want the driver to continuously validate pairings (recomputes every 30 seconds)

### Can I run both?

No — don't run both simultaneously. They both intercept resource requests and will conflict. Choose one per cluster.

---

## Shared Prerequisites

Both approaches require:

- Kubernetes 1.31+ with DRA enabled
- **NVIDIA DRA GPU Driver** — publishes GPUs as DRA devices with PCIe topology (`gpu.nvidia.com` device class)
- **dranet Driver** — publishes network interfaces as DRA devices with RDMA capability (`dranet` device class)
- Nodes with RDMA-capable NICs (InfiniBand or RoCE)
- **NVIDIA GPU Operator** — GPU driver, container toolkit
- **NVIDIA Network Operator** — MOFED/DOCA driver for Mellanox RDMA NICs

### Verify DRA drivers are installed

```bash
kubectl get deviceclass gpu.nvidia.com
kubectl get deviceclass dranet
```

### Verify devices are advertised

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

GPUs and RDMA NICs sharing the same `pcie=` value are on the same PCIe root complex.

---

## Option A: Webhook Approach

### Architecture

```
Pod requests: dra.llm-d.io/gpu-nic-pair: "8"
         ↓
Admission Webhook intercepts
         ↓
Webhook creates ResourceClaimTemplate with 8 GPU+NIC pairs
         ↓
PCIe affinity constraints ensure each pair shares the same PCIe root
         ↓
Scheduler places pod on node where all pairs can be satisfied
         ↓
DRA drivers inject 8 GPUs + 8 RDMA NICs into the pod
         ↓
NCCL auto-discovers injected interfaces (net0-net7)
```

Components:
- **DRA GPU-NIC Admission Webhook** — translates simple resource requests into full DRA claims
- **Reconciler** — cleans up orphaned ResourceClaimTemplates

### A1. Deploy the Webhook

The webhook is available at: https://github.com/openshift-psap/dra-rail-admission-webhook

```bash
git clone https://github.com/openshift-psap/dra-rail-admission-webhook.git
cd dra-rail-admission-webhook
make build
make deploy NAMESPACE=dra-webhook-system
```

This creates TLS certificates, MutatingWebhookConfiguration, webhook deployment, reconciler, ConfigMap, and RBAC.

Verify:
```bash
kubectl get pods -n dra-webhook-system
```

### A2. Configure the Webhook

#### Ethernet / RoCE Configuration

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

    preflightCheck: false

  reconciler.yaml: |
    interval: "5m"
    autoReap: false
    gracePeriod: "10m"
    statePath: "/data/reconciler-state.json"
```

#### InfiniBand Configuration

For InfiniBand clusters, use `ibRails` instead of `rails`:

```yaml
    nicConfig:
      mtu: 2044
      rdmaRequired: true
      ibRails:
        - gpu: "0001:00:00.0"
          nic: "0101:00:00.0"
        - gpu: "0002:00:00.0"
          nic: "0102:00:00.0"
        # ... one entry per GPU-NIC pair
```

Find your cluster's PCIe addresses:
```bash
# GPU PCIe bus IDs
kubectl get resourceslice -o json | jq -r '
  .items[] | select(.spec.driver=="gpu.nvidia.com") |
  .spec.devices[] | .attributes["resource.kubernetes.io/pciBusID"].string'

# NIC PCI addresses
kubectl get resourceslice -o json | jq -r '
  .items[] | select(.spec.driver=="dra.net") |
  .spec.devices[] | select(.attributes["dra.net/rdma"].bool==true) |
  .attributes["dra.net/pciAddress"].string'
```

Restart after config changes:
```bash
kubectl rollout restart deployment/dra-gpu-nic-webhook -n dra-webhook-system
```

### A3. Enable Namespaces

The webhook only intercepts pods in labeled namespaces:

```bash
kubectl label namespace my-namespace dra.llm-d.io/webhook-enabled=true
```

### A4. Use in Pods

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: inference-worker
  namespace: my-namespace
spec:
  containers:
  - name: model
    image: vllm/vllm-openai:latest
    env:
    - name: NCCL_SOCKET_IFNAME
      value: "net0"
    - name: GLOO_SOCKET_IFNAME
      value: "net0"
    resources:
      requests:
        dra.llm-d.io/gpu-nic-pair: "8"
      limits:
        dra.llm-d.io/gpu-nic-pair: "8"
```

The webhook intercepts the synthetic resource, creates a ResourceClaimTemplate with PCIe constraints, injects DRA claims, and annotates the pod with `dra.llm-d.io/mutated: "true"`.

#### Valid GPU+NIC pair counts

| Count | Behavior |
|-------|----------|
| 1-4 | Single NUMA zone (PCIe + NUMA affinity) |
| 5-7 | Rejected — add annotation `dra.llm-d.io/allow-cross-numa: "true"` to allow |
| 8 | Cross-NUMA, full node, both NUMA zones used automatically |
| >8 | Rejected — exceeds maximum per node |

### A5. Extended Resource Interception (Optional)

The webhook can also intercept standard `nvidia.com/gpu` requests:

```yaml
interceptExtendedResources:
  - resourceName: "nvidia.com/gpu"
    deviceClassName: "gpu.nvidia.com"
```

A pod requesting `nvidia.com/gpu: 2` gets converted to DRA claims automatically.

### A6. What the Webhook Creates

When you request `dra.llm-d.io/gpu-nic-pair: "8"`, the webhook generates:

```yaml
apiVersion: resource.k8s.io/v1
kind: ResourceClaimTemplate
spec:
  spec:
    devices:
      constraints:
      - matchAttribute: resource.kubernetes.io/pcieRoot
        requests: ["gpu0", "nic0"]
      # ... repeated for each pair
      requests:
      - name: gpu0
        exactly:
          count: 1
          deviceClassName: gpu.nvidia.com
      - name: nic0
        exactly:
          count: 1
          deviceClassName: dranet
          selectors:
          - cel:
              expression: 'device.attributes["dra.net"].rdma == true'
      # ... repeated for each pair
```

---

## Option B: Composite DRA Driver

### Architecture

```
NVIDIA DRA GPU Driver publishes GPUs with PCIe topology
                    \
                     -> Composite DRA Driver watches both, pairs by PCIe root
                    /
dranet Driver publishes RDMA NICs with PCIe topology

Result: "composite-gpu-nic-pair" devices — each a GPU + NIC on the same PCIe bus
```

Components:
- **DaemonSet** (`composite-dra-driver`) — runs on every node, discovers GPUs and NICs, pairs them by PCIe root, publishes composite ResourceSlices
- **Webhook** (`composite-dra-driver-webhook`) — converts pod resource requests into DRA claims

For the full step-by-step deployment guide with all YAMLs, see [`composite-dra-setup.md`](composite-dra-setup.md).

### B1. Quick Deploy

```bash
# Prerequisites: cert-manager must be installed

# Create namespace
kubectl create namespace composite-dra-system

# Create self-signed issuer for webhook TLS
kubectl apply -f - <<EOF
apiVersion: cert-manager.io/v1
kind: Issuer
metadata:
  name: composite-dra-selfsigned
  namespace: composite-dra-system
spec:
  selfSigned: {}
EOF

# Create dranet DeviceClass if not present
kubectl apply -f - <<EOF
apiVersion: resource.k8s.io/v1
kind: DeviceClass
metadata:
  name: dranet
spec:
  selectors:
  - cel:
      expression: "device.driver == 'dra.net'"
EOF

# Install via Helm
helm install composite ./composite-dra-driver \
  --namespace composite-dra-system \
  --set driver.image=ghcr.io/openshift-psap/composite-dra-driver:latest \
  --set webhook.image=ghcr.io/openshift-psap/composite-dra-webhook:latest
```

### B2. Verify

```bash
# Check pods
kubectl get pods -n composite-dra-system

# Check device classes
kubectl get deviceclass | grep composite

# Check published pairs per node
kubectl get resourceslice -o json | python3 -c "
import json, sys, collections
data = json.load(sys.stdin)
per_node = collections.defaultdict(lambda: {'gpu-nic-pair': 0, 'gpu': 0})
for item in data.get('items', []):
    if item.get('spec', {}).get('driver') != 'composite.dra.io':
        continue
    node = item['spec'].get('nodeName', '')
    for d in item['spec'].get('devices', []):
        comp = d.get('attributes', {}).get('composite/compositionName', {}).get('string', '')
        if comp:
            per_node[node][comp] += 1
for node, counts in sorted(per_node.items()):
    print(f'{node}: {counts[\"gpu-nic-pair\"]} GPU+NIC pairs, {counts[\"gpu\"]} standalone GPUs')
"
```

### B3. Use in Pods

```yaml
resources:
  requests:
    composite.dra.io/gpu-nic-pair: "8"
  limits:
    composite.dra.io/gpu-nic-pair: "8"
```

For GPU-only (no NIC pairing):
```yaml
resources:
  requests:
    composite.dra.io/gpu: "4"
```

---

## Verify GPU+NIC Pairing (Both Approaches)

Once a pod is running with GPU+NIC pairs:

```bash
kubectl exec -it <pod-name> -- bash

# Check GPUs
nvidia-smi -L

# Check injected RDMA interfaces
ip link show | grep net

# Check RDMA devices
rdma link show

# Check GPU-NIC PCIe topology
nvidia-smi topo -m
```

Expected: N GPUs visible, N `netN` interfaces, each NIC on the same PCIe root as its GPU.

---

## Troubleshooting (Both Approaches)

### TopologyAffinityError

Pods requesting 8 GPUs on nodes with `topologyManagerPolicy: single-numa-node` will fail because 8 GPUs span 2 NUMA zones. Fix:

```bash
kubectl patch kubeletconfig <config-name> --type merge \
  -p '{"spec":{"kubeletConfig":{"topologyManagerPolicy":"best-effort"}}}'
```

GPU nodes will reboot to apply the new policy.

### No RDMA NICs detected

If GPU+NIC pairs show 0 but standalone GPUs work:

1. Check NVIDIA Network Operator: `kubectl get pods -n nvidia-network-operator`
2. Check MOFED driver pods: `kubectl get pods -n nvidia-network-operator | grep mofed`
3. Verify RDMA devices on the node: `kubectl debug node/<node> -- rdma link show`
4. Check dranet sees RDMA NICs:
```bash
kubectl get resourceslice -o json | python3 -c "
import json, sys
for item in json.load(sys.stdin).get('items', []):
    if item['spec'].get('driver') != 'dra.net': continue
    node = item['spec'].get('nodeName', '')
    rdma = sum(1 for d in item['spec'].get('devices', [])
               if d.get('attributes', {}).get('dra.net/rdma', {}).get('bool', False))
    total = len(item['spec'].get('devices', []))
    print(f'{node}: {rdma}/{total} RDMA NICs')
"
```

### NRI plugin timeout (CRI-O)

When using multiple RDMA NICs per pod, CRI-O may disconnect the DRA-NET NRI plugin during setup. On every GPU worker node:

```bash
cat > /etc/crio/crio.conf.d/10-nri-timeout.conf << 'EOF'
[crio.nri]
enable_nri = true
nri_plugin_request_timeout = "60s"
nri_plugin_registration_timeout = "10s"
EOF
systemctl restart crio
```

### Pod stuck in Pending

```bash
kubectl describe pod <pod-name>
```

Common causes:
- **"not enough devices"** — not enough free GPUs or RDMA NICs on any node
- **"constraint not satisfiable"** — PCIe pairing can't be satisfied (check RDMA NICs)
- **Namespace not labeled** (webhook only) — add `dra.llm-d.io/webhook-enabled=true`
- **Webhook not running** — check pods in `dra-webhook-system` or `composite-dra-system`

### Check available devices

```bash
kubectl get resourceslice -o json | python3 -c "
import json, sys, collections
data = json.load(sys.stdin)
per_node = collections.defaultdict(lambda: {'gpus': 0, 'rdma_nics': 0})
for item in data.get('items', []):
    driver = item.get('spec', {}).get('driver', '')
    node = item.get('spec', {}).get('nodeName', '')
    for d in item.get('spec', {}).get('devices', []):
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

### Webhook-specific: verify pod was mutated

```bash
# Webhook approach
kubectl get pod <pod-name> -o jsonpath='{.metadata.annotations.dra\.llm-d\.io/mutated}'

# Composite approach — check for DRA resource claims
kubectl get pod <pod-name> -o jsonpath='{.spec.resourceClaims}'
```
