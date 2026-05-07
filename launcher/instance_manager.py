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
    try:
        r = subprocess.run(['oc', 'version', '--client'], capture_output=True, timeout=5)
        return r.returncode == 0
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
                    namespace: str = 'llm-d',
                    kubeconfig_data: str = None,
                    storage_class: str = None,
                    image: str = 'quay.io/bbenshab/vllm:inferecipe') -> Dict:
    """Create a new InfeRecipe instance for a user."""

    safe_name = _sanitize(f"{username}-{name}")
    deployment_name = f"inferecipe-{safe_name}"
    pvc_name = f"inferecipe-{safe_name}-storage"
    service_name = f"inferecipe-{safe_name}-ui"
    target_cluster = 'local'

    # Create kubeconfig Secret if provided
    kubeconfig_secret = None
    if kubeconfig_data:
        kubeconfig_secret = f"inferecipe-kubeconfig-{safe_name}"
        secret_yaml = json.dumps({
            "apiVersion": "v1", "kind": "Secret",
            "metadata": {"name": kubeconfig_secret, "namespace": namespace},
            "type": "Opaque",
            "stringData": {"kubeconfig": kubeconfig_data}
        })
        r = _kubectl(['apply', '-f', '-', '-n', namespace], input_data=secret_yaml)
        if r.returncode != 0:
            raise RuntimeError(f"Failed to create kubeconfig Secret: {r.stderr}")

        # Extract cluster URL from kubeconfig for display
        try:
            import yaml
            kc = yaml.safe_load(kubeconfig_data)
            target_cluster = kc.get('clusters', [{}])[0].get('cluster', {}).get('server', 'remote')
        except Exception:
            target_cluster = 'remote'

    # Insert DB record first to get ID
    with get_db() as conn:
        conn.execute('''
            INSERT INTO instances (name, owner_id, display_name, status, namespace,
                                   deployment_name, pvc_name, service_name,
                                   kubeconfig_secret, target_cluster, created_at)
            VALUES (?, ?, ?, 'creating', ?, ?, ?, ?, ?, ?, ?)
        ''', (safe_name, owner_id, name, namespace, deployment_name, pvc_name,
              service_name, kubeconfig_secret, target_cluster, datetime.now().isoformat()))
        instance_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]

    try:
        # Create PVC
        pvc_yaml = _render('pvc.yaml.j2',
            pvc_name=pvc_name, namespace=namespace,
            storage_size='100Gi', storage_class=storage_class or '')
        r = _kubectl(['apply', '-f', '-', '-n', namespace], input_data=pvc_yaml)
        if r.returncode != 0:
            raise RuntimeError(f"PVC creation failed: {r.stderr}")

        # Create Deployment (with AUTH_DISABLED + optional kubeconfig mount)
        deploy_yaml = _render('instance-deployment.yaml.j2',
            name=deployment_name, namespace=namespace, image=image,
            pvc_name=pvc_name, dev_mode='false', force_nad='false',
            auth_disabled='true',
            kubeconfig_secret=kubeconfig_secret or '',
            has_kubeconfig='true' if kubeconfig_secret else 'false')

        r = _kubectl(['apply', '-f', '-', '-n', namespace], input_data=deploy_yaml)
        if r.returncode != 0:
            raise RuntimeError(f"Deployment creation failed: {r.stderr}")

        # Create Service
        is_ocp = _is_oc()
        svc_yaml = _render('service.yaml.j2',
            name=deployment_name, namespace=namespace, is_openshift=is_ocp)
        r = _kubectl(['apply', '-f', '-', '-n', namespace], input_data=svc_yaml)
        if r.returncode != 0:
            raise RuntimeError(f"Service creation failed: {r.stderr}")

        # Determine service URL
        if is_ocp:
            r = _kubectl(['get', 'route', f'{deployment_name}-ui', '-n', namespace,
                          '-o', 'jsonpath={.spec.host}'])
            service_url = f"https://{r.stdout.strip()}" if r.stdout.strip() else None
        else:
            service_url = f"http://{service_name}.{namespace}.svc.cluster.local:5000"

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
        with get_db() as conn:
            conn.execute("UPDATE instances SET status = 'error' WHERE id = ?", (instance_id,))
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
        # Check live pod status
        r = _kubectl(['get', 'pod', '-l', f"app={inst['deployment_name']}",
                      '-n', inst['namespace'],
                      '-o', 'jsonpath={.items[0].status.phase}'])
        inst['pod_status'] = r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else 'Unknown'
        instances.append(inst)

    return instances
