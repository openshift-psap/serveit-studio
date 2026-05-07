"""
InfeRecipe Configuration Generator

Generates test configurations based on user inputs and cluster resources.
Determines which architectures to test based on optimization priority.
"""

import logging
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass, asdict, field
from datetime import datetime
from .system_scanner import SystemScanner, ClusterResources
from .cloud_constraints import CloudProvider
from .resource_calculator import calculate_pod_resources
from .networking import detect_rdma_device_resources

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


@dataclass
class TestConfig:
    """Configuration for a single test run."""
    test_id: str
    architecture: str  # 'aggregated', 'pd', 'ep'
    model_name: str
    namespace: str

    # Workload parameters
    isl: int
    osl: int
    num_users: int

    # Deployment parameters
    tensor_parallelism: int
    replicas: int

    # PD-specific (None for other architectures)
    prefill_replicas: Optional[int] = None
    decode_replicas: Optional[int] = None
    prefill_decode_ratio: Optional[str] = None
    prefill_tp: Optional[int] = None  # Separate TP for prefill pods
    decode_tp: Optional[int] = None   # Separate TP for decode pods

    # Resource parameters
    max_model_len: int = 8192  # Should be calculated as (isl + osl) * 1.2 for optimal memory usage
    gpu_memory_utilization: float = 0.95  # For aggregated architecture
    gpu_vram_gb: Optional[float] = None  # GPU VRAM in GiB (for memory profiling)
    prefill_gpu_memory_utilization: Optional[float] = None  # For PD prefill pods
    decode_gpu_memory_utilization: Optional[float] = None   # For PD decode pods
    max_num_seqs: Optional[int] = None  # For aggregated/EP
    prefill_max_num_seqs: Optional[int] = None  # For PD prefill pods
    decode_max_num_seqs: Optional[int] = None   # For PD decode pods
    max_num_batched_tokens: Optional[int] = None  # Limits tokens per forward pass
    # Advanced vLLM flags
    enable_prefix_caching: bool = True
    disable_custom_all_reduce: bool = False
    enable_auto_tool_choice: bool = False
    tool_call_parser: Optional[str] = None
    dtype: Optional[str] = None  # None = vLLM auto-detects
    kv_cache_dtype: Optional[str] = None  # None = auto
    pipeline_parallel_size: Optional[int] = None  # None = 1 (default)
    block_size: int = 16  # KV cache block size (auto-tuned from ISL+OSL)
    trust_remote_code: bool = True
    disable_log_requests: bool = True
    vllm_debug_logs: bool = False
    nccl_debug_logs: bool = False
    memory_request: str = '64Gi'  # System RAM per pod (overridden by dynamic calculation)
    memory_limit: str = '64Gi'  # System RAM per pod (overridden by dynamic calculation)
    cpu_request: str = '16'  # CPUs per pod (overridden by dynamic calculation)
    cpu_limit: Optional[str] = None  # CPU limit (defaults to cpu_request if not set)

    # Infrastructure
    image: str = 'ghcr.io/llm-d/llm-d-cuda:v0.5.1'
    pvc_name: str = 'model-cache'
    nccl_ib_hca: str = 'mlx'
    kv_connector: str = 'NixlConnector'

    # Networking
    network_type: str = 'dra'  # 'dra' or 'nad' - controls anti-affinity rules
    rdma_device_resources: List[str] = field(default_factory=list)  # RDMA resource keys from node allocatable
    rdma_nics_per_node: int = 0  # Physical NICs per node (from scanner, for RDMA request count)

    # Benchmark load parameters
    request_type: str = 'constant'  # 'constant', 'concurrent', 'throughput', 'poisson'
    request_rate: int = 1  # Requests per second (constant) or concurrent users (concurrent)

    # Test metadata
    optimization_goal: str = 'balanced'  # 'throughput', 'response_time', 'balanced'
    test_duration: int = 300
    stop_mode: str = 'duration'  # 'duration' or 'max_requests'
    max_requests: Optional[int] = None  # Alternative to test_duration
    isl_stdev: Optional[int] = None  # ISL standard deviation
    osl_stdev: Optional[int] = None  # OSL standard deviation
    turns: int = 1  # Number of conversation turns (1 = single-turn)

    # Node pinning
    selected_nodes: List[str] = field(default_factory=list)

    # Dataset workload (alternative to synthetic ISL/OSL)
    workload_mode: str = 'synthetic'  # 'synthetic' or 'dataset'
    dataset_source: Optional[str] = None  # HF dataset ID or file path
    dataset_column: Optional[str] = None  # Column name for prompt text
    dataset_max_output: int = 256  # Max output tokens when using dataset

    # EPP configuration (passed to prereq_manager for configmap generation)
    epp_config: Optional[Dict] = None


@dataclass
class OptimizationPlan:
    """Complete optimization plan with all test configurations."""
    run_name: str
    model_name: str
    isl: int
    osl: int
    num_users: int
    optimization_goal: str
    test_configs: List[TestConfig]
    cluster_resources: ClusterResources
    created_at: str


class ConfigGenerator:
    """Generates test configurations based on user inputs and cluster resources."""

    def __init__(
        self,
        namespace: str = 'inferecipe',
        kubeconfig: Optional[str] = None
    ):
        """
        Initialize ConfigGenerator.

        Args:
            namespace: Kubernetes namespace for deployments
            kubeconfig: Path to kubeconfig file
        """
        self.namespace = namespace
        self.scanner = SystemScanner(namespace=namespace, kubeconfig=kubeconfig)
        self.cluster_resources = None

    def _generate_test_id(
        self,
        architecture: str,
        tp: int,
        ratio: Optional[str] = None,
        timestamp: Optional[str] = None
    ) -> str:
        """
        Generate a unique test ID.

        Args:
            architecture: Architecture type
            tp: Tensor parallelism value
            ratio: Prefill/decode ratio (for PD only)
            timestamp: Optional timestamp string

        Returns:
            Unique test ID
        """
        if timestamp is None:
            timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')

        if architecture == 'pd' and ratio:
            # For PD: test-pd-1-2-tp4-20240326-120000
            ratio_str = ratio.replace(':', '-')
            return f"test-{architecture}-{ratio_str}-tp{tp}-{timestamp}"
        else:
            # For aggregated/ep: test-aggregated-tp8-20240326-120000
            return f"test-{architecture}-tp{tp}-{timestamp}"

    def _parse_pd_ratio(self, ratio_str: str) -> Tuple[int, int]:
        """
        Parse prefill:decode ratio string.

        Args:
            ratio_str: Ratio string like "1:2"

        Returns:
            Tuple of (prefill_count, decode_count)
        """
        parts = ratio_str.split(':')
        if len(parts) != 2:
            raise ValueError(f"Invalid ratio format: {ratio_str}. Expected 'X:Y'")

        try:
            prefill = int(parts[0])
            decode = int(parts[1])
            return prefill, decode
        except ValueError:
            raise ValueError(f"Invalid ratio values: {ratio_str}. Must be integers")

    def _detect_network_type(self) -> str:
        """
        Detect network type based on cloud provider.

        Returns:
            'dra' for IBM Cloud (DRANET), 'nad' for bare metal or other providers
        """
        import os

        # Check for manual override
        force_nad = os.getenv('INFE_RECIPE_FORCE_NAD', 'false').lower() == 'true'
        if force_nad:
            logger.info("Forcing NAD network type via INFE_RECIPE_FORCE_NAD env var")
            return 'nad'

        # Detect from cluster resources (cloud_provider is an Enum)
        if self.cluster_resources:
            if self.cluster_resources.cloud_provider == CloudProvider.IBM_CLOUD:
                logger.info("Detected IBM Cloud → Using DRA (DRANET) network type")
                return 'dra'
            if self.cluster_resources.cloud_provider == CloudProvider.COREWEAVE:
                logger.info("Detected CoreWeave → Using SharedDevice (rdma/ib) network type")
                return 'shared_device'

        logger.info("Non-IBM Cloud or unknown provider → Using NAD network type")
        return 'nad'

    def _detect_rdma_device_resources(self) -> List[str]:
        if not self.cluster_resources:
            return []
        return detect_rdma_device_resources(
            self.cluster_resources.nodes, self._detect_network_type()
        )

    def _detect_rdma_nics_per_node(self) -> int:
        """
        Get physical NIC count per node from scanner results.

        Returns the minimum physical NIC count across all RDMA-capable nodes,
        since pods may land on any node.
        """
        if not self.cluster_resources:
            return 0

        min_nics = None
        for node in self.cluster_resources.nodes:
            if not node.has_rdma:
                continue
            for nic in node.network_interfaces:
                if nic.type in ('InfiniBand', 'RoCE', 'RDMA'):
                    if min_nics is None or nic.count < min_nics:
                        min_nics = nic.count

        return min_nics or 0

    def _calculate_pod_resources(self, tp: int, total_pods: int) -> tuple:
        """Delegate to core.resource_calculator."""
        return calculate_pod_resources(tp, total_pods, self.cluster_resources)

    def _determine_architectures(self, optimization_goal: str) -> List[str]:
        """
        Determine which architectures to test based on optimization goal.

        Args:
            optimization_goal: 'throughput', 'response_time', or 'balanced'

        Returns:
            List of architecture names to test
        """
        if optimization_goal == 'throughput':
            # Throughput optimization: Test Aggregated vs EP
            architectures = ['aggregated', 'ep']
            logger.info("Optimization goal: Throughput → Testing Aggregated vs EP")

        elif optimization_goal == 'response_time':
            # Response time optimization: Test Aggregated vs PD
            architectures = ['aggregated', 'pd']
            logger.info("Optimization goal: Response Time → Testing Aggregated vs PD")

        else:  # balanced or any other
            # Balanced: Test all three
            architectures = ['aggregated', 'pd', 'ep']
            logger.info("Optimization goal: Balanced → Testing Aggregated, PD, and EP")

        return architectures

    def _generate_aggregated_configs(
        self,
        tp_values: List[int],
        model_name: str,
        isl: int,
        osl: int,
        num_users: int,
        optimization_goal: str,
        max_model_len: int,
        test_duration: int,
        timestamp: str
    ) -> List[TestConfig]:
        """Generate configurations for aggregated architecture."""
        configs = []

        # Detect network type and RDMA resource once
        network_type = self._detect_network_type()
        rdma_device_resources = self._detect_rdma_device_resources()
        rdma_nics_per_node = self._detect_rdma_nics_per_node()

        for tp in tp_values:
            test_id = self._generate_test_id('aggregated', tp, timestamp=timestamp)

            # Calculate resources for this specific TP and replica count
            replicas = 1  # Single replica for aggregated calibration
            memory_per_pod, cpu_per_pod = self._calculate_pod_resources(tp=tp, total_pods=replicas)

            config = TestConfig(
                test_id=test_id,
                architecture='aggregated',
                model_name=model_name,
                namespace=self.namespace,
                isl=isl,
                osl=osl,
                num_users=num_users,
                tensor_parallelism=tp,
                replicas=replicas,
                max_model_len=max_model_len,
                optimization_goal=optimization_goal,
                test_duration=test_duration,
                network_type=network_type,
                rdma_device_resources=rdma_device_resources,
                rdma_nics_per_node=rdma_nics_per_node,
                memory_request=memory_per_pod,
                memory_limit=memory_per_pod,
                cpu_request=cpu_per_pod,
                cpu_limit=cpu_per_pod
            )
            configs.append(config)
            logger.info(f"Generated config: {test_id} (TP={tp}, Memory={memory_per_pod})")

        return configs

    def _generate_pd_configs(
        self,
        tp_values: List[int],
        pd_ratios: List[str],
        model_name: str,
        isl: int,
        osl: int,
        num_users: int,
        optimization_goal: str,
        max_model_len: int,
        test_duration: int,
        timestamp: str
    ) -> List[TestConfig]:
        """Generate configurations for PD (Prefill/Decode) architecture."""
        configs = []

        # Detect network type and RDMA resource once
        network_type = self._detect_network_type()
        rdma_device_resources = self._detect_rdma_device_resources()
        rdma_nics_per_node = self._detect_rdma_nics_per_node()

        for tp in tp_values:
            for ratio_str in pd_ratios:
                prefill_count, decode_count = self._parse_pd_ratio(ratio_str)
                test_id = self._generate_test_id('pd', tp, ratio=ratio_str, timestamp=timestamp)

                # Calculate resources for this PD configuration
                total_pods = prefill_count + decode_count
                memory_per_pod, cpu_per_pod = self._calculate_pod_resources(tp=tp, total_pods=total_pods)

                config = TestConfig(
                    test_id=test_id,
                    architecture='pd',
                    model_name=model_name,
                    namespace=self.namespace,
                    isl=isl,
                    osl=osl,
                    num_users=num_users,
                    tensor_parallelism=tp,
                    replicas=1,  # Not used for PD
                    prefill_replicas=prefill_count,
                    decode_replicas=decode_count,
                    prefill_decode_ratio=ratio_str,
                    max_model_len=max_model_len,
                    optimization_goal=optimization_goal,
                    test_duration=test_duration,
                    network_type=network_type,
                    rdma_device_resources=rdma_device_resources,
                    rdma_nics_per_node=rdma_nics_per_node,
                    memory_request=memory_per_pod,
                    memory_limit=memory_per_pod,
                    cpu_request=cpu_per_pod,
                    cpu_limit=cpu_per_pod
                )
                configs.append(config)
                logger.info(f"Generated config: {test_id} (TP={tp}, Ratio={ratio_str}, Memory={memory_per_pod})")

        return configs

    def _generate_ep_configs(
        self,
        tp_values: List[int],
        model_name: str,
        isl: int,
        osl: int,
        num_users: int,
        optimization_goal: str,
        max_model_len: int,
        test_duration: int,
        timestamp: str,
        max_gpus: Optional[int] = None
    ) -> List[TestConfig]:
        """
        Generate configurations for EP (Expert Parallelism) architecture.

        Intelligently scales replicas based on TP and available GPUs:
        - For TP=1: Uses max replicas to maximize throughput (e.g., 16 experts)
        - For TP>1: Scales down replicas proportionally (e.g., TP=2 → 8 experts)
        """
        configs = []

        # Determine max_gpus from cluster resources if not provided
        if max_gpus is None and self.cluster_resources:
            # Use GPUs per node as baseline (typically 8 for H100 nodes)
            # Then scale up to total available
            max_gpus = min(
                self.cluster_resources.total_gpus,
                self.cluster_resources.max_gpus_per_node * 2  # Use 2 nodes by default
            )

        # Detect network type and RDMA resource once
        network_type = self._detect_network_type()
        rdma_device_resources = self._detect_rdma_device_resources()
        rdma_nics_per_node = self._detect_rdma_nics_per_node()

        for tp in tp_values:
            # Calculate optimal replica count based on TP
            # Total GPUs = replicas × TP
            # For small models with TP=1, use many replicas (e.g., 16)
            # For larger TP, scale down (e.g., TP=8 → replicas=2)
            if max_gpus:
                replicas = max(1, max_gpus // tp)
            else:
                replicas = 1  # Fallback if no cluster info

            test_id = self._generate_test_id('ep', tp, timestamp=timestamp)

            total_gpus = replicas * tp

            # Calculate resources for this EP configuration
            memory_per_pod, cpu_per_pod = self._calculate_pod_resources(tp=tp, total_pods=replicas)

            config = TestConfig(
                test_id=test_id,
                architecture='ep',
                model_name=model_name,
                namespace=self.namespace,
                isl=isl,
                osl=osl,
                num_users=num_users,
                tensor_parallelism=tp,
                replicas=replicas,
                max_model_len=max_model_len,
                optimization_goal=optimization_goal,
                test_duration=test_duration,
                network_type=network_type,
                rdma_device_resources=rdma_device_resources,
                rdma_nics_per_node=rdma_nics_per_node,
                memory_request=memory_per_pod,
                memory_limit=memory_per_pod,
                cpu_request=cpu_per_pod,
                cpu_limit=cpu_per_pod
            )
            configs.append(config)
            logger.info(f"Generated config: {test_id} (TP={tp}, Replicas={replicas}, Total GPUs={total_gpus}, Memory={memory_per_pod})")

        return configs

    def generate_optimization_plan(
        self,
        run_name: str,
        model_name: str,
        isl: int,
        osl: int,
        num_users: int,
        optimization_goal: str = 'balanced',
        tp_values: Optional[List[int]] = None,
        pd_ratios: Optional[List[str]] = None,
        max_model_len: int = 8192,
        test_duration: int = 300,
        scan_cluster: bool = True,
        max_gpus: Optional[int] = None
    ) -> OptimizationPlan:
        """
        Generate a complete optimization plan with all test configurations.

        Args:
            run_name: Name for this optimization run
            model_name: HuggingFace model name
            isl: Input sequence length
            osl: Output sequence length
            num_users: Number of concurrent users
            optimization_goal: 'throughput', 'response_time', or 'balanced'
            tp_values: List of TP values to test (auto-detected if None)
            pd_ratios: List of prefill:decode ratios for PD (default: ['1:1', '1:2', '1:4', '2:1'])
            max_model_len: Maximum model sequence length
            test_duration: Test duration in seconds per configuration
            scan_cluster: Whether to scan cluster for resources
            max_gpus: Maximum GPUs to use (for scaling EP replicas)

        Returns:
            OptimizationPlan with all test configurations
        """
        logger.info("=" * 70)
        logger.info(f"Generating Optimization Plan: {run_name}")
        logger.info("=" * 70)
        logger.info(f"Model: {model_name}")
        logger.info(f"Workload: ISL={isl}, OSL={osl}, Users={num_users}")
        logger.info(f"Goal: {optimization_goal}")

        # Scan cluster if requested
        if scan_cluster:
            logger.info("Scanning cluster for available resources...")
            self.cluster_resources = self.scanner.scan_cluster()

        if self.cluster_resources is None:
            raise RuntimeError("Cluster resources not available. Run scan_cluster first.")

        # Determine TP values if not provided
        if tp_values is None:
            tp_values = self.cluster_resources.get_tp_options()
            logger.info(f"Auto-detected TP values: {tp_values}")
        else:
            logger.info(f"Using provided TP values: {tp_values}")

        # Set default PD ratios
        if pd_ratios is None:
            pd_ratios = ['1:1', '1:2', '1:4', '2:1']
            logger.info(f"Using default PD ratios: {pd_ratios}")
        else:
            logger.info(f"Using provided PD ratios: {pd_ratios}")

        # Determine architectures to test
        architectures = self._determine_architectures(optimization_goal)

        # Generate timestamp for this run (all tests will share it)
        timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')

        # Generate configurations for each architecture
        all_configs = []

        for arch in architectures:
            if arch == 'aggregated':
                configs = self._generate_aggregated_configs(
                    tp_values, model_name, isl, osl, num_users,
                    optimization_goal, max_model_len, test_duration, timestamp
                )
                all_configs.extend(configs)

            elif arch == 'pd':
                configs = self._generate_pd_configs(
                    tp_values, pd_ratios, model_name, isl, osl, num_users,
                    optimization_goal, max_model_len, test_duration, timestamp
                )
                all_configs.extend(configs)

            elif arch == 'ep':
                configs = self._generate_ep_configs(
                    tp_values, model_name, isl, osl, num_users,
                    optimization_goal, max_model_len, test_duration, timestamp,
                    max_gpus=max_gpus
                )
                all_configs.extend(configs)

        # Create optimization plan
        plan = OptimizationPlan(
            run_name=run_name,
            model_name=model_name,
            isl=isl,
            osl=osl,
            num_users=num_users,
            optimization_goal=optimization_goal,
            test_configs=all_configs,
            cluster_resources=self.cluster_resources,
            created_at=datetime.now().isoformat()
        )

        logger.info("=" * 70)
        logger.info("Optimization Plan Summary")
        logger.info("=" * 70)
        logger.info(f"Total configurations: {len(all_configs)}")
        logger.info(f"Architectures: {', '.join(architectures)}")
        logger.info(f"TP values: {tp_values}")
        if 'pd' in architectures:
            logger.info(f"PD ratios: {pd_ratios}")
        logger.info(f"Estimated total test time: {len(all_configs) * test_duration / 60:.1f} minutes")
        logger.info("=" * 70)

        return plan

    def to_dict(self, plan: OptimizationPlan) -> Dict:
        """Convert OptimizationPlan to dictionary."""
        plan_dict = asdict(plan)
        # Convert cluster_resources separately
        plan_dict['cluster_resources'] = asdict(plan.cluster_resources)
        return plan_dict


def main():
    """Main entry point for standalone execution."""
    import argparse
    import json

    parser = argparse.ArgumentParser(
        description='Generate InfeRecipe optimization plan'
    )
    parser.add_argument('--run-name', required=True, help='Name for this optimization run')
    parser.add_argument('--model', required=True, help='HuggingFace model name')
    parser.add_argument('--isl', type=int, required=True, help='Input sequence length')
    parser.add_argument('--osl', type=int, required=True, help='Output sequence length')
    parser.add_argument('--users', type=int, required=True, help='Number of concurrent users')
    parser.add_argument(
        '--goal',
        choices=['throughput', 'response_time', 'balanced'],
        default='balanced',
        help='Optimization goal'
    )
    parser.add_argument('--namespace', default='inferecipe', help='Kubernetes namespace')
    parser.add_argument('--output', help='Output JSON file path')

    args = parser.parse_args()

    # Generate plan
    generator = ConfigGenerator(namespace=args.namespace)
    plan = generator.generate_optimization_plan(
        run_name=args.run_name,
        model_name=args.model,
        isl=args.isl,
        osl=args.osl,
        num_users=args.users,
        optimization_goal=args.goal
    )

    # Output results
    plan_dict = generator.to_dict(plan)
    output_json = json.dumps(plan_dict, indent=2)

    if args.output:
        with open(args.output, 'w') as f:
            f.write(output_json)
        logger.info(f"Plan saved to {args.output}")
    else:
        print(output_json)


if __name__ == '__main__':
    main()
