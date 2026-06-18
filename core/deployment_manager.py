"""
ServeIt Studio Deployment Manager

Manages deployment of test configurations to Kubernetes cluster.
"""

import json
import time
import logging
import subprocess
from typing import Dict, List, Optional, Callable
from dataclasses import dataclass

from .config_generator import TestConfig
from .template_manager import TemplateManager
from .k8s_utils import KubectlRunner

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


@dataclass
class DeploymentStatus:
    """Status of a deployment."""
    test_id: str
    architecture: str
    deployed: bool
    ready: bool
    pods_running: int
    pods_expected: int
    error_message: Optional[str] = None


class DeploymentManager:
    """Manages deployment of test configurations to Kubernetes."""

    def __init__(
        self,
        namespace: str = 'serveit',
        kubeconfig: Optional[str] = None,
        template_manager: Optional[TemplateManager] = None
    ):
        """
        Initialize DeploymentManager.

        Args:
            namespace: Kubernetes namespace for deployments
            kubeconfig: Path to kubeconfig file
            template_manager: TemplateManager instance (creates new if None)
        """
        self.namespace = namespace
        self.kubectl = KubectlRunner(kubeconfig=kubeconfig, namespace=namespace)
        self.template_manager = template_manager or TemplateManager()

    def deploy_manifest(
        self,
        manifest_content: str,
        log_callback: Optional[Callable[[str], None]] = None
    ) -> bool:
        """
        Deploy a manifest to Kubernetes.

        Args:
            manifest_content: YAML manifest content
            log_callback: Optional callback for logging (func(message))

        Returns:
            True if deployment succeeded
        """
        try:
            if log_callback:
                log_callback("📦 Applying manifest to cluster...")

            result = self.kubectl.run(
                ['apply', '-f', '-', '-n', self.namespace],
                input_data=manifest_content
            )

            if log_callback:
                log_callback(f"✅ {result.stdout.strip()}")

            logger.info(f"Manifest deployed successfully: {result.stdout.strip()}")
            return True

        except subprocess.CalledProcessError as e:
            error_msg = f"kubectl apply failed (exit {e.returncode})"
            logger.error(error_msg)
            logger.error(f"stderr: {e.stderr}")
            logger.error(f"manifest first 500 chars: {manifest_content[:500]}")
            if log_callback:
                log_callback(f"❌ {error_msg}")
                # Show actual error from kubectl
                if e.stderr:
                    for line in e.stderr.strip().split('\n'):
                        log_callback(f"   {line}")
            return False
        except Exception as e:
            error_msg = f"Failed to deploy manifest: {str(e)}"
            logger.error(error_msg)
            if log_callback:
                log_callback(f"❌ {error_msg}")
            return False

    def deploy_config(
        self,
        config: TestConfig,
        log_callback: Optional[Callable[[str], None]] = None
    ) -> bool:
        """
        Deploy a test configuration to Kubernetes.

        For PD architecture: Deploys pods with HIGHER GPU requirement first,
        waits for them to be ready, then deploys lower GPU pods. This prevents
        scheduling deadlocks where smaller pods spread across all nodes leaving
        no node with enough GPUs for larger pods.

        Args:
            config: Test configuration to deploy
            log_callback: Optional callback for logging

        Returns:
            True if all manifests deployed successfully
        """
        if log_callback:
            log_callback(f"🚀 Deploying {config.architecture} configuration: {config.test_id}")

        # Render manifests (already ordered by GPU requirement for PD)
        manifests = self.template_manager.render_config(config)

        # For PD/EP architecture: Deploy sequentially and wait
        if config.architecture in ('pd', 'ep'):
            return self._deploy_pd_sequential(config, manifests, log_callback)

        # For other architectures: Deploy all manifests at once
        success = True
        for manifest_name, manifest_content in manifests.items():
            if log_callback:
                log_callback(f"📄 Deploying {manifest_name}...")

            if not self.deploy_manifest(manifest_content, log_callback):
                success = False
                break

        return success

    def _deploy_pd_sequential(
        self,
        config: TestConfig,
        manifests: Dict[str, str],
        log_callback: Optional[Callable[[str], None]] = None
    ) -> bool:
        """
        Deploy PD manifests sequentially: higher-GPU pods first, wait, then lower-GPU pods.

        Args:
            config: Test configuration
            manifests: Rendered manifests (already ordered by GPU requirement)
            log_callback: Optional callback for logging

        Returns:
            True if all deployments succeeded
        """
        # Manifests are already ordered by template_manager (higher TP first)
        # Now we deploy them one by one and WAIT for pods to be ready

        manifest_list = list(manifests.items())

        for i, (manifest_name, manifest_content) in enumerate(manifest_list):
            # Skip services (deploy them at the end)
            if 'service' in manifest_name:
                continue

            if log_callback:
                log_callback(f"📄 Deploying {manifest_name}...")

            # Deploy the manifest
            if not self.deploy_manifest(manifest_content, log_callback):
                return False

            # Wait for this component's pods to be running before deploying next
            # This ensures high-GPU pods claim nodes before low-GPU pods can spread
            if 'prefill' in manifest_name or 'decode' in manifest_name:
                component = 'prefill' if 'prefill' in manifest_name else 'decode'
                lws_name = f"{config.test_id}-{component}"

                if log_callback:
                    log_callback(f"⏳ Waiting for {component} pods to be running...")

                # Wait up to 5 minutes for pods to be running
                timeout = 300
                start_time = time.time()

                while time.time() - start_time < timeout:
                    status = self._get_lws_status(lws_name)

                    if status['deployed'] and status['pods_running'] >= status['pods_expected']:
                        if log_callback:
                            log_callback(f"✅ {component} pods running ({status['pods_running']}/{status['pods_expected']})")
                        break

                    time.sleep(5)
                else:
                    if log_callback:
                        log_callback(f"⚠️  Timeout waiting for {component} pods")
                    return False

        # Now deploy services
        for manifest_name, manifest_content in manifest_list:
            if 'service' in manifest_name:
                if log_callback:
                    log_callback(f"📄 Deploying {manifest_name}...")
                if not self.deploy_manifest(manifest_content, log_callback):
                    return False

        return True

    def get_deployment_status(self, test_id: str, architecture: str) -> DeploymentStatus:
        """
        Get status of a deployed configuration.

        Args:
            test_id: Test ID
            architecture: Architecture type

        Returns:
            DeploymentStatus object
        """
        try:
            # For PD/EP, check both prefill and decode
            if architecture in ('pd', 'ep'):
                prefill_status = self._get_lws_status(f"{test_id}-prefill")
                decode_status = self._get_lws_status(f"{test_id}-decode")
                if not prefill_status['deployed'] or not decode_status['deployed']:
                    for role in ('prefill', 'decode'):
                        result = self.kubectl.run(
                            ['get', 'lws', '-l', f'test-id={test_id},role={role}', '-n', self.namespace,
                             '-o', 'jsonpath={.items[0].metadata.name}'],
                            check=False,
                        )
                        if result.returncode == 0 and result.stdout.strip():
                            if role == 'prefill':
                                prefill_status = self._get_lws_status(result.stdout.strip())
                            else:
                                decode_status = self._get_lws_status(result.stdout.strip())

                # Both must be deployed and ready
                deployed = prefill_status['deployed'] and decode_status['deployed']
                ready = prefill_status['ready'] and decode_status['ready']
                pods_running = prefill_status['pods_running'] + decode_status['pods_running']
                pods_expected = prefill_status['pods_expected'] + decode_status['pods_expected']

                return DeploymentStatus(
                    test_id=test_id,
                    architecture=architecture,
                    deployed=deployed,
                    ready=ready,
                    pods_running=pods_running,
                    pods_expected=pods_expected
                )
            else:
                # For aggregated: try test_id-based name, fallback to per_pod_storage name
                lws_name = f"{test_id}-{architecture}"
                status = self._get_lws_status(lws_name)
                if not status['deployed']:
                    # per_pod_storage uses a stable name like aggregated-tp{N}
                    result = self.kubectl.run(
                        ['get', 'lws', '-l', f'test-id={test_id}', '-n', self.namespace,
                         '-o', 'jsonpath={.items[0].metadata.name}'],
                        check=False,
                    )
                    if result.returncode == 0 and result.stdout.strip():
                        status = self._get_lws_status(result.stdout.strip())

                return DeploymentStatus(
                    test_id=test_id,
                    architecture=architecture,
                    deployed=status['deployed'],
                    ready=status['ready'],
                    pods_running=status['pods_running'],
                    pods_expected=status['pods_expected']
                )

        except Exception as e:
            logger.error(f"Failed to get deployment status: {e}")
            return DeploymentStatus(
                test_id=test_id,
                architecture=architecture,
                deployed=False,
                ready=False,
                pods_running=0,
                pods_expected=0,
                error_message=str(e)
            )

    def _get_lws_status(self, lws_name: str) -> Dict:
        """
        Get status of a LeaderWorkerSet.

        Args:
            lws_name: LeaderWorkerSet name

        Returns:
            Dictionary with deployment status
        """
        try:
            lws_data = self.kubectl.run_json(
                ['get', 'leaderworkerset', lws_name, '-n', self.namespace]
            )

            spec = lws_data.get('spec', {})
            status = lws_data.get('status', {})

            replicas = spec.get('replicas', 0)
            ready_replicas = status.get('readyReplicas', 0)
            # For PD sequential deployment: use actual replica count (running pods)
            # not ready replicas, since prefill pods can't be ready without decode
            actual_replicas = status.get('replicas', 0)

            return {
                'deployed': True,
                'ready': ready_replicas == replicas and replicas > 0,
                'pods_running': actual_replicas,  # Running pods, not ready pods
                'pods_expected': replicas
            }

        except Exception:
            return {
                'deployed': False,
                'ready': False,
                'pods_running': 0,
                'pods_expected': 0
            }

    def _get_pending_pods(self, test_id: str) -> List[Dict]:
        """
        Get pods stuck in Pending state for a test deployment.

        Returns:
            List of dicts with 'name' and 'pending_seconds' for each Pending pod
        """
        try:
            result = self.kubectl.run(
                ['get', 'pods', '-n', self.namespace,
                 '-l', f'llm-d.ai/test-id={test_id}',
                 '--field-selector', 'status.phase=Pending',
                 '-o', 'json'],
                check=False
            )
            if result.returncode != 0 or not result.stdout.strip():
                return []

            pods_data = json.loads(result.stdout)
            pending_pods = []
            for pod in pods_data.get('items', []):
                pod_name = pod.get('metadata', {}).get('name', '')
                creation = pod.get('metadata', {}).get('creationTimestamp', '')
                if not creation:
                    continue

                # Skip pods that are scheduled but still initialising (image pull,
                # container creation). Only restart pods that are unscheduled —
                # those are genuinely stuck on DRA/resource allocation.
                conditions = {
                    c.get('type'): c.get('status')
                    for c in pod.get('status', {}).get('conditions', [])
                }
                if conditions.get('PodScheduled') == 'True':
                    # Pod landed on a node — it's pulling an image or initialising,
                    # not stuck on resource allocation. Leave it alone.
                    continue

                from datetime import datetime, timezone
                created_dt = datetime.fromisoformat(creation.replace('Z', '+00:00'))
                now = datetime.now(timezone.utc)
                pending_seconds = (now - created_dt).total_seconds()
                pending_pods.append({
                    'name': pod_name,
                    'pending_seconds': pending_seconds
                })
            return pending_pods

        except Exception as e:
            logger.warning(f"Failed to check pending pods: {e}")
            return []

    def _restart_stuck_pods(
        self,
        test_id: str,
        pending_timeout: int = 180,
        log_callback: Optional[Callable[[str], None]] = None
    ) -> int:
        """
        Delete pods stuck in Pending state beyond the timeout.
        The LWS controller will recreate them with a new DRA allocation attempt.

        Args:
            test_id: Test ID
            pending_timeout: Seconds before a Pending pod is considered stuck
            log_callback: Optional callback for logging

        Returns:
            Number of pods restarted
        """
        pending_pods = self._get_pending_pods(test_id)
        restarted = 0

        for pod in pending_pods:
            if pod['pending_seconds'] > pending_timeout:
                pod_name = pod['name']
                mins = int(pod['pending_seconds'] // 60)
                if log_callback:
                    log_callback(
                        f"   🔄 Restarting stuck pod {pod_name} "
                        f"(Pending for {mins}m, DRA re-allocation)"
                    )
                try:
                    self.kubectl.run(
                        ['delete', 'pod', pod_name, '-n', self.namespace],
                        check=False
                    )
                    restarted += 1
                except Exception as e:
                    logger.warning(f"Failed to restart pod {pod_name}: {e}")

        return restarted

    def wait_for_ready(
        self,
        test_id: str,
        architecture: str,
        timeout: int = 3600,
        max_pod_restarts: int = 3,
        log_callback: Optional[Callable[[str], None]] = None,
        stop_check: Optional[Callable[[], bool]] = None
    ) -> bool:
        """
        Wait for deployment to be ready.
        Automatically restarts pods stuck in Pending for >3 minutes
        (common with DRA GPU-NIC pair allocation failures), up to
        max_pod_restarts times.

        Args:
            test_id: Test ID
            architecture: Architecture type
            timeout: Timeout in seconds
            max_pod_restarts: Max times to restart stuck pods before giving up
            log_callback: Optional callback for logging

        Returns:
            True if deployment became ready
        """
        if log_callback:
            log_callback(f"⏳ Waiting for {architecture} deployment to be ready...")

        start_time = time.time()
        last_status = None
        last_pending_check = 0
        last_progress_log = 0
        pending_check_interval = 30  # Check for stuck pods every 30s
        progress_log_interval = 30   # Log progress every 30s even if unchanged
        total_pod_restarts = 0

        while time.time() - start_time < timeout:
            if stop_check and stop_check():
                if log_callback:
                    log_callback("🛑 Deployment wait cancelled — optimization stopped")
                return False

            status = self.get_deployment_status(test_id, architecture)
            elapsed = int(time.time() - start_time)

            # Log status changes or periodic progress
            status_changed = status != last_status
            time_for_progress = (time.time() - last_progress_log) >= progress_log_interval

            if log_callback and (status_changed or time_for_progress):
                ready_label = "Ready" if status.ready else "Running (model is still loading into GPU)"
                elapsed_suffix = f" ({elapsed}s)" if elapsed > 0 else ""
                log_callback(
                    f"📊 Status: {status.pods_running}/{status.pods_expected} {'pod' if status.pods_expected == 1 else 'pods'} {ready_label}{elapsed_suffix}"
                )
                last_status = status
                last_progress_log = time.time()

            if status.ready or (status.pods_running == status.pods_expected and status.pods_expected > 0):
                # Check actual pod readiness (LWS readyReplicas can lag behind)
                if self._verify_pods_ready(test_id, status.pods_expected):
                    if log_callback:
                        elapsed_suffix = f" ({elapsed}s)" if elapsed > 0 else ""
                        log_callback(f"✅ Deployment ready!{elapsed_suffix}")
                    return True
                else:
                    if log_callback and status_changed:
                        log_callback(
                            f"   ⏳ Pods running but not yet serving — waiting for model load..."
                        )

            # Periodically check for pods stuck in Pending (DRA allocation failure)
            now = time.time()
            if now - last_pending_check > pending_check_interval:
                if total_pod_restarts < max_pod_restarts:
                    restarted = self._restart_stuck_pods(
                        test_id,
                        pending_timeout=180,
                        log_callback=log_callback
                    )
                    if restarted > 0:
                        total_pod_restarts += restarted
                        if log_callback:
                            log_callback(
                                f"   📈 Total pod restarts: {total_pod_restarts}/{max_pod_restarts}"
                            )
                        # Reset last_status to force a fresh status log after restart
                        last_status = None
                elif total_pod_restarts >= max_pod_restarts:
                    # Check if pods are still stuck — if so, give up
                    pending = self._get_pending_pods(test_id)
                    stuck = [p for p in pending if p['pending_seconds'] > 180]
                    if stuck:
                        if log_callback:
                            log_callback(
                                f"⛔ Pods still stuck after {total_pod_restarts} restart attempts — giving up"
                            )
                        return False
                last_pending_check = now

            time.sleep(5)

        if log_callback:
            log_callback(f"⏱️ Timeout waiting for deployment to be ready ({timeout}s)")

        return False

    def delete_deployment(
        self,
        test_id: str,
        architecture: str,
        log_callback: Optional[Callable[[str], None]] = None
    ) -> bool:
        """
        Delete a deployed configuration.

        Args:
            test_id: Test ID
            architecture: Architecture type
            log_callback: Optional callback for logging

        Returns:
            True if deletion succeeded
        """
        if log_callback:
            log_callback(f"🗑️ Deleting {architecture} deployment: {test_id}")

        try:
            lws_result = self.kubectl.run(
                ['delete', 'leaderworkerset', '-l', f'test-id={test_id}', '-n', self.namespace],
                check=False
            )
            # Services are NOT owned by LWS and must be deleted separately
            svc_result = self.kubectl.run(
                ['delete', 'service', '-l', f'test-id={test_id}', '-n', self.namespace],
                check=False
            )

            ok = lws_result.returncode == 0
            if log_callback:
                if ok:
                    log_callback("✅ Deployment deleted")
                else:
                    log_callback(f"⚠️ Deletion warning: {lws_result.stderr.strip()}")

            return ok

        except Exception as e:
            error_msg = f"Failed to delete deployment: {e}"
            logger.error(error_msg)
            if log_callback:
                log_callback(f"❌ {error_msg}")
            return False

    def _verify_pods_ready(self, test_id: str, expected_count: int) -> bool:
        """Verify pods are actually ready by checking container readiness directly."""
        try:
            result = self.kubectl.run(
                ['get', 'pods', '-n', self.namespace,
                 '-l', f'llm-d.ai/test-id={test_id}',
                 '-o', 'jsonpath={range .items[*]}{.status.conditions[?(@.type=="Ready")].status}{" "}{end}'],
                check=False
            )
            if result.returncode != 0:
                return False

            statuses = result.stdout.strip().split()
            ready_count = sum(1 for s in statuses if s == 'True')
            return ready_count >= expected_count and expected_count > 0
        except Exception:
            return False

    def wait_for_pods_terminated(
        self,
        test_id: str,
        timeout: int = 300,
        log_callback: Optional[Callable[[str], None]] = None
    ) -> bool:
        """
        Wait until all pods with the given test-id label are fully terminated.

        DRA resources (GPU-NIC pairs) are not released until the pod is
        completely gone, so deploying new pods while old ones are still
        Terminating will fail to acquire GPUs.

        Args:
            test_id: Test ID label to watch
            timeout: Maximum seconds to wait
            log_callback: Optional callback for logging

        Returns:
            True if all pods terminated within timeout
        """
        start = time.time()
        poll_interval = 5
        last_log = 0

        while time.time() - start < timeout:
            try:
                result = self.kubectl.run(
                    ['get', 'pods', '-n', self.namespace,
                     '-l', f'llm-d.ai/test-id={test_id}',
                     '--no-headers'],
                    check=False
                )
                output = result.stdout.strip()
                if not output:
                    if log_callback:
                        log_callback("✅ All pods from previous deployment fully terminated")
                    return True

                remaining = len(output.splitlines())
                elapsed = int(time.time() - start)

                # Log every 15 seconds to avoid spam
                if elapsed - last_log >= 15:
                    if log_callback:
                        log_callback(
                            f"⏳ Waiting for {remaining} pod(s) to terminate... "
                            f"({elapsed}s elapsed)"
                        )
                    last_log = elapsed

            except Exception as e:
                logger.warning(f"Error checking pod termination: {e}")

            time.sleep(poll_interval)

        if log_callback:
            log_callback(
                f"⚠️ Timed out after {timeout}s waiting for pods to terminate. "
                f"Proceeding with deployment anyway."
            )
        return False

    def cleanup_all_tests(
        self,
        log_callback: Optional[Callable[[str], None]] = None
    ) -> bool:
        """
        Delete all ServeIt Studio test deployments.

        Args:
            log_callback: Optional callback for logging

        Returns:
            True if cleanup succeeded
        """
        if log_callback:
            log_callback("🧹 Cleaning up all ServeIt Studio test deployments...")

        try:
            lws_result = self.kubectl.run(
                ['delete', 'leaderworkerset', '-l', 'component=serveit-test', '-n', self.namespace],
                check=False
            )
            self.kubectl.run(
                ['delete', 'service', '-l', 'component=serveit-test', '-n', self.namespace],
                check=False
            )

            if log_callback:
                if lws_result.returncode == 0:
                    log_callback("✅ All test deployments cleaned up")
                else:
                    log_callback(f"⚠️ Cleanup warning: {lws_result.stderr.strip()}")

            return lws_result.returncode == 0

        except Exception as e:
            error_msg = f"Failed to cleanup deployments: {e}"
            logger.error(error_msg)
            if log_callback:
                log_callback(f"❌ {error_msg}")
            return False


def main():
    """Main entry point for standalone execution."""
    import argparse

    parser = argparse.ArgumentParser(
        description='Manage ServeIt Studio test deployments'
    )
    parser.add_argument('action', choices=['deploy', 'status', 'delete', 'cleanup'],
                        help='Action to perform')
    parser.add_argument('--test-id', help='Test ID')
    parser.add_argument('--architecture', choices=['aggregated', 'pd', 'ep'],
                        help='Architecture type')
    parser.add_argument('--namespace', default='serveit', help='Kubernetes namespace')
    parser.add_argument('--timeout', type=int, default=600,
                        help='Timeout for wait operations (seconds)')

    args = parser.parse_args()

    manager = DeploymentManager(namespace=args.namespace)

    if args.action == 'status':
        if not args.test_id or not args.architecture:
            print("Error: --test-id and --architecture required for status")
            return

        status = manager.get_deployment_status(args.test_id, args.architecture)
        print("Deployment Status:")
        print(f"  Test ID: {status.test_id}")
        print(f"  Architecture: {status.architecture}")
        print(f"  Deployed: {status.deployed}")
        print(f"  Ready: {status.ready}")
        print(f"  Pods: {status.pods_running}/{status.pods_expected}")

    elif args.action == 'delete':
        if not args.test_id or not args.architecture:
            print("Error: --test-id and --architecture required for delete")
            return

        success = manager.delete_deployment(args.test_id, args.architecture)
        if success:
            print("✅ Deployment deleted")
        else:
            print("❌ Failed to delete deployment")

    elif args.action == 'cleanup':
        success = manager.cleanup_all_tests()
        if success:
            print("✅ All test deployments cleaned up")
        else:
            print("❌ Failed to cleanup deployments")

    else:
        print(f"Action '{args.action}' not yet implemented")


if __name__ == '__main__':
    main()
