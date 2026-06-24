# OpenShift GPU + RDMA Deployment Guide

Complete deployment guide for setting up an OpenShift cluster with NVIDIA GPUs and RDMA networking for LLM inference. Covers three networking options: DRA (cloud), SR-IOV (bare-metal L2), and SR-IOV + nv-ipam (bare-metal L3).

---

## Part 1: Common Setup (All Environments)

These steps are required regardless of networking type.

### Deployment Order

```
1. Node Feature Discovery (NFD)
2. NVIDIA Network Operator (MOFED driver only)
3. NVIDIA GPU Operator
4. Networking (choose one):
   - Option A: DRA (cloud VMs with pre-assigned NICs)
   - Option B: SR-IOV (bare-metal, L2 flat network)
   - Option C: SR-IOV + nv-ipam + sbr (bare-metal, L3 routed)
5. LeaderWorkerSet (LWS) Operator
```

> **Important:** The NVIDIA Network Operator (MOFED) must be installed and ready **before** the GPU Operator. The GPU operator's driver container depends on MOFED kernel modules for RDMA (nvidia-peermem).

---

### Step 1: Node Feature Discovery (NFD)

NFD detects hardware features (GPUs, NICs, RDMA capability) and labels nodes accordingly. Both NVIDIA operators depend on these labels.

#### 1a. Create namespace and install operator

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: openshift-nfd
---
apiVersion: operators.coreos.com/v1
kind: OperatorGroup
metadata:
  name: nfd
  namespace: openshift-nfd
spec:
  targetNamespaces:
  - openshift-nfd
---
apiVersion: operators.coreos.com/v1alpha1
kind: Subscription
metadata:
  name: nfd
  namespace: openshift-nfd
spec:
  channel: stable
  name: nfd
  source: redhat-operators
  sourceNamespace: openshift-marketplace
  installPlanApproval: Manual
```

After applying, approve the InstallPlan:

```bash
oc get installplan -n openshift-nfd
oc patch installplan <plan-name> -n openshift-nfd --type merge -p '{"spec":{"approved":true}}'
```

#### 1b. Create the NodeFeatureDiscovery instance

```yaml
apiVersion: nfd.openshift.io/v1
kind: NodeFeatureDiscovery
metadata:
  name: nfd-instance
  namespace: openshift-nfd
spec:
  enableTaints: false
  operand:
    imagePullPolicy: IfNotPresent
    servicePort: 12000
  topologyUpdater: false
  workerConfig:
    configData: |
      core:
        sleepInterval: 60s
      sources:
        pci:
          deviceClassWhitelist:
            - "0200"
            - "03"
            - "12"
          deviceLabelFields:
            - vendor
```

#### 1c. Verify

```bash
oc get nodes -l feature.node.kubernetes.io/pci-10de.present=true   # NVIDIA GPU
oc get nodes -l feature.node.kubernetes.io/pci-15b3.present=true   # Mellanox NIC
oc get nodes -l feature.node.kubernetes.io/rdma.capable=true       # RDMA capable
```

Expected output (GPU nodes have all three labels):

```
NAME                                            STATUS   ROLES        AGE
rits-llmd-rhoai-lrrzc-worker-h100-3-gdr-665m9   Ready    gdr,worker   11d
rits-llmd-rhoai-lrrzc-worker-h100-3-gdr-8ghjl   Ready    gdr,worker   11d
...
```

---

### Step 2: NVIDIA Network Operator (MOFED Driver)

Deploys the DOCA/MOFED driver on GPU nodes. Provides the kernel modules needed for RDMA (mlx5_core, mlx5_ib, ib_core, rdma_cm, nvidia-peermem).

#### 2a. Create namespace and install operator

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: nvidia-network-operator
---
apiVersion: operators.coreos.com/v1
kind: OperatorGroup
metadata:
  name: nvidia-network-operator
  namespace: nvidia-network-operator
spec:
  targetNamespaces:
  - nvidia-network-operator
---
apiVersion: operators.coreos.com/v1alpha1
kind: Subscription
metadata:
  name: nvidia-network-operator
  namespace: nvidia-network-operator
spec:
  channel: stable
  name: nvidia-network-operator
  source: certified-operators
  sourceNamespace: openshift-marketplace
  installPlanApproval: Manual
```

Approve the InstallPlan:

```bash
oc get installplan -n nvidia-network-operator
oc patch installplan <plan-name> -n nvidia-network-operator --type merge -p '{"spec":{"approved":true}}'
```

#### 2b. Create NicClusterPolicy

For **cloud / DRA** setups — MOFED driver only:

```yaml
apiVersion: mellanox.com/v1alpha1
kind: NicClusterPolicy
metadata:
  name: nic-cluster-policy
spec:
  ofedDriver:
    image: doca-driver
    repository: nvcr.io/nvidia/mellanox
    version: doca3.3.0-26.01-1.0.0.0-0
    forcePrecompiled: false
    terminationGracePeriodSeconds: 300
    env:
    - name: UNLOAD_STORAGE_MODULES
      value: "true"
    startupProbe:
      initialDelaySeconds: 30
      periodSeconds: 60
    livenessProbe:
      initialDelaySeconds: 30
      periodSeconds: 60
    readinessProbe:
      initialDelaySeconds: 10
      periodSeconds: 60
    upgradePolicy:
      autoUpgrade: false
      maxParallelUpgrades: 1
      safeLoad: false
      drain:
        enable: true
        force: true
        deleteEmptyDir: true
        podSelector: ""
        timeoutSeconds: 300
```

For **bare-metal / L3** setups — MOFED driver + nv-ipam (see [Option C](#option-c-sr-iov--nv-ipam--sbr-bare-metal-l3-routed)):

```yaml
apiVersion: mellanox.com/v1alpha1
kind: NicClusterPolicy
metadata:
  name: nic-cluster-policy
spec:
  ofedDriver:
    image: doca-driver
    repository: nvcr.io/nvidia/mellanox
    version: doca3.3.0-26.01-1.0.0.0-0
    forcePrecompiled: false
    terminationGracePeriodSeconds: 300
    env:
    - name: UNLOAD_STORAGE_MODULES
      value: "true"
    - name: CREATE_IFNAMES_UDEV
      value: "true"
    startupProbe:
      initialDelaySeconds: 10
      periodSeconds: 20
    livenessProbe:
      initialDelaySeconds: 30
      periodSeconds: 30
    readinessProbe:
      initialDelaySeconds: 10
      periodSeconds: 30
    upgradePolicy:
      autoUpgrade: true
      maxParallelUpgrades: 2
      drain:
        enable: true
        force: true
        deleteEmptyDir: true
        podSelector: ""
        timeoutSeconds: 300
  nvIpam:
    enableWebhook: false
    image: nvidia-k8s-ipam
    repository: nvcr.io/nvidia/mellanox
    version: network-operator-v26.1.0
```

> **Note:** The `nvIpam` section deploys the nv-ipam IPAM plugin, which is only needed for L3/routed bare-metal setups. Cloud setups skip it.

#### 2c. Verify

```bash
oc get pods -n nvidia-network-operator -l app=mofed
oc get nicclusterpolicy nic-cluster-policy -o jsonpath='{.status.state}'
```

Expected output:

```
NAME                                   READY   STATUS    RESTARTS   AGE
mofed-rhel9.6-579d85d47d-ds-2l74v     2/2     Running   0          11d
mofed-rhel9.6-579d85d47d-ds-6cqnh     2/2     Running   0          11d
...
ready
```

All MOFED pods should show `2/2 Running`. This can take 10-15 minutes on first install.

---

### Step 3: NVIDIA GPU Operator

Deploys the GPU driver, device plugin, DCGM, toolkit, and feature discovery.

#### 3a. Create namespace and install operator

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: nvidia-gpu-operator
---
apiVersion: operators.coreos.com/v1
kind: OperatorGroup
metadata:
  name: nvidia-gpu-operator
  namespace: nvidia-gpu-operator
spec:
  targetNamespaces:
  - nvidia-gpu-operator
---
apiVersion: operators.coreos.com/v1alpha1
kind: Subscription
metadata:
  name: gpu-operator-certified
  namespace: nvidia-gpu-operator
spec:
  channel: stable
  name: gpu-operator-certified
  source: certified-operators
  sourceNamespace: openshift-marketplace
  installPlanApproval: Manual
```

Approve the InstallPlan:

```bash
oc get installplan -n nvidia-gpu-operator
oc patch installplan <plan-name> -n nvidia-gpu-operator --type merge -p '{"spec":{"approved":true}}'
```

#### 3b. Create ClusterPolicy

```yaml
apiVersion: nvidia.com/v1
kind: ClusterPolicy
metadata:
  name: gpu-cluster-policy
spec:
  operator:
    defaultRuntime: crio
    use_ocp_driver_toolkit: true
    runtimeClass: nvidia
    initContainer: {}
  daemonsets:
    updateStrategy: RollingUpdate
    rollingUpdate:
      maxUnavailable: "1"
  driver:
    enabled: true
    kernelModuleType: auto
    rdma:
      enabled: true
      useHostMofed: false
    certConfig:
      name: ""
    licensingConfig:
      nlsEnabled: true
      secretName: ""
    repoConfig:
      configMapName: ""
    kernelModuleConfig:
      name: ""
    virtualTopology:
      config: ""
    useNvidiaDriverCRD: false
    upgradePolicy:
      autoUpgrade: true
      maxParallelUpgrades: 1
      maxUnavailable: 25%
      drain:
        enable: false
        force: false
        deleteEmptyDir: false
        timeoutSeconds: 300
      podDeletion:
        force: false
        deleteEmptyDir: false
        timeoutSeconds: 300
      waitForCompletion:
        timeoutSeconds: 0
  toolkit:
    enabled: true
    installDir: /usr/local/nvidia
  devicePlugin:
    enabled: true
    config:
      name: ""
      default: ""
    mps:
      root: /run/nvidia/mps
  dcgm:
    enabled: true
  dcgmExporter:
    enabled: true
    config:
      name: ""
    serviceMonitor:
      enabled: true
      interval: 1s
  gfd:
    enabled: true
  mig:
    strategy: single
  migManager:
    enabled: true
  nodeStatusExporter:
    enabled: true
  cdi:
    enabled: true
    default: false
  sandboxWorkloads:
    enabled: false
    defaultWorkload: container
  sandboxDevicePlugin:
    enabled: true
  vfioManager:
    enabled: true
  vgpuManager:
    enabled: false
  vgpuDeviceManager:
    enabled: true
  gdrcopy:
    enabled: true
  gds:
    enabled: false
  validator:
    plugin:
      env: []
```

> **Key settings:**
> - `driver.rdma.enabled: true` — loads nvidia-peermem for GPU Direct RDMA
> - `driver.rdma.useHostMofed: false` — uses the containerized MOFED from the Network Operator
> - `cdi.enabled: true` — enables Container Device Interface for GPU passthrough
> - `gdrcopy.enabled: true` — enables GPU Direct Copy for low-latency GPU memory transfers (required for optimal RDMA performance)

#### 3c. Verify

```bash
oc get clusterpolicy gpu-cluster-policy -o jsonpath='{.status.state}'
oc get nodes -l nvidia.com/gpu.present=true -o custom-columns='NAME:.metadata.name,GPUs:.status.allocatable.nvidia\.com/gpu'
```

Expected output:

```
ready

NAME                                            GPUs
rits-llmd-rhoai-lrrzc-worker-h100-3-gdr-665m9   8
rits-llmd-rhoai-lrrzc-worker-h100-3-gdr-8ghjl   8
rits-llmd-rhoai-lrrzc-worker-h100-3-gdr-c2zbs   8
...
```

---

### LeaderWorkerSet (LWS) Operator

Required for all setups. Manages pod groups for distributed inference.

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: openshift-lws-operator
---
apiVersion: operators.coreos.com/v1
kind: OperatorGroup
metadata:
  name: openshift-lws-operator
  namespace: openshift-lws-operator
spec:
  targetNamespaces:
  - openshift-lws-operator
---
apiVersion: operators.coreos.com/v1alpha1
kind: Subscription
metadata:
  name: leader-worker-set
  namespace: openshift-lws-operator
spec:
  channel: stable-v1.0
  name: leader-worker-set
  source: redhat-operators
  sourceNamespace: openshift-marketplace
  installPlanApproval: Automatic
```

---

## Part 2: Networking Options

Choose one based on your environment.

| Option | Environment | NICs | IP Assignment | GPU-NIC Pairing |
|--------|-------------|------|---------------|-----------------|
| **A: DRA** | Cloud VMs (IBM Cloud VPC) | Pre-assigned VFs, 1 per GPU | Cloud-managed IPs | Automatic (PCIe root) |
| **B: SR-IOV** | Bare-metal, L2 flat | PFs → create VFs | Cloud/switch-managed | Manual (per-PF policy) |
| **C: SR-IOV + nv-ipam** | Bare-metal, L3 routed | PFs → create VFs | nv-ipam CIDRPools | Manual (per-PF policy) |

---

## Option A: DRA (Cloud VMs)

**Use when:** NICs are passed through to the VM as VFs by the hypervisor (e.g., IBM Cloud VPC). You cannot create additional VFs from VFs, so the SR-IOV operator has nothing to manage. DRA works directly with the NICs and provides GPU-NIC PCIe affinity pairing.

**Tested on:** IBM Cloud VPC `gx3d-160x1792x8h100-research`, 8× H100 per node, ConnectX-7 VFs

### Understanding GPU-NIC Rail Topology

Each GPU has a physically closest NIC on the same PCIe root complex — this pairing is called a "rail." DRA uses the PCIe root to automatically pair them.

#### How to discover rails

```bash
oc debug node/<gpu-node> -- chroot /host bash -c "
echo 'NIC          | NIC PCI      | GPU PCI      | NUMA | PCIe Root'
for dev in /sys/class/net/enp*; do
  iface=\$(basename \$dev)
  nic_pci=\$(readlink \$dev/device | xargs basename)
  numa=\$(cat \$dev/device/numa_node 2>/dev/null)
  nic_root=\$(readlink -f \$dev/device | sed 's|.*pci0000:\([0-9a-f]*\)/.*|\1|')
  ip=\$(ip -4 addr show \$iface 2>/dev/null | grep inet | awk '{print \$2}')
  echo \"\$iface | \$nic_pci | | \$numa | \$nic_root | \$ip\"
done"
```

Example output (IBM Cloud VPC `gx3d-160x1792x8h100-research`):

```
NIC          | NIC PCI        |              | NUMA | PCIe Root | IP
enp3s0       | 0000:03:00.0   |              | 0    | 00        | 10.241.129.44/24   (management — skip)
enp163s0     | 0000:a3:00.0   |              | 0    | a0        | 10.0.0.18/16
enp173s0     | 0000:ad:00.0   |              | 0    | aa        | 10.1.0.18/16
enp183s0     | 0000:b7:00.0   |              | 0    | b4        | 10.2.0.18/16
enp193s0     | 0000:c1:00.0   |              | 0    | be        | 10.3.0.18/16
enp203s0     | 0000:cb:00.0   |              | 1    | c8        | 10.4.0.18/16
enp213s0     | 0000:d5:00.0   |              | 1    | d2        | 10.6.0.18/16
enp223s0     | 0000:df:00.0   |              | 1    | dc        | 10.5.0.18/16
enp233s0     | 0000:e9:00.0   |              | 1    | e6        | 10.7.0.18/16
```

Cross-reference with GPU PCI addresses to find the pairing:

```
Rail | NIC PCI      | GPU PCI      | PCIe Root (shared)
0    | 0000:a3:00.0 | 0000:a4:00.0 | a0
1    | 0000:ad:00.0 | 0000:ae:00.0 | aa
2    | 0000:b7:00.0 | 0000:b8:00.0 | b4
3    | 0000:c1:00.0 | 0000:c2:00.0 | be
4    | 0000:cb:00.0 | 0000:cc:00.0 | c8
5    | 0000:d5:00.0 | 0000:d6:00.0 | d2
6    | 0000:df:00.0 | 0000:e0:00.0 | dc
7    | 0000:e9:00.0 | 0000:ea:00.0 | e6
```

The key indicator: when a NIC and GPU share the same PCIe root (e.g., both under `pci0000:a0`), they're on the same rail. Data between them stays within the PCIe switch — no CPU socket crossing.

#### Verify NIC naming consistency across nodes

```bash
for node in $(oc get nodes -l node-role.kubernetes.io/gdr= -o name); do
  echo "=== $(echo $node | cut -d/ -f2) ==="
  oc debug $node -- chroot /host ls /sys/class/net/ 2>&1 | grep -v "Starting\|Removing\|To use\|lo"
done
```

Expected output (names should be identical across all nodes):

```
=== worker-h100-3-gdr-665m9 ===
enp163s0 enp173s0 enp183s0 enp193s0 enp203s0 enp213s0 enp223s0 enp233s0 enp3s0
=== worker-h100-3-gdr-8ghjl ===
enp163s0 enp173s0 enp183s0 enp193s0 enp203s0 enp213s0 enp223s0 enp233s0 enp3s0
...
```

If names differ between nodes, you'll need udev rules to rename them, or list all variants in the SR-IOV `pfNames` list.

### A0. Enable NRI in CRI-O (Required for DRANET)

DRANET uses NRI (Node Resource Interface) hooks to move NICs into pod network namespaces. NRI must be explicitly enabled in CRI-O — without it, DRANET's NIC injection fails with `file exists` errors and orphaned network interfaces.

On OpenShift, create a MachineConfig targeting the GPU nodes:

```yaml
apiVersion: machineconfiguration.openshift.io/v1
kind: MachineConfig
metadata:
  name: 99-gdr-enable-nri
  labels:
    machineconfiguration.openshift.io/role: gdr
spec:
  config:
    ignition:
      version: 3.2.0
    storage:
      files:
      - path: /etc/crio/crio.conf.d/99-nri.conf
        mode: 0644
        overwrite: true
        contents:
          source: data:text/plain;charset=utf-8;base64,W2NyaW8ubnJpXQplbmFibGVfbnJpID0gdHJ1ZQpucmlfcGx1Z2luX3JlcXVlc3RfdGltZW91dCA9ICIzMHMiCm5yaV9wbHVnaW5fcmVnaXN0cmF0aW9uX3RpbWVvdXQgPSAiMTBzIgo=
```

The base64 content decodes to:

```ini
[crio.nri]
enable_nri = true
nri_plugin_request_timeout = "30s"
nri_plugin_registration_timeout = "10s"
```

> **Warning:** This triggers a rolling reboot of all GPU nodes. Wait for the MachineConfigPool to show `UPDATED=True` before proceeding.

To speed up the rollout, increase parallel updates (default is 1 node at a time):

```bash
oc patch mcp gdr --type merge -p '{"spec":{"maxUnavailable":4}}'
```

```bash
# Monitor rollout progress
oc get mcp gdr -w
```

#### Verify NRI is enabled

```bash
oc debug node/<gpu-node> -- chroot /host cat /etc/crio/crio.conf.d/99-nri.conf
```

### A1. DRANET Driver

DRANET discovers network interfaces and advertises them as DRA devices with PCIe topology attributes.

#### RBAC

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: dranet
  namespace: kube-system
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: dranet
rules:
- apiGroups: [""]
  resources: ["nodes"]
  verbs: ["get"]
- apiGroups: ["resource.k8s.io"]
  resources: ["resourceslices"]
  verbs: ["list", "watch", "create", "update", "delete"]
- apiGroups: ["resource.k8s.io"]
  resources: ["resourceclaims", "deviceclasses"]
  verbs: ["get"]
- apiGroups: ["resource.k8s.io"]
  resources: ["resourceclaims/status"]
  verbs: ["patch", "update"]
- apiGroups: ["resource.k8s.io"]
  resourceNames: ["dra.net"]
  resources: ["resourceclaims/driver"]
  verbs: ["associated-node:patch", "associated-node:update"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: dranet
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: dranet
subjects:
- kind: ServiceAccount
  name: dranet
  namespace: kube-system
```

On OpenShift:

```bash
oc adm policy add-scc-to-user privileged -z dranet -n kube-system
```

#### DaemonSet

```yaml
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: dranet
  namespace: kube-system
  labels:
    app: dranet
spec:
  selector:
    matchLabels:
      app: dranet
  template:
    metadata:
      labels:
        app: dranet
    spec:
      serviceAccountName: dranet
      hostNetwork: true
      tolerations:
      - effect: NoSchedule
        operator: Exists
      containers:
      - name: dranet
        image: registry.k8s.io/networking/dranet:v1.2.0
        args: ["/dranet", "--v=4", "--hostname-override=$(NODE_NAME)"]
        env:
        - name: NODE_NAME
          valueFrom:
            fieldRef:
              fieldPath: spec.nodeName
        securityContext:
          privileged: true
        resources:
          requests:
            cpu: 100m
            memory: 50Mi
        readinessProbe:
          httpGet:
            path: /healthz
            port: 9177
        volumeMounts:
        - { mountPath: /var/lib/kubelet/plugins, name: device-plugin }
        - { mountPath: /var/lib/kubelet/plugins_registry, name: plugin-registry }
        - { mountPath: /var/run/nri, name: nri-plugin }
        - { mountPath: /var/run/netns, mountPropagation: HostToContainer, name: netns }
        - { mountPath: /dev/infiniband, mountPropagation: HostToContainer, name: infiniband }
        - { mountPath: /sys/fs/bpf, mountPropagation: HostToContainer, name: bpf-programs }
        - { mountPath: /var/run/dranet, name: dranet-run }
      volumes:
      - { hostPath: { path: /var/lib/kubelet/plugins }, name: device-plugin }
      - { hostPath: { path: /var/lib/kubelet/plugins_registry }, name: plugin-registry }
      - { hostPath: { path: /var/run/nri }, name: nri-plugin }
      - { hostPath: { path: /var/run/netns }, name: netns }
      - { hostPath: { path: /dev/infiniband }, name: infiniband }
      - { hostPath: { path: /sys/fs/bpf }, name: bpf-programs }
      - { hostPath: { path: /var/run/dranet, type: DirectoryOrCreate }, name: dranet-run }
---
apiVersion: resource.k8s.io/v1
kind: DeviceClass
metadata:
  name: dranet
spec:
  selectors:
  - cel:
      expression: device.driver == "dra.net"
```

#### Verify

```bash
oc get pods -n kube-system -l app=dranet
oc get resourceslices | grep dra.net
```

Expected output:

```
NAME           READY   STATUS    AGE
dranet-554vr   1/1     Running   20s
dranet-822kd   1/1     Running   20s
...  (one per node)

rits-llmd-...-665m9-dra.net-k4fsq   ...   dra.net   6s
rits-llmd-...-8ghjl-dra.net-ccckm   ...   dra.net   6s
...  (one ResourceSlice per node)
```

### A2. NVIDIA DRA GPU Driver

```bash
oc label nodes -l nvidia.com/gpu.present=true nvidia.com/dra-kubelet-plugin=true

helm repo add nvidia https://helm.ngc.nvidia.com/nvidia && helm repo update

helm install nvidia-dra-driver-gpu nvidia/nvidia-dra-driver-gpu \
  --namespace nvidia-dra-driver-gpu --create-namespace \
  --set image.pullPolicy=IfNotPresent \
  --set nvidiaDriverRoot=/run/nvidia/driver \
  --set gpuResourcesEnabledOverride=true \
  --set-string 'kubeletPlugin.nodeSelector.nvidia\.com/dra-kubelet-plugin=true' \
  --set 'controller.tolerations[0].key=node-role.kubernetes.io/master' \
  --set 'controller.tolerations[0].operator=Exists' \
  --set 'controller.tolerations[0].effect=NoSchedule' \
  --version 25.12.0

oc adm policy add-scc-to-user privileged \
  -z nvidia-dra-driver-gpu-service-account-kubeletplugin \
  -n nvidia-dra-driver-gpu
```

### A3. DRA GPU-NIC Admission Webhook

The admission webhook intercepts pods requesting `dra.llm-d.io/gpu-nic-pair` and automatically creates ResourceClaimTemplates with proper GPU+NIC pairing, PCIe affinity constraints, and DRANET opaque config (interface naming, IP assignment, routing).

Source: [github.com/openshift-psap/dra-rail-admission-webhook](https://github.com/openshift-psap/dra-rail-admission-webhook)

#### Build the webhook image (optional)

If you need to build a custom image, the repo includes a multi-stage Dockerfile:

```dockerfile
FROM golang:1.25 AS builder
WORKDIR /workspace
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -o webhook ./cmd/webhook/
RUN CGO_ENABLED=0 GOOS=linux go build -o reconciler ./cmd/reconciler/

FROM gcr.io/distroless/static:nonroot AS webhook
WORKDIR /
COPY --from=builder /workspace/webhook .
USER 65532:65532
ENTRYPOINT ["/webhook"]

FROM gcr.io/distroless/static:nonroot AS reconciler
WORKDIR /
COPY --from=builder /workspace/reconciler .
USER 65532:65532
ENTRYPOINT ["/reconciler"]
```

Build and push:

```bash
git clone https://github.com/openshift-psap/dra-rail-admission-webhook.git
cd dra-rail-admission-webhook

# Build webhook and reconciler images
docker build -t <your-registry>/dra-gpu-nic-webhook:latest --target webhook .
docker build -t <your-registry>/dra-gpu-nic-reconciler:latest --target reconciler .
docker push <your-registry>/dra-gpu-nic-webhook:latest
docker push <your-registry>/dra-gpu-nic-reconciler:latest
```

> **Note:** Pre-built images are available in the repo's deployment manifests. Building your own is only needed for custom modifications.

#### Clone and generate TLS certificates

The webhook requires a valid TLS certificate. The certificate's CN and SAN must match the webhook service name exactly:

```bash
git clone https://github.com/openshift-psap/dra-rail-admission-webhook.git
cd dra-rail-admission-webhook

NAMESPACE=dra-webhook-system

# Create namespace
oc create namespace $NAMESPACE --dry-run=client -o yaml | oc apply -f -

# Generate self-signed cert
mkdir -p certs
openssl req -x509 -newkey rsa:4096 \
  -keyout certs/tls.key -out certs/tls.crt \
  -days 365 -nodes \
  -subj "/CN=dra-gpu-nic-webhook.${NAMESPACE}.svc" \
  -addext "subjectAltName=DNS:dra-gpu-nic-webhook.${NAMESPACE}.svc,DNS:dra-gpu-nic-webhook.${NAMESPACE}.svc.cluster.local"

# Create TLS secret
oc create secret tls dra-gpu-nic-webhook-tls \
  --cert=certs/tls.crt --key=certs/tls.key \
  -n $NAMESPACE --dry-run=client -o yaml | oc apply -f -
```

#### Configure the network rails

Create a ConfigMap matching your cluster's RDMA network layout. One rail entry per NIC port:

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

Adjust `rails` to match your cluster's RDMA network subnets. One rail per NIC port — 8 entries for an 8-GPU-per-node cluster.

#### Deploy the webhook and reconciler

Apply all manifests, injecting the correct caBundle into the webhook configuration:

```bash
NAMESPACE=dra-webhook-system
CABUNDLE=$(cat certs/tls.crt | base64 | tr -d '\n')

# Apply everything except webhook-config
for f in deploy/base/rbac.yaml deploy/base/webhook-deployment.yaml \
         deploy/base/reconciler-deployment.yaml deploy/base/reconciler-pvc.yaml \
         deploy/base/webhook-service.yaml deploy/base/webhook-pdb.yaml; do
  oc apply -f "$f" -n $NAMESPACE 2>/dev/null || oc apply -f "$f"
done

# Apply webhook-config with correct caBundle
sed "s|caBundle:.*|caBundle: ${CABUNDLE}|g" deploy/base/webhook-config.yaml | oc apply -f -
```

> **Important:** Do NOT use `make deploy` — it applies the YAML with a stale caBundle. Always inject the caBundle from your generated certificate as shown above.

#### Label namespaces

The webhook only operates on namespaces you explicitly enable:

```bash
oc label namespace serveit dra.llm-d.io/webhook-enabled=true
```

#### Verify

```bash
oc get pods -n dra-webhook-system
```

Expected:

```
NAME                                     READY   STATUS    AGE
dra-gpu-nic-reconciler-xxxxx             1/1     Running   1m
dra-gpu-nic-webhook-xxxxx                1/1     Running   1m
dra-gpu-nic-webhook-yyyyy                1/1     Running   1m
```

### RDMA connectivity test

With the webhook, pods just request `dra.llm-d.io/gpu-nic-pair: "N"` in resources — the webhook automatically creates ResourceClaimTemplates with PCIe-affinitized GPU+NIC pairs.

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: dra-test-1
  namespace: serveit
spec:
  restartPolicy: Never
  containers:
  - name: test
    image: quay.io/dagray/rdma-tools:tiny
    command: ["sleep", "infinity"]
    resources:
      requests:
        cpu: "1"
        memory: "4Gi"
        dra.llm-d.io/gpu-nic-pair: "2"
      limits:
        cpu: "2"
        memory: "8Gi"
        dra.llm-d.io/gpu-nic-pair: "2"
```

The webhook intercepts this pod and:
1. Creates a ResourceClaimTemplate with 2 GPU+NIC pairs constrained by PCIe root
2. Injects `resourceClaims` into the pod spec with DRANET opaque config (interface name, IP, routes)
3. Strips the `dra.llm-d.io/gpu-nic-pair` resource
4. Pins the pod to a node where both pairs can be satisfied
5. Annotates the pod with `dra.llm-d.io/mutated: "true"`

Verify inside the pod:

```bash
oc exec -n serveit dra-test-1 -- ip -4 addr show | grep -E "net[0-9]|inet "
```

Expected output (2 GPU-NIC pairs requested):

```
net1: 10.1.0.12/16
net0: 10.0.0.12/16
```

```bash
oc exec -n serveit dra-test-1 -- nvidia-smi -L
```

Expected output:

```
GPU 0: NVIDIA H100 80GB HBM3 (UUID: GPU-...)
GPU 1: NVIDIA H100 80GB HBM3 (UUID: GPU-...)
```

```bash
# Verify the discovery script correctly detects DRA mode
oc exec -n serveit dra-test-1 -- bash -c 'source /scripts/discover_ib_hca.sh 2>/dev/null; echo "NETWORK_MODE=$NETWORK_MODE"; echo "UCX_NET_DEVICES=$UCX_NET_DEVICES"; echo "NCCL_IB_HCA=$NCCL_IB_HCA"'
```

Expected output:

```
NETWORK_MODE=dra-net
UCX_NET_DEVICES=net1,net0
NCCL_IB_HCA=mlx5_7:1,mlx5_8:1
```

```bash
# Verify webhook mutation
oc get pod dra-test-1 -o jsonpath='{.metadata.annotations.dra\.llm-d\.io/mutated}'
# Expected: true
```

#### Cross-node RDMA test

Deploy two pods requesting 8 GPU-NIC pairs each — the webhook will place them on separate nodes:

```bash
# Start server on pod 2
oc exec -n serveit dra-test-2 -- bash -c 'nohup ib_write_bw -d mlx5_8 --report_gbits -D 5 > /dev/null 2>&1 &'
sleep 3
# Get pod 2's net0 IP
NODE2_IP=$(oc exec -n serveit dra-test-2 -- ip -4 addr show net0 | grep inet | awk '{print $2}' | cut -d/ -f1)
# Run client from pod 1
oc exec -n serveit dra-test-1 -- ib_write_bw -d mlx5_8 --report_gbits -D 5 $NODE2_IP
```

Expected output (IBM Cloud VPC):

```
 #bytes     #iterations    BW peak[Gb/sec]    BW average[Gb/sec]   MsgRate[Mpps]
 65536      1011535          0.00               176.77               0.337170
```

~160-180 Gb/s per rail is the normal line rate for IBM Cloud VPC. Bare-metal with CX-7 400GbE NICs shows ~380-400 Gb/s.

#### Regenerating TLS certificates

If certificates expire or need rotation:

```bash
# Regenerate cert
openssl req -x509 -newkey rsa:4096 \
  -keyout certs/tls.key -out certs/tls.crt \
  -days 365 -nodes \
  -subj "/CN=dra-gpu-nic-webhook.dra-webhook-system.svc" \
  -addext "subjectAltName=DNS:dra-gpu-nic-webhook.dra-webhook-system.svc,DNS:dra-gpu-nic-webhook.dra-webhook-system.svc.cluster.local"

# Update secret
oc create secret tls dra-gpu-nic-webhook-tls \
  --cert=certs/tls.crt --key=certs/tls.key \
  -n dra-webhook-system --dry-run=client -o yaml | oc apply -f -

# Update caBundle
CABUNDLE=$(cat certs/tls.crt | base64 | tr -d '\n')
oc patch mutatingwebhookconfiguration dra-gpu-nic-webhook --type=json -p "[
  {\"op\":\"replace\",\"path\":\"/webhooks/0/clientConfig/caBundle\",\"value\":\"$CABUNDLE\"},
  {\"op\":\"replace\",\"path\":\"/webhooks/1/clientConfig/caBundle\",\"value\":\"$CABUNDLE\"}
]"

# Restart webhook
oc rollout restart deployment/dra-gpu-nic-webhook -n dra-webhook-system
```

---

## Option B: SR-IOV (Bare-Metal, L2 Flat Network)

**Use when:** You have bare-metal nodes with PFs (Physical Functions) and the RDMA network is a flat L2 fabric (all nodes on the same subnet per rail, IPs managed by the switch or manually).

### B1. Install the SR-IOV Network Operator

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: openshift-sriov-network-operator
  annotations:
    workload.openshift.io/allowed: management
---
apiVersion: operators.coreos.com/v1alpha1
kind: Subscription
metadata:
  name: sriov-network-operator
  namespace: openshift-sriov-network-operator
spec:
  channel: stable
  name: sriov-network-operator
  source: redhat-operators
  sourceNamespace: openshift-marketplace
  installPlanApproval: Automatic
```

### B2. Discover NIC PF names

Before creating policies, find PF names on each GPU node and verify they're consistent:

```bash
for node in $(oc get nodes -l nvidia.com/gpu.present=true -o name); do
  echo "=== $(echo $node | cut -d/ -f2) ==="
  oc debug $node -- chroot /host bash -c \
    "ls /sys/class/net/ | while read iface; do
       driver=\$(readlink /sys/class/net/\$iface/device/driver 2>/dev/null | xargs basename)
       if [ \"\$driver\" = 'mlx5_core' ]; then
         pci=\$(readlink /sys/class/net/\$iface/device | xargs basename)
         echo \"\$iface  \$pci\"
       fi
     done" 2>&1 | grep -v "Starting\|Removing\|To use"
done
```

Example output (Dell XE9680L with 10× BF3 SuperNICs):

```
=== dell-b200-1 ===
ens31f0np0  0000:ba:00.0
ens32f0np0  0000:51:00.0
ens34f0np0  0000:17:00.0
ens35f0np0  0000:3a:00.0
ens36f0np0  0000:3b:00.0
ens37f0np0  0000:ca:00.0
ens38f0np0  0000:cb:00.0
ens40f0np0  0000:d9:00.0
ens41f0np0  0000:da:00.0
ens42f0np0  0000:18:00.0
=== dell-b200-2 ===
ens31f0np0  0000:ba:00.0
ens32f0np0  0000:51:00.0
...  (should match node 1)
```

If NIC names differ between nodes, list all naming variants in `pfNames` (see example below).

### B3. Create SriovNetworkNodePolicy (one per PF/rail)

Each policy creates VFs from one PF. Create one policy per rail:

```yaml
apiVersion: sriovnetwork.openshift.io/v1
kind: SriovNetworkNodePolicy
metadata:
  name: policy-roce-rail0
  namespace: openshift-sriov-network-operator
spec:
  resourceName: rail0_roce
  nodeSelector:
    feature.node.kubernetes.io/pci-15b3.present: "true"
  priority: 90
  numVfs: 8
  mtu: 9000
  deviceType: netdevice
  isRdma: true
  nicSelector:
    vendor: "15b3"
    pfNames:
    - ens40f0np0
```

Repeat for each rail (rail1 through rail9), changing `name`, `resourceName`, `priority` (decrement by 1), and `pfNames`.

> **Warning:** Applying policies triggers node drain and reboot. The operator drains one node at a time.

### B4. Create SriovNetwork (NAD per rail)

Each SriovNetwork creates a NetworkAttachmentDefinition (NAD) in the target namespace:

```yaml
apiVersion: sriovnetwork.openshift.io/v1
kind: SriovNetwork
metadata:
  name: sriov-roce-net-0
  namespace: openshift-sriov-network-operator
  annotations:
    pf: ens40f0np0
    rail: "0"
spec:
  resourceName: rail0_roce
  networkNamespace: serveit
  ipam: |
    { "type": "host-local", "subnet": "192.168.0.0/24" }
```

### B5. Verify

```bash
oc get sriovnetworknodestates -n openshift-sriov-network-operator
oc get net-attach-def -n serveit
oc get nodes -l nvidia.com/gpu.present=true -o custom-columns='NAME:.metadata.name,RAIL0:.status.allocatable.openshift\.io/rail0_roce'
```

Expected output:

```
NAME           SYNC STATUS   AGE
dell-b200-1    Succeeded     10d
dell-b200-2    Succeeded     10d

NAME               AGE
sriov-roce-net-0   10d
sriov-roce-net-1   10d
...

NAME          RAIL0
dell-b200-1   8
dell-b200-2   8
```

---

## Option C: SR-IOV + nv-ipam + sbr (Bare-Metal, L3 Routed)

**Use when:** You have bare-metal nodes with PFs, but the RDMA network is L3 routed — nodes are on different subnets per rail, and you need automatic IP assignment with source-based routing so each NIC sends traffic through its own gateway.

**Tested on:** Dell XE9680L with 10× BF3 SuperNICs, 2 nodes

This option adds two components on top of SR-IOV:
- **nv-ipam** — assigns per-node IP ranges from CIDRPools (deployed via NicClusterPolicy, see Step 2b)
- **sbr-custom** — source-based routing plugin, ensures each VF's traffic uses the correct gateway

### C1. Install the SR-IOV Network Operator

Same as [Option B, Step B1](#b1-install-the-sr-iov-network-operator).

### C2. NicClusterPolicy with nv-ipam

Use the bare-metal NicClusterPolicy from Step 2b (the one with the `nvIpam` section). This deploys the nv-ipam IPAM daemon alongside MOFED.

### C3. Create SriovNetworkNodePolicy (one per PF/rail)

Same as [Option B, Step B3](#b3-create-sriovnetworknodepolicy-one-per-pfrail).

### C4. Create CIDRPools (one per rail)

Each CIDRPool defines the IP range for one rail. nv-ipam assigns a `/24` per node from the pool's `/16` CIDR, and each pod VF gets an IP from its node's `/24`.

```yaml
apiVersion: nv-ipam.nvidia.com/v1alpha1
kind: CIDRPool
metadata:
  name: cidr-rail0
  namespace: nvidia-network-operator
spec:
  cidr: 172.16.0.0/16
  perNodeNetworkPrefix: 24
  gatewayIndex: 254
  defaultGateway: true
  perNodeExclusions:
  - startIndex: 1
    endIndex: 1
  nodeSelector:
    nodeSelectorTerms:
    - matchExpressions:
      - key: kubernetes.io/hostname
        operator: In
        values:
        - node-1.example.com
        - node-2.example.com
  staticAllocations:
  - nodeName: node-1.example.com
    prefix: 172.16.1.0/24
  - nodeName: node-2.example.com
    prefix: 172.16.2.0/24
  routes:
  - dst: 172.16.0.0/16
  - dst: 172.16.0.0/12
```

Create one CIDRPool per rail. The CIDR scheme:

| Rail | CIDRPool | CIDR | Node 1 prefix | Node 2 prefix | Cross-rail supernet |
|------|----------|------|---------------|---------------|---------------------|
| 0 | cidr-rail0 | 172.16.0.0/16 | 172.16.1.0/24 | 172.16.2.0/24 | 172.16.0.0/12 |
| 1 | cidr-rail1 | 172.17.0.0/16 | 172.17.1.0/24 | 172.17.2.0/24 | 172.16.0.0/12 |
| 2 | cidr-rail2 | 172.18.0.0/16 | 172.18.1.0/24 | 172.18.2.0/24 | 172.16.0.0/12 |
| ... | ... | ... | ... | ... | ... |
| 9 | cidr-rail9 | 172.25.0.0/16 | 172.25.1.0/24 | 172.25.2.0/24 | 172.16.0.0/12 |

Key fields:
- `perNodeNetworkPrefix: 24` — each node gets a /24 from the /16
- `gatewayIndex: 254` — gateway is `.254` of each /24 (e.g., 172.16.1.254)
- `staticAllocations` — pin specific /24 ranges to specific nodes for predictable routing
- `routes` — the `/16` route for same-rail traffic and the `/12` supernet for cross-rail traffic

### C5. Create SriovNetwork with nv-ipam + sbr (one per rail)

```yaml
apiVersion: sriovnetwork.openshift.io/v1
kind: SriovNetwork
metadata:
  name: sriov-roce-net-0
  namespace: openshift-sriov-network-operator
  annotations:
    pf: ens40f0np0
    rail: "0"
spec:
  resourceName: rail0_roce
  networkNamespace: serveit
  ipam: '{"type": "nv-ipam", "poolName": "cidr-rail0", "poolType": "cidrpool"}'
  metaPlugins: '{"type": "sbr-custom", "addSourceHints": true}'
```

Key differences from Option B:
- **`ipam: nv-ipam`** — IPs come from the CIDRPool instead of static/host-local
- **`metaPlugins: sbr-custom`** — source-based routing ensures each VF's traffic uses its own gateway, preventing asymmetric routing on L3 networks

Repeat for each rail (rail0 through rail9), changing `name`, `resourceName`, `poolName`, and annotations.

### C6. Verify

```bash
# CIDRPools should show allocations per node
oc get cidrpools -n nvidia-network-operator

# NADs should exist in the target namespace
oc get net-attach-def -n serveit

# Test pod should get IPs from the correct per-node /24
oc exec <test-pod> -- ip -4 addr show
```

Expected output:

```
NAME         AGE
cidr-rail0   30d
cidr-rail1   30d
...

NAME               AGE
sriov-roce-net-0   30d
sriov-roce-net-1   30d
...

# Inside a test pod on dell-b200-1, rail0 VF gets 172.16.1.x:
net1: 172.16.1.2/24
net2: 172.17.1.3/24
...
```

Each VF gets an IP from its node's `/24` within the rail's `/16`. The `sbr-custom` plugin ensures traffic from `172.16.1.x` uses gateway `172.16.1.254` (not the pod's default route), and the `/12` supernet route enables cross-rail communication (e.g., rail0 pod reaching rail1 pod on another node).

---

## Part 3: Storage (Optional)

### LVM Storage (LVMS)

For clusters without a shared filesystem (no CephFS, no NFS), LVMS provides local NVMe-backed storage with dynamic provisioning. Each GPU node gets a volume group from its local NVMe drives.

#### Install the LVMS Operator

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: openshift-storage
---
apiVersion: operators.coreos.com/v1
kind: OperatorGroup
metadata:
  name: lvms-operator
  namespace: openshift-storage
spec:
  targetNamespaces:
  - openshift-storage
---
apiVersion: operators.coreos.com/v1alpha1
kind: Subscription
metadata:
  name: lvms-operator
  namespace: openshift-storage
spec:
  channel: stable-4.21
  name: lvms-operator
  source: redhat-operators
  sourceNamespace: openshift-marketplace
  installPlanApproval: Automatic
```

#### Create LVMCluster

Split NVMe drives between LVM (main storage) and NFS (shared model cache). Example: 6 drives for LVM, 2 for NFS.

```yaml
apiVersion: lvm.topolvm.io/v1alpha1
kind: LVMCluster
metadata:
  name: lvms-cluster
  namespace: openshift-storage
spec:
  storage:
    deviceClasses:
    - name: nvme
      default: true
      nodeSelector:
        nodeSelectorTerms:
        - matchExpressions:
          - key: node-role.kubernetes.io/gdr
            operator: Exists
      deviceSelector:
        paths:
        - /dev/nvme0n1
        - /dev/nvme1n1
        - /dev/nvme2n1
        - /dev/nvme3n1
        - /dev/nvme4n1
        - /dev/nvme5n1
    - name: nfs
      default: false
      nodeSelector:
        nodeSelectorTerms:
        - matchExpressions:
          - key: node-role.kubernetes.io/gdr
            operator: Exists
      deviceSelector:
        paths:
        - /dev/nvme6n1
        - /dev/nvme7n1
```

> **Note:** Do not use `thinPoolConfig` — it causes issues when VGs are recreated. Thick provisioning is simpler and more reliable.

#### Verify

```bash
oc get lvmcluster -n openshift-storage -o jsonpath='{.items[0].status.state}'
# Expected: Ready

oc get sc | grep lvms
# Expected:
# lvms-nvme   topolvm.io   Delete   WaitForFirstConsumer   true
# lvms-nfs    topolvm.io   Delete   WaitForFirstConsumer   true
```

### NFS Provisioner (per-node RWX storage)

The NFS Provisioner Operator creates per-node NFS servers backed by LVM volumes. This gives each node a ReadWriteMany storage class — useful for shared model caches where multiple pods on the same node need simultaneous access.

#### Install the NFS Provisioner Operator

Install from OperatorHub: **Operators → OperatorHub → search "NFS Provisioner Operator" → Install** (community-operators, alpha channel).

#### Create NFSProvisioner per node

Each NFSProvisioner creates an NFS server pod pinned to one GPU node, backed by `lvms-nfs`. All pods on that node can mount PVCs from it with ReadWriteMany.

```yaml
apiVersion: cache.jhouse.com/v1alpha1
kind: NFSProvisioner
metadata:
  name: nfs-node-665m9
  namespace: openshift-operators
spec:
  scForNFS: "nfs-665m9"
  scForNFSPvc: "lvms-nfs"
  storageSize: "13000Gi"
  nodeSelector:
    kubernetes.io/hostname: rits-llmd-rhoai-lrrzc-worker-h100-3-gdr-665m9
```

Repeat for each GPU node, changing `name`, `scForNFS`, and `nodeSelector.kubernetes.io/hostname`.

#### Verify

```bash
oc get sc | grep nfs
# Expected: one nfs-{node} storage class per GPU node, all with Immediate binding

oc get pods -n openshift-operators | grep nfs
# Expected: one nfs-provisioner pod per GPU node
```

---

## Verification Checklist

```bash
# Common
oc get nodes -l feature.node.kubernetes.io/pci-10de.present=true        # NFD
oc get nicclusterpolicy -o jsonpath='{.items[0].status.state}'          # MOFED ready
oc get clusterpolicy -o jsonpath='{.items[0].status.state}'             # GPU ready
oc get crd leaderworkersets.leaderworkerset.x-k8s.io                   # LWS

# Option A (DRA)
oc get pods -n kube-system -l app=dranet --no-headers | wc -l          # DRANET
oc get pods -n nvidia-dra-driver-gpu --no-headers                       # GPU DRA
oc get pods -n dra-webhook-system --no-headers                          # DRA Webhook
oc get deviceclass dranet gpu.nvidia.com                                # DeviceClasses
oc get resourceslices --no-headers | wc -l                              # ResourceSlices

# Option B/C (SR-IOV)
oc get pods -n openshift-sriov-network-operator --no-headers            # SR-IOV operator
oc get sriovnetworknodestates -n openshift-sriov-network-operator       # Node states
oc get net-attach-def -n serveit                                        # NADs

# Option C only (nv-ipam)
oc get cidrpools -n nvidia-network-operator                             # CIDRPools
```

---

## Component Versions (tested)

| Component | Version | Notes |
|-----------|---------|-------|
| OpenShift | 4.21.18 | Kubernetes 1.34.8 |
| NFD Operator | 4.19.0 | |
| NVIDIA Network Operator | 26.1.0 | DOCA/MOFED doca3.3.0-26.01-1.0.0.0-0 |
| NVIDIA GPU Operator | 26.3.0 | CUDA Driver 580.105, gdrcopy enabled |
| DRANET | v1.2.0 | |
| NVIDIA DRA GPU Driver | 25.12.0 | |
| DRA GPU-NIC Admission Webhook | latest | [openshift-psap/dra-rail-admission-webhook](https://github.com/openshift-psap/dra-rail-admission-webhook) |
| Gateway / Istio | OpenShift Service Mesh 3.3.4 | Uses `data-science-gateway-class` (OpenShift) or `istio` (upstream). llm-d upstream ships Istio 1.28.x with `ENABLE_GATEWAY_API_INFERENCE_EXTENSION=true` |
| SR-IOV Network Operator | 4.21.0 | |
| LWS Operator | 0.8.0 | Upstream, installed via Helm (Red Hat build v1.0.0 lacks `volumeClaimTemplates`) |
| LVM Storage (LVMS) | 4.21.0 | |
| NFS Provisioner Operator | 0.0.9 | |
