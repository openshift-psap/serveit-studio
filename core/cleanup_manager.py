"""
InfeRecipe Cleanup Manager

Cleans up deployed test configurations by looking at database state.
"""

import logging
from typing import Optional, List
from .k8s_utils import KubectlRunner
from .deployment_manager import DeploymentManager

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class CleanupManager:
    """Manages cleanup of deployed test resources."""

    def __init__(self, namespace: str = 'inferecipe', kubeconfig: Optional[str] = None):
        """
        Initialize CleanupManager.

        Args:
            namespace: Kubernetes namespace
            kubeconfig: Path to kubeconfig file
        """
        self.namespace = namespace
        self.kubectl = KubectlRunner(kubeconfig=kubeconfig, namespace=namespace)
        self.deployment_manager = DeploymentManager(namespace=namespace, kubeconfig=kubeconfig)

    def cleanup_last_deployment(self, pods_deployed: List[str], log_callback=None) -> bool:
        """
        Clean up pods from last deployment.

        Args:
            pods_deployed: List of pod names or LWS names to delete
            log_callback: Optional callback for logging

        Returns:
            True if cleanup succeeded
        """
        if not pods_deployed:
            if log_callback:
                log_callback('⚠️ No pods to clean up')
            return True

        success = True
        for resource_name in pods_deployed:
            try:
                if log_callback:
                    log_callback(f'🗑️ Deleting {resource_name}...')

                # Try to delete as LeaderWorkerSet first
                result = self.kubectl.run(
                    ['delete', 'leaderworkerset', resource_name, '-n', self.namespace, '--ignore-not-found=true'],
                    check=False
                )

                if result.returncode == 0:
                    if 'deleted' in result.stdout.lower():
                        if log_callback:
                            log_callback(f'✅ Deleted LeaderWorkerSet: {resource_name}')
                    else:
                        if log_callback:
                            log_callback(f'ℹ️  LeaderWorkerSet {resource_name} not found (already deleted)')
                else:
                    # Try as pod
                    result = self.kubectl.run(
                        ['delete', 'pod', resource_name, '-n', self.namespace, '--ignore-not-found=true'],
                        check=False
                    )
                    if result.returncode == 0 and 'deleted' in result.stdout.lower():
                        if log_callback:
                            log_callback(f'✅ Deleted pod: {resource_name}')

                # Also delete associated services
                service_name = resource_name.replace('-aggregated', '').replace('-prefill', '').replace('-decode', '')
                self.kubectl.run(
                    ['delete', 'service', '-l', f'test-id={service_name}', '-n', self.namespace, '--ignore-not-found=true'],
                    check=False
                )

            except Exception as e:
                logger.error(f"Failed to delete {resource_name}: {e}")
                if log_callback:
                    log_callback(f'❌ Failed to delete {resource_name}: {str(e)}')
                success = False

        return success

    def cleanup_all_test_deployments(self, log_callback=None) -> bool:
        """
        Clean up all InfeRecipe test deployments.

        Args:
            log_callback: Optional callback for logging

        Returns:
            True if cleanup succeeded
        """
        try:
            if log_callback:
                log_callback('🧹 Cleaning up all InfeRecipe test deployments...')

            # Delete all LeaderWorkerSets with inferecipe label
            result = self.kubectl.run(
                ['delete', 'leaderworkerset', '-l', 'component=inferecipe-test', '-n', self.namespace, '--ignore-not-found=true'],
                check=False
            )

            if result.returncode == 0:
                if 'deleted' in result.stdout.lower():
                    if log_callback:
                        log_callback(f'✅ {result.stdout.strip()}')
                else:
                    if log_callback:
                        log_callback('ℹ️  No InfeRecipe test deployments found')

            # Delete all associated services
            self.kubectl.run(
                ['delete', 'service', '-l', 'component=inferecipe-test', '-n', self.namespace, '--ignore-not-found=true'],
                check=False
            )

            if log_callback:
                log_callback('✅ Cleanup complete')

            return True

        except Exception as e:
            logger.error(f"Failed to cleanup deployments: {e}")
            if log_callback:
                log_callback(f'❌ Cleanup failed: {str(e)}')
            return False

    def cleanup_test(self, test_id: str, log_callback=None) -> bool:
        """
        Clean up all resources for a specific test.

        Deletes:
        - LeaderWorkerSets with label test-id=<test_id>
        - Services with label test-id=<test_id>
        - ResourceClaimTemplates with label test-id=<test_id>

        Args:
            test_id: Test identifier
            log_callback: Optional callback for logging

        Returns:
            True if cleanup succeeded
        """
        try:
            if log_callback:
                log_callback(f'🧹 Cleaning up test: {test_id}')

            # Delete LeaderWorkerSets
            result = self.kubectl.run(
                ['delete', 'leaderworkerset', '-l', f'test-id={test_id}', '-n', self.namespace, '--ignore-not-found=true'],
                check=False
            )
            if result.returncode == 0 and 'deleted' in result.stdout.lower():
                if log_callback:
                    log_callback(f'✅ Deleted LeaderWorkerSets for {test_id}')

            # Delete Services
            result = self.kubectl.run(
                ['delete', 'service', '-l', f'test-id={test_id}', '-n', self.namespace, '--ignore-not-found=true'],
                check=False
            )
            if result.returncode == 0 and 'deleted' in result.stdout.lower():
                if log_callback:
                    log_callback(f'✅ Deleted Services for {test_id}')

            # Delete ResourceClaimTemplates (DRA)
            result = self.kubectl.run(
                ['delete', 'resourceclaimtemplate', '-l', f'test-id={test_id}', '-n', self.namespace, '--ignore-not-found=true'],
                check=False
            )
            if result.returncode == 0 and 'deleted' in result.stdout.lower():
                if log_callback:
                    log_callback(f'✅ Deleted ResourceClaimTemplates for {test_id}')

            # Delete NetworkAttachmentDefinitions (NAD)
            result = self.kubectl.run(
                ['delete', 'networkattachmentdefinition', '-l', f'test-id={test_id}', '-n', self.namespace, '--ignore-not-found=true'],
                check=False
            )
            if result.returncode == 0 and 'deleted' in result.stdout.lower():
                if log_callback:
                    log_callback(f'✅ Deleted NetworkAttachmentDefinitions for {test_id}')

            return True

        except Exception as e:
            logger.error(f"Failed to cleanup test {test_id}: {e}")
            if log_callback:
                log_callback(f'❌ Cleanup failed: {str(e)}')
            return False

    def get_deployed_resources(self) -> List[str]:
        """
        Get list of currently deployed InfeRecipe resources.

        Returns:
            List of LeaderWorkerSet names
        """
        try:
            result = self.kubectl.run_json(
                ['get', 'leaderworkerset', '-l', 'component=inferecipe-test', '-n', self.namespace, '-o', 'json']
            )

            if 'items' in result:
                return [item['metadata']['name'] for item in result['items']]
            return []

        except Exception as e:
            logger.error(f"Failed to get deployed resources: {e}")
            return []


def main():
    """Main entry point for standalone execution."""
    import argparse

    parser = argparse.ArgumentParser(
        description='Clean up InfeRecipe test deployments'
    )
    parser.add_argument('action', choices=['cleanup', 'list'],
                        help='Action to perform')
    parser.add_argument('--namespace', default='inferecipe', help='Kubernetes namespace')
    parser.add_argument('--resource', help='Specific resource to cleanup')

    args = parser.parse_args()

    manager = CleanupManager(namespace=args.namespace)

    if args.action == 'list':
        resources = manager.get_deployed_resources()
        if resources:
            print(f"Found {len(resources)} deployed resources:")
            for r in resources:
                print(f"  - {r}")
        else:
            print("No InfeRecipe deployments found")

    elif args.action == 'cleanup':
        if args.resource:
            success = manager.cleanup_last_deployment([args.resource], log_callback=print)
        else:
            success = manager.cleanup_all_test_deployments(log_callback=print)

        if success:
            print("✅ Cleanup successful")
        else:
            print("❌ Cleanup failed")


if __name__ == '__main__':
    main()
