# Local NVMe HostPath Storage Setup

Fast local NVMe storage for model caching using the Local Storage Operator (LSO) + HostPath Provisioner (HPP). Reduces model loading from ~30 min (NFS) to ~2 min (direct NVMe).

## Architecture

```
LSO discovers NVMe disks
  -> creates PVs with 'local-nvme' StorageClass
    -> HPP creates backing PVC per node from 'local-nvme'
      -> HPP serves user PVCs from 'hostpath-nvme' StorageClass
        -> Pods get directories on raw NVMe
```

## Prerequisites

- OpenShift 4.14+
- Local Storage Operator installed (via OperatorHub)
- cert-manager installed (required by HPP)
- GPU nodes with available NVMe disks

## Step 1: Identify Available NVMe Disks

### Option A: Use Local Storage Operator Discovery

Create a `LocalVolumeDiscovery` to scan GPU nodes:

```yaml
apiVersion: local.storage.openshift.io/v1alpha1
kind: LocalVolumeDiscovery
metadata:
  name: auto-discover-devices
  namespace: openshift-local-storage
spec:
  nodeSelector:
    nodeSelectorTerms:
    - matchExpressions:
      - key: node.kubernetes.io/instance-type
        operator: In
        values:
        - <your-gpu-instance-type>    # e.g. gx3d-160x1792x8h200
```

Check results:

```bash
kubectl get localvolumediscoveryresults -n openshift-local-storage -o yaml | \
  grep -B2 -A5 "nvme" | grep -E "path:|state:|size:"
```

Look for disks with `state: Available`. Disks in existing VGs show `state: NotAvailable`.

### Option B: Manual Check

SSH or debug into each GPU node:

```bash
kubectl debug node/<node-name> -it --image=busybox -- sh -c "
  cat /host/proc/partitions | grep nvme
"
```

Check which disks are in use:

```bash
kubectl exec -n openshift-lvm-storage <vg-manager-pod> -- sh -c "
  nsenter -t 1 -m -u -i -n -p -- pvs --units g
"
```

Disks NOT listed in `pvs` output are free.

### Get Device IDs

For the `LocalVolume` CR, use device IDs (stable across reboots):

```bash
kubectl debug node/<node-name> -it --image=busybox -- sh -c "
  ls -la /host/dev/disk/by-id/ | grep nvme
"
```

Or from LSO discovery results:

```bash
kubectl get localvolumediscoveryresults -n openshift-local-storage -o yaml | \
  grep "deviceID.*nvme"
```

## Step 2: Create LocalVolume (LSO)

Pick one available disk per GPU node. Use the **same device path** if it exists on all nodes (e.g., `/dev/nvme3n1`):

```yaml
apiVersion: local.storage.openshift.io/v1
kind: LocalVolume
metadata:
  name: local-nvme-model-cache
  namespace: openshift-local-storage
spec:
  nodeSelector:
    nodeSelectorTerms:
    - matchExpressions:
      - key: node.kubernetes.io/instance-type
        operator: In
        values:
        - <your-gpu-instance-type>
  storageClassDevices:
  - storageClassName: local-nvme
    devicePaths:
    - /dev/nvme3n1                    # Same disk path on all GPU nodes
    volumeMode: Filesystem
    fsType: xfs
```

Verify PVs are created (one per GPU node):

```bash
kubectl get pv | grep local-nvme
```

Expected: one PV per GPU node, 7+ Ti each, `Available` status.

## Step 3: Install HostPath Provisioner Operator

```bash
# Namespace
kubectl apply -f https://github.com/kubevirt/hostpath-provisioner-operator/releases/latest/download/namespace.yaml

# Operator (install in hostpath-provisioner namespace!)
kubectl apply -n hostpath-provisioner -f https://github.com/kubevirt/hostpath-provisioner-operator/releases/latest/download/operator.yaml

# Webhook
kubectl apply -n hostpath-provisioner -f https://github.com/kubevirt/hostpath-provisioner-operator/releases/latest/download/webhook.yaml
```

Verify:

```bash
kubectl get pods -n hostpath-provisioner
# Should show: hostpath-provisioner-operator-... Running
```

## Step 4: Create HostPathProvisioner CR

```yaml
apiVersion: hostpathprovisioner.kubevirt.io/v1beta1
kind: HostPathProvisioner
metadata:
  name: hostpath-provisioner
spec:
  imagePullPolicy: IfNotPresent
  storagePools:
  - name: local-nvme
    pvcTemplate:
      accessModes:
      - ReadWriteOnce
      resources:
        requests:
          storage: 7000Gi           # Size of NVMe disk
      storageClassName: local-nvme  # LSO StorageClass from Step 2
    path: /var/hpvolumes/local-nvme
  workload:
    nodeSelector:
      node.kubernetes.io/instance-type: <your-gpu-instance-type>
```

This creates:
- A backing PVC on each GPU node using the LSO `local-nvme` StorageClass
- CSI driver DaemonSet pods on GPU nodes
- Pool mount pods that mount the NVMe-backed PVC to the hostpath directory

Verify:

```bash
# Backing PVCs (one per node, bound to LSO PVs)
kubectl get pvc -n hostpath-provisioner

# CSI and pool pods
kubectl get pods -n hostpath-provisioner -o wide
```

## Step 5: Create StorageClass for Users

```yaml
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: hostpath-nvme
provisioner: kubevirt.io.hostpath-provisioner
reclaimPolicy: Retain
volumeBindingMode: WaitForFirstConsumer
parameters:
  storagePool: local-nvme
```

Key settings:
- `reclaimPolicy: Retain` — PVCs survive pod deletion, model cache persists
- `volumeBindingMode: WaitForFirstConsumer` — PVC binds to the node where the pod is scheduled

## Step 6: Use in ServeIt Studio

Select `hostpath-nvme` as the storage class in the ServeIt Studio UI (Step 6: Storage Configuration). The system will:

1. Create PVCs using `hostpath-nvme` StorageClass
2. HPP provisions directories on the NVMe-backed storage pool
3. `WaitForFirstConsumer` ensures PVC binds to the correct node
4. Model downloads directly to NVMe (~2 min for large models)
5. PVCs persist across test restarts (Retain policy)

## Troubleshooting

### LSO PVs not created

```bash
kubectl get pods -n openshift-local-storage    # diskmaker-manager pods running?
kubectl logs -n openshift-local-storage -l app=diskmaker-manager
```

### HPP backing PVC stuck Pending

```bash
kubectl get pvc -n hostpath-provisioner         # Check StorageClass matches
kubectl get pv | grep local-nvme                # PVs available?
```

### Old PVC stuck in Terminating

Check if a pod still references it:

```bash
kubectl get pods -n hostpath-provisioner -o json | python3 -c "
import json, sys
for pod in json.load(sys.stdin)['items']:
    for vol in pod['spec'].get('volumes', []):
        pvc = vol.get('persistentVolumeClaim', {}).get('claimName', '')
        if pvc: print(f\"{pod['metadata']['name']}: {pvc}\")
"
```

Delete the stale pod, HPP will recreate with new PVC.

### Disk shows NotAvailable in LSO Discovery

Disk is already in an LVM VG or has a filesystem. Check:

```bash
kubectl exec -n openshift-lvm-storage <vg-manager-pod> -- sh -c "
  nsenter -t 1 -m -u -i -n -p -- pvs
"
```

To free a disk from an LVM VG (CAUTION: destroys data):

```bash
# Remove LV, VG, then wipe PV labels
lvremove -f <vg-name>/<lv-name>
vgremove -f <vg-name>
pvremove /dev/nvmeXn1
```

## Cleanup

To remove the entire setup:

```bash
# Delete StorageClass
kubectl delete sc hostpath-nvme

# Delete HostPathProvisioner CR (deletes backing PVCs and CSI pods)
kubectl delete hostpathprovisioner hostpath-provisioner

# Delete HPP operator
kubectl delete -n hostpath-provisioner -f https://github.com/kubevirt/hostpath-provisioner-operator/releases/latest/download/operator.yaml
kubectl delete -n hostpath-provisioner -f https://github.com/kubevirt/hostpath-provisioner-operator/releases/latest/download/webhook.yaml
kubectl delete ns hostpath-provisioner

# Delete LocalVolume (releases disks)
kubectl delete localvolume local-nvme-model-cache -n openshift-local-storage

# Delete LocalVolumeDiscovery
kubectl delete localvolumediscovery auto-discover-devices -n openshift-local-storage
```
