"""
In-S8 Prerequisite Manager

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

    def __init__(self, namespace: str = 'llm-d', kubeconfig: Optional[str] = None,
                 kubectl_runner: Optional[KubectlRunner] = None):
        """
        Initialize PrereqManager.

        Args:
            namespace: Kubernetes namespace
            kubeconfig: Path to kubeconfig file
            kubectl_runner: Existing KubectlRunner to reuse (creates new if None)
        """
        self.namespace = namespace
        self.kubectl = kubectl_runner or KubectlRunner(kubeconfig=kubeconfig, namespace=namespace)
        self.template_mgr = TemplateManager()

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

    def deploy_prereqs(self, architecture: str = 'aggregated', log_callback=None) -> bool:
        """
        Deploy prerequisite infrastructure.

        Args:
            architecture: 'aggregated', 'ep', or 'pd'
            log_callback: Optional callback for logging

        Returns:
            True if deployment succeeded
        """
        def log(msg):
            if log_callback:
                log_callback(msg)
            logger.info(msg)

        try:
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
                return self._check_prereqs_ready(config['gaie_name'], log_callback=log)

            log(f'📦 Deploying prerequisite infrastructure for {architecture} architecture...')

            # Template parameters
            context = {
                'namespace': self.namespace,
                'architecture': architecture,
                'gaie_name': config['gaie_name'],
                'gaie_pool_name': config['gaie_pool_name'],
                'config_file': config['config_file'],
                'gaie_replicas': 1,
                'gaie_image': 'ghcr.io/llm-d/llm-d-inference-scheduler:v0.6.0',
                'gateway_name': config['gateway_name']
            }

            # Create modelserver ServiceAccount + RBAC (used by LWS test pods)
            self._ensure_modelserver_rbac(log)

            # Deploy RDMA discovery ConfigMap (used by vLLM pods for InfiniBand HCA detection)
            self._ensure_rdma_discovery(context, log)

            # Deploy in order (RBAC -> ConfigMap -> Service -> Deployment -> InferencePool -> Gateway)
            # Use architecture-specific ConfigMap template
            configmap_template = f'prereq/gaie-configmap-{architecture}.yaml.j2'

            resources = [
                # Note: Optimizer RBAC is deployed with the optimizer pod itself (deployment/in-s8-optimizer.yaml)
                # GAIE RBAC
                ('ServiceAccount', 'prereq/gaie-serviceaccount.yaml.j2'),
                ('Role', 'prereq/gaie-role.yaml.j2'),
                ('RoleBinding', 'prereq/gaie-rolebinding.yaml.j2'),
                ('ClusterRole', 'prereq/gaie-clusterrole.yaml.j2'),
                ('ClusterRoleBinding', 'prereq/gaie-clusterrolebinding.yaml.j2'),
                # GAIE deployment resources
                ('ConfigMap', configmap_template),
                ('Service', 'prereq/gaie-service.yaml.j2'),
                ('Deployment', 'prereq/gaie-deployment.yaml.j2'),
                ('InferencePool', 'prereq/gaie-inferencepool.yaml.j2'),
                ('Gateway', 'prereq/gateway.yaml.j2'),
                ('HTTPRoute', 'prereq/httproute.yaml.j2'),
                ('DestinationRule', 'prereq/gaie-destinationrule.yaml.j2'),
            ]

            for resource_type, template_path in resources:
                # Skip if already exists
                resource_name = context['gaie_name'] if 'gaie' in template_path else context.get('gateway_name', 'infra-pd-inference-gateway')

                # Check existence based on resource type
                check_cmd = None
                if resource_type in ['ServiceAccount', 'Role', 'RoleBinding', 'ConfigMap', 'Service', 'Deployment']:
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
                log(f'   Creating {resource_type}...')
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

            # Wait for Gateway pod (Istio creates a deployment named {gateway_name}-istio)
            gateway_deployment = f"{config['gateway_name']}-istio"
            log(f'⏳ Waiting for Gateway deployment ({gateway_deployment}) to be ready...')
            ready = self._wait_for_deployment_ready(gateway_deployment, timeout=300, log_callback=log)
            if not ready:
                log('❌ Gateway deployment did not become ready')
                return False

            log('✅ All prerequisite infrastructure deployed and ready')
            return True

        except Exception as e:
            log(f'❌ Failed to deploy prerequisites: {str(e)}')
            import traceback
            traceback.print_exc()
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
        description='Manage In-S8 prerequisite infrastructure'
    )
    parser.add_argument('action', choices=['deploy', 'check', 'cleanup'],
                        help='Action to perform')
    parser.add_argument('--namespace', default='llm-d', help='Kubernetes namespace')
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
