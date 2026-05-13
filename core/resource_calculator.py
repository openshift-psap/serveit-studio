"""
Resource calculator for pod CPU and memory allocation.

Computes per-pod resources based on cluster state, tensor parallelism,
and total pod count. Used by config_generator and template_values.
"""

import math
import logging
from typing import Tuple, Optional

logger = logging.getLogger(__name__)


def calculate_pod_resources(
    tp: int,
    total_pods: int,
    cluster_resources=None
) -> Tuple[str, str]:
    """
    Calculate memory and CPU per pod for a deployment.

    Divides node resources proportionally:
      pods_per_node = ceil(total_pods / num_gpu_nodes)
      memory = (node_memory * 0.85) / pods_per_node
      cpu    = (node_cpus   * 0.80) / pods_per_node

    Args:
        tp: Tensor parallelism for this deployment
        total_pods: Total number of pods being deployed
        cluster_resources: ClusterResources from system_scanner

    Returns:
        (memory_str, cpu_str) e.g. ("64Gi", "16")
    """
    if not cluster_resources:
        logger.warning("No cluster resources, using defaults: 64Gi / 16 CPU")
        return '64Gi', '16'

    gpu_nodes = [n for n in cluster_resources.nodes if n.gpus > 0]
    if not gpu_nodes:
        logger.warning("No GPU nodes found, using defaults: 64Gi / 16 CPU")
        return '64Gi', '16'

    num_gpu_nodes = len(gpu_nodes)
    max_gpus_per_node = max(n.gpus for n in gpu_nodes)

    pods_from_deployment = math.ceil(total_pods / num_gpu_nodes)
    pods_from_tp = max_gpus_per_node // tp if tp > 0 else 1
    pods_per_node = max(pods_from_deployment, pods_from_tp, 1)

    avg_node_memory_gb = sum(n.memory_gb for n in gpu_nodes) / num_gpu_nodes
    system_reserve_gb = max(avg_node_memory_gb * 0.15, 16)
    usable_memory_gb = avg_node_memory_gb - system_reserve_gb
    memory_per_pod_gb = max(1, int(usable_memory_gb / pods_per_node))
    mem_str = f"{memory_per_pod_gb}Gi"

    avg_node_cpus = sum(n.cpu_cores for n in gpu_nodes) / num_gpu_nodes
    system_reserve_cpus = max(avg_node_cpus * 0.20, 4)
    usable_cpus = avg_node_cpus - system_reserve_cpus
    cpus_per_pod = max(1, int(usable_cpus / pods_per_node))
    cpu_str = str(max(cpus_per_pod, 1))

    logger.info(
        f"Resource calculation: {total_pods} pods, TP={tp}, "
        f"{num_gpu_nodes} GPU nodes → {pods_per_node} pods/node → "
        f"{mem_str} memory, {cpu_str} CPUs per pod"
    )

    return mem_str, cpu_str
