"""
Kubernetes Utilities

Shared utilities for interacting with Kubernetes/OpenShift clusters.
Eliminates code duplication across deployment_manager and system_scanner.
"""

import os
import json
import subprocess
import logging
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_cached_kubectl_cmd: Optional[str] = None


def detect_kubectl_command() -> str:
    """Detect whether to use kubectl or oc CLI.

    Returns:
        'oc' if OpenShift CLI is available, otherwise 'kubectl'

    Raises:
        RuntimeError: If neither kubectl nor oc is found
    """
    global _cached_kubectl_cmd
    if _cached_kubectl_cmd is not None:
        return _cached_kubectl_cmd

    # Check if this is an OpenShift cluster (not just if oc binary exists)
    try:
        r = subprocess.run(['kubectl', 'api-resources', '--api-group=route.openshift.io'],
                          capture_output=True, text=True, timeout=15)
        if r.returncode == 0 and 'Route' in r.stdout:
            logger.info("Using OpenShift CLI (oc)")
            _cached_kubectl_cmd = 'oc'
            return 'oc'
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Check if kubectl is available
    try:
        subprocess.run(['kubectl', 'version', '--client'], capture_output=True, timeout=10)
        logger.info("Using Kubernetes CLI (kubectl)")
        _cached_kubectl_cmd = 'kubectl'
        return 'kubectl'
    except (FileNotFoundError, subprocess.TimeoutExpired):
        raise RuntimeError("Neither kubectl nor oc found. Please install Kubernetes CLI.")


class KubectlRunner:
    """Shared kubectl/oc command executor with consistent error handling."""

    def __init__(self, kubeconfig: Optional[str] = None, namespace: str = 'default'):
        """Initialize KubectlRunner.

        Args:
            kubeconfig: Path to kubeconfig file (uses default if None)
            namespace: Default namespace for commands
        """
        self.namespace = namespace
        self.kubeconfig = kubeconfig or os.environ.get('KUBECONFIG', '~/.kube/kubeconfig')
        self.kubectl_cmd = 'kubectl'

        # Prepare environment once (avoid copying on every call)
        self._env = os.environ.copy()
        self._env['KUBECONFIG'] = os.path.expanduser(self.kubeconfig)

    def run(
        self,
        args: List[str],
        input_data: Optional[str] = None,
        check: bool = True
    ) -> subprocess.CompletedProcess:
        """Run kubectl command and return result.

        Args:
            args: Command arguments (without kubectl/oc prefix)
            input_data: Optional stdin data for the command
            check: Whether to raise exception on non-zero exit

        Returns:
            CompletedProcess result

        Raises:
            subprocess.CalledProcessError: If check=True and command fails
        """
        cmd = [self.kubectl_cmd] + args

        try:
            result = subprocess.run(
                cmd,
                input=input_data,
                capture_output=True,
                text=True,
                check=check,
                env=self._env
            )
            return result
        except subprocess.CalledProcessError as e:
            logger.error(f"kubectl command failed: {e.stderr}")
            raise

    def run_json(self, args: List[str]) -> Dict:
        """Run kubectl command with JSON output and parse result.

        Args:
            args: Command arguments (without -o json, will be added)

        Returns:
            Parsed JSON response

        Raises:
            subprocess.CalledProcessError: If command fails
            json.JSONDecodeError: If output is not valid JSON
        """
        # Add -o json if not already present
        if '-o' not in args:
            args = args + ['-o', 'json']

        result = self.run(args, check=True)

        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse kubectl JSON output: {e}")
            logger.error(f"Output was: {result.stdout[:500]}")
            raise

    def get_resource(self, resource_type: str, name: str, namespace: Optional[str] = None) -> Optional[Dict]:
        """Get a Kubernetes resource by type and name.

        Args:
            resource_type: Resource type (e.g., 'pod', 'service', 'deployment')
            name: Resource name
            namespace: Namespace (uses default if None)

        Returns:
            Resource as dict, or None if not found
        """
        ns = namespace or self.namespace
        result = self.run(
            ['get', resource_type, name, '-n', ns, '-o', 'json'],
            check=False
        )

        if result.returncode == 0:
            return json.loads(result.stdout)
        return None

    def apply(self, manifest: str, namespace: Optional[str] = None) -> bool:
        """Apply a Kubernetes manifest.

        Args:
            manifest: YAML manifest content
            namespace: Namespace (uses default if None)

        Returns:
            True if successful, False otherwise
        """
        ns = namespace or self.namespace
        result = self.run(
            ['apply', '-f', '-', '-n', ns],
            input_data=manifest,
            check=False
        )
        return result.returncode == 0

    def delete(
        self,
        resource_type: str,
        name: str,
        namespace: Optional[str] = None,
        wait: bool = False
    ) -> bool:
        """Delete a Kubernetes resource.

        Args:
            resource_type: Resource type
            name: Resource name
            namespace: Namespace (uses default if None)
            wait: Whether to wait for deletion

        Returns:
            True if successful, False otherwise
        """
        ns = namespace or self.namespace
        args = ['delete', resource_type, name, '-n', ns]
        if wait:
            args.append('--wait')

        result = self.run(args, check=False)
        return result.returncode == 0
