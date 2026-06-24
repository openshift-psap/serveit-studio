"""
ServeIt Studio Prerequisite Manager

Manages deployment of prerequisite infrastructure (GAIE, Gateway, etc.)
"""

import logging
import time
from typing import Optional, Dict
from .k8s_utils import KubectlRunner
from .template_manager import TemplateManager

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class PrereqManager:
    """Manages prerequisite infrastructure deployment."""

    def __init__(self, namespace: str = 'serveit', kubeconfig: Optional[str] = None,
                 kubectl_runner: Optional[KubectlRunner] = None,
                 scheduler_image: Optional[str] = None,
                 gateway_class: Optional[str] = None):
        """
        Initialize PrereqManager.

        Args:
            namespace: Kubernetes namespace
            kubeconfig: Path to kubeconfig file
            kubectl_runner: Existing KubectlRunner to reuse (creates new if None)
            scheduler_image: Custom EPP scheduler image (default: llm-d-inference-scheduler:v0.7.1)
        """
        self.namespace = namespace
        self.kubectl = kubectl_runner or KubectlRunner(kubeconfig=kubeconfig, namespace=namespace)
        self.template_mgr = TemplateManager()
        self.scheduler_image = scheduler_image
        self.gateway_class = gateway_class or self._detect_gateway_class()

    def _detect_gateway_class(self) -> str:
        """Detect the best available GatewayClass from the cluster."""
        try:
            r = self.kubectl.run(['get', 'gatewayclass', '-o',
                                  'jsonpath={range .items[*]}{.metadata.name}{"\\t"}'
                                  '{.status.conditions[0].status}{"\\n"}{end}'], check=False)
            if r.returncode == 0 and r.stdout.strip():
                classes = {}
                for line in r.stdout.strip().splitlines():
                    parts = line.strip().split('\t')
                    if len(parts) == 2:
                        classes[parts[0]] = parts[1]
                for preferred in ('istio', 'data-science-gateway-class', 'openshift-default'):
                    if classes.get(preferred) == 'True':
                        return preferred
                for name, accepted in classes.items():
                    if accepted == 'True':
                        return name
        except Exception:
            pass
        return 'istio'

    def check_prereqs_exist(self, gaie_name: str = 'gaie-pd-epp',
                           pool_name: str = 'gaie-pd',
                           gateway_name: str = 'infra-pd-inference-gateway') -> Dict[str, bool]:
        """
        Check which prerequisite resources already exist.

        Args:
            gaie_name: Name of GAIE deployment/service
            pool_name: Name of InferencePool
            gateway_name: Name of Gateway

        Returns:
            Dict with resource names and existence status
        """
        status = {}

        # Check for Gateway
        result = self.kubectl.run(
            ['get', 'gateway', gateway_name, '-n', self.namespace],
            check=False
        )
        status['gateway'] = result.returncode == 0

        # Check for GAIE deployment
        result = self.kubectl.run(
            ['get', 'deployment', gaie_name, '-n', self.namespace],
            check=False
        )
        status['gaie_deployment'] = result.returncode == 0

        # Check for InferencePool
        result = self.kubectl.run(
            ['get', 'inferencepool', pool_name, '-n', self.namespace],
            check=False
        )
        status['inferencepool'] = result.returncode == 0

        # Check for Service
        result = self.kubectl.run(
            ['get', 'service', gaie_name, '-n', self.namespace],
            check=False
        )
        status['gaie_service'] = result.returncode == 0

        return status

    def deploy_prereqs(self, architecture: str = 'aggregated', log_callback=None,
                       epp_config: dict = None, optimizer_config=None) -> bool:
        """
        Deploy prerequisite infrastructure.

        Args:
            architecture: 'aggregated', 'ep', or 'pd'
            log_callback: Optional callback for logging
            epp_config: EPP scoring configuration (preset, plugins, weights, parameters)

        Returns:
            True if deployment succeeded
        """
        def log(msg):
            if log_callback:
                log_callback(msg)
            logger.info(msg)

        try:
            # Ensure pull secret exists in namespace (needed for Red Hat registry images)
            try:
                r = self.kubectl.run(['get', 'secret', 'rhaii-pull-secret', '-n', self.namespace], check=False)
                if r.returncode != 0:
                    # Try to copy from istio-system
                    r2 = self.kubectl.run(['get', 'secret', 'rhaii-pull-secret', '-n', 'istio-system', '-o', 'json'], check=False)
                    if r2.returncode == 0 and r2.stdout.strip():
                        import json as _json
                        secret = _json.loads(r2.stdout)
                        secret['metadata'] = {'name': 'rhaii-pull-secret', 'namespace': self.namespace}
                        self.kubectl.run(['apply', '-f', '-'], input_data=_json.dumps(secret), check=False)
                        log('   ✓ Copied pull secret from istio-system')
            except Exception:
                pass

            log(f'🔍 Checking prerequisite infrastructure for {architecture} architecture...')

            # Architecture-specific names and config
            arch_config = {
                'aggregated': {
                    'gaie_name': 'gaie-aggregated-epp',
                    'gaie_pool_name': 'gaie-aggregated',
                    'config_file': 'aggregated-config.yaml',
                    'gateway_name': 'infra-aggregated-inference-gateway'
                },
                'ep': {
                    'gaie_name': 'gaie-ep-epp',
                    'gaie_pool_name': 'gaie-ep',
                    'config_file': 'ep-config.yaml',
                    'gateway_name': 'infra-ep-inference-gateway'
                },
                'pd': {
                    'gaie_name': 'gaie-pd-epp',
                    'gaie_pool_name': 'gaie-pd',
                    'config_file': 'pd-config.yaml',
                    'gateway_name': 'infra-pd-inference-gateway'
                }
            }

            if architecture not in arch_config:
                log(f'❌ Unknown architecture: {architecture}. Must be aggregated, ep, or pd')
                return False

            config = arch_config[architecture]

            status = self.check_prereqs_exist(config['gaie_name'], config['gaie_pool_name'], config['gateway_name'])

            if all(status.values()):
                log(f'✅ All prerequisites for {architecture} already deployed')
                # Ensure RDMA configmap and network resources exist even on the fast path
                context = {'namespace': self.namespace}
                self._ensure_rdma_discovery(context, log)
                if optimizer_config:
                    self._ensure_network_resources(optimizer_config, log)
                if self._check_prereqs_ready(config['gaie_name'], log_callback=log):
                    return True
                log(f'   ⏳ Waiting for GAIE deployment to become ready...')
                return self._wait_for_deployment_ready(config['gaie_name'], timeout=60, log_callback=log)

            log(f'📦 Deploying prerequisite infrastructure for {architecture} architecture...')

            # Resolve EPP plugin weights from preset
            epp = epp_config or {}
            epp_preset = epp.get('preset', 'balanced')
            epp_presets = {
                'balanced': {'prefix_cache_weight': 3.0, 'kv_cache_weight': 2.0, 'queue_weight': 2.0, 'active_request_enabled': True, 'active_request_weight': 2.0, 'decode_prefix_cache_weight': 1.0, 'decode_active_request_weight': 3.0, 'slo_enabled': False, 'slo_weight': 0},
                'cache_optimized': {'prefix_cache_weight': 5.0, 'kv_cache_weight': 1.0, 'queue_weight': 2.0, 'active_request_enabled': True, 'active_request_weight': 1.0, 'decode_prefix_cache_weight': 1.0, 'decode_active_request_weight': 3.0, 'slo_enabled': False, 'slo_weight': 0},
                'queue_balanced': {'prefix_cache_weight': 1.0, 'kv_cache_weight': 1.0, 'queue_weight': 3.0, 'active_request_enabled': True, 'active_request_weight': 3.0, 'decode_prefix_cache_weight': 1.0, 'decode_active_request_weight': 3.0, 'slo_enabled': False, 'slo_weight': 0},
                'latency_aware': {'prefix_cache_weight': 3.0, 'kv_cache_weight': 2.0, 'queue_weight': 2.0, 'active_request_enabled': True, 'active_request_weight': 2.0, 'decode_prefix_cache_weight': 1.0, 'decode_active_request_weight': 3.0, 'slo_enabled': True, 'slo_weight': 3.0},
            }
            if epp_preset == 'custom' and epp.get('plugins'):
                plugins = epp['plugins']
                epp_weights = {
                    'prefix_cache_weight': plugins.get('prefix_cache', {}).get('weight', 3.0) if plugins.get('prefix_cache', {}).get('enabled', True) else 0,
                    'kv_cache_weight': plugins.get('kv_cache', {}).get('weight', 2.0) if plugins.get('kv_cache', {}).get('enabled', True) else 0,
                    'queue_weight': plugins.get('queue', {}).get('weight', 2.0) if plugins.get('queue', {}).get('enabled', True) else 0,
                    'slo_enabled': plugins.get('slo', {}).get('enabled', False),
                    'slo_weight': plugins.get('slo', {}).get('weight', 3.0) if plugins.get('slo', {}).get('enabled', False) else 0,
                    'precise_prefix_cache_enabled': plugins.get('precise_prefix_cache', {}).get('enabled', False),
                    'precise_prefix_cache_weight': plugins.get('precise_prefix_cache', {}).get('weight', 3.0) if plugins.get('precise_prefix_cache', {}).get('enabled', False) else 0,
                    'active_request_enabled': plugins.get('active_request', {}).get('enabled', False),
                    'active_request_weight': plugins.get('active_request', {}).get('weight', 2.0) if plugins.get('active_request', {}).get('enabled', False) else 0,
                    'no_hit_lru_enabled': plugins.get('no_hit_lru', {}).get('enabled', False),
                    'no_hit_lru_weight': plugins.get('no_hit_lru', {}).get('weight', 1.0) if plugins.get('no_hit_lru', {}).get('enabled', False) else 0,
                    'session_aware_enabled': plugins.get('session_aware', {}).get('enabled', False),
                    'session_aware_weight': plugins.get('session_aware', {}).get('weight', 2.0) if plugins.get('session_aware', {}).get('enabled', False) else 0,
                }
            else:
                epp_weights = epp_presets.get(epp_preset, epp_presets['balanced'])
                epp_weights.setdefault('precise_prefix_cache_enabled', False)
                epp_weights.setdefault('precise_prefix_cache_weight', 0)
                epp_weights.setdefault('active_request_enabled', False)
                epp_weights.setdefault('active_request_weight', 0)
                epp_weights.setdefault('no_hit_lru_enabled', False)
                epp_weights.setdefault('no_hit_lru_weight', 0)
                epp_weights.setdefault('session_aware_enabled', False)
                epp_weights.setdefault('session_aware_weight', 0)

            # Check if user wants llm-d default EPP (no custom config)
            epp_use_defaults = not epp.get('preset') or epp.get('preset') == 'default'

            # Template parameters
            context = {
                'namespace': self.namespace,
                'architecture': architecture,
                'gaie_name': config['gaie_name'],
                'gaie_pool_name': config['gaie_pool_name'],
                'config_file': 'default-plugins.yaml' if epp_use_defaults else config['config_file'],
                'gaie_replicas': 1,
                'gaie_image': self.scheduler_image or 'ghcr.io/llm-d/llm-d-inference-scheduler:v0.7.1',
                'gateway_name': config['gateway_name'],
                'gateway_class': self.gateway_class,
                'prefix_cache_weight': epp_weights['prefix_cache_weight'],
                'kv_cache_weight': epp_weights['kv_cache_weight'],
                'queue_weight': epp_weights['queue_weight'],
                'slo_enabled': epp_weights['slo_enabled'],
                'slo_weight': epp_weights.get('slo_weight', 3.0),
                'precise_prefix_cache_enabled': epp_weights['precise_prefix_cache_enabled'],
                'precise_prefix_cache_weight': epp_weights['precise_prefix_cache_weight'],
                'active_request_enabled': epp_weights['active_request_enabled'],
                'active_request_weight': epp_weights['active_request_weight'],
                'decode_prefix_cache_weight': epp_weights.get('decode_prefix_cache_weight', 1.0),
                'decode_active_request_weight': epp_weights.get('decode_active_request_weight', 3.0),
                'no_hit_lru_enabled': epp_weights['no_hit_lru_enabled'],
                'no_hit_lru_weight': epp_weights['no_hit_lru_weight'],
                'session_aware_enabled': epp_weights['session_aware_enabled'],
                'session_aware_weight': epp_weights['session_aware_weight'],
                'max_prefix_blocks': epp.get('maxPrefixBlocksToMatch', 256),
                'lru_capacity': epp.get('lruCapacityPerServer', 31250),
                'non_cached_tokens': epp.get('nonCachedTokens', 16),
            }

            # Label namespace for DRA webhook (if DRA is in use)
            if self.gateway_class != 'istio' or True:  # Always label — harmless if webhook isn't deployed
                self.kubectl.run(
                    ['label', 'namespace', self.namespace, 'dra.llm-d.io/webhook-enabled=true', '--overwrite'],
                    check=False)

            # Create modelserver ServiceAccount + RBAC (used by LWS test pods)
            self._ensure_modelserver_rbac(log)

            # Deploy RDMA discovery ConfigMap (used by vLLM pods for InfiniBand HCA detection)
            self._ensure_rdma_discovery(context, log)

            # Create network resources (NADs/SriovNetworks) based on user's network selection
            if optimizer_config:
                self._ensure_network_resources(optimizer_config, log)

            # Create per-node NFS PVCs if per-node storage is enabled
            if optimizer_config and getattr(optimizer_config, 'per_node_storage', False):
                node_nfs_pvcs = self._ensure_per_node_pvcs(optimizer_config, log)
                if node_nfs_pvcs:
                    optimizer_config.node_nfs_pvcs = node_nfs_pvcs

            # Deploy in order (RBAC -> ConfigMap -> Service -> Deployment -> InferencePool -> Gateway)
            configmap_template = f'prereq/gaie-configmap-{architecture}.yaml.j2'

            resources = [
                ('ServiceAccount', 'prereq/gaie-serviceaccount.yaml.j2'),
                ('Role', 'prereq/gaie-role.yaml.j2'),
                ('RoleBinding', 'prereq/gaie-rolebinding.yaml.j2'),
                ('ClusterRole', 'prereq/gaie-clusterrole.yaml.j2'),
                ('ClusterRoleBinding', 'prereq/gaie-clusterrolebinding.yaml.j2'),
            ]
            if epp_use_defaults:
                if architecture == 'pd':
                    resources.append(('ConfigMap', 'prereq/gaie-configmap-default-pd.yaml.j2'))
                    log('   Using llm-d default PD EPP configuration (prefill: prefix:2/queue:1, decode: prefix:2/queue:1)')
                else:
                    resources.append(('ConfigMap', 'prereq/gaie-configmap-default.yaml.j2'))
                    log('   Using llm-d default EPP configuration (queue:2, kv-cache:2, prefix-cache:3)')
            else:
                resources.append(('ConfigMap', configmap_template))
            resources += [
                ('Service', 'prereq/gaie-service.yaml.j2'),
                ('Deployment', 'prereq/gaie-deployment.yaml.j2'),
                ('InferencePool', 'prereq/gaie-inferencepool.yaml.j2'),
                ('Gateway', 'prereq/gateway.yaml.j2'),
                ('HTTPRoute', 'prereq/httproute.yaml.j2'),
                ('DestinationRule', 'prereq/gaie-destinationrule.yaml.j2'),
            ]

            for resource_type, template_path in resources:
                resource_name = context['gaie_name'] if 'gaie' in template_path else context.get('gateway_name', 'infra-pd-inference-gateway')

                # Always apply Deployment and ConfigMap so image/config changes take effect.
                # Skip-if-exists only for immutable resources (RBAC, networking).
                if resource_type not in ('Deployment', 'ConfigMap'):
                    check_cmd = None
                    if resource_type in ['ServiceAccount', 'Role', 'RoleBinding', 'Service']:
                        check_cmd = ['get', resource_type.lower(), resource_name, '-n', self.namespace]
                    elif resource_type in ['ClusterRole', 'ClusterRoleBinding']:
                        check_cmd = ['get', resource_type.lower(), f"{context['gaie_pool_name']}-{self.namespace}-epp"]
                    elif resource_type == 'InferencePool':
                        check_cmd = ['get', 'inferencepool', context['gaie_pool_name'], '-n', self.namespace]
                    elif resource_type == 'Gateway':
                        check_cmd = ['get', 'gateway', resource_name, '-n', self.namespace]
                    elif resource_type == 'HTTPRoute':
                        check_cmd = ['get', 'httproute', f'llm-d-{architecture}', '-n', self.namespace]
                    elif resource_type == 'DestinationRule':
                        check_cmd = ['get', 'destinationrule', context['gaie_name'], '-n', self.namespace]
                    if check_cmd:
                        result = self.kubectl.run(check_cmd, check=False)
                        if result.returncode == 0:
                            log(f'   ✓ {resource_type} already exists')
                            continue

                # Render template
                log(f'   Applying {resource_type}...')
                manifest = self.template_mgr.render_template(template_path, **context)

                # Apply manifest
                result = self.kubectl.run(['apply', '-f', '-', '-n', self.namespace], input_data=manifest)

                if result.returncode != 0:
                    log(f'❌ Failed to create {resource_type}: {result.stderr}')
                    return False

                log(f'   ✅ {resource_type} created')

            log('')
            log('⏳ Waiting for GAIE deployment to be ready...')

            # Wait for GAIE deployment
            ready = self._wait_for_deployment_ready(context['gaie_name'], timeout=300, log_callback=log)
            if not ready:
                log('❌ GAIE deployment did not become ready')
                return False

            # Wait for Gateway deployment
            # Istio creates {gateway_name}-istio in the same namespace
            # OpenShift gateway controller creates {gateway_name}-{class} in openshift-ingress
            if self.gateway_class == 'istio':
                gateway_deployment = f"{config['gateway_name']}-istio"
                log(f'⏳ Waiting for Gateway deployment ({gateway_deployment}) to be ready...')
                ready = self._wait_for_deployment_ready(gateway_deployment, timeout=300, log_callback=log)
                if not ready:
                    log('❌ Gateway deployment did not become ready')
                    return False
            else:
                # OpenShift-managed gateway — deployment is in openshift-ingress, always up
                log(f'✅ Gateway managed by OpenShift ({self.gateway_class}) — skipping deployment wait')

            log('✅ All prerequisite infrastructure deployed and ready')
            return True

        except Exception as e:
            log(f'❌ Failed to deploy prerequisites: {str(e)}')
            import traceback
            traceback.print_exc()
            return False

    def update_epp_config(self, architecture: str, epp_config: dict,
                          log_callback=None) -> bool:
        """Update EPP configmap and restart the EPP pod to apply changes.

        Args:
            architecture: 'aggregated', 'pd', or 'ep'
            epp_config: EPP config dict with preset, plugins, weights
            log_callback: Optional callback for logging

        Returns:
            True if update succeeded
        """
        def log(msg):
            if log_callback:
                log_callback(msg)

        arch_config = {
            'aggregated': {'gaie_name': 'gaie-aggregated-epp', 'config_file': 'aggregated-config.yaml'},
            'pd': {'gaie_name': 'gaie-pd-epp', 'config_file': 'pd-config.yaml'},
            'ep': {'gaie_name': 'gaie-ep-epp', 'config_file': 'ep-config.yaml'},
        }
        if architecture not in arch_config:
            return False

        config = arch_config[architecture]
        epp = epp_config or {}
        epp_preset = epp.get('preset', 'balanced')
        epp_presets = {
            'balanced': {'prefix_cache_weight': 3.0, 'kv_cache_weight': 2.0, 'queue_weight': 2.0, 'active_request_enabled': True, 'active_request_weight': 2.0, 'decode_prefix_cache_weight': 1.0, 'decode_active_request_weight': 3.0, 'slo_enabled': False, 'slo_weight': 0},
            'cache_optimized': {'prefix_cache_weight': 5.0, 'kv_cache_weight': 1.0, 'queue_weight': 2.0, 'active_request_enabled': True, 'active_request_weight': 1.0, 'decode_prefix_cache_weight': 1.0, 'decode_active_request_weight': 3.0, 'slo_enabled': False, 'slo_weight': 0},
            'queue_balanced': {'prefix_cache_weight': 1.0, 'kv_cache_weight': 1.0, 'queue_weight': 3.0, 'active_request_enabled': True, 'active_request_weight': 3.0, 'decode_prefix_cache_weight': 1.0, 'decode_active_request_weight': 3.0, 'slo_enabled': False, 'slo_weight': 0},
            'latency_aware': {'prefix_cache_weight': 3.0, 'kv_cache_weight': 2.0, 'queue_weight': 2.0, 'active_request_enabled': True, 'active_request_weight': 2.0, 'decode_prefix_cache_weight': 1.0, 'decode_active_request_weight': 3.0, 'slo_enabled': True, 'slo_weight': 3.0},
        }
        if epp_preset == 'custom' and epp.get('plugins'):
            plugins = epp['plugins']
            epp_weights = {
                'prefix_cache_weight': plugins.get('prefix_cache', {}).get('weight', 3.0) if plugins.get('prefix_cache', {}).get('enabled', True) else 0,
                'kv_cache_weight': plugins.get('kv_cache', {}).get('weight', 2.0) if plugins.get('kv_cache', {}).get('enabled', True) else 0,
                'queue_weight': plugins.get('queue', {}).get('weight', 2.0) if plugins.get('queue', {}).get('enabled', True) else 0,
                'slo_enabled': plugins.get('slo', {}).get('enabled', False),
                'precise_prefix_cache_enabled': plugins.get('precise_prefix_cache', {}).get('enabled', False),
                'precise_prefix_cache_weight': plugins.get('precise_prefix_cache', {}).get('weight', 3.0) if plugins.get('precise_prefix_cache', {}).get('enabled', False) else 0,
                'active_request_enabled': plugins.get('active_request', {}).get('enabled', False),
                'active_request_weight': plugins.get('active_request', {}).get('weight', 2.0) if plugins.get('active_request', {}).get('enabled', False) else 0,
                'no_hit_lru_enabled': plugins.get('no_hit_lru', {}).get('enabled', False),
                'no_hit_lru_weight': plugins.get('no_hit_lru', {}).get('weight', 1.0) if plugins.get('no_hit_lru', {}).get('enabled', False) else 0,
                'session_aware_enabled': plugins.get('session_aware', {}).get('enabled', False),
                'session_aware_weight': plugins.get('session_aware', {}).get('weight', 2.0) if plugins.get('session_aware', {}).get('enabled', False) else 0,
            }
        else:
            epp_weights = epp_presets.get(epp_preset, epp_presets['balanced'])
            epp_weights.setdefault('precise_prefix_cache_enabled', False)
            epp_weights.setdefault('precise_prefix_cache_weight', 0)
            epp_weights.setdefault('active_request_enabled', False)
            epp_weights.setdefault('active_request_weight', 0)
            epp_weights.setdefault('no_hit_lru_enabled', False)
            epp_weights.setdefault('no_hit_lru_weight', 0)
            epp_weights.setdefault('session_aware_enabled', False)
            epp_weights.setdefault('session_aware_weight', 0)

        context = {
            'namespace': self.namespace,
            'gaie_name': config['gaie_name'],
            'config_file': config['config_file'],
            'prefix_cache_weight': epp_weights['prefix_cache_weight'],
            'kv_cache_weight': epp_weights['kv_cache_weight'],
            'queue_weight': epp_weights['queue_weight'],
            'slo_enabled': epp_weights['slo_enabled'],
            'precise_prefix_cache_enabled': epp_weights['precise_prefix_cache_enabled'],
            'precise_prefix_cache_weight': epp_weights['precise_prefix_cache_weight'],
            'active_request_enabled': epp_weights['active_request_enabled'],
            'active_request_weight': epp_weights['active_request_weight'],
            'no_hit_lru_enabled': epp_weights['no_hit_lru_enabled'],
            'no_hit_lru_weight': epp_weights['no_hit_lru_weight'],
            'session_aware_enabled': epp_weights['session_aware_enabled'],
            'session_aware_weight': epp_weights['session_aware_weight'],
            'max_prefix_blocks': epp.get('maxPrefixBlocksToMatch', 256),
            'lru_capacity': epp.get('lruCapacityPerServer', 31250),
            'non_cached_tokens': epp.get('nonCachedTokens', 16),
        }

        try:
            template = f'prereq/gaie-configmap-{architecture}.yaml.j2'
            manifest = self.template_mgr.render_template(template, **context)
            result = self.kubectl.run(['apply', '-f', '-', '-n', self.namespace], input_data=manifest)
            if result.returncode != 0:
                log(f'❌ Failed to update EPP configmap: {result.stderr}')
                return False
            log(f'✅ EPP configmap updated ({epp_preset})')

            result = self.kubectl.run(
                ['rollout', 'restart', f'deployment/{config["gaie_name"]}', '-n', self.namespace],
                check=False
            )
            if result.returncode == 0:
                log(f'✅ EPP pod restarting...')
            else:
                log(f'⚠️  Could not restart EPP pod: {result.stderr}')

            import time
            # Wait for rollout to complete (new EPP pod ready with updated configmap)
            rollout_result = self.kubectl.run(
                ['rollout', 'status', f'deployment/{config["gaie_name"]}', '-n', self.namespace,
                 '--timeout=60s'], check=False
            )
            if rollout_result.returncode == 0:
                log(f'✅ EPP pod ready')
            else:
                log(f'⚠️  EPP rollout timeout — waiting 15s')
                time.sleep(15)
            time.sleep(5)
            return True
        except Exception as e:
            log(f'❌ Failed to update EPP config: {e}')
            return False

    def _ensure_modelserver_rbac(self, log_callback=None):
        """Create llm-d-modelserver ServiceAccount + Role + RoleBinding if missing."""
        def log(msg):
            if log_callback:
                log_callback(msg)

        context = {'namespace': self.namespace}

        resources = [
            ('ServiceAccount', 'prereq/modelserver-serviceaccount.yaml.j2',
             ['get', 'sa', 'llm-d-modelserver', '-n', self.namespace]),
            ('Role', 'prereq/modelserver-role.yaml.j2',
             ['get', 'role', 'llm-d-modelserver', '-n', self.namespace]),
            ('RoleBinding', 'prereq/modelserver-rolebinding.yaml.j2',
             ['get', 'rolebinding', 'llm-d-modelserver', '-n', self.namespace]),
        ]

        for resource_type, template_path, check_cmd in resources:
            result = self.kubectl.run(check_cmd, check=False)
            if result.returncode == 0:
                log(f'   ✓ {resource_type} llm-d-modelserver already exists')
                continue

            manifest = self.template_mgr.render_template(template_path, **context)
            result = self.kubectl.run(
                ['apply', '-f', '-'],
                input_data=manifest,
                check=False
            )
            if result.returncode != 0:
                log(f'   ❌ Failed to create {resource_type}: {result.stderr}')
                return
            log(f'   ✅ {resource_type} llm-d-modelserver created')

        # OpenShift: create SCC for modelserver (allows IPC_LOCK, SYS_RAWIO, runAsUser: 0)
        scc_check = self.kubectl.run(
            ['get', 'scc', f'llm-d-modelserver-scc-{self.namespace}'], check=False)
        if scc_check.returncode != 0:
            api_check = self.kubectl.run(['api-resources', '--api-group=security.openshift.io'], check=False)
            if api_check.returncode == 0 and 'securitycontextconstraints' in api_check.stdout:
                manifest = self.template_mgr.render_template('prereq/modelserver-scc.yaml.j2', **context)
                result = self.kubectl.run(['apply', '-f', '-'], input_data=manifest, check=False)
                if result.returncode == 0:
                    log(f'   ✅ SCC llm-d-modelserver created (OpenShift)')
                else:
                    log(f'   ⚠️  Failed to create SCC: {result.stderr[:100]}')

    def _ensure_rdma_discovery(self, context, log_callback=None):
        """Deploy rdma-discovery-script ConfigMap if missing."""
        def log(msg):
            if log_callback:
                log_callback(msg)

        result = self.kubectl.run(
            ['get', 'configmap', 'rdma-discovery-script', '-n', self.namespace],
            check=False
        )
        if result.returncode == 0:
            log('   ✓ ConfigMap rdma-discovery-script already exists')
            return

        manifest = self.template_mgr.render_template(
            'prereq/rdma-discovery-configmap.yaml.j2', **context
        )
        result = self.kubectl.run(
            ['apply', '-f', '-'],
            input_data=manifest,
            check=False
        )
        if result.returncode != 0:
            log(f'   ❌ Failed to create RDMA discovery ConfigMap: {result.stderr}')
            return
        log('   ✅ ConfigMap rdma-discovery-script created')

    def _ensure_network_resources(self, config, log_callback=None):
        """Create network resources (NADs/SriovNetworks) based on user's network selection.

        For SR-IOV: creates SriovNetwork CRs from selected policies.
        For NAD/NMState: creates NADs from selected NICs if they don't exist.
        For DRA/Shared Device/eth0: nothing to create.
        """
        def log(msg):
            if log_callback:
                log_callback(msg)

        network_type = getattr(config, 'network_type', None)
        if not network_type or network_type in ('eth0', 'dra', 'shared_device'):
            return

        if network_type == 'sriov_multinic':
            selected_policies = getattr(config, 'selected_sriov_policies', None)
            if selected_policies:
                from core.networking.sriov import ensure_sriov_networks
                same_subnet = getattr(config, 'sriov_same_subnet', False)
                annotation = ensure_sriov_networks(
                    self.kubectl, self.namespace,
                    policy_resource_names=selected_policies,
                    same_subnet=same_subnet,
                )
                if annotation:
                    config.rdma_network_annotation = annotation
                    log(f'   ✅ SR-IOV networks created for {len(selected_policies)} policies')
                else:
                    log('   ⚠️  SR-IOV network creation failed — using existing NADs if available')

        elif network_type in ('nad', 'nmstate'):
            # Check if user-selected NADs already exist
            annotation = getattr(config, 'rdma_network_annotation', None)
            if annotation:
                import json
                try:
                    nads = json.loads(annotation)
                    for nad in nads:
                        r = self.kubectl.run(
                            ['get', 'net-attach-def', nad['name'], '-n', nad.get('namespace', self.namespace)],
                            check=False
                        )
                        if r.returncode == 0:
                            log(f'   ✓ NAD {nad["name"]} already exists')
                        else:
                            log(f'   ⚠️  NAD {nad["name"]} not found — admin must create it')
                except Exception:
                    pass

    def _check_prereqs_ready(self, gaie_name: str, log_callback=None) -> bool:
        """Check if prerequisites are ready."""
        def log(msg):
            if log_callback:
                log_callback(msg)

        # Check GAIE deployment
        result = self.kubectl.run_json([
            'get', 'deployment', gaie_name, '-n', self.namespace, '-o', 'json'
        ])

        if not result:
            return False

        status = result.get('status', {})
        ready_replicas = status.get('readyReplicas', 0)
        replicas = status.get('replicas', 1)

        if ready_replicas == replicas:
            log('   ✅ GAIE deployment ready')
            return True
        else:
            log(f'   ⏳ GAIE deployment not ready ({ready_replicas}/{replicas})')
            return False

    def _wait_for_deployment_ready(self, deployment_name: str, timeout: int = 300, log_callback=None) -> bool:
        """
        Wait for a deployment to be ready, with image pull error detection.

        Args:
            deployment_name: Name of the deployment
            timeout: Max wait time in seconds
            log_callback: Optional callback for logging

        Returns:
            True if ready within timeout
        """
        def log(msg):
            if log_callback:
                log_callback(msg)

        start_time = time.time()

        while time.time() - start_time < timeout:
            # Check deployment status
            result = self.kubectl.run_json([
                'get', 'deployment', deployment_name, '-n', self.namespace, '-o', 'json'
            ])

            if result:
                status = result.get('status', {})
                ready_replicas = status.get('readyReplicas', 0)
                replicas = status.get('replicas', 1)

                if ready_replicas == replicas:
                    log(f'   ✅ {deployment_name} ready')
                    return True

                # Check pods for fatal errors (ImagePullBackOff, ErrImagePull)
                # Use the deployment's own selector to find its pods
                match_labels = result.get('spec', {}).get('selector', {}).get('matchLabels', {})
                label_selector = ','.join(f'{k}={v}' for k, v in match_labels.items())
                pod_result = self.kubectl.run([
                    'get', 'pods', '-n', self.namespace,
                    '-l', label_selector,
                    '-o', 'jsonpath={range .items[*]}{.status.containerStatuses[*].state.waiting.reason}{" "}{end}'
                ], check=False)

                if pod_result.returncode == 0:
                    waiting_reasons = pod_result.stdout.strip()
                    if 'ImagePullBackOff' in waiting_reasons or 'ErrImagePull' in waiting_reasons:
                        log(f'   ❌ {deployment_name} has image pull errors — check pull secrets and image availability')
                        return False

                elapsed = int(time.time() - start_time)
                log(f'   ⏳ Waiting for {deployment_name}... ({ready_replicas}/{replicas} ready) [{elapsed}s]')

            time.sleep(5)

        log(f'   ❌ Timeout waiting for {deployment_name} ({timeout}s)')
        return False

    def _ensure_per_node_pvcs(self, config, log_callback=None) -> list:
        """Create per-node NFS PVCs for per-node storage mode.

        Detects NFS storage classes matching GPU node suffixes, creates one PVC
        per node, and returns the mapping list for template rendering.
        """
        def log(msg):
            if log_callback:
                log_callback(msg)

        import json

        # Get GPU node names
        r = self.kubectl.run([
            'get', 'nodes', '-l', 'nvidia.com/gpu.present=true',
            '-o', 'jsonpath={range .items[*]}{.metadata.name}{"\\n"}{end}'
        ], check=False)
        if r.returncode != 0 or not r.stdout.strip():
            log('   ⚠️  No GPU nodes found for per-node storage')
            return []

        gpu_nodes = [n.strip() for n in r.stdout.strip().splitlines() if n.strip()]

        # Get NFS storage classes (nfs-<suffix> pattern)
        r = self.kubectl.run([
            'get', 'sc', '-o', 'jsonpath={range .items[*]}{.metadata.name}{"\\n"}{end}'
        ], check=False)
        if r.returncode != 0:
            return []

        nfs_classes = {}
        for sc in r.stdout.strip().splitlines():
            sc = sc.strip()
            if sc.startswith('nfs-') and sc != 'nfs':
                suffix = sc[4:]  # strip 'nfs-' prefix
                nfs_classes[suffix] = sc

        # Match GPU nodes to NFS storage classes by suffix
        node_nfs_pvcs = []
        pvc_size = getattr(config, 'pvc_size', None) or '200Gi'
        for node in gpu_nodes:
            matched_suffix = None
            for suffix in nfs_classes:
                if node.endswith(suffix):
                    matched_suffix = suffix
                    break
            if not matched_suffix:
                continue

            pvc_name = f"model-cache-{matched_suffix}"
            node_nfs_pvcs.append({'suffix': matched_suffix, 'pvc_name': pvc_name})

            # Check if PVC already exists
            r = self.kubectl.run(
                ['get', 'pvc', pvc_name, '-n', self.namespace], check=False)
            if r.returncode == 0:
                continue

            # Create PVC
            pvc_yaml = json.dumps({
                'apiVersion': 'v1',
                'kind': 'PersistentVolumeClaim',
                'metadata': {
                    'name': pvc_name,
                    'namespace': self.namespace,
                    'labels': {'app': 'serveit-cache', 'node-suffix': matched_suffix}
                },
                'spec': {
                    'accessModes': ['ReadWriteMany'],
                    'storageClassName': nfs_classes[matched_suffix],
                    'resources': {'requests': {'storage': pvc_size}}
                }
            })
            r = self.kubectl.run(
                ['apply', '-f', '-', '-n', self.namespace], input_data=pvc_yaml, check=False)
            if r.returncode == 0:
                log(f'   ✅ Created PVC {pvc_name} (nfs-{matched_suffix})')
            else:
                log(f'   ❌ Failed to create PVC {pvc_name}: {r.stderr}')

        if node_nfs_pvcs:
            log(f'   📦 Per-node NFS storage: {len(node_nfs_pvcs)} PVCs for {len(gpu_nodes)} GPU nodes')
        else:
            log('   ⚠️  No matching NFS storage classes found for GPU nodes')

        return node_nfs_pvcs

    def cleanup_prereqs(self, architecture: str = 'aggregated', log_callback=None) -> bool:
        """
        Clean up prerequisite infrastructure.

        Args:
            architecture: 'aggregated', 'ep', or 'pd'
            log_callback: Optional callback for logging

        Returns:
            True if cleanup succeeded
        """
        def log(msg):
            if log_callback:
                log_callback(msg)

        try:
            log(f'🧹 Cleaning up prerequisite infrastructure for {architecture} architecture...')

            # Architecture-specific names
            arch_config = {
                'aggregated': {
                    'gaie_name': 'gaie-aggregated-epp',
                    'gaie_pool_name': 'gaie-aggregated',
                    'gateway_name': 'infra-aggregated-inference-gateway'
                },
                'ep': {
                    'gaie_name': 'gaie-ep-epp',
                    'gaie_pool_name': 'gaie-ep',
                    'gateway_name': 'infra-ep-inference-gateway'
                },
                'pd': {
                    'gaie_name': 'gaie-pd-epp',
                    'gaie_pool_name': 'gaie-pd',
                    'gateway_name': 'infra-pd-inference-gateway'
                }
            }

            if architecture not in arch_config:
                log(f'❌ Unknown architecture: {architecture}')
                return False

            config = arch_config[architecture]

            # Delete in reverse order
            # Note: Optimizer RBAC is NOT cleaned up here (managed by optimizer deployment)
            resources = [
                ('DestinationRule', config['gaie_name']),
                ('HTTPRoute', f'llm-d-{architecture}'),
                ('Gateway', config['gateway_name']),
                ('InferencePool', config['gaie_pool_name']),
                ('Deployment', config['gaie_name']),
                ('Service', config['gaie_name']),
                ('ConfigMap', config['gaie_name']),
                ('ClusterRoleBinding', f"{config['gaie_pool_name']}-{self.namespace}-epp"),
                ('ClusterRole', f"{config['gaie_pool_name']}-{self.namespace}-epp"),
                ('RoleBinding', config['gaie_name']),
                ('Role', config['gaie_name']),
                ('ServiceAccount', config['gaie_name']),
                # Modelserver RBAC
                ('RoleBinding', 'llm-d-modelserver'),
                ('Role', 'llm-d-modelserver'),
                ('ServiceAccount', 'llm-d-modelserver'),
            ]

            for resource_type, resource_name in resources:
                cmd = ['delete', resource_type.lower(), resource_name, '--ignore-not-found=true']

                # Namespace-scoped vs cluster-scoped
                if resource_type not in ['ClusterRole', 'ClusterRoleBinding']:
                    cmd.extend(['-n', self.namespace])

                result = self.kubectl.run(cmd, check=False)

                if result.returncode == 0 and 'deleted' in result.stdout.lower():
                    log(f'   ✅ Deleted {resource_type}: {resource_name}')

            log('✅ Prerequisite cleanup complete')
            return True

        except Exception as e:
            log(f'❌ Cleanup failed: {str(e)}')
            return False


def main():
    """Main entry point for standalone execution."""
    import argparse

    parser = argparse.ArgumentParser(
        description='Manage ServeIt Studio prerequisite infrastructure'
    )
    parser.add_argument('action', choices=['deploy', 'check', 'cleanup'],
                        help='Action to perform')
    parser.add_argument('--namespace', default='serveit', help='Kubernetes namespace')
    parser.add_argument('--architecture', '-a', default='aggregated',
                        choices=['aggregated', 'ep', 'pd'],
                        help='Architecture type (default: aggregated)')

    args = parser.parse_args()

    manager = PrereqManager(namespace=args.namespace)

    if args.action == 'check':
        # Check for specific architecture
        arch_config = {
            'aggregated': ('gaie-aggregated-epp', 'gaie-aggregated', 'infra-aggregated-inference-gateway'),
            'ep': ('gaie-ep-epp', 'gaie-ep', 'infra-ep-inference-gateway'),
            'pd': ('gaie-pd-epp', 'gaie-pd', 'infra-pd-inference-gateway')
        }
        gaie_name, pool_name, gateway_name = arch_config[args.architecture]

        status = manager.check_prereqs_exist(gaie_name, pool_name, gateway_name)
        print(f"Prerequisite Status ({args.architecture} architecture):")
        for resource, exists in status.items():
            status_str = "✅ Exists" if exists else "❌ Missing"
            print(f"  {resource}: {status_str}")

        if all(status.values()):
            print("\n✅ All prerequisites deployed")
        else:
            print("\n⚠️  Some prerequisites are missing")

    elif args.action == 'deploy':
        success = manager.deploy_prereqs(architecture=args.architecture, log_callback=print)
        if success:
            print("\n✅ Deployment successful")
        else:
            print("\n❌ Deployment failed")

    elif args.action == 'cleanup':
        success = manager.cleanup_prereqs(architecture=args.architecture, log_callback=print)
        if success:
            print("\n✅ Cleanup successful")
        else:
            print("\n❌ Cleanup failed")


if __name__ == '__main__':
    main()
