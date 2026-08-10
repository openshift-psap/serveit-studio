# Composite DRA Driver: GPU + RDMA NIC Pairing with PCIe Affinity

Deploy the composite DRA driver for automatic GPU+NIC PCIe-aware allocation on Kubernetes/OpenShift. This is an alternative to the webhook-based DRA-NET approach — it pre-composes GPU+NIC pairs as composite devices, making allocation simpler and faster.

## How It Works

```
NVIDIA DRA GPU Driver publishes GPUs with PCIe topology
                    \
                     -> Composite DRA Driver watches both, pairs by PCIe root
                    /
dranet Driver publishes RDMA NICs with PCIe topology

Result: "composite-gpu-nic-pair" devices — each a GPU + NIC on the same PCIe bus
```

The composite driver runs on every node as a DaemonSet. It:
1. Watches ResourceSlices from the NVIDIA GPU driver and dranet driver
2. Filters NICs to only RDMA-capable ones
3. Pairs each GPU with the NIC sharing the same PCIe root complex
4. Publishes composite devices as new ResourceSlices
5. A webhook converts pod resource requests into proper DRA claims

## Prerequisites

Before deploying the composite driver, these must be installed:

| Component | Namespace | Purpose |
|-----------|-----------|---------|
| NVIDIA DRA GPU Driver | nvidia-dra-driver-gpu | Publishes GPUs as DRA devices with PCIe topology |
| dranet DRA Driver | kube-system | Publishes NICs as DRA devices with RDMA + PCIe info |
| NVIDIA GPU Operator | nvidia-gpu-operator | GPU driver, container toolkit, DCGM |
| NVIDIA Network Operator | nvidia-network-operator | MOFED/DOCA driver for Mellanox RDMA NICs |
| cert-manager | cert-manager | TLS certificate automation for webhook |

Verify prerequisites:

```bash
# GPU DRA driver
kubectl get deviceclass gpu.nvidia.com
kubectl get pods -n nvidia-dra-driver-gpu

# dranet driver
kubectl get deviceclass dranet
kubectl get ds dranet -n kube-system

# cert-manager
kubectl get pods -n cert-manager
```

## Step 1: Create Namespace and Issuer

```bash
kubectl create namespace composite-dra-system
```

Create a self-signed cert-manager Issuer for the webhook TLS:

```yaml
apiVersion: cert-manager.io/v1
kind: Issuer
metadata:
  name: composite-dra-selfsigned
  namespace: composite-dra-system
spec:
  selfSigned: {}
```

```bash
kubectl apply -f issuer.yaml
```

## Step 2: Create the dranet DeviceClass

If not already present:

```yaml
apiVersion: resource.k8s.io/v1
kind: DeviceClass
metadata:
  name: dranet
spec:
  selectors:
  - cel:
      expression: "device.driver == 'dra.net'"
```

```bash
kubectl apply -f dranet-deviceclass.yaml
```

## Step 3: Create the Driver Configuration

### Main config (`composite-dra-config`)

This defines how GPUs and NICs are discovered and paired:

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: composite-dra-config
  namespace: composite-dra-system
data:
  config.yaml: |
    driver:
      name: "composite.dra.io"

    sources:
      - name: gpu
        driver: gpu.nvidia.com
        deviceClassName: gpu.nvidia.com
        forwardAttributes:
          - domain: resource.kubernetes.io
            attributes: [pciBusID, pcieRoot]
          - domain: gpu.nvidia.com
            attributes: [model, memory]

      - name: nic
        driver: dra.net
        deviceClassName: dranet
        forwardAttributes:
          - domain: dra.net
            attributes: [ifName, pciAddress, numaNode, rdma, encapsulation, ipv4, mac]
          - domain: resource.kubernetes.io
            attributes: [pcieRoot]

    compositions:
      - name: gpu-nic-pair
        pairingMode: auto
        transportMode: ethernet
        members:
          - source: gpu
            count: 1
          - source: nic
            count: 1
        constraints:
          - type: matchAttribute
            attribute: resource.kubernetes.io/pcieRoot
        filters:
          nic:
            cel: device.attributes["dra.net"].rdma == true

      - name: gpu
        members:
          - source: gpu
            count: 1

    deviceParams:
      configMapPath: /etc/composite-dra/device-params/params.yaml
```

Key fields:
- **sources**: Watch GPUs from `gpu.nvidia.com` and NICs from `dra.net`, forwarding PCIe topology attributes
- **compositions.gpu-nic-pair**: Pair 1 GPU + 1 NIC constrained by matching `pcieRoot`. Only RDMA NICs pass the filter.
- **compositions.gpu**: Standalone GPU wrapper (no NIC pairing)
- **pairingMode: auto**: Driver automatically discovers pairs based on constraints

### Network parameters (`composite-dra-device-params`)

Configure per-rail networking. Each RDMA NIC gets its own subnet, routing table, and cross-rail routes:

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: composite-dra-device-params
  namespace: composite-dra-system
data:
  params.yaml: |
    nic:
      params: |
        {
          "interface": {
            "name": "net{{.PairOrdinal}}",
            "mtu": {{device "dra.net/mtu"}},
            "addresses": ["{{device "dra.net/ipv4"}}"]
          },
          "routes": [
            {"destination": "{{network (device "dra.net/ipv4")}}", "scope": 253, "table": {{.Table}}}
            {{- range .CrossRails}},
            {"destination": "{{.}}", "gateway": "{{$.Gateway}}"}
            {{- end}},
            {"destination": "0.0.0.0/0", "gateway": "{{.Gateway}}", "table": {{.Table}}}
          ],
          "rules": [
            {"source": "{{network (device "dra.net/ipv4")}}", "table": {{.Table}}, "priority": 32765}
          ]
        }
      entries:
        - match: { "dra.net/ipv4": {prefix: "10.0."} }
          values: { Gateway: "10.0.0.1", Table: 100, CrossRails: ["10.1.0.0/16","10.2.0.0/16","10.3.0.0/16","10.4.0.0/16","10.5.0.0/16","10.6.0.0/16","10.7.0.0/16"] }
        - match: { "dra.net/ipv4": {prefix: "10.1."} }
          values: { Gateway: "10.1.0.1", Table: 101, CrossRails: ["10.0.0.0/16","10.2.0.0/16","10.3.0.0/16","10.4.0.0/16","10.5.0.0/16","10.6.0.0/16","10.7.0.0/16"] }
        - match: { "dra.net/ipv4": {prefix: "10.2."} }
          values: { Gateway: "10.2.0.1", Table: 102, CrossRails: ["10.0.0.0/16","10.1.0.0/16","10.3.0.0/16","10.4.0.0/16","10.5.0.0/16","10.6.0.0/16","10.7.0.0/16"] }
        - match: { "dra.net/ipv4": {prefix: "10.3."} }
          values: { Gateway: "10.3.0.1", Table: 103, CrossRails: ["10.0.0.0/16","10.1.0.0/16","10.2.0.0/16","10.4.0.0/16","10.5.0.0/16","10.6.0.0/16","10.7.0.0/16"] }
        - match: { "dra.net/ipv4": {prefix: "10.4."} }
          values: { Gateway: "10.4.0.1", Table: 104, CrossRails: ["10.0.0.0/16","10.1.0.0/16","10.2.0.0/16","10.3.0.0/16","10.5.0.0/16","10.6.0.0/16","10.7.0.0/16"] }
        - match: { "dra.net/ipv4": {prefix: "10.5."} }
          values: { Gateway: "10.5.0.1", Table: 105, CrossRails: ["10.0.0.0/16","10.1.0.0/16","10.2.0.0/16","10.3.0.0/16","10.4.0.0/16","10.6.0.0/16","10.7.0.0/16"] }
        - match: { "dra.net/ipv4": {prefix: "10.6."} }
          values: { Gateway: "10.6.0.1", Table: 106, CrossRails: ["10.0.0.0/16","10.1.0.0/16","10.2.0.0/16","10.3.0.0/16","10.4.0.0/16","10.5.0.0/16","10.7.0.0/16"] }
        - match: { "dra.net/ipv4": {prefix: "10.7."} }
          values: { Gateway: "10.7.0.1", Table: 107, CrossRails: ["10.0.0.0/16","10.1.0.0/16","10.2.0.0/16","10.3.0.0/16","10.4.0.0/16","10.5.0.0/16","10.6.0.0/16"] }
```

Adjust the subnets and gateways to match your cluster's RDMA network topology.

```bash
kubectl apply -f composite-dra-config.yaml -n composite-dra-system
kubectl apply -f composite-dra-device-params.yaml -n composite-dra-system
```

## Step 4: Install via Helm

```bash
helm install composite ./composite-dra-driver \
  --namespace composite-dra-system \
  --set driver.image=ghcr.io/openshift-psap/composite-dra-driver:latest \
  --set webhook.image=ghcr.io/openshift-psap/composite-dra-webhook:latest
```

The chart creates:
- **DaemonSet** `composite-dra-driver` — runs on all nodes, discovers and pairs devices
- **Deployment** `composite-dra-driver-webhook` — mutating webhook that converts resource requests to DRA claims
- **DeviceClasses** — `composite-gpu` and `composite-gpu-nic-pair`
- **Certificate** — TLS for webhook via cert-manager
- **MutatingWebhookConfiguration** — intercepts pod creation
- **RBAC** — ServiceAccount, ClusterRole, ClusterRoleBinding

## Step 5: Verify

### Check pods

```bash
kubectl get pods -n composite-dra-system
```

Expected: one DaemonSet pod per node + one webhook pod:

```
composite-dra-driver-xxxxx    1/1   Running   (one per node)
composite-dra-driver-webhook-xxxxx   1/1   Running   (single replica)
```

### Check device classes

```bash
kubectl get deviceclass | grep composite
```

```
composite-gpu              42d
composite-gpu-nic-pair     42d
```

### Check composite devices are published

```bash
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

Expected (for a node with 8 GPUs and 8 RDMA NICs):
```
gpu-worker-1: 8 GPU+NIC pairs, 8 standalone GPUs
gpu-worker-2: 8 GPU+NIC pairs, 8 standalone GPUs
```

If a node shows 0 GPU+NIC pairs but 8 standalone GPUs, the RDMA NICs aren't configured — check the Network Operator and MOFED driver.

### Verify PCIe pairing

```bash
kubectl get resourceslice -o json | python3 -c "
import json, sys
data = json.load(sys.stdin)
for item in data.get('items', []):
    if item.get('spec', {}).get('driver') != 'composite.dra.io':
        continue
    node = item['spec'].get('nodeName', '')
    for d in item['spec'].get('devices', []):
        attrs = d.get('attributes', {})
        comp = attrs.get('composite/compositionName', {}).get('string', '')
        if comp != 'gpu-nic-pair':
            continue
        gpu_pcie = attrs.get('gpu/pcieRoot', {}).get('string', '')
        nic_pcie = attrs.get('nic/pcieRoot', {}).get('string', '')
        gpu_bus = attrs.get('gpu/pciBusID', {}).get('string', '')
        nic_addr = attrs.get('nic/pciAddress', {}).get('string', '')
        nic_if = attrs.get('nic/ifName', {}).get('string', '')
        nic_ip = attrs.get('nic/ipv4', {}).get('string', '')
        match = '✅' if gpu_pcie == nic_pcie else '❌'
        print(f'{match} {node[:20]}  GPU={gpu_bus}  NIC={nic_addr} ({nic_if})  PCIe={gpu_pcie}  IP={nic_ip}')
"
```

All pairs should show matching PCIe roots.

### Check driver logs

```bash
# On a GPU node
kubectl logs -n composite-dra-system -l app.kubernetes.io/name=composite-dra-driver \
  --field-selector spec.nodeName=<gpu-node-name> | grep -E "source device|filter|composite devices|pool published"
```

Healthy output:
```
synthesizer: source device count  source="nic" count=13
synthesizer: source device count  source="gpu" count=8
pairer: CEL filter applied  expression="..." passed=8 total=13
synthesizer: computed composite devices  count=16 sourceDevices=21
publisher: pool published  pool="...gpu-nic-pair" count=8 slices=1
publisher: pool published  pool="...gpu" count=8 slices=1
```

## Step 6: Use in Pods

Request GPU+NIC pairs using the synthetic resource:

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: inference-worker
spec:
  containers:
  - name: vllm
    image: ghcr.io/llm-d/llm-d-cuda:v0.8.0
    resources:
      requests:
        composite.dra.io/gpu-nic-pair: "8"
      limits:
        composite.dra.io/gpu-nic-pair: "8"
    env:
    - name: NCCL_SOCKET_IFNAME
      value: "net0"
    - name: GLOO_SOCKET_IFNAME
      value: "net0"
```

The webhook intercepts the `composite.dra.io/gpu-nic-pair` resource, creates proper DRA ResourceClaims referencing the `composite-gpu-nic-pair` DeviceClass, and injects them into the pod spec.

For GPU-only (no NIC pairing):
```yaml
resources:
  requests:
    composite.dra.io/gpu: "4"
```

## Troubleshooting

### No GPU+NIC pairs on a node

Check if RDMA NICs are present:
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

If 0 RDMA NICs: check NVIDIA Network Operator, MOFED driver, and NIC firmware.

### Webhook not mutating pods

```bash
# Check webhook is running
kubectl get pods -n composite-dra-system -l app.kubernetes.io/name=composite-dra-driver-webhook

# Check webhook logs
kubectl logs -n composite-dra-system deploy/composite-dra-driver-webhook

# Verify namespace is not excluded
kubectl get mutatingwebhookconfiguration composite-dra-driver-webhook -o yaml | grep -A20 namespaceSelector
```

### TopologyAffinityError

If pods get `TopologyAffinityError`, the kubelet topology manager is set to `single-numa-node` and the GPU+NIC pair spans NUMA zones. Fix:

```bash
# Patch topology manager to best-effort
kubectl patch kubeletconfig <config-name> --type merge \
  -p '{"spec":{"kubeletConfig":{"topologyManagerPolicy":"best-effort"}}}'
```

GPU nodes will reboot to apply.

## Cleanup

```bash
helm uninstall composite -n composite-dra-system
kubectl delete namespace composite-dra-system
kubectl delete deviceclass composite-gpu composite-gpu-nic-pair
kubectl delete clusterrole composite-dra-driver
kubectl delete clusterrolebinding composite-dra-driver
```
