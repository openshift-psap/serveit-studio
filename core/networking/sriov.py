"""
SR-IOV Network creator for ServeIt Studio.

Creates SriovNetwork CRs in the openshift-sriov-network-operator namespace,
which triggers the SR-IOV operator to create NetworkAttachmentDefinitions
in the target namespace. Pods then reference the NAD via annotations.

Flow:
  1. Admin creates SriovNetworkNodePolicy (configures VFs on physical NICs)
  2. We detect available policies and their resourceName
  3. We create SriovNetwork CR → operator creates NAD in target namespace
  4. Pods reference NAD: k8s.v1.cni.cncf.io/networks: <nad-name>
"""

import json
import logging
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

SRIOV_OPERATOR_NS = 'openshift-sriov-network-operator'


def detect_sriov_policies(kubectl_runner) -> List[Dict]:
    """Detect available SriovNetworkNodePolicy resources.

    Returns list of policies with resourceName, nicSelector, and device info.
    """
    policies = []
    try:
        r = kubectl_runner.run(
            ['get', 'sriovnetworknodepolicies', '-n', SRIOV_OPERATOR_NS,
             '-o', 'json'], check=False)
        if r.returncode != 0:
            return []
        data = json.loads(r.stdout)
        for item in data.get('items', []):
            name = item['metadata']['name']
            if name == 'default':
                continue
            spec = item.get('spec', {})
            policies.append({
                'name': name,
                'resourceName': spec.get('resourceName', ''),
                'numVfs': spec.get('numVfs', 0),
                'mtu': spec.get('mtu', 1500),
                'isRdma': spec.get('isRdma', False),
                'deviceType': spec.get('deviceType', 'netdevice'),
                'vendor': spec.get('nicSelector', {}).get('vendor', ''),
                'deviceID': spec.get('nicSelector', {}).get('deviceID', ''),
            })
    except Exception as e:
        logger.warning(f"Failed to detect SR-IOV policies: {e}")
    return policies


def detect_existing_sriov_networks(kubectl_runner, target_namespace: str) -> List[Dict]:
    """Detect SriovNetwork CRs that target a specific namespace."""
    networks = []
    try:
        r = kubectl_runner.run(
            ['get', 'sriovnetwork', '-n', SRIOV_OPERATOR_NS,
             '-o', 'json'], check=False)
        if r.returncode != 0:
            return []
        data = json.loads(r.stdout)
        for item in data.get('items', []):
            spec = item.get('spec', {})
            net_ns = spec.get('networkNamespace', '')
            if net_ns == target_namespace:
                networks.append({
                    'name': item['metadata']['name'],
                    'resourceName': spec.get('resourceName', ''),
                    'networkNamespace': net_ns,
                })
    except Exception as e:
        logger.warning(f"Failed to detect SR-IOV networks: {e}")
    return networks


def create_sriov_network(
    kubectl_runner,
    name: str,
    resource_name: str,
    target_namespace: str,
    ipam_range: str = '192.168.100.0/24',
    mtu: int = 9000,
) -> bool:
    """Create a SriovNetwork CR that generates a NAD in the target namespace.

    Args:
        kubectl_runner: KubectlRunner instance
        name: Name for the SriovNetwork CR (also becomes the NAD name)
        resource_name: resourceName from SriovNetworkNodePolicy
        target_namespace: Namespace where the NAD should be created
        ipam_range: IP range for whereabouts IPAM
        mtu: MTU for the network

    Returns:
        True if created successfully
    """
    sriov_network = {
        'apiVersion': 'sriovnetwork.openshift.io/v1',
        'kind': 'SriovNetwork',
        'metadata': {
            'name': name,
            'namespace': SRIOV_OPERATOR_NS,
            'labels': {
                'app': 'serveit-studio',
                'managed-by': 'serveit',
            },
        },
        'spec': {
            'resourceName': resource_name,
            'networkNamespace': target_namespace,
            'ipam': json.dumps({
                'type': 'whereabouts',
                'range': ipam_range,
                'exclude': [
                    ipam_range.rsplit('.', 1)[0] + '.1',
                    ipam_range.rsplit('.', 1)[0] + '.255',
                ],
            }),
        },
    }

    yaml_str = json.dumps(sriov_network)
    r = kubectl_runner.run(
        ['apply', '-f', '-'],
        input=yaml_str, check=False)

    if r.returncode != 0:
        logger.error(f"Failed to create SriovNetwork {name}: {r.stderr}")
        return False

    logger.info(f"Created SriovNetwork {name} targeting namespace {target_namespace}")
    return True


def ensure_sriov_network(
    kubectl_runner,
    target_namespace: str,
    policy_resource_name: str = None,
) -> Optional[str]:
    """Ensure a SriovNetwork exists for the target namespace.

    If one already exists, returns its name. Otherwise creates one
    from the first available RDMA-capable policy.

    Returns the NAD name (same as SriovNetwork name), or None on failure.
    """
    existing = detect_existing_sriov_networks(kubectl_runner, target_namespace)
    if existing:
        logger.info(f"Found existing SriovNetwork for {target_namespace}: {existing[0]['name']}")
        return existing[0]['name']

    policies = detect_sriov_policies(kubectl_runner)
    rdma_policies = [p for p in policies if p['isRdma']]

    if not rdma_policies:
        logger.warning("No RDMA-capable SriovNetworkNodePolicy found")
        return None

    if policy_resource_name:
        policy = next((p for p in rdma_policies if p['resourceName'] == policy_resource_name), None)
        if not policy:
            logger.warning(f"Policy with resourceName={policy_resource_name} not found")
            policy = rdma_policies[0]
    else:
        policy = rdma_policies[0]

    nad_name = f"serveit-rdma-{target_namespace}"
    success = create_sriov_network(
        kubectl_runner,
        name=nad_name,
        resource_name=policy['resourceName'],
        target_namespace=target_namespace,
        mtu=policy.get('mtu', 9000),
    )

    if success:
        return nad_name
    return None
