# DRA-NET Setup Guide: GPU + RDMA NIC Pairing with PCIe Affinity

This guide explains how to set up DRA-NET (Dynamic Resource Allocation for Networking) on a Kubernetes cluster to pair GPUs with their closest RDMA NIC using PCIe topology constraints.

## What DRA-NET Does

When a GPU communicates over the network (e.g., NCCL AllReduce between nodes), the data travels from GPU → PCIe bus → NIC → network. If the GPU and NIC are on different PCIe root complexes, the data must cross the CPU, adding latency. DRA-NET ensures each GPU is paired with the NIC on the same PCIe root complex, giving the shortest possible path.

Without DRA-NET, the kernel or container runtime picks NICs arbitrarily — GPU 0 might use a NIC that's physically closest to GPU 7, wasting PCIe bandwidth.

## Prerequisites

- Kubernetes 1.31+ (DRA v1 API)
- NVIDIA DRA driver installed (provides `gpu.nvidia.com` device class)
- DRA-NET driver installed (provides `dra.net` device class)
- Nodes with RDMA-capable NICs (InfiniBand or RoCE)
- GPUs and NICs must expose `resource.kubernetes.io/pcieRoot` attribute

## Step 1: Verify DRA Drivers Are Running

Check that both the GPU and network DRA drivers are installed:

```bash
kubectl get deviceclass
```

Expected output:
```
NAME                                        AGE
dranet                                      34d
gpu.nvidia.com                              35d
```

If these are missing, install the NVIDIA DRA GPU driver and DRA-NET driver first.

## Step 2: Verify Device Attributes

Check that GPUs and NICs on your worker nodes expose PCIe root information:

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

Expected output (showing GPU+NIC pairs sharing PCIe roots):
```
worker-3-5g8cv  gpu.nvidia.com        gpu-0                      pcie=pci0000:e6      rdma=False  if=
worker-3-5g8cv  gpu.nvidia.com        gpu-1                      pcie=pci0000:dc      rdma=False  if=
worker-3-5g8cv  gpu.nvidia.com        gpu-2                      pcie=pci0000:d2      rdma=False  if=
worker-3-5g8cv  gpu.nvidia.com        gpu-3                      pcie=pci0000:c8      rdma=False  if=
worker-3-5g8cv  gpu.nvidia.com        gpu-4                      pcie=pci0000:be      rdma=False  if=
worker-3-5g8cv  gpu.nvidia.com        gpu-5                      pcie=pci0000:b4      rdma=False  if=
worker-3-5g8cv  gpu.nvidia.com        gpu-6                      pcie=pci0000:aa      rdma=False  if=
worker-3-5g8cv  gpu.nvidia.com        gpu-7                      pcie=pci0000:a0      rdma=False  if=
worker-3-5g8cv  dra.net               pci-0000-e9-00-0           pcie=pci0000:e6      rdma=True   if=enp233s0
worker-3-5g8cv  dra.net               pci-0000-df-00-0           pcie=pci0000:dc      rdma=True   if=enp223s0
worker-3-5g8cv  dra.net               pci-0000-d5-00-0           pcie=pci0000:d2      rdma=True   if=enp213s0
worker-3-5g8cv  dra.net               pci-0000-cb-00-0           pcie=pci0000:c8      rdma=True   if=enp203s0
worker-3-5g8cv  dra.net               pci-0000-c1-00-0           pcie=pci0000:be      rdma=True   if=enp193s0
worker-3-5g8cv  dra.net               pci-0000-b7-00-0           pcie=pci0000:b4      rdma=True   if=enp183s0
worker-3-5g8cv  dra.net               pci-0000-ad-00-0           pcie=pci0000:aa      rdma=True   if=enp173s0
worker-3-5g8cv  dra.net               pci-0000-a3-00-0           pcie=pci0000:a0      rdma=True   if=enp163s0
```

Notice: gpu-0 and pci-0000-e9-00-0 both have `pcie=pci0000:e6` — they share the same PCIe root complex. This is the pair that DRA-NET will enforce.

## Step 3: Create the DeviceClasses (if not already present)

### GPU DeviceClass

```yaml
apiVersion: resource.k8s.io/v1
kind: DeviceClass
metadata:
  name: gpu.nvidia.com
spec:
  selectors:
  - cel:
      expression: >-
        device.driver == 'gpu.nvidia.com' &&
        device.attributes['gpu.nvidia.com'].type == 'gpu'
```

This selects only GPU devices from the NVIDIA DRA driver (excluding MIG slices, compute domains, etc.).

### DRA-NET DeviceClass

```yaml
apiVersion: resource.k8s.io/v1
kind: DeviceClass
metadata:
  name: dranet
spec:
  selectors:
  - cel:
      expression: device.driver == "dra.net"
```

This selects all network devices managed by the DRA-NET driver.

Apply both:
```bash
kubectl apply -f gpu-deviceclass.yaml
kubectl apply -f dranet-deviceclass.yaml
```

## Step 4: Create a ResourceClaimTemplate

The ResourceClaimTemplate defines what resources a pod needs. For 8 GPUs with 8 paired RDMA NICs:

```yaml
apiVersion: resource.k8s.io/v1
kind: ResourceClaimTemplate
metadata:
  name: 8gpu-8nic
  namespace: my-namespace    # change to your namespace
spec:
  spec:
    devices:
      #
      # CONSTRAINTS: Each GPU must be paired with a NIC on the same PCIe root.
      # The scheduler will only place the pod on a node where all 8 pairs
      # can be satisfied simultaneously.
      #
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

      #
      # REQUESTS: 8 GPUs and 8 RDMA NICs.
      # Each request gets a name (gpu0, nic0, etc.) that the constraints
      # reference above.
      #
      requests:
      # --- Pair 0 ---
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

      # --- Pair 1 ---
      - name: gpu1
        exactly:
          count: 1
          deviceClassName: gpu.nvidia.com
      - name: nic1
        exactly:
          count: 1
          deviceClassName: dranet
          selectors:
          - cel:
              expression: 'device.attributes["dra.net"].rdma == true'

      # --- Pair 2 ---
      - name: gpu2
        exactly:
          count: 1
          deviceClassName: gpu.nvidia.com
      - name: nic2
        exactly:
          count: 1
          deviceClassName: dranet
          selectors:
          - cel:
              expression: 'device.attributes["dra.net"].rdma == true'

      # --- Pair 3 ---
      - name: gpu3
        exactly:
          count: 1
          deviceClassName: gpu.nvidia.com
      - name: nic3
        exactly:
          count: 1
          deviceClassName: dranet
          selectors:
          - cel:
              expression: 'device.attributes["dra.net"].rdma == true'

      # --- Pair 4 ---
      - name: gpu4
        exactly:
          count: 1
          deviceClassName: gpu.nvidia.com
      - name: nic4
        exactly:
          count: 1
          deviceClassName: dranet
          selectors:
          - cel:
              expression: 'device.attributes["dra.net"].rdma == true'

      # --- Pair 5 ---
      - name: gpu5
        exactly:
          count: 1
          deviceClassName: gpu.nvidia.com
      - name: nic5
        exactly:
          count: 1
          deviceClassName: dranet
          selectors:
          - cel:
              expression: 'device.attributes["dra.net"].rdma == true'

      # --- Pair 6 ---
      - name: gpu6
        exactly:
          count: 1
          deviceClassName: gpu.nvidia.com
      - name: nic6
        exactly:
          count: 1
          deviceClassName: dranet
          selectors:
          - cel:
              expression: 'device.attributes["dra.net"].rdma == true'

      # --- Pair 7 ---
      - name: gpu7
        exactly:
          count: 1
          deviceClassName: gpu.nvidia.com
      - name: nic7
        exactly:
          count: 1
          deviceClassName: dranet
          selectors:
          - cel:
              expression: 'device.attributes["dra.net"].rdma == true'
```

Apply it:
```bash
kubectl apply -f resourceclaimtemplate.yaml
```

### How the constraints work

Each constraint block says: "the GPU request and the NIC request in this pair MUST be allocated from devices that have the same `resource.kubernetes.io/pcieRoot` attribute value."

For example:
```yaml
- matchAttribute: resource.kubernetes.io/pcieRoot
  requests: ["gpu0", "nic0"]
```

This tells the scheduler: "when allocating gpu0 and nic0, pick devices where `pcieRoot` matches." If gpu0 gets assigned `gpu-0` (pcieRoot=`pci0000:e6`), then nic0 MUST be a device with pcieRoot=`pci0000:e6` — which is `pci-0000-e9-00-0` (enp233s0).

### How the NIC selector works

```yaml
selectors:
- cel:
    expression: 'device.attributes["dra.net"].rdma == true'
```

This CEL (Common Expression Language) filter ensures the NIC supports RDMA. Without it, the scheduler might pick a non-RDMA interface (like `br-int` or `ovn-k8s-mp0`).

## Step 5: Create a Pod Using the Claim

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: gpu-rdma-test
  namespace: my-namespace
spec:
  restartPolicy: Never
  containers:
  - name: worker
    image: nvcr.io/nvidia/pytorch:24.01-py3
    command: ["sleep", "infinity"]
    resources:
      claims:
      - name: gpu-nic-resources
  # Reference the ResourceClaimTemplate
  resourceClaims:
  - name: gpu-nic-resources
    resourceClaimTemplateName: 8gpu-8nic
```

Apply and wait for scheduling:
```bash
kubectl apply -f pod.yaml
kubectl get pod gpu-rdma-test -w
```

## Step 6: Verify Inside the Pod

Once the pod is running, verify the GPU+NIC pairing:

```bash
kubectl exec -it gpu-rdma-test -- bash

# Check GPUs are visible
nvidia-smi -L

# Check RDMA NICs are injected
ip link show | grep -E "net[0-9]|enp"

# Check RDMA devices
rdma link show

# Verify GPU-NIC affinity via nvidia-smi topology
nvidia-smi topo -m
```

Expected: 8 GPUs visible, 8 network interfaces injected (net0-net7), and each NIC should be on the same PCIe root as its corresponding GPU.

## Scaling to Different GPU Counts

### 4 GPUs + 4 NICs

Use the same pattern but with 4 pairs instead of 8:

```yaml
apiVersion: resource.k8s.io/v1
kind: ResourceClaimTemplate
metadata:
  name: 4gpu-4nic
  namespace: my-namespace
spec:
  spec:
    devices:
      constraints:
      - matchAttribute: resource.kubernetes.io/pcieRoot
        requests: ["gpu0", "nic0"]
      - matchAttribute: resource.kubernetes.io/pcieRoot
        requests: ["gpu1", "nic1"]
      - matchAttribute: resource.kubernetes.io/pcieRoot
        requests: ["gpu2", "nic2"]
      - matchAttribute: resource.kubernetes.io/pcieRoot
        requests: ["gpu3", "nic3"]
      requests:
      - name: gpu0
        exactly: { count: 1, deviceClassName: gpu.nvidia.com }
      - name: nic0
        exactly:
          count: 1
          deviceClassName: dranet
          selectors:
          - cel: { expression: 'device.attributes["dra.net"].rdma == true' }
      - name: gpu1
        exactly: { count: 1, deviceClassName: gpu.nvidia.com }
      - name: nic1
        exactly:
          count: 1
          deviceClassName: dranet
          selectors:
          - cel: { expression: 'device.attributes["dra.net"].rdma == true' }
      - name: gpu2
        exactly: { count: 1, deviceClassName: gpu.nvidia.com }
      - name: nic2
        exactly:
          count: 1
          deviceClassName: dranet
          selectors:
          - cel: { expression: 'device.attributes["dra.net"].rdma == true' }
      - name: gpu3
        exactly: { count: 1, deviceClassName: gpu.nvidia.com }
      - name: nic3
        exactly:
          count: 1
          deviceClassName: dranet
          selectors:
          - cel: { expression: 'device.attributes["dra.net"].rdma == true' }
```

### 1 GPU + 1 NIC (minimal test)

```yaml
apiVersion: resource.k8s.io/v1
kind: ResourceClaimTemplate
metadata:
  name: 1gpu-1nic
  namespace: my-namespace
spec:
  spec:
    devices:
      constraints:
      - matchAttribute: resource.kubernetes.io/pcieRoot
        requests: ["gpu0", "nic0"]
      requests:
      - name: gpu0
        exactly: { count: 1, deviceClassName: gpu.nvidia.com }
      - name: nic0
        exactly:
          count: 1
          deviceClassName: dranet
          selectors:
          - cel: { expression: 'device.attributes["dra.net"].rdma == true' }
```

## Troubleshooting

### Pod stuck in Pending

```bash
kubectl describe pod <pod-name>
```

Common causes:
- **"not enough devices"**: Not enough free GPUs or RDMA NICs on any single node
- **"constraint not satisfiable"**: The PCIe pairing can't be satisfied — some GPUs don't have a co-located RDMA NIC
- **DeviceClass not found**: The `gpu.nvidia.com` or `dranet` DeviceClass doesn't exist

### Check available devices per node

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
for node, counts in sorted(per_node.items()):
    if counts['gpus'] > 0:
        print(f'{node}: {counts[\"gpus\"]} GPUs, {counts[\"rdma_nics\"]} RDMA NICs')
"
```

### Verify PCIe root pairing exists

If a GPU has no matching RDMA NIC on the same PCIe root, the constraint can never be satisfied. Use the verification script from Step 2 to confirm all GPUs have a co-located NIC.

## Full Pod Example with Resource Allocation

This is a complete working example of a pod that requests 8 GPUs with 8 paired RDMA NICs, including resource limits:

```yaml
apiVersion: resource.k8s.io/v1
kind: ResourceClaimTemplate
metadata:
  name: vllm-8gpu-8nic
  namespace: llm-serving
spec:
  spec:
    devices:
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
      requests:
      - name: gpu0
        exactly: { count: 1, deviceClassName: gpu.nvidia.com }
      - name: nic0
        exactly:
          count: 1
          deviceClassName: dranet
          selectors:
          - cel: { expression: 'device.attributes["dra.net"].rdma == true' }
      - name: gpu1
        exactly: { count: 1, deviceClassName: gpu.nvidia.com }
      - name: nic1
        exactly:
          count: 1
          deviceClassName: dranet
          selectors:
          - cel: { expression: 'device.attributes["dra.net"].rdma == true' }
      - name: gpu2
        exactly: { count: 1, deviceClassName: gpu.nvidia.com }
      - name: nic2
        exactly:
          count: 1
          deviceClassName: dranet
          selectors:
          - cel: { expression: 'device.attributes["dra.net"].rdma == true' }
      - name: gpu3
        exactly: { count: 1, deviceClassName: gpu.nvidia.com }
      - name: nic3
        exactly:
          count: 1
          deviceClassName: dranet
          selectors:
          - cel: { expression: 'device.attributes["dra.net"].rdma == true' }
      - name: gpu4
        exactly: { count: 1, deviceClassName: gpu.nvidia.com }
      - name: nic4
        exactly:
          count: 1
          deviceClassName: dranet
          selectors:
          - cel: { expression: 'device.attributes["dra.net"].rdma == true' }
      - name: gpu5
        exactly: { count: 1, deviceClassName: gpu.nvidia.com }
      - name: nic5
        exactly:
          count: 1
          deviceClassName: dranet
          selectors:
          - cel: { expression: 'device.attributes["dra.net"].rdma == true' }
      - name: gpu6
        exactly: { count: 1, deviceClassName: gpu.nvidia.com }
      - name: nic6
        exactly:
          count: 1
          deviceClassName: dranet
          selectors:
          - cel: { expression: 'device.attributes["dra.net"].rdma == true' }
      - name: gpu7
        exactly: { count: 1, deviceClassName: gpu.nvidia.com }
      - name: nic7
        exactly:
          count: 1
          deviceClassName: dranet
          selectors:
          - cel: { expression: 'device.attributes["dra.net"].rdma == true' }
---
apiVersion: v1
kind: Pod
metadata:
  name: vllm-server
  namespace: llm-serving
spec:
  restartPolicy: Never
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
    # NCCL will auto-detect the DRA-NET injected interfaces
    - name: NCCL_SOCKET_IFNAME
      value: "net0"
    - name: GLOO_SOCKET_IFNAME
      value: "net0"
    ports:
    - containerPort: 8000
    resources:
      claims:
      - name: gpu-nic
      requests:
        cpu: "16"
        memory: "128Gi"
      limits:
        cpu: "32"
        memory: "256Gi"
  resourceClaims:
  - name: gpu-nic
    resourceClaimTemplateName: vllm-8gpu-8nic
```

### What gets injected into the pod

When the pod starts, the DRA drivers inject:

1. **8 NVIDIA GPUs** — visible via `nvidia-smi`, CUDA device indices 0-7
2. **8 RDMA network interfaces** — named `net0` through `net7` inside the pod
3. **RDMA device files** — `/dev/infiniband/` devices for each NIC

The pod sees:
```
$ ip link show | grep net
3: net0: <BROADCAST,MULTICAST,UP> mtu 4096 ...
4: net1: <BROADCAST,MULTICAST,UP> mtu 4096 ...
5: net2: <BROADCAST,MULTICAST,UP> mtu 4096 ...
6: net3: <BROADCAST,MULTICAST,UP> mtu 4096 ...
7: net4: <BROADCAST,MULTICAST,UP> mtu 4096 ...
8: net5: <BROADCAST,MULTICAST,UP> mtu 4096 ...
9: net6: <BROADCAST,MULTICAST,UP> mtu 4096 ...
10: net7: <BROADCAST,MULTICAST,UP> mtu 4096 ...
```

Each `netN` interface corresponds to the RDMA NIC paired with GPU N via PCIe affinity.

### Resource allocation summary

| Resource | Source | How Allocated |
|----------|--------|--------------|
| GPUs (8×) | `gpu.nvidia.com` DeviceClass | DRA ResourceClaim with PCIe constraint |
| RDMA NICs (8×) | `dranet` DeviceClass | DRA ResourceClaim, filtered by `rdma == true` |
| GPU↔NIC pairing | `resource.kubernetes.io/pcieRoot` | DRA constraint ensures same PCIe root |
| CPU | Standard K8s resources | `requests` / `limits` in container spec |
| Memory | Standard K8s resources | `requests` / `limits` in container spec |

## How Inftune Studio Uses DRA-NET

Inftune Studio auto-detects DRA device classes on the cluster. When DRA is available and the user selects it as the network type, the optimizer:

1. Generates a ResourceClaimTemplate with the correct number of GPU+NIC pairs based on the TP (tensor parallelism) value
2. Sets the PCIe root constraint for each pair
3. Filters NICs with `rdma == true` to exclude non-RDMA interfaces
4. Deploys vLLM pods using the claim — each pod gets GPUs with their closest RDMA NICs
5. NCCL auto-discovers the injected interfaces and uses them for inter-node communication
