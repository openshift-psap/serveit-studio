# DRA-NET Guide: GPU + RDMA NIC Pairing with PCIe Affinity

Complete guide to deploying and using DRA-NET for GPU+NIC PCIe-aware allocation on Kubernetes.

---

## What Is DRA-NET?

When GPUs communicate across nodes (e.g., NCCL AllReduce during distributed training), data flows from GPU → PCIe bus → NIC → network. If a GPU and NIC are on different PCIe root complexes, the data crosses the CPU socket, adding latency and reducing bandwidth.

DRA-NET solves this by using Kubernetes Dynamic Resource Allocation (DRA) to pair each GPU with the RDMA NIC on the same PCIe root complex. A mutating admission webhook intercepts pod creation and automatically generates the correct ResourceClaimTemplates with PCIe affinity constraints.

**Without DRA-NET:** The container runtime picks NICs arbitrarily — GPU 0 might get a NIC closest to GPU 7.

**With DRA-NET:** Each GPU is guaranteed the physically closest RDMA NIC, giving the shortest PCIe path for GPUDirect RDMA transfers.

---

## Architecture

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
- **NVIDIA DRA GPU Driver** — advertises GPUs as DRA devices with PCIe topology
- **DRA-NET Driver** — advertises network interfaces as DRA devices with RDMA capability
- **DRA GPU-NIC Admission Webhook** — translates simple resource requests into full DRA claims
- **Reconciler** — cleans up orphaned ResourceClaimTemplates

---

## Prerequisites

- Kubernetes 1.31+ with DRA enabled
- NVIDIA DRA GPU driver installed (`gpu.nvidia.com` device class)
- DRA-NET driver installed (`dranet` device class)
- Nodes with RDMA-capable NICs (InfiniBand or RoCE)
- `cert-manager` or ability to generate TLS certificates

---

## Step 1: Install DRA Drivers

### NVIDIA DRA GPU Driver

The NVIDIA DRA driver exposes GPUs as DRA devices with PCIe topology attributes. Install via Helm or the NVIDIA GPU Operator.

After installation, verify:
```bash
kubectl get deviceclass gpu.nvidia.com
```

Expected:
```
NAME             AGE
gpu.nvidia.com   35d
```

### DRA-NET Driver

DRA-NET exposes network interfaces as DRA devices. Install it on the cluster following the DRA-NET project documentation.

After installation, verify:
```bash
kubectl get deviceclass dranet
```

Expected:
```
NAME     AGE
dranet   34d
```

### Verify Devices Are Advertised

Check that GPUs and RDMA NICs appear in ResourceSlices with PCIe root attributes:

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

Expected output showing GPU+NIC pairs sharing PCIe roots:
```
worker-3-5g8cv  gpu.nvidia.com        gpu-0                      pcie=pci0000:e6      rdma=False  if=
worker-3-5g8cv  gpu.nvidia.com        gpu-1                      pcie=pci0000:dc      rdma=False  if=
worker-3-5g8cv  dra.net               pci-0000-e9-00-0           pcie=pci0000:e6      rdma=True   if=enp233s0
worker-3-5g8cv  dra.net               pci-0000-df-00-0           pcie=pci0000:dc      rdma=True   if=enp223s0
```

Notice: `gpu-0` and `pci-0000-e9-00-0` share `pcie=pci0000:e6` — they are on the same PCIe root.

---

## Step 2: Deploy the Admission Webhook

The webhook is available at: https://github.com/openshift-psap/dra-rail-admission-webhook

### Clone and Build

```bash
git clone https://github.com/openshift-psap/dra-rail-admission-webhook.git
cd dra-rail-admission-webhook
make build
```

### Deploy

This generates TLS certificates and deploys all components:

```bash
make deploy NAMESPACE=dra-webhook-system
```

This creates:
- TLS certificates (self-signed CA)
- MutatingWebhookConfiguration with `/mutate` and `/mutate-ext` endpoints
- Webhook deployment
- Reconciler deployment (cleans up orphaned ResourceClaimTemplates)
- ConfigMap with network configuration
- RBAC (ClusterRole, ClusterRoleBinding, ServiceAccount)

### Verify Deployment

```bash
kubectl get pods -n dra-webhook-system
```

Expected:
```
NAME                                    READY   STATUS    RESTARTS   AGE
dra-gpu-nic-webhook-xxxxx               1/1     Running   0          5m
dra-gpu-nic-reconciler-xxxxx            1/1     Running   0          5m
```

---

## Step 3: Configure the Webhook

The webhook reads its configuration from a ConfigMap at startup. Edit it to match your cluster's network topology.

### Ethernet / RoCE Configuration

For clusters with RoCE (RDMA over Converged Ethernet) NICs:

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: dra-gpu-nic-webhook-config
  namespace: dra-webhook-system
data:
  config.yaml: |
    # Device class names (must match what DRA drivers advertise)
    gpuDeviceClassName: gpu.nvidia.com
    nicDeviceClassName: dranet

    # NUMA topology limits
    maxPairsPerNUMA: 4          # Max GPU+NIC pairs per NUMA zone
    maxPairsPerNode: 8          # Max GPU+NIC pairs per node (typically = GPUs per node)

    # NIC configuration
    nicConfig:
      mtu: 9000                 # Jumbo frames for RDMA performance
      rdmaRequired: true        # Only select RDMA-capable NICs
      interfacePrefix: "net"    # Interfaces appear as net0, net1, ... in the pod
      startingTableId: 100      # Policy routing table IDs start here
      crossRailCIDR: "10.0.0.0/13"  # Supernet covering all rails

      # Rails — one entry per NIC port (8 rails for 8-GPU node)
      # Each rail is a separate network subnet for multi-rail RDMA
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

    # Optional: pre-flight check (adds admission latency but gives
    # immediate denial instead of pods stuck in Pending)
    preflightCheck: false

  reconciler.yaml: |
    interval: "5m"
    autoReap: false
    gracePeriod: "10m"
    statePath: "/data/reconciler-state.json"
```

### InfiniBand Configuration

For clusters with InfiniBand NICs, use `ibRails` instead of `rails`:

```yaml
    nicConfig:
      mtu: 2044                   # IPoIB MTU
      rdmaRequired: true
      ibRails:                    # GPU+NIC PCIe address pairs
        - gpu: "0001:00:00.0"    # rail 0
          nic: "0101:00:00.0"
        - gpu: "0002:00:00.0"    # rail 1
          nic: "0102:00:00.0"
        # ... one entry per GPU-NIC pair
```

Find your cluster's PCIe addresses:
```bash
# GPU PCIe bus IDs
kubectl get resourceslice -o json | jq -r '
  .items[] |
  select(.spec.driver=="gpu.nvidia.com") |
  .spec.devices[] |
  .attributes["resource.kubernetes.io/pciBusID"].string'

# NIC PCI addresses
kubectl get resourceslice -o json | jq -r '
  .items[] |
  select(.spec.driver=="dra.net") |
  .spec.devices[] |
  select(.attributes["dra.net/rdma"].bool==true) |
  .attributes["dra.net/pciAddress"].string'
```

### Kustomize Overlays

For cluster-specific configuration, use a kustomize overlay:

```text
deploy/
  base/                      # Default manifests
  overlays/
    my-cluster/
      kustomization.yaml
      configmap-patch.yaml   # Your cluster's rail config
```

Deploy with:
```bash
kubectl apply -k deploy/overlays/my-cluster/
```

### Restart After Config Changes

The webhook loads config at startup only. After editing the ConfigMap:

```bash
kubectl rollout restart deployment/dra-gpu-nic-webhook -n dra-webhook-system
```

---

## Step 4: Enable Namespaces

The webhook only intercepts pods in labeled namespaces. Label each namespace that should use GPU+NIC pairing:

```bash
kubectl label namespace my-namespace dra.llm-d.io/webhook-enabled=true
```

Without this label, pods in the namespace will NOT get DRA-NET GPU+NIC pairing.

---

## Step 5: NRI Plugin Timeout (Important)

When using DRA-NET with multiple RDMA NICs per pod, increase the CRI-O NRI plugin timeout on every GPU worker node. Without this, CRI-O disconnects the DRA-NET NRI plugin during multi-NIC setup, causing crashes.

Create on every GPU worker node:

```bash
cat > /etc/crio/crio.conf.d/10-nri-timeout.conf << 'EOF'
[crio.nri]
enable_nri = true
nri_plugin_request_timeout = "60s"
nri_plugin_registration_timeout = "10s"
EOF
```

Restart CRI-O:
```bash
systemctl restart crio
```

---

## Step 6: Request GPU+NIC Pairs in Your Pods

### Simple Pod

Request 8 GPU+NIC pairs — the webhook handles everything:

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: inference-worker
  namespace: my-namespace     # must be labeled with dra.llm-d.io/webhook-enabled=true
spec:
  containers:
  - name: model
    image: vllm/vllm-openai:latest
    command: ["python3", "-m", "vllm.entrypoints.openai.api_server",
              "--model=meta-llama/Llama-3.1-70B-Instruct",
              "--tensor-parallel-size=8"]
    env:
    - name: NCCL_SOCKET_IFNAME
      value: "net0"
    - name: GLOO_SOCKET_IFNAME
      value: "net0"
    resources:
      requests:
        cpu: "16"
        memory: "128Gi"
        dra.llm-d.io/gpu-nic-pair: "8"    # <-- This is the magic line
      limits:
        cpu: "32"
        memory: "256Gi"
        dra.llm-d.io/gpu-nic-pair: "8"
    ports:
    - containerPort: 8000
```

That's it. The `dra.llm-d.io/gpu-nic-pair: "8"` resource request is a synthetic resource. The webhook:

1. Intercepts the pod at admission time
2. Creates a ResourceClaimTemplate with 8 GPU+NIC pairs and PCIe constraints
3. Injects `resourceClaims` into the pod spec
4. Strips the synthetic resource from `requests`/`limits`
5. Pins the pod to a node where all 8 pairs can be satisfied
6. Annotates the pod with `dra.llm-d.io/mutated: "true"`

### Deployment

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
            dra.llm-d.io/gpu-nic-pair: "4"    # 4 GPUs + 4 NICs per replica
          limits:
            cpu: "32"
            memory: "256Gi"
            dra.llm-d.io/gpu-nic-pair: "4"
```

### Valid GPU+NIC Pair Counts

| Count | Behavior | Notes |
|-------|----------|-------|
| 1-4 | Single NUMA zone | All pairs on one NUMA zone (PCIe + NUMA affinity) |
| 5-7 | Rejected | Exceeds single NUMA capacity. Add annotation to allow (see below) |
| 8 | Cross-NUMA | Full node, both NUMA zones used automatically |
| >8 | Rejected | Exceeds maximum per node |

For counts 5-7, add this annotation to allow cross-NUMA allocation:
```yaml
metadata:
  annotations:
    dra.llm-d.io/allow-cross-numa: "true"
```

---

## Step 7: Verify GPU+NIC Pairing

Once the pod is running:

```bash
kubectl exec -it inference-worker -- bash

# Check GPUs
nvidia-smi -L

# Check injected RDMA interfaces
ip link show | grep net

# Check RDMA devices
rdma link show

# Check GPU-NIC PCIe topology
nvidia-smi topo -m
```

Expected: 8 GPUs visible, 8 `netN` interfaces, each NIC on the same PCIe root as its GPU.

---

## What the Webhook Creates (Behind the Scenes)

When you request `dra.llm-d.io/gpu-nic-pair: "8"`, the webhook generates a ResourceClaimTemplate like this:

```yaml
apiVersion: resource.k8s.io/v1
kind: ResourceClaimTemplate
metadata:
  name: <auto-generated>
  namespace: my-namespace
spec:
  spec:
    devices:
      # PCIe affinity constraints — each GPU+NIC pair must share the same PCIe root
      constraints:
      - matchAttribute: resource.kubernetes.io/pcieRoot
        requests: ["gpu0", "nic0"]
      - matchAttribute: resource.kubernetes.io/pcieRoot
        requests: ["gpu1", "nic1"]
      - matchAttribute: resource.kubernetes.io/pcieRoot
        requests: ["gpu2", "nic2"]
      - matchAttribute: resource.kubernetes.io/pcieRoot
        requests: ["gpu3", "nic3"]
      - matchAttribute: resource.kubernetes.io/pcieRoot
        requests: ["gpu4", "nic4"]
      - matchAttribute: resource.kubernetes.io/pcieRoot
        requests: ["gpu5", "nic5"]
      - matchAttribute: resource.kubernetes.io/pcieRoot
        requests: ["gpu6", "nic6"]
      - matchAttribute: resource.kubernetes.io/pcieRoot
        requests: ["gpu7", "nic7"]

      # Device requests
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
      # ... repeated for gpu1/nic1 through gpu7/nic7
```

The `matchAttribute: resource.kubernetes.io/pcieRoot` constraint tells the scheduler: "gpu0 and nic0 must be allocated from devices that have the same `pcieRoot` value." This guarantees the shortest PCIe path between each GPU and its NIC.

---

## Extended Resource Interception (Optional)

The webhook can also intercept standard `nvidia.com/gpu` requests and convert them to DRA claims. This ensures ALL GPU allocation goes through the DRA allocator:

```yaml
# In the ConfigMap
interceptExtendedResources:
  - resourceName: "nvidia.com/gpu"
    deviceClassName: "gpu.nvidia.com"
```

With this enabled, a pod requesting `nvidia.com/gpu: 2` gets converted to DRA claims automatically — no code changes needed. The resource is stripped and replaced with proper DRA references.

Note: A pod cannot request both `dra.llm-d.io/gpu-nic-pair` and an intercepted resource — both allocate from the same GPU pool.

---

## Troubleshooting

### Pod Stuck in Pending

```bash
kubectl describe pod <pod-name>
```

Common causes:
- **"not enough devices"** — Not enough free GPUs or RDMA NICs on any node
- **"constraint not satisfiable"** — PCIe pairing can't be satisfied
- **Namespace not labeled** — Add `dra.llm-d.io/webhook-enabled=true`
- **Webhook not running** — Check `kubectl get pods -n dra-webhook-system`

### Check Available Devices

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
kubectl logs -n dra-webhook-system deploy/dra-gpu-nic-webhook
```

### Verify Pod Was Mutated

```bash
kubectl get pod <pod-name> -o jsonpath='{.metadata.annotations.dra\.llm-d\.io/mutated}'
```

Should return `true`.

---

## Choosing Your Approach: Webhook vs Composite DRA

There are two ways to achieve GPU+NIC PCIe affinity with DRA. Both deliver the same result — each GPU gets the RDMA NIC on the same PCIe root complex — but they work differently.

### Comparison

| | Webhook Approach | Composite DRA Driver |
|---|---|---|
| **How it works** | Admission webhook intercepts pods at creation, generates ResourceClaimTemplates on-the-fly | DaemonSet pre-discovers and pairs devices, publishes composite ResourceSlices |
| **Pod spec** | `dra.llm-d.io/gpu-nic-pair: "8"` (synthetic extended resource) | `composite.dra.io/gpu-nic-pair: "8"` (synthetic extended resource) |
| **Device pairing** | Done at admission time by the webhook | Done continuously by the DaemonSet — pairs are pre-computed |
| **Latency** | Adds admission latency (webhook call per pod create) | No admission latency for pairing — webhook only converts resource to DRA claim |
| **Visibility** | Pairs are created on-demand, not visible until pod is scheduled | Pairs visible as ResourceSlices — can inspect with `kubectl get resourceslice` |
| **Dependencies** | Webhook + reconciler + TLS certs | DaemonSet + webhook + TLS certs (cert-manager) |
| **Configuration** | ConfigMap with rail subnets, NIC config | ConfigMap with sources, compositions, and network params |
| **Maturity** | Original approach, battle-tested | Newer approach, simpler architecture |
| **Best for** | Clusters already using the DRA-NET webhook | New deployments, clusters wanting pre-computed device inventory |

### When to use the Webhook approach

- You already have the `dra-rail-admission-webhook` deployed
- You want on-demand pairing without a persistent DaemonSet
- Your cluster uses InfiniBand (the webhook has explicit IB rail support)

### When to use the Composite DRA Driver

- New deployment from scratch
- You want to see available GPU+NIC pairs before scheduling pods (`kubectl get resourceslice`)
- You prefer Helm-based deployment
- Your cluster uses RoCE/Ethernet RDMA
- You want the driver to continuously validate pairings (it recomputes every 30 seconds)

### Can I run both?

No — don't run both simultaneously. They both try to intercept the same resource requests and will conflict. Choose one approach per cluster.

---

## Composite DRA Driver Setup

For the full step-by-step deployment guide, see [`composite-dra-setup.md`](composite-dra-setup.md).

Quick overview:

1. **Prerequisites**: NVIDIA DRA GPU driver, dranet driver, GPU Operator, Network Operator, cert-manager
2. **Create namespace**: `composite-dra-system`
3. **Create cert-manager Issuer**: self-signed for webhook TLS
4. **Configure sources and compositions**: ConfigMap defining how GPUs and NICs are discovered and paired by PCIe root
5. **Configure network parameters**: Per-rail subnets, gateways, routing tables for RDMA
6. **Install via Helm**: DaemonSet (device discovery), webhook (resource conversion), DeviceClasses

### Verify composite devices

```bash
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

### Use in pods

```yaml
resources:
  requests:
    composite.dra.io/gpu-nic-pair: "8"
  limits:
    composite.dra.io/gpu-nic-pair: "8"
```

---

## Common Issues (Both Approaches)

### TopologyAffinityError

Pods requesting 8 GPUs on nodes with `topologyManagerPolicy: single-numa-node` will fail because 8 GPUs span 2 NUMA zones. Fix:

```bash
kubectl patch kubeletconfig <config-name> --type merge \
  -p '{"spec":{"kubeletConfig":{"topologyManagerPolicy":"best-effort"}}}'
```

GPU nodes will reboot to apply the new policy.

### No RDMA NICs detected

If GPU+NIC pairs show 0 but standalone GPUs work:
1. Check NVIDIA Network Operator is running: `kubectl get pods -n nvidia-network-operator`
2. Check MOFED driver pods: `kubectl get pods -n nvidia-network-operator | grep mofed`
3. Verify RDMA devices on the node: `kubectl debug node/<node> -- rdma link show`
4. Check dranet sees RDMA NICs: `kubectl get resourceslice -o json | python3 -c "..."` (filter for `dra.net/rdma == true`)

### NRI plugin timeout (CRI-O)

When using multiple RDMA NICs per pod, CRI-O may disconnect the DRA-NET NRI plugin during multi-NIC setup. On every GPU worker node:

```bash
cat > /etc/crio/crio.conf.d/10-nri-timeout.conf << 'EOF'
[crio.nri]
enable_nri = true
nri_plugin_request_timeout = "60s"
nri_plugin_registration_timeout = "10s"
EOF
systemctl restart crio
```
