"""
Web UI deployment system for InferRecipe.

Consolidates deployment orchestration, network integration, resource application,
and K8s resource generation used by the web interface. The CLI path uses
template_manager.py + Jinja2 templates instead.
"""

import json
import logging
import os
import time
import yaml
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Any, Optional

from .networking import (
    NetworkType, RDMAType, NetworkConfig,
    NADNetworkCreator, DRANetworkCreator, SharedDeviceNetworkCreator,
    compute_network_values,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

class Architecture(Enum):
    PD = "pd"
    AGGREGATED = "aggregated"
    EP = "ep"


@dataclass
class DeploymentConfig:
    test_id: str
    namespace: str
    architecture: Architecture

    model_name: str
    image: str

    tensor_parallelism: int
    gpu_memory_utilization: float = 0.95
    max_model_len: Optional[int] = None

    # PD-specific
    prefill_pods: int = 1
    decode_pods: int = 1
    prefill_tp: Optional[int] = None
    decode_tp: Optional[int] = None

    # EP-specific
    ep_pods: int = 1
    num_experts: int = 256

    # Aggregated-specific
    agg_pods: int = 1

    num_nics: int = 8

    pvc_name: str = "inferrecipe-model-cache"
    kv_connector: str = "NixlConnector"

    memory_request: str = "64Gi"
    memory_limit: str = "64Gi"
    cpu_request: str = "16"
    cpu_limit: Optional[str] = None

    extra_env: Dict[str, str] = field(default_factory=dict)
    extra_labels: Dict[str, str] = field(default_factory=dict)
    extra_annotations: Dict[str, str] = field(default_factory=dict)

    def __post_init__(self):
        if isinstance(self.architecture, str):
            self.architecture = Architecture(self.architecture)
        if self.architecture == Architecture.PD:
            if self.prefill_tp is None:
                self.prefill_tp = self.tensor_parallelism
            if self.decode_tp is None:
                self.decode_tp = self.tensor_parallelism


@dataclass
class NetworkIntegration:
    network_type: NetworkType
    resources: List[Any] = field(default_factory=list)
    pod_annotations: Dict[str, str] = field(default_factory=dict)
    pod_resource_claims: List[Dict[str, Any]] = field(default_factory=list)
    container_claims: List[Dict[str, str]] = field(default_factory=list)
    container_resources: Dict[str, Any] = field(default_factory=dict)
    device_plugin: str = ""
    network_names: List[str] = field(default_factory=list)
    description: str = ""


# ---------------------------------------------------------------------------
# Resource applier
# ---------------------------------------------------------------------------

class DeploymentApplier:

    def __init__(self, kubectl_runner):
        self.kubectl = kubectl_runner
        self.logger = logging.getLogger(__name__)

    def apply_resources(self, resources: List[Dict[str, Any]], namespace: Optional[str] = None) -> None:
        for resource in resources:
            self.apply_resource(resource, namespace)

    def apply_resource(self, resource: Dict[str, Any], namespace: Optional[str] = None) -> None:
        kind = resource.get('kind', 'Unknown')
        name = resource.get('metadata', {}).get('name', 'unknown')
        try:
            if namespace:
                resource.setdefault('metadata', {})['namespace'] = namespace
            resource_yaml = yaml.dump(resource)
            result = self.kubectl.run(['apply', '-f', '-'], input_data=resource_yaml, check=False)
            if result.returncode != 0:
                self.logger.error(f"Failed to apply {kind}/{name}: {result.stderr}")
                raise RuntimeError(f"Failed to apply {kind}/{name}")
            self.logger.info(f"Applied {kind}/{name}")
        except Exception as e:
            self.logger.error(f"Error applying {kind}/{name}: {e}")
            raise

    def wait_for_lws_ready(self, namespace: str, lws_name: str, timeout: int = 300) -> bool:
        self.logger.info(f"Waiting for LeaderWorkerSet {lws_name} to be ready...")
        start_time = time.time()
        while time.time() - start_time < timeout:
            try:
                result = self.kubectl.run(
                    ['get', 'leaderworkerset', lws_name, '-n', namespace, '-o', 'json'],
                    check=False,
                )
                if result.returncode == 0:
                    lws = json.loads(result.stdout)
                    status = lws.get('status', {})
                    replicas = status.get('replicas', 0)
                    ready = status.get('readyReplicas', 0)
                    if replicas > 0 and ready == replicas:
                        self.logger.info(f"LeaderWorkerSet {lws_name} is ready ({ready}/{replicas})")
                        return True
            except Exception:
                pass
            time.sleep(5)
        self.logger.warning(f"Timeout waiting for LeaderWorkerSet {lws_name}")
        return False

    def wait_for_pods_ready(self, namespace: str, label_selector: str, expected_count: int, timeout: int = 300) -> bool:
        self.logger.info(f"Waiting for {expected_count} pods with {label_selector} to be ready...")
        start_time = time.time()
        while time.time() - start_time < timeout:
            try:
                result = self.kubectl.run(
                    ['get', 'pods', '-n', namespace, '-l', label_selector, '-o', 'json'],
                    check=False,
                )
                if result.returncode == 0:
                    pods = json.loads(result.stdout).get('items', [])
                    ready_count = sum(
                        1 for pod in pods
                        for c in pod.get('status', {}).get('conditions', [])
                        if c.get('type') == 'Ready' and c.get('status') == 'True'
                    )
                    if ready_count >= expected_count:
                        self.logger.info(f"Pods ready: {ready_count}/{expected_count}")
                        return True
            except Exception:
                pass
            time.sleep(5)
        self.logger.warning("Timeout waiting for pods")
        return False

    def delete_resources(self, namespace: str, label_selector: str) -> None:
        for resource_type in ['leaderworkerset', 'deployment', 'service', 'configmap',
                              'networkattachmentdefinition', 'resourceclaimtemplate']:
            try:
                self.kubectl.run([
                    'delete', resource_type, '-n', namespace,
                    '-l', label_selector, '--ignore-not-found',
                ])
            except Exception as e:
                self.logger.warning(f"Error deleting {resource_type}: {e}")


# ---------------------------------------------------------------------------
# Network integrator
# ---------------------------------------------------------------------------

class NetworkIntegrator:

    def __init__(self, provider, kubectl_runner):
        self.provider = provider
        self.kubectl = kubectl_runner
        self.logger = logging.getLogger(__name__)

    def setup_network(self, namespace: str, num_nics: int, base_name: str = "rdma") -> NetworkIntegration:
        network_type = self._select_network_type()
        self.logger.info(f"Selected network type: {network_type.value}")

        network_creator = self._create_network_creator(network_type, num_nics)

        network_resources = network_creator.create_network_resources(
            namespace=namespace, base_name=base_name, num_resources=num_nics,
        )
        self.logger.info(f"Generated {len(network_resources)} network resources")

        self._apply_network_resources(network_resources)

        resource_names = [r.name for r in network_resources]
        provider_network = self.provider.profile.network

        return NetworkIntegration(
            network_type=network_type,
            resources=network_resources,
            pod_annotations=network_creator.get_pod_annotations(resource_names),
            pod_resource_claims=network_creator.get_pod_resource_claims(resource_names),
            container_claims=(
                network_creator.get_container_resource_claims(num_nics)
                if network_type == NetworkType.DRA else []
            ),
            container_resources=network_creator.get_resource_requirements(),
            device_plugin=getattr(provider_network, 'rdma_device_plugin', '') or '',
            network_names=resource_names,
            description=f"{network_type.value.upper()} network with {num_nics} NICs",
        )

    def _select_network_type(self) -> NetworkType:
        force_nad = os.getenv('INFER_RECIPE_FORCE_NAD', 'false').lower() == 'true'

        provider_id = self.provider.get_provider_id()
        if provider_id == 'ibm_cloud':
            if force_nad:
                self.logger.info("IBM Cloud: Forcing NAD via INFER_RECIPE_FORCE_NAD env var")
                return NetworkType.NAD
            self.logger.info("IBM Cloud: Defaulting to DRA (DRANET)")
            return NetworkType.DRA

        if provider_id == 'coreweave':
            self.logger.info("CoreWeave: Using SharedDevice (rdma/ib device plugin)")
            return NetworkType.SHARED_DEVICE

        if provider_id == 'baremetal':
            self.logger.info("Bare metal: Using NAD")
            return NetworkType.NAD

        self.logger.info("Unknown provider: Defaulting to NAD")
        return NetworkType.NAD

    def _create_network_creator(self, network_type: NetworkType, num_rails: int):
        provider_network = self.provider.profile.network
        config = NetworkConfig(
            network_type=network_type,
            rdma_type=self._parse_rdma_type(provider_network.rdma_type),
            rdma_enabled=provider_network.rdma_type != 'tcp',
            device_plugin=provider_network.rdma_device_plugin,
            mtu=9000,
            gateway=provider_network.config.get('gateway', '10.0.0.1'),
            num_rails=num_rails,
            ip_prefix='10.',
            pcie_affinity=True,
        )

        creators = {
            NetworkType.DRA: DRANetworkCreator,
            NetworkType.NAD: NADNetworkCreator,
            NetworkType.SHARED_DEVICE: SharedDeviceNetworkCreator,
        }
        cls = creators.get(network_type)
        if not cls:
            raise ValueError(f"Unsupported network type: {network_type}")
        return cls(config)

    def _parse_rdma_type(self, rdma_type_str: str) -> RDMAType:
        mapping = {
            'infiniband': RDMAType.INFINIBAND,
            'roce': RDMAType.ROCE,
            'virtio-roce': RDMAType.VIRTIO_ROCE,
            'tcp': RDMAType.TCP,
        }
        return mapping.get(rdma_type_str.lower(), RDMAType.TCP)

    def _apply_network_resources(self, network_resources: List) -> None:
        for resource in network_resources:
            try:
                resource_dict = resource.to_dict()
                yaml_manifest = yaml.dump(resource_dict, default_flow_style=False)
                namespace = resource_dict['metadata']['namespace']
                result = self.kubectl.run(
                    ['apply', '-f', '-', '-n', namespace],
                    input_data=yaml_manifest, check=False,
                )
                if result.returncode != 0:
                    error_msg = f"Failed to apply resource {resource.name}"
                    if result.stderr:
                        error_msg += f": {result.stderr}"
                    self.logger.error(error_msg)
                    raise RuntimeError(error_msg)
            except Exception as e:
                self.logger.error(f"Failed to apply {resource.name}: {e}")
                raise

    def cleanup_network(self, namespace: str, resource_names: List[str], network_type: NetworkType = None) -> None:
        for name in resource_names:
            try:
                if network_type == NetworkType.DRA:
                    self.kubectl.run(['delete', 'resourceclaimtemplate', name, '-n', namespace, '--ignore-not-found'])
                elif network_type == NetworkType.NAD:
                    self.kubectl.run(['delete', 'nad', name, '-n', namespace, '--ignore-not-found'])
                else:
                    self.kubectl.run(['delete', 'nad', name, '-n', namespace, '--ignore-not-found'], check=False)
                    self.kubectl.run(['delete', 'resourceclaimtemplate', name, '-n', namespace, '--ignore-not-found'], check=False)
            except Exception as e:
                self.logger.warning(f"Failed to delete {name}: {e}")


# ---------------------------------------------------------------------------
# Base generator
# ---------------------------------------------------------------------------

class BaseDeploymentGenerator(ABC):

    def __init__(self):
        self.logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

    @abstractmethod
    def generate(self, config: DeploymentConfig, network: NetworkIntegration) -> List[Dict[str, Any]]:
        pass

    def _apply_network_to_pod_spec(self, pod_spec: Dict[str, Any], network: NetworkIntegration) -> Dict[str, Any]:
        if network.pod_annotations:
            pod_spec.setdefault('metadata', {}).setdefault('annotations', {}).update(network.pod_annotations)

        if network.pod_resource_claims:
            pod_spec.setdefault('spec', {})['resourceClaims'] = network.pod_resource_claims

        if network.container_claims and 'spec' in pod_spec:
            for container in pod_spec['spec'].get('containers', []):
                container.setdefault('resources', {}).setdefault('claims', []).extend(network.container_claims)

        if network.container_resources and 'spec' in pod_spec:
            for container in pod_spec['spec'].get('containers', []):
                res = container.setdefault('resources', {})
                res.setdefault('limits', {}).update(network.container_resources)
                res.setdefault('requests', {}).update(network.container_resources)

        return pod_spec

    def _build_common_labels(self, config: DeploymentConfig) -> Dict[str, str]:
        labels = {
            'app': 'llm-d',
            'component': 'inferrecipe-test',
            'test-id': config.test_id,
            'architecture': config.architecture.value,
            'llm-d.ai/guide': f'inferrecipe-{config.architecture.value}',
        }
        labels.update(config.extra_labels)
        return labels

    def _build_common_annotations(self, config: DeploymentConfig) -> Dict[str, str]:
        annotations = {
            'llm-d.ai/test-id': config.test_id,
            'llm-d.ai/architecture': config.architecture.value,
        }
        annotations.update(config.extra_annotations)
        return annotations


# ---------------------------------------------------------------------------
# Prerequisite generator
# ---------------------------------------------------------------------------

class PrerequisiteGenerator:

    def __init__(self):
        self.logger = logging.getLogger(__name__)

    def generate_all(self, namespace: str, config: Dict[str, Any]) -> List[Dict[str, Any]]:
        resources = []
        resources.extend(self._generate_rbac(namespace))
        resources.append(self._generate_gateway(namespace, config))
        resources.extend(self._generate_gaie(namespace, config))
        resources.append(self._generate_hardware_discovery(namespace))
        resources.append(self._generate_model_cache_pvc(namespace, config))
        self.logger.info(f"Generated {len(resources)} prerequisite resources")
        return resources

    def _generate_rbac(self, namespace: str) -> List[Dict[str, Any]]:
        return [
            {
                'apiVersion': 'v1', 'kind': 'ServiceAccount',
                'metadata': {'name': 'llm-d-modelserver', 'namespace': namespace},
            },
            {
                'apiVersion': 'rbac.authorization.k8s.io/v1', 'kind': 'Role',
                'metadata': {'name': 'llm-d-modelserver', 'namespace': namespace},
                'rules': [
                    {'apiGroups': [''], 'resources': ['pods', 'services', 'endpoints'], 'verbs': ['get', 'list', 'watch']},
                    {'apiGroups': [''], 'resources': ['configmaps'], 'verbs': ['get', 'list', 'watch']},
                ],
            },
            {
                'apiVersion': 'rbac.authorization.k8s.io/v1', 'kind': 'RoleBinding',
                'metadata': {'name': 'llm-d-modelserver', 'namespace': namespace},
                'roleRef': {'apiGroup': 'rbac.authorization.k8s.io', 'kind': 'Role', 'name': 'llm-d-modelserver'},
                'subjects': [{'kind': 'ServiceAccount', 'name': 'llm-d-modelserver', 'namespace': namespace}],
            },
            {
                'apiVersion': 'v1', 'kind': 'ServiceAccount',
                'metadata': {'name': 'llm-d-gaie', 'namespace': namespace},
            },
            {
                'apiVersion': 'rbac.authorization.k8s.io/v1', 'kind': 'ClusterRole',
                'metadata': {'name': 'llm-d-gaie'},
                'rules': [
                    {'apiGroups': [''], 'resources': ['pods', 'services', 'endpoints'], 'verbs': ['get', 'list', 'watch']},
                    {'apiGroups': ['apps'], 'resources': ['deployments', 'replicasets'], 'verbs': ['get', 'list', 'watch']},
                    {'apiGroups': ['gateway.networking.k8s.io'], 'resources': ['httproutes', 'gateways'], 'verbs': ['get', 'list', 'watch', 'update', 'patch']},
                ],
            },
            {
                'apiVersion': 'rbac.authorization.k8s.io/v1', 'kind': 'ClusterRoleBinding',
                'metadata': {'name': f'llm-d-gaie-{namespace}'},
                'roleRef': {'apiGroup': 'rbac.authorization.k8s.io', 'kind': 'ClusterRole', 'name': 'llm-d-gaie'},
                'subjects': [{'kind': 'ServiceAccount', 'name': 'llm-d-gaie', 'namespace': namespace}],
            },
            {
                'apiVersion': 'rbac.authorization.k8s.io/v1', 'kind': 'RoleBinding',
                'metadata': {'name': 'inferrecipe-optimizer', 'namespace': namespace},
                'roleRef': {'apiGroup': 'rbac.authorization.k8s.io', 'kind': 'ClusterRole', 'name': 'edit'},
                'subjects': [{'kind': 'ServiceAccount', 'name': 'default', 'namespace': namespace}],
            },
        ]

    def _generate_gateway(self, namespace: str, config: Dict) -> Dict:
        return {
            'apiVersion': 'gateway.networking.k8s.io/v1', 'kind': 'Gateway',
            'metadata': {'name': 'llm-d-gateway', 'namespace': namespace},
            'spec': {
                'gatewayClassName': 'istio',
                'listeners': [{'name': 'http', 'protocol': 'HTTP', 'port': 8080,
                                'allowedRoutes': {'namespaces': {'from': 'Same'}}}],
            },
        }

    def _generate_gaie(self, namespace: str, config: Dict) -> List[Dict]:
        gaie_image = config.get('gaie_image', 'ghcr.io/llm-d/gaie:v0.5.0')
        return [
            {
                'apiVersion': 'apps/v1', 'kind': 'Deployment',
                'metadata': {'name': 'llm-d-gaie', 'namespace': namespace},
                'spec': {
                    'replicas': 1,
                    'selector': {'matchLabels': {'app': 'llm-d-gaie'}},
                    'template': {
                        'metadata': {'labels': {'app': 'llm-d-gaie'}},
                        'spec': {
                            'serviceAccountName': 'llm-d-gaie',
                            'containers': [{
                                'name': 'gaie', 'image': gaie_image,
                                'ports': [{'containerPort': 8080}],
                                'env': [
                                    {'name': 'LOG_LEVEL', 'value': 'info'},
                                    {'name': 'NAMESPACE', 'value': namespace},
                                ],
                            }],
                        },
                    },
                },
            },
            {
                'apiVersion': 'v1', 'kind': 'Service',
                'metadata': {'name': 'llm-d-gaie', 'namespace': namespace},
                'spec': {
                    'selector': {'app': 'llm-d-gaie'},
                    'ports': [{'protocol': 'TCP', 'port': 8080, 'targetPort': 8080}],
                },
            },
            {'apiVersion': 'v1', 'kind': 'ConfigMap',
             'metadata': {'name': 'llm-d-gaie-config-pd', 'namespace': namespace},
             'data': {'architecture': 'pd', 'routing_strategy': 'prefill_decode_split'}},
            {'apiVersion': 'v1', 'kind': 'ConfigMap',
             'metadata': {'name': 'llm-d-gaie-config-agg', 'namespace': namespace},
             'data': {'architecture': 'aggregated', 'routing_strategy': 'round_robin'}},
            {'apiVersion': 'v1', 'kind': 'ConfigMap',
             'metadata': {'name': 'llm-d-gaie-config-ep', 'namespace': namespace},
             'data': {'architecture': 'ep', 'routing_strategy': 'expert_routing'}},
            {
                'apiVersion': 'gaie.llm-d.ai/v1alpha1', 'kind': 'InferencePool',
                'metadata': {'name': 'llm-d-pool', 'namespace': namespace},
                'spec': {'selector': {'matchLabels': {'llm-d.ai/inference-serving': 'true'}}},
            },
        ]

    def _generate_hardware_discovery(self, namespace: str) -> Dict:
        discovery_script = (
            '#!/bin/bash\n'
            'while true; do\n'
            '  if [ -d /dev/infiniband ]; then\n'
            '    NCCL_IB_HCA=$(ls /dev/infiniband | grep -v uverbs | tr \'\\n\' \',\' | sed \'s/,$//\')\n'
            '    echo "Discovered HCAs: $NCCL_IB_HCA"\n'
            '    echo "export NCCL_IB_HCA=$NCCL_IB_HCA" > /tmp/ib_hca.env\n'
            '  fi\n'
            '  sleep 300\n'
            'done\n'
        )
        return {
            'apiVersion': 'apps/v1', 'kind': 'DaemonSet',
            'metadata': {'name': 'ib-hca-discovery', 'namespace': namespace},
            'spec': {
                'selector': {'matchLabels': {'app': 'ib-hca-discovery'}},
                'template': {
                    'metadata': {'labels': {'app': 'ib-hca-discovery'}},
                    'spec': {
                        'hostNetwork': True,
                        'containers': [{
                            'name': 'discovery',
                            'image': 'registry.access.redhat.com/ubi9/ubi-minimal:latest',
                            'command': ['/bin/bash', '-c'],
                            'args': [discovery_script],
                            'securityContext': {'privileged': True},
                            'volumeMounts': [
                                {'name': 'dev-infiniband', 'mountPath': '/dev/infiniband'},
                                {'name': 'sys', 'mountPath': '/sys', 'readOnly': True},
                            ],
                        }],
                        'volumes': [
                            {'name': 'dev-infiniband', 'hostPath': {'path': '/dev/infiniband'}},
                            {'name': 'sys', 'hostPath': {'path': '/sys'}},
                        ],
                    },
                },
            },
        }

    def _generate_model_cache_pvc(self, namespace: str, config: Dict) -> Dict:
        return {
            'apiVersion': 'v1', 'kind': 'PersistentVolumeClaim',
            'metadata': {'name': 'inferrecipe-model-cache', 'namespace': namespace},
            'spec': {
                'accessModes': ['ReadWriteMany'],
                'resources': {'requests': {'storage': config.get('cache_size', '500Gi')}},
            },
        }


# ---------------------------------------------------------------------------
# PD deployment generator
# ---------------------------------------------------------------------------

class PDDeploymentGenerator(BaseDeploymentGenerator):

    def generate(self, config: DeploymentConfig, network: NetworkIntegration) -> List[Dict[str, Any]]:
        resources = []
        resources.append(self._generate_prefill_lws(config, network))
        resources.append(self._generate_prefill_service(config))
        resources.append(self._generate_decode_lws(config, network))
        resources.append(self._generate_decode_service(config))
        self.logger.info(f"Generated PD deployment: {config.prefill_pods} prefill, {config.decode_pods} decode pods")
        return resources

    def _generate_prefill_lws(self, config: DeploymentConfig, network: NetworkIntegration) -> Dict[str, Any]:
        labels = self._build_common_labels(config)
        labels.update({'role': 'prefill', 'llm-d.ai/role': 'prefill', 'llm-d.ai/inference-serving': 'true'})

        pod_spec = {
            'serviceAccountName': 'llm-d-modelserver',
            'tolerations': self._build_tolerations(),
            'volumes': self._build_volumes(config),
            'containers': [self._build_container(config, network, role='prefill')],
        }

        affinity = self._build_affinity(network, anti_role='decode')
        if affinity:
            pod_spec['affinity'] = affinity

        pod_template = {
            'metadata': {'labels': labels, 'annotations': self._build_common_annotations(config)},
            'spec': pod_spec,
        }
        self._apply_network_to_pod_spec(pod_template, network)

        return {
            'apiVersion': 'leaderworkerset.x-k8s.io/v1', 'kind': 'LeaderWorkerSet',
            'metadata': {'name': f'{config.test_id}-prefill', 'namespace': config.namespace, 'labels': labels},
            'spec': {
                'replicas': config.prefill_pods,
                'leaderWorkerTemplate': {
                    'size': 1,
                    'leaderTemplate': pod_template,
                    'workerTemplate': {'metadata': pod_template['metadata'].copy(), 'spec': pod_template['spec'].copy()},
                },
            },
        }

    def _generate_decode_lws(self, config: DeploymentConfig, network: NetworkIntegration) -> Dict[str, Any]:
        labels = self._build_common_labels(config)
        labels.update({'role': 'decode', 'llm-d.ai/role': 'decode', 'llm-d.ai/inference-serving': 'true'})

        pod_spec = {
            'serviceAccountName': 'llm-d-modelserver',
            'tolerations': self._build_tolerations(),
            'volumes': self._build_volumes(config),
            'containers': [self._build_container(config, network, role='decode')],
        }

        affinity = self._build_affinity(network, anti_role='prefill')
        if affinity:
            pod_spec['affinity'] = affinity

        pod_template = {
            'metadata': {'labels': labels, 'annotations': self._build_common_annotations(config)},
            'spec': pod_spec,
        }
        self._apply_network_to_pod_spec(pod_template, network)

        return {
            'apiVersion': 'leaderworkerset.x-k8s.io/v1', 'kind': 'LeaderWorkerSet',
            'metadata': {'name': f'{config.test_id}-decode', 'namespace': config.namespace, 'labels': labels},
            'spec': {
                'replicas': config.decode_pods,
                'leaderWorkerTemplate': {
                    'size': 1,
                    'leaderTemplate': pod_template,
                    'workerTemplate': {'metadata': pod_template['metadata'].copy(), 'spec': pod_template['spec'].copy()},
                },
            },
        }

    def _build_container(self, config: DeploymentConfig, network: NetworkIntegration, role: str) -> Dict[str, Any]:
        tp = config.prefill_tp if role == 'prefill' else config.decode_tp
        vllm_args = self._build_vllm_command(config, role)
        device_plugin = getattr(network, 'device_plugin', '') or ''
        net_values = compute_network_values(network.network_type.value, [device_plugin] if device_plugin else [])

        resources = {
            'limits': {
                'memory': config.memory_limit,
                'cpu': config.cpu_limit or config.cpu_request,
                net_values['gpu_resource_key']: str(tp),
            },
            'requests': {
                'memory': config.memory_request,
                'cpu': config.cpu_request,
                net_values['gpu_resource_key']: str(tp),
            },
        }
        for r in net_values['extra_device_resources']:
            resources['limits'][r['key']] = r['value']
            resources['requests'][r['key']] = r['value']

        return {
            'name': 'vllm', 'image': config.image,
            'command': ['/bin/bash', '-c'], 'args': [vllm_args],
            'env': self._build_env_vars(config),
            'ports': [{'containerPort': 8000, 'name': 'http'}],
            'volumeMounts': [
                {'name': 'dshm', 'mountPath': '/dev/shm'},
                {'name': 'hf-cache', 'mountPath': '/root/.cache/huggingface'},
                {'name': 'hf-token', 'mountPath': '/root/.cache/huggingface/token', 'subPath': 'HF_TOKEN'},
                {'name': 'rdma-script', 'mountPath': '/scripts'},
                {'name': 'dev-infiniband', 'mountPath': '/dev/infiniband'},
            ],
            'resources': resources,
            'securityContext': {'capabilities': {'add': ['IPC_LOCK']}, 'privileged': True},
        }

    def _build_vllm_command(self, config: DeploymentConfig, role: str) -> str:
        tp = config.prefill_tp if role == 'prefill' else config.decode_tp
        kv_role = "kv_producer" if role == 'prefill' else "kv_consumer"

        cmd = f"""
ulimit -l unlimited || true

if [ -f /scripts/discover_ib_hca.sh ]; then
  source /scripts/discover_ib_hca.sh
  echo "Loaded RDMA environment: $NCCL_IB_HCA"
fi

echo "========================================="
echo "InferRecipe Test: {config.test_id}"
echo "Architecture: PD ({role.capitalize()})"
echo "Model: {config.model_name}"
echo "TP: {tp}"
echo "KV Role: {kv_role}"
echo "NCCL_IB_HCA: $NCCL_IB_HCA"
echo "========================================="

vllm serve {config.model_name} \\
  --port 8000 \\
  --tensor-parallel-size {tp} \\
  --block-size 128 \\
  --kv-transfer-config '{{"kv_connector":"{config.kv_connector}", "kv_role":"{kv_role}"}}' \\
  --disable-log-requests \\
  --disable-uvicorn-access-log \\
  --gpu-memory-utilization {config.gpu_memory_utilization} \\
  --trust-remote-code || sleep infinity
"""
        if config.max_model_len:
            cmd = cmd.replace('--trust-remote-code', f'--max-model-len {config.max_model_len} \\\n  --trust-remote-code')
        return cmd

    def _build_env_vars(self, config: DeploymentConfig) -> List[Dict[str, Any]]:
        env_vars = [
            {'name': 'VLLM_NIXL_SIDE_CHANNEL_HOST', 'valueFrom': {'fieldRef': {'fieldPath': 'status.podIP'}}},
            {'name': 'VLLM_NIXL_SIDE_CHANNEL_PORT', 'value': '14100'},
            {'name': 'NCCL_DEBUG', 'value': 'INFO'},
            {'name': 'NCCL_IB_DISABLE', 'value': '0'},
            {'name': 'NCCL_SOCKET_IFNAME', 'value': 'eth0'},
        ]
        for key, value in config.extra_env.items():
            env_vars.append({'name': key, 'value': value})
        return env_vars

    def _build_affinity(self, network: NetworkIntegration, anti_role: str) -> Optional[Dict[str, Any]]:
        device_plugin = getattr(network, 'device_plugin', '') or ''
        net_values = compute_network_values(network.network_type.value, [device_plugin] if device_plugin else [])
        if not net_values['use_anti_affinity']:
            return None
        return {
            'podAntiAffinity': {
                'requiredDuringSchedulingIgnoredDuringExecution': [{
                    'labelSelector': {'matchExpressions': [
                        {'key': 'llm-d.ai/inference-serving', 'operator': 'In', 'values': ['true']},
                        {'key': 'llm-d.ai/role', 'operator': 'In', 'values': [anti_role]},
                    ]},
                    'topologyKey': 'kubernetes.io/hostname',
                }],
            },
        }

    def _build_tolerations(self) -> List[Dict[str, Any]]:
        return [
            {'key': 'node.kubernetes.io/disk-pressure', 'operator': 'Exists', 'effect': 'NoSchedule'},
            {'key': 'node.kubernetes.io/memory-pressure', 'operator': 'Exists', 'effect': 'NoSchedule'},
        ]

    def _build_volumes(self, config: DeploymentConfig) -> List[Dict[str, Any]]:
        return [
            {'name': 'dshm', 'emptyDir': {'medium': 'Memory', 'sizeLimit': '2Gi'}},
            {'name': 'hf-cache', 'persistentVolumeClaim': {'claimName': config.pvc_name}},
            {'name': 'hf-token', 'secret': {'secretName': 'llm-d-hf-token', 'optional': True}},
            {'name': 'rdma-script', 'configMap': {'name': 'rdma-discovery-script', 'defaultMode': 0o755}},
            {'name': 'dev-infiniband', 'hostPath': {'path': '/dev/infiniband'}},
        ]

    def _generate_prefill_service(self, config: DeploymentConfig) -> Dict[str, Any]:
        return {
            'apiVersion': 'v1', 'kind': 'Service',
            'metadata': {'name': f'{config.test_id}-prefill', 'namespace': config.namespace,
                         'labels': self._build_common_labels(config)},
            'spec': {
                'selector': {'test-id': config.test_id, 'role': 'prefill'},
                'ports': [{'protocol': 'TCP', 'port': 8000, 'targetPort': 8000, 'name': 'http'}],
                'type': 'ClusterIP',
            },
        }

    def _generate_decode_service(self, config: DeploymentConfig) -> Dict[str, Any]:
        return {
            'apiVersion': 'v1', 'kind': 'Service',
            'metadata': {'name': f'{config.test_id}-decode', 'namespace': config.namespace,
                         'labels': self._build_common_labels(config)},
            'spec': {
                'selector': {'test-id': config.test_id, 'role': 'decode'},
                'ports': [{'protocol': 'TCP', 'port': 8000, 'targetPort': 8000, 'name': 'http'}],
                'type': 'ClusterIP',
            },
        }


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

class DeploymentOrchestrator:

    def __init__(self, kubectl_runner):
        self.kubectl = kubectl_runner
        self.applier = DeploymentApplier(kubectl_runner)
        self.logger = logging.getLogger(__name__)

    def deploy(self, test_config: Dict[str, Any]) -> str:
        config = self._parse_config(test_config)

        self.logger.info(f"Deploying test: {config.test_id}")
        self.logger.info(f"Architecture: {config.architecture.value}")

        provider = self._detect_provider()
        self.logger.info(f"Provider: {provider.get_display_name()}")

        network = self._setup_network(provider, config)
        self.logger.info(f"Network type: {network.network_type.value}")

        self._deploy_prerequisites(config, test_config)

        deployment_resources = self._generate_deployment(config, network)
        self.logger.info(f"Generated {len(deployment_resources)} deployment resources")

        self.applier.apply_resources(deployment_resources)

        self._wait_for_ready(config)

        self.logger.info(f"Deployment complete: {config.test_id}")
        return config.test_id

    def _parse_config(self, test_config: Dict[str, Any]) -> DeploymentConfig:
        return DeploymentConfig(
            test_id=test_config['test_id'],
            namespace=test_config['namespace'],
            architecture=Architecture(test_config['architecture']),
            model_name=test_config['model_name'],
            image=test_config['image'],
            tensor_parallelism=test_config.get('tensor_parallelism', 1),
            prefill_pods=test_config.get('prefill_pods', 1),
            decode_pods=test_config.get('decode_pods', 1),
            prefill_tp=test_config.get('prefill_tp'),
            decode_tp=test_config.get('decode_tp'),
            ep_pods=test_config.get('ep_pods', 1),
            num_experts=test_config.get('num_experts', 256),
            agg_pods=test_config.get('agg_pods', 1),
            num_nics=test_config.get('num_nics', test_config.get('tensor_parallelism', 1)),
            pvc_name=test_config.get('pvc_name', 'inferrecipe-model-cache'),
            kv_connector=test_config.get('kv_connector', 'NixlConnector'),
            gpu_memory_utilization=test_config.get('gpu_memory_utilization', 0.95),
            max_model_len=test_config.get('max_model_len'),
            memory_request=test_config.get('memory_request', '64Gi'),
            memory_limit=test_config.get('memory_limit', '64Gi'),
            cpu_request=test_config.get('cpu_request', '16'),
            cpu_limit=test_config.get('cpu_limit'),
            extra_env=test_config.get('extra_env', {}),
            extra_labels=test_config.get('extra_labels', {}),
            extra_annotations=test_config.get('extra_annotations', {}),
        )

    def _detect_provider(self):
        from .providers import ProviderRegistry
        return ProviderRegistry.detect_provider(self.kubectl)

    def _setup_network(self, provider, config: DeploymentConfig):
        integrator = NetworkIntegrator(provider, self.kubectl)
        return integrator.setup_network(
            namespace=config.namespace,
            num_nics=config.num_nics,
            base_name=f"{config.test_id}-rdma",
        )

    def _deploy_prerequisites(self, config: DeploymentConfig, test_config: Dict[str, Any]) -> None:
        if self._prereqs_exist(config.namespace):
            self.logger.info("Prerequisites already deployed, skipping")
            return
        self.logger.info("Deploying prerequisites...")
        prereq_gen = PrerequisiteGenerator()
        prereq_resources = prereq_gen.generate_all(namespace=config.namespace, config=test_config)
        self.applier.apply_resources(prereq_resources)
        self.logger.info("Prerequisites deployed")

    def _prereqs_exist(self, namespace: str) -> bool:
        result = self.kubectl.run(
            ['get', 'sa', 'llm-d-modelserver', '-n', namespace, '--ignore-not-found'],
            check=False,
        )
        return bool(result.stdout.strip())

    def _generate_deployment(self, config: DeploymentConfig, network) -> list:
        if config.architecture == Architecture.PD:
            generator = PDDeploymentGenerator()
        elif config.architecture == Architecture.AGGREGATED:
            raise NotImplementedError("Aggregated architecture not yet implemented")
        elif config.architecture == Architecture.EP:
            raise NotImplementedError("EP architecture not yet implemented")
        else:
            raise ValueError(f"Unknown architecture: {config.architecture}")
        return generator.generate(config, network)

    def _wait_for_ready(self, config: DeploymentConfig) -> None:
        self.logger.info(f"Waiting for {config.test_id} to be ready...")
        if config.architecture == Architecture.PD:
            prefill_ready = self.applier.wait_for_lws_ready(
                namespace=config.namespace, lws_name=f"{config.test_id}-prefill", timeout=300,
            )
            decode_ready = self.applier.wait_for_lws_ready(
                namespace=config.namespace, lws_name=f"{config.test_id}-decode", timeout=300,
            )
            if not (prefill_ready and decode_ready):
                raise RuntimeError(f"Deployment {config.test_id} failed to become ready")
        else:
            total_pods = config.agg_pods if config.architecture == Architecture.AGGREGATED else config.ep_pods
            ready = self.applier.wait_for_pods_ready(
                namespace=config.namespace,
                label_selector=f"test-id={config.test_id}",
                expected_count=total_pods,
                timeout=300,
            )
            if not ready:
                raise RuntimeError(f"Deployment {config.test_id} failed to become ready")
        self.logger.info(f"Deployment {config.test_id} is ready!")

    def cleanup(self, namespace: str, test_id: str) -> None:
        self.logger.info(f"Cleaning up test: {test_id}")
        self.applier.delete_resources(namespace=namespace, label_selector=f"test-id={test_id}")
        self.logger.info(f"Cleanup complete: {test_id}")
