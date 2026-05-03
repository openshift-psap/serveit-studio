# Networking Module

**Modular network resource creation for InfeRecipe**

## Overview

The networking module provides a modular system for creating Kubernetes network resources:

- **NAD** (NetworkAttachmentDefinition) - Multus CNI approach
- **DRA** (Dynamic Resource Allocation) - DRANET approach with GPU+NIC affinity

## Architecture

```
core/networking/
├── __init__.py       # Package exports
├── base.py          # Abstract interfaces and data models (250 lines)
├── nad.py           # NAD creator for Multus CNI (280 lines)
├── dra.py           # DRA creator for DRANET (350 lines)
├── factory.py       # Network type selection (160 lines)
└── README.md        # This file
```

All files follow **modular-code** guidelines (150-500 lines = optimal for AI tooling).

## Network Types

### NAD (NetworkAttachmentDefinition)

**Use case**: Simple multi-NIC attachment via Multus CNI

**How it works**:
- Creates NAD resources with CNI plugins (host-device, IPAM, tuning)
- Pods reference NADs via annotation: `k8s.v1.cni.cncf.io/networks`
- No GPU+NIC affinity guarantee

**Example**:
```yaml
apiVersion: k8s.cni.cncf.io/v1
kind: NetworkAttachmentDefinition
metadata:
  name: ibm-rdma-port-1
spec:
  config: |
    {
      "cniVersion": "0.3.1",
      "plugins": [
        {"type": "host-device", "device": "enp233s0", "isRdma": true},
        {"type": "sbr-custom", "gateway": "10.0.0.1"},
        {"type": "tuning", "mtu": 9000}
      ]
    }
```

### DRA (Dynamic Resource Allocation)

**Use case**: DRANET with GPU+NIC pairing and multi-rail RDMA

**How it works**:
- Creates ResourceClaimTemplates with device constraints
- Ensures GPU and NIC are on same PCIe root
- Supports multi-rail (8x GPU+NIC for H100)
- Pods reference via `resourceClaims`

**Example**:
```yaml
apiVersion: resource.k8s.io/v1
kind: ResourceClaimTemplate
metadata:
  name: gpu-nic-rail0-template
spec:
  spec:
    devices:
      constraints:
        - matchAttribute: resource.kubernetes.io/pcieRoot
          requests: [gpu, nic]
      requests:
        - name: gpu
          exactly: {count: 1, deviceClassName: gpu.nvidia.com}
        - name: nic
          exactly: {count: 1, deviceClassName: dranet}
```

## Usage

### Basic NAD Creation

```python
from core.networking import NetworkFactory, NetworkConfig, NetworkType, RDMAType

# Configure NAD network
config = NetworkConfig(
    network_type=NetworkType.NAD,
    rdma_type=RDMAType.VIRTIO_ROCE,
    rdma_enabled=True,
    device_name="enp233s0",
    mtu=9000,
    ipam_type="dhcp",
    gateway="10.0.0.1"
)

# Create network creator
creator = NetworkFactory.create_network_creator(config)

# Generate NAD resources (8 ports)
resources = creator.create_network_resources(
    namespace="llm-d",
    base_name="ibm-rdma",
    num_resources=8
)

# Get pod annotations
annotations = creator.get_pod_annotations([r.name for r in resources])
# Returns: {"k8s.v1.cni.cncf.io/networks": "ibm-rdma-port-1,ibm-rdma-port-2,..."}
```

### Basic DRA Creation

```python
from core.networking import NetworkFactory, NetworkConfig, NetworkType, RDMAType

# Configure DRA network (DRANET)
config = NetworkConfig(
    network_type=NetworkType.DRA,
    rdma_type=RDMAType.VIRTIO_ROCE,
    rdma_enabled=True,
    num_rails=8,
    ip_prefix="10.0.",
    pcie_affinity=True,
    mtu=9000,
    gateway="10.0.0.1"
)

# Create network creator
creator = NetworkFactory.create_network_creator(config, provider_type="ibm_cloud")

# Generate ResourceClaimTemplates (8 rails)
resources = creator.create_network_resources(
    namespace="llm-d",
    base_name="gpu-nic"
)

# Get pod resourceClaims
claims = creator.get_pod_resource_claims([r.name for r in resources])
# Returns: [{"name": "gpu-nic-rail0", "resourceClaimTemplateName": "gpu-nic-rail0-template"}, ...]

# Get container claims
container_claims = creator.get_container_resource_claims(num_rails=8)
# Returns: [{"name": "gpu-nic-rail0"}, {"name": "gpu-nic-rail1"}, ...]
```

### Auto-Detection from Provider

```python
from core.providers import ProviderRegistry
from core.networking import NetworkFactory

# Detect provider
provider = ProviderRegistry.detect_provider(kubectl_runner=kubectl)

# Create network from provider config
network_creator = NetworkFactory.from_provider(
    provider,
    num_rails=8  # Override if needed
)

# Generate resources
resources = network_creator.create_network_resources(
    namespace="llm-d",
    base_name="rdma-network"
)
```

## Integration with Providers

Each provider specifies network configuration in its profile YAML:

**IBM Cloud + DRANET**:
```yaml
network:
  rdma_type: virtio-roce
  rdma_device_plugin: nvidia.com/roce
  requires_cni: true
  cni_type: multus
  max_bandwidth_gbps: 400
```

The networking module reads this and creates the appropriate resources (NAD or DRA).

## Network Resource Output

All creators return `List[NetworkResource]` objects:

```python
@dataclass
class NetworkResource:
    resource_type: NetworkType  # NAD or DRA
    api_version: str
    kind: str
    metadata: Dict[str, Any]
    spec: Dict[str, Any]
    name: str
    namespace: str
    description: str

    def to_dict(self) -> Dict[str, Any]:
        # Convert to K8s YAML
```

## File Size Compliance

Following **modular-code** principles:

| File | Lines | Status |
|------|-------|--------|
| `base.py` | ~250 | ✅ Optimal (150-500) |
| `nad.py` | ~280 | ✅ Optimal (150-500) |
| `dra.py` | ~350 | ✅ Optimal (150-500) |
| `factory.py` | ~160 | ✅ Optimal (150-500) |

Each file is self-contained and optimally sized for AI code editors.

## Testing

```python
# Test NAD creation
python3 -c "
from core.networking import NADNetworkCreator, NetworkConfig, RDMAType

config = NetworkConfig(rdma_type=RDMAType.VIRTIO_ROCE)
creator = NADNetworkCreator(config)
resources = creator.create_network_resources('test-ns', 'test-net', 2)
print(f'Created {len(resources)} NAD resources')
for r in resources:
    print(f'  - {r.name}')
"

# Test DRA creation
python3 -c "
from core.networking import DRANetworkCreator, NetworkConfig, RDMAType

config = NetworkConfig(rdma_type=RDMAType.VIRTIO_ROCE, num_rails=4)
creator = DRANetworkCreator(config)
resources = creator.create_network_resources('test-ns', 'gpu-nic', 4)
print(f'Created {len(resources)} DRA resources')
for r in resources:
    print(f'  - {r.name}')
"
```

## Future Enhancements

1. **Network validation**: Verify NAD/DRA resources work before deployment
2. **Bandwidth testing**: Measure RDMA bandwidth between pods
3. **Auto-discovery**: Detect available NICs and auto-configure
4. **Templates**: Pre-built network configs for common scenarios

## References

- [Multus CNI](https://github.com/k8snetworkplumbingwg/multus-cni)
- [Kubernetes DRA](https://kubernetes.io/docs/concepts/scheduling-eviction/dynamic-resource-allocation/)
- [IBM Cloud DRANET Setup](https://github.com/openshift-psap/ibmcloud-roce-dra-net-setup)
