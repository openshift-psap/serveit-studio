"""Instance lifecycle management — create, delete, list InfeRecipe instances."""

import json
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict

from launcher.database import get_db

TEMPLATES_DIR = Path(__file__).parent.parent / 'deployment' / 'templates'


def _kubectl(args: list, input_data: str = None) -> subprocess.CompletedProcess:
    cmd = 'oc' if _is_oc() else 'kubectl'
    return subprocess.run([cmd] + args, input=input_data, capture_output=True, text=True, timeout=60)


def _is_oc() -> bool:
    """Check if we're on OpenShift (not just if oc CLI exists)."""
    try:
        r = subprocess.run(['kubectl', 'api-resources', '--api-group=route.openshift.io'],
                          capture_output=True, text=True, timeout=15)
        return r.returncode == 0 and 'Route' in r.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _render(template_name: str, **ctx) -> str:
    try:
        from jinja2 import Environment, FileSystemLoader
        env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)), keep_trailing_newline=True)
        return env.get_template(template_name).render(**ctx)
    except ImportError:
        content = (TEMPLATES_DIR / template_name).read_text()
        for k, v in ctx.items():
            content = content.replace('{{ ' + k + ' }}', str(v))
        return content


def _sanitize(name: str) -> str:
    return re.sub(r'[^a-z0-9-]', '-', name.lower())[:40]


def create_instance(owner_id: int, username: str, name: str,
                    namespace: str = 'inferecipe',
                    kubeconfig_data: str = None,
                    storage_class: str = None,
                    image: str = 'quay.io/bbenshab/inferecipe:server') -> Dict:
    """Create a new InfeRecipe instance for a user.

    Each instance gets its own namespace (inferecipe-{username}-{name}) to avoid
    resource collisions when multiple instances target the same cluster.
    """

    safe_name = _sanitize(f"{username}-{name}")
    # Each instance gets its own namespace for isolation
    instance_namespace = f"inferecipe-{safe_name}"
    deployment_name = f"inferecipe-{safe_name}"
    pvc_name = f"inferecipe-{safe_name}-storage"
    service_name = f"inferecipe-{safe_name}-ui"
    target_cluster = 'local'

    # Create kubeconfig Secret if provided
    kubeconfig_secret = None
    if kubeconfig_data:
        # Extract cluster URL from kubeconfig
        try:
            import yaml
            kc = yaml.safe_load(kubeconfig_data)
            target_cluster = kc.get('clusters', [{}])[0].get('cluster', {}).get('server', 'remote')
        except Exception:
            target_cluster = 'remote'

        # Validate kubeconfig — test connectivity to the target cluster
        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', suffix='.kubeconfig', delete=False) as tmp:
            tmp.write(kubeconfig_data)
            tmp_path = tmp.name
        try:
            r = subprocess.run(
                ['kubectl', '--kubeconfig', tmp_path, 'cluster-info'],
                capture_output=True, text=True, timeout=15
            )
            if r.returncode != 0:
                raise RuntimeError(
                    f"Cannot connect to cluster {target_cluster}. "
                    f"Verify the kubeconfig is correct and the cluster is reachable.\n"
                    f"Error: {r.stderr.strip()[:200]}"
                )
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                f"Connection to {target_cluster} timed out. "
                f"Verify the cluster is reachable from this network."
            )
        finally:
            os.unlink(tmp_path)

        # Create instance_namespace on remote cluster if it doesn't exist
        import tempfile as _tf2
        with _tf2.NamedTemporaryFile(mode='w', suffix='.kubeconfig', delete=False) as tmp2:
            tmp2.write(kubeconfig_data)
            tmp2_path = tmp2.name
        try:
            r = subprocess.run(
                ['kubectl', '--kubeconfig', tmp2_path, 'get', 'namespace', instance_namespace],
                capture_output=True, text=True, timeout=15)
            if r.returncode != 0:
                r2 = subprocess.run(
                    ['kubectl', '--kubeconfig', tmp2_path, 'create', 'namespace', instance_namespace],
                    capture_output=True, text=True, timeout=15)
                if r2.returncode != 0:
                    raise RuntimeError(
                        f"Failed to create namespace '{instance_namespace}' on remote cluster: {r2.stderr.strip()[:200]}")
        finally:
            os.unlink(tmp2_path)

        kubeconfig_secret = f"inferecipe-kubeconfig-{safe_name}"
        secret_yaml = json.dumps({
            "apiVersion": "v1", "kind": "Secret",
            "metadata": {"name": kubeconfig_secret, "namespace": instance_namespace},
            "type": "Opaque",
            "stringData": {"kubeconfig": kubeconfig_data}
        })
        r = _kubectl(['apply', '-f', '-', '-n', instance_namespace], input_data=secret_yaml)
        if r.returncode != 0:
            raise RuntimeError(f"Failed to create kubeconfig Secret: {r.stderr}")

    # Insert DB record first to get ID
    with get_db() as conn:
        conn.execute('''
            INSERT INTO instances (name, owner_id, display_name, status, namespace,
                                   deployment_name, pvc_name, service_name,
                                   kubeconfig_secret, target_cluster, created_at)
            VALUES (?, ?, ?, 'creating', ?, ?, ?, ?, ?, ?, ?)
        ''', (safe_name, owner_id, name, instance_namespace, deployment_name, pvc_name,
              service_name, kubeconfig_secret, target_cluster, datetime.now().isoformat()))
        instance_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]

    # Create instance namespace on local cluster
    r = _kubectl(['get', 'namespace', instance_namespace])
    if r.returncode != 0:
        r = _kubectl(['create', 'namespace', instance_namespace])
        if r.returncode != 0:
            raise RuntimeError(f"Failed to create namespace {instance_namespace}: {r.stderr}")

    try:
        # Create PVC
        pvc_yaml = _render('pvc.yaml.j2',
            pvc_name=pvc_name, namespace=instance_namespace,
            storage_size='100Gi', storage_class=storage_class or '')
        r = _kubectl(['apply', '-f', '-', '-n', instance_namespace], input_data=pvc_yaml)
        if r.returncode != 0:
            raise RuntimeError(f"PVC creation failed: {r.stderr}")

        # Create Deployment (with AUTH_DISABLED + optional kubeconfig mount)
        # Detect launcher's PVC for shared code access
        code_pvc = ''
        r = _kubectl(['get', 'pod', '-l', 'app=inferecipe-launcher', '-n', namespace,
                      '-o', 'jsonpath={.items[0].spec.volumes[?(@.persistentVolumeClaim)].persistentVolumeClaim.claimName}'])
        if r.returncode == 0 and r.stdout.strip():
            code_pvc = r.stdout.strip().split()[0]

        deploy_yaml = _render('instance-deployment.yaml.j2',
            name=deployment_name, namespace=instance_namespace, image=image,
            pvc_name=pvc_name, code_pvc_name=code_pvc,
            dev_mode='false', force_nad='false',
            auth_disabled='true',
            kubeconfig_secret=kubeconfig_secret or '',
            has_kubeconfig='true' if kubeconfig_secret else 'false')

        r = _kubectl(['apply', '-f', '-', '-n', instance_namespace], input_data=deploy_yaml)
        if r.returncode != 0:
            raise RuntimeError(f"Deployment creation failed: {r.stderr}")

        # Create Service
        is_ocp = _is_oc()
        svc_yaml = _render('service.yaml.j2',
            name=deployment_name, namespace=instance_namespace, is_openshift=is_ocp)
        r = _kubectl(['apply', '-f', '-', '-n', instance_namespace], input_data=svc_yaml)
        if r.returncode != 0:
            raise RuntimeError(f"Service creation failed: {r.stderr}")

        # Determine service URL — wait for external IP on LoadBalancer
        service_url = None
        if is_ocp:
            r = _kubectl(['get', 'route', f'{deployment_name}-ui', '-n', instance_namespace,
                          '-o', 'jsonpath={.spec.host}'])
            service_url = f"https://{r.stdout.strip()}" if r.stdout.strip() else None
        else:
            # Wait up to 30s for LoadBalancer external IP
            for _ in range(15):
                r = _kubectl(['get', 'svc', f'{deployment_name}-ui', '-n', instance_namespace,
                              '-o', 'jsonpath={.status.loadBalancer.ingress[0].ip}'])
                ext_ip = r.stdout.strip() if r.returncode == 0 else ''
                if ext_ip and ext_ip != '<pending>':
                    service_url = f"http://{ext_ip}:5000"
                    break
                # Also check hostname (some cloud providers use hostname instead of IP)
                r2 = _kubectl(['get', 'svc', f'{deployment_name}-ui', '-n', instance_namespace,
                               '-o', 'jsonpath={.status.loadBalancer.ingress[0].hostname}'])
                ext_host = r2.stdout.strip() if r2.returncode == 0 else ''
                if ext_host:
                    service_url = f"http://{ext_host}:5000"
                    break
                time.sleep(2)
            if not service_url:
                # Fallback: use NodePort
                r = _kubectl(['get', 'svc', f'{deployment_name}-ui', '-n', instance_namespace,
                              '-o', 'jsonpath={.spec.ports[0].nodePort}'])
                node_port = r.stdout.strip() if r.returncode == 0 else ''
                if node_port:
                    service_url = f"http://localhost:{node_port}"
                else:
                    service_url = f"http://{service_name}.{instance_namespace}.svc.cluster.local:5000"

        # Update DB
        with get_db() as conn:
            conn.execute('''
                UPDATE instances SET status = 'running', service_url = ? WHERE id = ?
            ''', (service_url, instance_id))

        return {
            'id': instance_id, 'name': name, 'deployment': deployment_name,
            'service_url': service_url, 'target_cluster': target_cluster,
            'status': 'running',
        }

    except Exception as e:
        # Clean up the DB record and any partial K8s resources on failure
        with get_db() as conn:
            conn.execute("DELETE FROM instances WHERE id = ?", (instance_id,))
        _kubectl(['delete', 'deployment', deployment_name, '-n', instance_namespace, '--ignore-not-found=true'])
        _kubectl(['delete', 'svc', f'{deployment_name}-ui', '-n', instance_namespace, '--ignore-not-found=true'])
        _kubectl(['delete', 'pvc', pvc_name, '-n', instance_namespace, '--ignore-not-found=true'])
        if kubeconfig_secret:
            _kubectl(['delete', 'secret', kubeconfig_secret, '-n', instance_namespace, '--ignore-not-found=true'])
        raise


def delete_instance(instance_id: int, owner_id: int) -> bool:
    """Delete an instance and all its K8s resources."""
    with get_db() as conn:
        row = conn.execute(
            'SELECT * FROM instances WHERE id = ? AND owner_id = ?',
            (instance_id, owner_id)
        ).fetchone()
        if not row:
            return False
        row = dict(row)

    ns = row['namespace']

    # Delete K8s resources (ignore errors — resource may already be gone)
    for resource in [
        f"deployment/{row['deployment_name']}",
        f"service/{row['service_name']}",
        f"pvc/{row['pvc_name']}",
    ]:
        _kubectl(['delete', resource, '-n', ns, '--ignore-not-found=true'])

    if row.get('kubeconfig_secret'):
        _kubectl(['delete', 'secret', row['kubeconfig_secret'], '-n', ns, '--ignore-not-found=true'])

    # Delete Route on OpenShift
    if _is_oc():
        _kubectl(['delete', 'route', f"{row['deployment_name']}-ui", '-n', ns, '--ignore-not-found=true'])

    # Delete the instance namespace (cleans up everything in it)
    _kubectl(['delete', 'namespace', ns, '--ignore-not-found=true'])

    with get_db() as conn:
        conn.execute('DELETE FROM instances WHERE id = ?', (instance_id,))

    return True


def list_instances(owner_id: int) -> List[Dict]:
    """List all instances for a user with live status."""
    with get_db() as conn:
        rows = conn.execute(
            'SELECT * FROM instances WHERE owner_id = ? ORDER BY created_at DESC',
            (owner_id,)
        ).fetchall()

    instances = []
    for row in rows:
        inst = dict(row)
        # Check live pod status (detect CrashLoopBackOff and Error)
        r = _kubectl(['get', 'pod', '-l', f"app={inst['deployment_name']}",
                      '-n', inst['namespace'],
                      '-o', 'jsonpath={.items[0].status.phase}:{.items[0].status.containerStatuses[0].state.waiting.reason}:{.items[0].status.containerStatuses[0].restartCount}'])
        raw = r.stdout.strip() if r.returncode == 0 else ''
        parts = raw.split(':')
        phase = parts[0] if parts else 'Unknown'
        waiting_reason = parts[1] if len(parts) > 1 else ''
        restarts = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        if waiting_reason in ('CrashLoopBackOff', 'Error', 'ImagePullBackOff'):
            inst['pod_status'] = waiting_reason
        elif phase == 'Running' and restarts > 2:
            inst['pod_status'] = 'CrashLoop'
        elif phase in ('Terminating', 'Pending', 'Failed', 'Succeeded', 'Running'):
            inst['pod_status'] = phase
        elif not raw or not phase:
            # Check if the deployment itself still exists
            dep_r = _kubectl(['get', 'deployment', inst['deployment_name'],
                              '-n', inst['namespace'], '--ignore-not-found', '-o', 'name'])
            if not dep_r.stdout.strip():
                inst['pod_status'] = 'Deleted'
            else:
                inst['pod_status'] = 'Starting'
        else:
            inst['pod_status'] = phase or 'Unknown'
        instances.append(inst)

    return instances
