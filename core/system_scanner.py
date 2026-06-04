"""
ServeIt Studio System Scanner

Scans Kubernetes cluster for available resources (GPUs, RDMA NICs, nodes).
"""

import json
import logging
from typing import Dict, List, Optional
from dataclasses import dataclass, asdict
from core.k8s_utils import KubectlRunner
from core.utils import next_power_of_2
from core.cloud_constraints import CloudProvider, CloudConstraints

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


@dataclass
class NetworkInterface:
    """Network interface information."""
    name: str
    type: str  # ethernet, infiniband, roce
    vendor: str  # Mellanox, Intel, Broadcom, etc.
    model: str  # ConnectX-5, ConnectX-6, mlx5, etc.
    count: int  # Physical NIC count (not device plugin pool size)
    pool_size: int = 0  # Device plugin pool size from allocatable (e.g., rdmaHcaMax)
    speed_gbps: float = 0.0  # Per-port link speed in Gbps (0 = unknown)

@dataclass
class NodeResources:
    """Resources available on a single node."""
    name: str
    gpus: int
    gpu_type: str  # e.g., "nvidia.com/gpu", "amd.com/gpu"
    gpu_vendor: str  # e.g., "NVIDIA", "AMD", "Intel"
    gpu_model: str  # e.g., "H100-80GB-HBM3", "A100-SXM4-80GB"
    gpu_memory_mb: int  # Total GPU VRAM in MB
    cpu_cores: int  # Total CPU cores
    cpu_model: str  # CPU model name
    memory_gb: int  # Total RAM in GB
    has_rdma: bool
    rdma_devices: List[str]
    network_interfaces: List[NetworkInterface]
    labels: Dict[str, str]
    status: str = 'Unknown'  # Ready, NotReady, Unknown


@dataclass
class StorageClassInfo:
    """Storage class information."""
    name: str
    provisioner: str
    reclaim_policy: str
    volume_binding_mode: str
    allow_volume_expansion: bool

@dataclass
class ClusterResources:
    """Aggregated cluster resources."""
    total_gpus: int
    gpus_per_node: Dict[str, int]
    max_gpus_per_node: int
    min_gpus_per_node: int
    total_gpu_memory_gb: int  # Total GPU VRAM across cluster in GB
    gpu_memory_per_gpu_mb: int  # Average VRAM per GPU in MB
    total_cpu_cores: int
    total_memory_gb: int
    node_count: int
    gpu_node_count: int  # Number of nodes with GPUs
    has_rdma: bool
    rdma_capable_nodes: int
    gpu_type: str
    gpu_vendor: str  # Most common GPU vendor
    gpu_model: str  # Most common GPU model
    total_network_interfaces: int
    network_interfaces_by_type: Dict[str, int]  # e.g., {'infiniband': 3, 'roce': 2, 'ethernet': 4}
    network_interfaces_by_vendor: Dict[str, int]  # e.g., {'mellanox': 5, 'intel': 2}
    storage_classes: List[StorageClassInfo]
    nodes: List[NodeResources]
    cloud_provider: CloudProvider = CloudProvider.UNKNOWN  # Detected cloud provider
    cpu_model: str = 'unknown'  # CPU model name or vendor
    host_model: str = 'unknown'  # Host/instance type
    nic_models: List[str] = None  # List of NIC model names
    nic_speeds: Dict[str, float] = None  # NIC name -> speed in Gbps

    def get_max_tp(self) -> int:
        """Get maximum tensor parallelism value based on max GPUs per node."""
        return self.max_gpus_per_node

    def get_tp_options(self) -> List[int]:
        """Get available TP options (powers of 2 up to max_gpus_per_node)."""
        options = []
        tp = 1
        while tp <= self.max_gpus_per_node:
            options.append(tp)
            tp *= 2
        return options

    def estimate_model_gpu_requirement(self, model_size_gb: float, dtype: str = 'fp16',
                                        is_moe: bool = False) -> int:
        """
        Estimate minimum number of GPUs needed to load a model.

        Args:
            model_size_gb: Model size in GB (total weight size)
            dtype: Data type (fp16, fp8, int8, int4)
            is_moe: If True, use lower overhead (MoE activations are sparse)

        Returns:
            Minimum number of GPUs required (power of 2)
        """
        # MoE models have sparse activations — only top_k experts active per
        # token, so activation memory is much lower than dense equivalent.
        overhead_factor = 1.15 if is_moe else 1.4
        required_memory_gb = model_size_gb * overhead_factor

        gpu_memory_gb = self.gpu_memory_per_gpu_mb / 1024
        min_gpus = int(required_memory_gb / gpu_memory_gb) + 1

        # TP must be power of 2
        tp = next_power_of_2(min_gpus)

        return min(tp, self.max_gpus_per_node)


class SystemScanner:
    """Scans Kubernetes cluster for available resources."""

    def __init__(self, namespace: str = 'serveit', kubeconfig: Optional[str] = None):
        """
        Initialize SystemScanner.

        Args:
            namespace: Kubernetes namespace to scan
            kubeconfig: Path to kubeconfig file (uses default if None)
        """
        self.namespace = namespace
        self.kubectl = KubectlRunner(kubeconfig=kubeconfig, namespace=namespace)

    def _get_gpu_count(self, node_data: Dict) -> tuple[int, str, str, str]:
        """
        Extract GPU count, type, vendor, and model from node data.

        Args:
            node_data: Node data from kubectl

        Returns:
            Tuple of (gpu_count, gpu_type, gpu_vendor, gpu_model)
        """
        allocatable = node_data.get('status', {}).get('allocatable', {})
        labels = node_data.get('metadata', {}).get('labels', {})

        gpu_vendor = 'unknown'
        gpu_model = 'unknown'

        # Check for NVIDIA GPUs
        if 'nvidia.com/gpu' in allocatable:
            gpu_count = int(allocatable['nvidia.com/gpu'])
            gpu_vendor = 'NVIDIA'

            # Try to get GPU model from labels
            gpu_product = labels.get('nvidia.com/gpu.product', '')
            if gpu_product:
                gpu_model = gpu_product.replace('-', ' ')
                # Strip vendor prefix to avoid "NVIDIA NVIDIA H200" in display
                for prefix in ['NVIDIA ', 'AMD ', 'Intel ']:
                    if gpu_model.startswith(prefix):
                        gpu_model = gpu_model[len(prefix):]
                        break
            else:
                # Try to detect from other labels
                for label_key, label_value in labels.items():
                    if 'gpu' in label_key.lower() and any(x in label_value.upper() for x in ['H100', 'A100', 'V100', 'T4', 'L4', 'L40', 'A10']):
                        gpu_model = label_value
                        break

            return gpu_count, 'nvidia.com/gpu', gpu_vendor, gpu_model

        # Check for AMD GPUs
        if 'amd.com/gpu' in allocatable:
            gpu_count = int(allocatable['amd.com/gpu'])
            gpu_vendor = 'AMD'

            # Try to get AMD GPU model
            for label_key, label_value in labels.items():
                if 'gpu' in label_key.lower() or 'amd' in label_key.lower():
                    if any(x in label_value.upper() for x in ['MI300', 'MI250', 'MI210', 'MI100']):
                        gpu_model = label_value
                        break

            return gpu_count, 'amd.com/gpu', gpu_vendor, gpu_model

        # Check for Intel GPUs
        if 'gpu.intel.com/i915' in allocatable:
            gpu_count = int(allocatable['gpu.intel.com/i915'])
            gpu_vendor = 'Intel'

            # Try to get Intel GPU model
            for label_key, label_value in labels.items():
                if 'gpu' in label_key.lower() or 'intel' in label_key.lower():
                    if any(x in label_value.upper() for x in ['ATS', 'PVC', 'FLEX']):
                        gpu_model = label_value
                        break

            return gpu_count, 'gpu.intel.com/i915', gpu_vendor, gpu_model

        return 0, 'unknown', 'unknown', 'unknown'

    def _get_cpu_model(self, node_data: Dict) -> str:
        """
        Extract CPU model from node labels.

        Args:
            node_data: Node data from kubectl

        Returns:
            CPU model string
        """
        labels = node_data.get('metadata', {}).get('labels', {})

        # CoreWeave-specific CPU labels
        cw_cpu_family = labels.get('cpu.coreweave.cloud/family', '').strip()
        cw_cpu_cores = labels.get('cpu.coreweave.cloud/cores', '').strip()
        if cw_cpu_family:
            cpu_name = cw_cpu_family.capitalize()
            if cw_cpu_cores:
                return f'Intel {cpu_name} ({cw_cpu_cores} cores)'
            return f'Intel {cpu_name}'

        # Standard NFD labels
        cpu_vendor = labels.get('feature.node.kubernetes.io/cpu-model.vendor_id', '').strip()
        cpu_family = labels.get('feature.node.kubernetes.io/cpu-model.family', '').strip()
        cpu_model_id = labels.get('feature.node.kubernetes.io/cpu-model.id', '').strip()

        if not cpu_vendor:
            return 'unknown'

        if cpu_vendor == 'Intel' and cpu_family == '6':
            if cpu_model_id == '143':
                return 'Intel Xeon (Sapphire Rapids)'
            elif cpu_model_id in ['106', '108']:
                return 'Intel Xeon (Ice Lake)'
            elif cpu_model_id in ['85', '79']:
                return 'Intel Xeon (Cascade Lake / Skylake)'
            else:
                return f'Intel Xeon (Family {cpu_family}, Model {cpu_model_id})'
        elif cpu_vendor == 'AMD':
            return f'AMD (Family {cpu_family}, Model {cpu_model_id})'

        return cpu_vendor

    def _check_rdma_support(self, node_data: Dict) -> tuple[bool, List[str]]:
        """
        Check if node has RDMA support.

        Args:
            node_data: Node data from kubectl

        Returns:
            Tuple of (has_rdma, rdma_device_list)
        """
        allocatable = node_data.get('status', {}).get('allocatable', {})
        rdma_devices = []

        # Check for RDMA resources (rdma/ib, nvidia.com/roce, etc.)
        for key in allocatable.keys():
            key_lower = key.lower()
            if 'rdma' in key_lower or 'roce' in key_lower or ('ib' in key_lower and '/' in key):
                rdma_devices.append(key)

        # Also check node labels for RDMA capability
        if not rdma_devices:
            labels = node_data.get('metadata', {}).get('labels', {})
            for lk, lv in labels.items():
                if 'rdma.capable' in lk and lv == 'true':
                    rdma_devices.append('rdma-capable (label)')
                    break

        return len(rdma_devices) > 0, rdma_devices

    def _detect_network_interfaces(self, node_data: Dict) -> List[NetworkInterface]:
        """
        Detect network interfaces from node allocatable resources.

        Args:
            node_data: Node data from kubectl

        Returns:
            List of NetworkInterface objects
        """
        allocatable = node_data.get('status', {}).get('allocatable', {})
        labels = node_data.get('metadata', {}).get('labels', {})
        interfaces = []

        # Check allocatable resources for network devices
        for key, value in allocatable.items():
            interface_name = None
            interface_type = 'unknown'
            vendor = 'unknown'
            model = 'unknown'

            # Detect InfiniBand/RoCE devices
            if 'rdma' in key.lower() or 'ib' in key.lower() or 'infiniband' in key.lower() or 'roce' in key.lower():
                interface_name = key
                if 'roce' in key.lower():
                    interface_type = 'RoCE'
                elif 'ib' in key.lower() or 'infiniband' in key.lower():
                    interface_type = 'InfiniBand'
                else:
                    interface_type = 'RDMA'

                # Detect vendor and model from key name
                key_lower = key.lower()
                if 'mlx5' in key_lower:
                    vendor = 'Mellanox'
                    model = 'ConnectX-5/6/7 (mlx5)'
                elif 'mlx4' in key_lower:
                    vendor = 'Mellanox'
                    model = 'ConnectX-3/4 (mlx4)'
                elif 'mlx' in key_lower or 'mellanox' in key_lower:
                    vendor = 'Mellanox'
                    model = 'ConnectX Series'
                elif 'nvidia.com/roce' in key_lower:
                    vendor = 'Mellanox'
                    model = 'VirtIO RoCE'
                elif 'intel' in key_lower:
                    vendor = 'Intel'
                    if 'e810' in key_lower:
                        model = 'E810'
                    elif 'x722' in key_lower:
                        model = 'X722'
                elif 'broadcom' in key_lower or 'bcm' in key_lower:
                    vendor = 'Broadcom'

                # Check labels for additional vendor/model info
                for label_key, label_value in labels.items():
                    if 'network' in label_key.lower() or 'nic' in label_key.lower() or 'rdma' in label_key.lower():
                        if 'mellanox' in label_value.lower():
                            vendor = 'Mellanox'
                            if 'connectx-6' in label_value.lower():
                                model = 'ConnectX-6'
                            elif 'connectx-7' in label_value.lower():
                                model = 'ConnectX-7'
                            elif 'connectx-5' in label_value.lower():
                                model = 'ConnectX-5'
                        elif 'intel' in label_value.lower():
                            vendor = 'Intel'

                # For generic "rdma/ib" resources (e.g., CoreWeave), infer from
                # RDMA capability labels and GPU family — H100/H200 nodes
                # typically have Mellanox ConnectX-7 NICs
                if vendor == 'unknown' and key == 'rdma/ib':
                    rdma_available = labels.get('feature.node.kubernetes.io/custom-rdma.available', '')
                    gpu_family = labels.get('nvidia.com/gpu.family', '')
                    if rdma_available == 'true' or gpu_family in ('hopper', 'blackwell'):
                        vendor = 'NVIDIA/Mellanox'
                        model = 'ConnectX-7'

            # Detect Ethernet devices
            elif 'net' in key.lower() and 'nvidia.com/gpu' not in key:
                interface_name = key
                interface_type = 'Ethernet'

                # Check labels for vendor info
                for label_key, label_value in labels.items():
                    if 'network' in label_key.lower() or 'nic' in label_key.lower():
                        if 'mellanox' in label_value.lower() or 'mlx' in label_value.lower():
                            vendor = 'Mellanox'
                        elif 'intel' in label_value.lower():
                            vendor = 'Intel'
                        elif 'broadcom' in label_value.lower():
                            vendor = 'Broadcom'

            if interface_name:
                # Parse pool size from allocatable (device plugin virtual count, NOT physical NICs)
                try:
                    pool_size = int(value)
                except (ValueError, TypeError):
                    pool_size = 1

                # Detect actual physical NIC count
                physical_count = self._count_physical_nics(node_data, interface_name, interface_type)

                # Estimate speed for InfiniBand based on GPU family
                speed = 0.0
                if interface_type == 'InfiniBand' and model == 'ConnectX-7':
                    gpu_family = labels.get('nvidia.com/gpu.family', '')
                    if gpu_family in ('hopper', 'blackwell'):
                        speed = 400.0  # NDR 400Gbps per port

                interfaces.append(NetworkInterface(
                    name=interface_name,
                    type=interface_type,
                    vendor=vendor,
                    model=model,
                    count=physical_count,
                    pool_size=pool_size,
                    speed_gbps=speed
                ))

        return interfaces

    def _count_physical_nics(self, node_data: Dict, resource_name: str, interface_type: str) -> int:
        """
        Detect the number of physical NICs behind an RDMA device plugin resource.

        The device plugin allocatable (e.g., rdma/ib: 64) is a virtual pool size,
        not the physical NIC count. This method uses multiple strategies to find
        the real count.

        Strategies (in priority order):
        1. IB port labels — count distinct port identifiers in node labels
        2. Total IB speed ÷ per-port speed — from node speed labels
        3. RDMA shared device plugin ConfigMap — parse configList
        4. Fallback — use GPUs per node (1:1 GPU:NIC is standard on modern GPU servers)

        Works on CoreWeave, OpenShift, and bare metal.
        """
        labels = node_data.get('metadata', {}).get('labels', {})

        # Strategy 1: Count distinct IB port labels
        # Matches patterns like: *.ibp0.*, *.ib0.*, *.port0.*, etc.
        # Works for CoreWeave (ib.coreweave.cloud/neighbors.current.ibp0..ibp7)
        # Works for OpenShift with NFD (feature.node.kubernetes.io/network-sriov.device-*)
        if interface_type in ('InfiniBand', 'RoCE', 'RDMA'):
            ib_ports = set()
            import re
            for label_key in labels:
                # CoreWeave pattern: *.ibpN.* or *.ibN.*
                match = re.search(r'\.ibp?(\d+)\.', label_key)
                if match:
                    ib_ports.add(match.group(1))
                # OpenShift/generic pattern: *sriov.device-* or *rdma-device-*
                match = re.search(r'sriov\.device-(\d+)', label_key)
                if match:
                    ib_ports.add(match.group(1))

            if ib_ports:
                count = len(ib_ports)
                logger.info(f"Physical NIC count from port labels: {count} ports detected")
                return count

        # Strategy 2: Total speed ÷ per-port speed
        # CoreWeave: ib.coreweave.cloud/speed: 3200G → 3200/400 = 8 ports
        # OpenShift: may have similar labels
        total_speed = 0
        for label_key, label_value in labels.items():
            if label_key in ('ib.coreweave.cloud/speed', 'ib.coreweave.cloud/speed.current',
                             'ib.coreweave.cloud/speed.expected'):
                speed_str = label_value.rstrip('Gg')
                try:
                    total_speed = int(speed_str)
                    break
                except ValueError:
                    pass

        if total_speed > 0:
            per_port = 400  # NDR default
            gpu_family = labels.get('nvidia.com/gpu.family', '')
            if gpu_family == 'ampere':
                per_port = 200  # HDR
            count = total_speed // per_port
            if count > 0:
                logger.info(f"Physical NIC count from speed: {total_speed}G / {per_port}G = {count} ports")
                return count

        # Strategy 3: RDMA shared device plugin ConfigMap
        count = self._count_nics_from_device_plugin_config(resource_name)
        if count > 0:
            return count

        # Strategy 4: Fallback — GPUs per node (1:1 GPU:NIC is standard)
        allocatable = node_data.get('status', {}).get('allocatable', {})
        gpu_count = 0
        for key, val in allocatable.items():
            if 'gpu' in key.lower() and 'nvidia' in key.lower():
                try:
                    gpu_count = int(val)
                except ValueError:
                    pass
        if gpu_count > 0:
            logger.info(f"Physical NIC count from GPU count fallback: {gpu_count} (1:1 GPU:NIC assumed)")
            return gpu_count

        # Last resort
        logger.warning(f"Could not determine physical NIC count for {resource_name}, defaulting to 1")
        return 1

    def _count_nics_from_device_plugin_config(self, resource_name: str) -> int:
        """
        Try to find and parse the RDMA shared device plugin ConfigMap
        to determine the number of physical NICs.

        The k8s-rdma-shared-dev-plugin stores its config in a ConfigMap
        with a configList. Each entry in configList with a different
        resourceName maps to a separate allocatable resource. If there
        are multiple entries, the physical count = number of entries
        matching this resource.
        """
        try:
            result = self.kubectl.run(
                ['get', 'daemonset', '--all-namespaces', '-o', 'json'],
                check=False
            )
            if result.returncode != 0:
                return 0

            import json as json_mod
            ds_data = json_mod.loads(result.stdout)

            for ds in ds_data.get('items', []):
                ds_name = ds['metadata']['name']
                ds_ns = ds['metadata']['namespace']

                # Find RDMA shared device plugin DaemonSets
                is_rdma_dp = False
                for container in ds['spec']['template']['spec'].get('containers', []):
                    image = container.get('image', '')
                    if 'rdma-shared' in image or 'rdma_shared' in image:
                        is_rdma_dp = True
                        break

                if not is_rdma_dp:
                    continue

                # Find ConfigMap volumes
                for volume in ds['spec']['template']['spec'].get('volumes', []):
                    cm_ref = volume.get('configMap', {})
                    cm_name = cm_ref.get('name', '')
                    if not cm_name:
                        continue

                    cm_result = self.kubectl.run(
                        ['get', 'configmap', cm_name, '-n', ds_ns, '-o', 'json'],
                        check=False
                    )
                    if cm_result.returncode != 0:
                        continue

                    cm_data = json_mod.loads(cm_result.stdout)
                    config_json = cm_data.get('data', {}).get('config.json', '')
                    if not config_json:
                        continue

                    config = json_mod.loads(config_json)
                    config_list = config.get('configList', [])

                    # Count entries matching this resource name
                    # resource_name is like "rdma/ib", config entry has "resourceName": "ib"
                    resource_suffix = resource_name.split('/')[-1] if '/' in resource_name else resource_name

                    matching = [e for e in config_list if e.get('resourceName') == resource_suffix]
                    if matching:
                        # Multiple config entries = multiple distinct resources
                        # Single entry = shared pool (physical count unknown from config alone)
                        if len(config_list) > 1:
                            count = len(matching)
                            logger.info(f"Physical NIC count from device plugin config: {count} entries for {resource_suffix}")
                            return count
                        # Single entry — can't determine physical count from config
                        logger.info(f"Single RDMA device plugin entry for {resource_suffix}, rdmaHcaMax={matching[0].get('rdmaHcaMax', '?')}")
                        return 0

        except Exception as e:
            logger.debug(f"Could not read RDMA device plugin config: {e}")

        return 0

    def _get_cpu_memory(self, node_data: Dict) -> tuple[int, int]:
        """
        Extract CPU and memory from node data.

        Args:
            node_data: Node data from kubectl

        Returns:
            Tuple of (cpu_cores, memory_gb)
        """
        allocatable = node_data.get('status', {}).get('allocatable', {})

        # Parse CPU (can be in cores like "32" or with suffix like "32000m")
        cpu_str = allocatable.get('cpu', '0')
        if 'm' in cpu_str:  # milliCPU
            cpu_cores = int(cpu_str.replace('m', '')) // 1000
        else:
            cpu_cores = int(cpu_str)

        # Parse memory (usually in Ki, Mi, Gi)
        memory_str = allocatable.get('memory', '0Ki')
        if 'Ki' in memory_str:
            memory_gb = int(memory_str.replace('Ki', '')) / (1024 * 1024)
        elif 'Mi' in memory_str:
            memory_gb = int(memory_str.replace('Mi', '')) / 1024
        elif 'Gi' in memory_str:
            memory_gb = int(memory_str.replace('Gi', ''))
        else:
            memory_gb = 0

        return cpu_cores, int(memory_gb)

    def _estimate_gpu_memory(self, gpu_type: str, gpu_count: int, node_labels: Dict[str, str]) -> int:
        """
        Estimate GPU VRAM based on GPU type and node labels.

        Args:
            gpu_type: Type of GPU
            gpu_count: Number of GPUs
            node_labels: Node labels that might contain GPU info

        Returns:
            Total GPU memory in MB
        """
        # Prefer exact VRAM from nvidia labels (MB)
        gpu_memory_label = node_labels.get('nvidia.com/gpu.memory', '')
        if gpu_memory_label:
            try:
                per_gpu_mb = int(gpu_memory_label)
                logger.info(f"GPU VRAM from nvidia.com/gpu.memory label: {per_gpu_mb} MB per GPU")
                return per_gpu_mb * gpu_count
            except ValueError:
                pass

        # Try CoreWeave vram label (GB)
        cw_vram = node_labels.get('gpu.nvidia.com/vram', '')
        if cw_vram:
            try:
                per_gpu_gb = int(cw_vram)
                logger.info(f"GPU VRAM from gpu.nvidia.com/vram label: {per_gpu_gb} GB per GPU")
                return per_gpu_gb * 1024 * gpu_count
            except ValueError:
                pass

        # Fall back to model-based estimation
        gpu_model = node_labels.get('nvidia.com/gpu.product', '').lower()

        gpu_vram_map = {
            'h200': 141000,
            'h100': 80000,
            'a100': 80000,
            'a10': 24000,
            'v100': 32000,
            'l4': 24000,
            'l40': 48000,
            'l40s': 48000,
            't4': 16000,
            'b200': 192000,
            'b100': 192000,
            'mi300x': 192000,
            'mi300': 192000,
            'mi250': 128000,
        }

        for key, vram in gpu_vram_map.items():
            if key in gpu_model:
                return vram * gpu_count

        logger.warning(f"Unknown GPU type {gpu_type}, assuming 80GB VRAM per GPU")
        return 80000 * gpu_count

    def _get_hardware_info_from_configmap(self, node_name: str) -> Optional[Dict]:
        """
        Read hardware info from ConfigMap created by hardware-discovery DaemonSet.

        Args:
            node_name: Name of the node

        Returns:
            Hardware info dict or None if not found
        """
        try:
            configmap_name = f"hardware-info-{node_name}"
            result = self.kubectl.run_json(['get', 'configmap', configmap_name])

            if result and 'data' in result:
                hardware_json = result['data'].get('hardware.json', '{}')
                return json.loads(hardware_json)
        except Exception as e:
            logger.debug(f"Could not read hardware info for node {node_name}: {e}")
            return None

    def scan_nodes(self) -> List[NodeResources]:
        """
        Scan all nodes in the cluster for GPU and RDMA resources.
        Tries to read from hardware-discovery ConfigMaps first, falls back to node allocatable.

        Returns:
            List of NodeResources
        """
        logger.info("Scanning nodes in cluster...")

        nodes_data = self.kubectl.run_json(['get', 'nodes'])
        nodes = []

        for node_data in nodes_data.get('items', []):
            node_name = node_data['metadata']['name']
            labels = node_data['metadata'].get('labels', {})

            # Try to get hardware info from ConfigMap (created by DaemonSet)
            hw_info = self._get_hardware_info_from_configmap(node_name)

            if hw_info:
                logger.info(f"Using hardware discovery data for node {node_name}")

                gpus = hw_info.get('gpus', [])
                if gpus:
                    gpu_info = gpus[0]
                    gpu_count = gpu_info.get('count', 0)
                    gpu_vendor = gpu_info.get('vendor', 'unknown')
                    gpu_model = gpu_info.get('model', 'unknown')
                    gpu_memory_mb = gpu_info.get('memory_mb', 0)
                    gpu_type = f"{gpu_vendor.lower()}.com/gpu"
                else:
                    # Hardware discovery exists but no GPUs detected
                    # Fall back to node allocatable in case discovery missed them (e.g., nvidia-smi not available)
                    gpu_count, gpu_type, gpu_vendor, gpu_model = self._get_gpu_count(node_data)
                    gpu_memory_mb = self._estimate_gpu_memory(gpu_type, gpu_count, labels)

                cpu_info = hw_info.get('cpu', {})
                cpu_cores = cpu_info.get('cores', 0)
                cpu_model = cpu_info.get('model', self._get_cpu_model(node_data))

                mem_info = hw_info.get('memory', {})
                memory_gb = mem_info.get('total_gb', 0)

                nics = hw_info.get('nics', [])
                network_interfaces = []
                has_rdma = False
                rdma_devices = []

                for nic in nics:
                    network_interfaces.append(NetworkInterface(
                        name=nic.get('name', 'unknown'),
                        type=nic.get('type', 'unknown'),
                        vendor=nic.get('vendor', 'unknown'),
                        model=nic.get('model', 'unknown'),
                        count=1,
                        speed_gbps=float(nic.get('speed_gbps', 0))
                    ))
                    if nic.get('type') in ['RDMA', 'RoCE', 'InfiniBand']:
                        has_rdma = True
                        rdma_devices.append(nic.get('name', 'unknown'))

            else:
                logger.info(f"Using node allocatable resources for node {node_name} (hardware discovery not available)")

                gpu_count, gpu_type, gpu_vendor, gpu_model = self._get_gpu_count(node_data)
                cpu_cores, memory_gb = self._get_cpu_memory(node_data)
                cpu_model = self._get_cpu_model(node_data)
                gpu_memory_mb = self._estimate_gpu_memory(gpu_type, gpu_count, labels)
                has_rdma, rdma_devices = self._check_rdma_support(node_data)
                network_interfaces = self._detect_network_interfaces(node_data)

            # Extract node status from conditions
            node_status = 'Unknown'
            for cond in node_data.get('status', {}).get('conditions', []):
                if cond.get('type') == 'Ready':
                    node_status = 'Ready' if cond.get('status') == 'True' else 'NotReady'
                    break

            node_resources = NodeResources(
                name=node_name,
                gpus=gpu_count,
                gpu_type=gpu_type,
                gpu_vendor=gpu_vendor,
                gpu_model=gpu_model,
                gpu_memory_mb=gpu_memory_mb,
                cpu_cores=cpu_cores,
                cpu_model=cpu_model,
                memory_gb=memory_gb,
                has_rdma=has_rdma,
                rdma_devices=rdma_devices,
                network_interfaces=network_interfaces,
                labels=labels,
                status=node_status
            )

            nodes.append(node_resources)

            total_physical_nics = sum(nic.count for nic in network_interfaces)
            nic_summary = f"{total_physical_nics} NICs"
            if network_interfaces:
                nic_parts = []
                for nic in network_interfaces:
                    speed_str = f" {nic.speed_gbps}Gbps" if nic.speed_gbps > 0 else ""
                    pool_str = f" pool={nic.pool_size}" if nic.pool_size != nic.count else ""
                    nic_parts.append(f"{nic.count}× {nic.type}{speed_str}{pool_str}")
                nic_summary = f"{total_physical_nics} NICs ({', '.join(nic_parts)})"

            gpu_desc = f"{gpu_vendor} {gpu_model}" if gpu_model != 'unknown' else gpu_vendor
            logger.info(
                f"Node: {node_name}, GPUs: {gpu_count} × {gpu_desc}, "
                f"VRAM: {gpu_memory_mb}MB, CPU: {cpu_cores} cores, RAM: {memory_gb}GB, "
                f"RDMA: {has_rdma}, {nic_summary}"
            )

        # Clean up hardware-discovery DaemonSet now that we've read the data
        try:
            self.kubectl.run(
                ['delete', 'daemonset', 'hardware-discovery', '-n', self.namespace, '--ignore-not-found=true'],
                check=False
            )
            logger.info("Cleaned up hardware-discovery DaemonSet")
        except Exception as e:
            logger.debug(f"Could not clean up hardware-discovery DaemonSet: {e}")

        return nodes

    def scan_storage_classes(self) -> List[StorageClassInfo]:
        """
        Scan available storage classes in the cluster.

        Returns:
            List of StorageClassInfo
        """
        logger.info("Scanning storage classes...")

        try:
            sc_data = self.kubectl.run_json(['get', 'storageclasses'])
            storage_classes = []

            for sc in sc_data.get('items', []):
                name = sc['metadata']['name']
                provisioner = sc.get('provisioner', 'unknown')
                reclaim_policy = sc.get('reclaimPolicy', 'Delete')
                volume_binding_mode = sc.get('volumeBindingMode', 'Immediate')
                allow_expansion = sc.get('allowVolumeExpansion', False)

                sc_info = StorageClassInfo(
                    name=name,
                    provisioner=provisioner,
                    reclaim_policy=reclaim_policy,
                    volume_binding_mode=volume_binding_mode,
                    allow_volume_expansion=allow_expansion
                )

                storage_classes.append(sc_info)
                logger.info(f"StorageClass: {name} (provisioner: {provisioner})")

            return storage_classes

        except Exception as e:
            logger.warning(f"Failed to scan storage classes: {e}")
            return []

    def scan_cluster(self) -> ClusterResources:
        """
        Scan entire cluster and aggregate resource information.

        Returns:
            ClusterResources with aggregated data
        """
        logger.info("=" * 60)
        logger.info("Starting cluster resource scan")
        logger.info("=" * 60)

        nodes = self.scan_nodes()
        storage_classes = self.scan_storage_classes()

        if not nodes:
            logger.warning("No nodes found in cluster!")
            return ClusterResources(
                total_gpus=0,
                gpus_per_node={},
                max_gpus_per_node=0,
                min_gpus_per_node=0,
                total_gpu_memory_gb=0,
                gpu_memory_per_gpu_mb=0,
                total_cpu_cores=0,
                total_memory_gb=0,
                node_count=0,
                gpu_node_count=0,
                has_rdma=False,
                rdma_capable_nodes=0,
                gpu_type='unknown',
                gpu_vendor='unknown',
                gpu_model='unknown',
                total_network_interfaces=0,
                network_interfaces_by_type={},
                network_interfaces_by_vendor={},
                storage_classes=storage_classes,
                nodes=[],
                cloud_provider=CloudProvider.UNKNOWN
            )

        total_gpus = sum(node.gpus for node in nodes)
        gpus_per_node = {node.name: node.gpus for node in nodes}
        gpu_nodes = [node for node in nodes if node.gpus > 0]

        max_gpus_per_node = max((node.gpus for node in gpu_nodes), default=0)
        min_gpus_per_node = min((node.gpus for node in gpu_nodes), default=0) if gpu_nodes else 0

        total_gpu_memory_mb = sum(node.gpu_memory_mb for node in gpu_nodes)
        total_gpu_memory_gb = total_gpu_memory_mb // 1024
        gpu_memory_per_gpu_mb = total_gpu_memory_mb // total_gpus if total_gpus > 0 else 0

        total_cpu_cores = sum(node.cpu_cores for node in nodes)
        total_memory_gb = sum(node.memory_gb for node in nodes)

        rdma_capable_nodes = sum(1 for node in nodes if node.has_rdma)
        has_rdma = rdma_capable_nodes > 0

        gpu_types = [node.gpu_type for node in gpu_nodes if node.gpu_type != 'unknown']
        gpu_type = gpu_types[0] if gpu_types else 'unknown'

        gpu_vendors = [node.gpu_vendor for node in gpu_nodes if node.gpu_vendor != 'unknown']
        gpu_vendor = gpu_vendors[0] if gpu_vendors else 'unknown'

        gpu_models = [node.gpu_model for node in gpu_nodes if node.gpu_model != 'unknown']
        gpu_model = gpu_models[0] if gpu_models else 'unknown'

        gpu_node_count = len(gpu_nodes)

        total_network_interfaces = 0
        network_interfaces_by_type = {}
        network_interfaces_by_vendor = {}

        for node in nodes:
            for nic in node.network_interfaces:
                nic_count = getattr(nic, 'count', 1)
                total_network_interfaces += nic_count
                network_interfaces_by_type[nic.type] = network_interfaces_by_type.get(nic.type, 0) + nic_count
                network_interfaces_by_vendor[nic.vendor] = network_interfaces_by_vendor.get(nic.vendor, 0) + nic_count

        # Detect cloud provider from cluster infrastructure
        cloud_provider = CloudConstraints.detect_cloud_provider(self.kubectl)

        # Extract CPU model, host model, and NIC models from nodes
        cpu_model = 'unknown'
        host_model = 'unknown'
        nic_models = []

        # Get CPU model from first GPU node if available
        for node in gpu_nodes:
            if node.cpu_model and node.cpu_model != 'unknown':
                cpu_model = node.cpu_model
                break

        # Get host/instance type from first GPU node
        host_machine = 'unknown'
        for node in gpu_nodes:
            labels = node.labels
            for label_key in ['node.kubernetes.io/instance-type', 'beta.kubernetes.io/instance-type']:
                if label_key in labels:
                    host_model = labels[label_key]
                    break
            machine = labels.get('nvidia.com/gpu.machine', '')
            if machine:
                host_machine = machine
            if host_model != 'unknown':
                break
        if host_model != 'unknown' and host_machine != 'unknown':
            host_model = f"{host_model} ({host_machine})"

        # Collect unique NIC models and speeds
        nic_model_set = set()
        nic_speeds = {}
        for node in nodes:
            for nic in node.network_interfaces:
                if nic.model and nic.model != 'unknown':
                    nic_model_set.add(nic.model)
                if nic.speed_gbps > 0:
                    nic_speeds[nic.name] = nic.speed_gbps
        nic_models = sorted(list(nic_model_set))

        cluster_resources = ClusterResources(
            total_gpus=total_gpus,
            gpus_per_node=gpus_per_node,
            max_gpus_per_node=max_gpus_per_node,
            min_gpus_per_node=min_gpus_per_node,
            total_gpu_memory_gb=total_gpu_memory_gb,
            gpu_memory_per_gpu_mb=gpu_memory_per_gpu_mb,
            total_cpu_cores=total_cpu_cores,
            total_memory_gb=total_memory_gb,
            node_count=len(nodes),
            gpu_node_count=gpu_node_count,
            has_rdma=has_rdma,
            rdma_capable_nodes=rdma_capable_nodes,
            gpu_type=gpu_type,
            gpu_vendor=gpu_vendor,
            gpu_model=gpu_model,
            total_network_interfaces=total_network_interfaces,
            network_interfaces_by_type=network_interfaces_by_type,
            network_interfaces_by_vendor=network_interfaces_by_vendor,
            storage_classes=storage_classes,
            nodes=nodes,
            cloud_provider=cloud_provider,
            cpu_model=cpu_model,
            host_model=host_model,
            nic_models=nic_models,
            nic_speeds=nic_speeds
        )

        logger.info("=" * 60)
        logger.info("Cluster Resource Summary")
        logger.info("=" * 60)
        logger.info(f"Cloud Provider: {cloud_provider.value}")
        logger.info(f"Total Nodes: {cluster_resources.node_count} ({gpu_node_count} with GPUs)")
        logger.info(f"Total GPUs: {cluster_resources.total_gpus}")
        gpu_desc = f"{gpu_vendor} {gpu_model}" if gpu_model != 'unknown' else gpu_vendor
        logger.info(f"GPU: {gpu_desc}")
        logger.info(f"GPUs per Node (min/max): {min_gpus_per_node}/{max_gpus_per_node}")
        logger.info(f"Total GPU Memory: {total_gpu_memory_gb} GB")
        logger.info(f"GPU Memory per GPU: {gpu_memory_per_gpu_mb // 1024} GB")
        logger.info(f"Total CPU Cores: {total_cpu_cores}")
        logger.info(f"Total System RAM: {total_memory_gb} GB")
        logger.info(f"Max TP Value: {cluster_resources.get_max_tp()}")
        logger.info(f"TP Options: {cluster_resources.get_tp_options()}")
        logger.info(f"RDMA Support: {has_rdma} ({rdma_capable_nodes} nodes)")
        logger.info(f"Network Interfaces: {total_network_interfaces} total")
        if network_interfaces_by_type:
            nic_summary = ", ".join([f"{count}× {ntype}" for ntype, count in network_interfaces_by_type.items()])
            logger.info(f"  By Type: {nic_summary}")
        if network_interfaces_by_vendor:
            vendor_summary = ", ".join([f"{count}× {vendor}" for vendor, count in network_interfaces_by_vendor.items()])
            logger.info(f"  By Vendor: {vendor_summary}")
        logger.info(f"Storage Classes: {len(storage_classes)}")
        logger.info("=" * 60)

        return cluster_resources

    def get_available_gpus(self) -> Dict[str, int]:
        """
        Get currently available (unallocated) GPUs per node.

        TODO: Query running pods and subtract allocated GPUs for precise availability.

        Returns:
            Dictionary of node_name -> available_gpu_count
        """
        resources = self.scan_cluster()
        return resources.gpus_per_node

    def detect_rdma_nics(self, node_name: Optional[str] = None) -> Dict[str, List[str]]:
        """
        Detect RDMA NICs on nodes.

        Args:
            node_name: Specific node to check (None for all nodes)

        Returns:
            Dictionary of node_name -> list of RDMA device names
        """
        resources = self.scan_cluster()

        if node_name:
            node = next((n for n in resources.nodes if n.name == node_name), None)
            if node:
                return {node.name: node.rdma_devices}
            return {}

        return {
            node.name: node.rdma_devices
            for node in resources.nodes
            if node.has_rdma
        }

    def to_json(self, cluster_resources: ClusterResources) -> str:
        """
        Convert ClusterResources to JSON string.

        Args:
            cluster_resources: ClusterResources to serialize

        Returns:
            JSON string
        """
        return json.dumps(asdict(cluster_resources), indent=2)


def main():
    """Main entry point for standalone execution."""
    import argparse

    parser = argparse.ArgumentParser(
        description='Scan Kubernetes cluster for GPU and RDMA resources'
    )
    parser.add_argument(
        '--namespace',
        default='serveit',
        help='Kubernetes namespace to scan (default: llm-d)'
    )
    parser.add_argument(
        '--kubeconfig',
        help='Path to kubeconfig file (default: ~/.kube/kubeconfig)'
    )
    parser.add_argument(
        '--output',
        help='Output JSON file path (optional)'
    )
    parser.add_argument(
        '--verbose',
        action='store_true',
        help='Enable verbose logging'
    )

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Scan cluster
    scanner = SystemScanner(namespace=args.namespace, kubeconfig=args.kubeconfig)
    resources = scanner.scan_cluster()

    # Output results
    output_json = scanner.to_json(resources)

    if args.output:
        with open(args.output, 'w') as f:
            f.write(output_json)
        logger.info(f"Results saved to {args.output}")
    else:
        print(output_json)


if __name__ == '__main__':
    main()
