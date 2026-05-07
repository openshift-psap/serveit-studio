"""Instance lifecycle management — create, delete, list InfeRecipe instances.

UI pods live in the shared launcher namespace (e.g. 'inferecipe').
Workloads (LWS, guidellm, EPP) get their own per-instance namespace.
Instances are organized into user-defined groups.
"""

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


# ── Group CRUD ──────────────────────────────────────────────────────────────

def create_group(owner_id: int, name: str, icon: str = '📦') -> Dict:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO groups_ (name, icon, owner_id, created_at) VALUES (?, ?, ?, ?)",
            (name, icon, owner_id, datetime.now().isoformat())
        )
        gid = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
    return {'id': gid, 'name': name, 'icon': icon}


def delete_group(group_id: int, owner_id: int) -> bool:
    with get_db() as conn:
        row = conn.execute(
            'SELECT id FROM groups_ WHERE id = ? AND owner_id = ?',
            (group_id, owner_id)
        ).fetchone()
        if not row:
            return False
        instances = conn.execute(
            'SELECT id FROM instances WHERE group_id = ? AND owner_id = ?',
            (group_id, owner_id)
        ).fetchall()

    for inst in instances:
        delete_instance(inst['id'], owner_id)

    with get_db() as conn:
        conn.execute('DELETE FROM groups_ WHERE id = ?', (group_id,))
    return True


def list_groups(owner_id: int) -> List[Dict]:
    with get_db() as conn:
        rows = conn.execute(
            '''SELECT g.*, COUNT(i.id) as instance_count
               FROM groups_ g LEFT JOIN instances i ON g.id = i.group_id
               WHERE g.owner_id = ?
               GROUP BY g.id ORDER BY g.created_at''',
            (owner_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def update_group(group_id: int, owner_id: int, name: str = None, icon: str = None) -> bool:
    with get_db() as conn:
        row = conn.execute(
            'SELECT id FROM groups_ WHERE id = ? AND owner_id = ?',
            (group_id, owner_id)
        ).fetchone()
        if not row:
            return False
        if name is not None:
            conn.execute('UPDATE groups_ SET name = ? WHERE id = ?', (name, group_id))
        if icon is not None:
            conn.execute('UPDATE groups_ SET icon = ? WHERE id = ?', (icon, group_id))
    return True


# ── Instance CRUD ───────────────────────────────────────────────────────────

def create_instance(owner_id: int, username: str, name: str,
                    group_id: int = None,
                    namespace: str = 'inferecipe',
                    kubeconfig_data: str = None,
                    storage_class: str = None,
                    image: str = 'quay.io/bbenshab/inferecipe:server') -> Dict:
    safe_name = _sanitize(f"{username}-{name}")
    workload_namespace = f"inferecipe-{safe_name}"
    deployment_name = f"inferecipe-{safe_name}"
    pvc_name = f"inferecipe-{safe_name}-storage"
    service_name = f"inferecipe-{safe_name}-ui"
    target_cluster = 'local'

    kubeconfig_secret = None
    if kubeconfig_data:
        try:
            import yaml
            kc = yaml.safe_load(kubeconfig_data)
            target_cluster = kc.get('clusters', [{}])[0].get('cluster', {}).get('server', 'remote')
        except Exception:
            target_cluster = 'remote'

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
                    f"Error: {r.stderr.strip()[:200]}")
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                f"Connection to {target_cluster} timed out. "
                f"Verify the cluster is reachable from this network.")
        finally:
            os.unlink(tmp_path)

        import tempfile as _tf2
        with _tf2.NamedTemporaryFile(mode='w', suffix='.kubeconfig', delete=False) as tmp2:
            tmp2.write(kubeconfig_data)
            tmp2_path = tmp2.name
        try:
            def _remote(args, input=None):
                return subprocess.run(['kubectl', '--kubeconfig', tmp2_path] + args,
                                      input=input, capture_output=True, text=True, timeout=30)

            r = _remote(['get', 'namespace', workload_namespace])
            if r.returncode != 0:
                r2 = _remote(['create', 'namespace', workload_namespace])
                if r2.returncode != 0:
                    raise RuntimeError(
                        f"Failed to create namespace '{workload_namespace}' on remote cluster: {r2.stderr.strip()[:200]}")

            r = _remote(['api-resources', '--api-group=route.openshift.io'])
            remote_is_openshift = r.returncode == 0 and 'Route' in r.stdout

            if remote_is_openshift:
                prom_yaml = json.dumps({
                    "apiVersion": "rbac.authorization.k8s.io/v1",
                    "kind": "ClusterRoleBinding",
                    "metadata": {"name": f"inferecipe-prometheus-{safe_name}"},
                    "subjects": [{"kind": "ServiceAccount", "name": "default",
                                  "namespace": workload_namespace}],
                    "roleRef": {"kind": "ClusterRole", "name": "prometheus-k8s",
                                "apiGroup": "rbac.authorization.k8s.io"}
                })
                _remote(['apply', '-f', '-'], input=prom_yaml)

            remote_rbac = json.dumps({
                "apiVersion": "v1", "kind": "List", "items": [
                    {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role",
                     "metadata": {"name": "inferecipe-full-access", "namespace": workload_namespace},
                     "rules": [
                         {"apiGroups": [""], "resources": ["pods", "pods/log", "pods/exec", "services",
                          "persistentvolumeclaims", "serviceaccounts", "configmaps", "secrets"],
                          "verbs": ["get", "list", "create", "delete", "patch", "watch"]},
                         {"apiGroups": ["apps"], "resources": ["deployments", "statefulsets"],
                          "verbs": ["get", "list", "create", "delete", "patch", "update"]},
                         {"apiGroups": ["batch"], "resources": ["jobs"],
                          "verbs": ["get", "list", "create", "delete", "patch", "watch"]},
                         {"apiGroups": ["leaderworkerset.x-k8s.io"], "resources": ["leaderworkersets"],
                          "verbs": ["get", "list", "create", "delete", "patch", "update"]},
                         {"apiGroups": ["rbac.authorization.k8s.io"], "resources": ["roles", "rolebindings"],
                          "verbs": ["get", "list", "create", "delete", "patch"]},
                         {"apiGroups": ["inference.networking.k8s.io"], "resources": ["inferencepools"],
                          "verbs": ["get", "list", "create", "delete", "patch", "watch"]},
                         {"apiGroups": ["gateway.networking.k8s.io"], "resources": ["gateways", "httproutes"],
                          "verbs": ["get", "list", "create", "delete", "patch"]},
                         {"apiGroups": ["networking.istio.io"], "resources": ["destinationrules"],
                          "verbs": ["get", "list", "create", "delete", "patch"]},
                         {"apiGroups": ["resource.k8s.io"], "resources": ["resourceclaimtemplates", "resourceclaims"],
                          "verbs": ["get", "list", "create", "delete", "patch", "update"]},
                     ]},
                    {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding",
                     "metadata": {"name": "inferecipe-access", "namespace": workload_namespace},
                     "subjects": [{"kind": "ServiceAccount", "name": "default", "namespace": workload_namespace}],
                     "roleRef": {"kind": "Role", "name": "inferecipe-full-access",
                                 "apiGroup": "rbac.authorization.k8s.io"}},
                ]
            })
            _remote(['apply', '-f', '-'], input=remote_rbac)
        finally:
            os.unlink(tmp2_path)

        kubeconfig_secret = f"inferecipe-kubeconfig-{safe_name}"

    with get_db() as conn:
        conn.execute('''
            INSERT INTO instances (name, owner_id, group_id, display_name, status, namespace,
                                   workload_namespace, deployment_name, pvc_name, service_name,
                                   kubeconfig_secret, target_cluster, created_at)
            VALUES (?, ?, ?, ?, 'creating', ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (safe_name, owner_id, group_id, name, namespace, workload_namespace,
              deployment_name, pvc_name, service_name,
              kubeconfig_secret, target_cluster, datetime.now().isoformat()))
        instance_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]

    try:
        if kubeconfig_data and kubeconfig_secret:
            secret_yaml = json.dumps({
                "apiVersion": "v1", "kind": "Secret",
                "metadata": {"name": kubeconfig_secret, "namespace": namespace},
                "type": "Opaque",
                "stringData": {"kubeconfig": kubeconfig_data}
            })
            r = _kubectl(['apply', '-f', '-', '-n', namespace], input_data=secret_yaml)
            if r.returncode != 0:
                raise RuntimeError(f"Failed to create kubeconfig Secret: {r.stderr}")

        pvc_yaml = _render('pvc.yaml.j2',
            pvc_name=pvc_name, namespace=namespace,
            storage_size='100Gi', storage_class=storage_class or '')
        r = _kubectl(['apply', '-f', '-', '-n', namespace], input_data=pvc_yaml)
        if r.returncode != 0:
            raise RuntimeError(f"PVC creation failed: {r.stderr}")

        deploy_yaml = _render('instance-deployment.yaml.j2',
            name=deployment_name, namespace=namespace, image=image,
            pvc_name=pvc_name, code_pvc_name='',
            workload_namespace=workload_namespace,
            dev_mode='false', force_nad='false',
            auth_disabled='true',
            kubeconfig_secret=kubeconfig_secret or '',
            has_kubeconfig='true' if kubeconfig_secret else 'false')

        r = _kubectl(['apply', '-f', '-', '-n', namespace], input_data=deploy_yaml)
        if r.returncode != 0:
            raise RuntimeError(f"Deployment creation failed: {r.stderr}")

        is_ocp = _is_oc()
        svc_yaml = _render('service.yaml.j2',
            name=deployment_name, namespace=namespace, is_openshift=is_ocp)
        r = _kubectl(['apply', '-f', '-', '-n', namespace], input_data=svc_yaml)
        if r.returncode != 0:
            raise RuntimeError(f"Service creation failed: {r.stderr}")

        service_url = None
        if is_ocp:
            r = _kubectl(['get', 'route', f'{deployment_name}-ui', '-n', namespace,
                          '-o', 'jsonpath={.spec.host}'])
            service_url = f"https://{r.stdout.strip()}" if r.stdout.strip() else None
        else:
            for _ in range(15):
                r = _kubectl(['get', 'svc', f'{deployment_name}-ui', '-n', namespace,
                              '-o', 'jsonpath={.status.loadBalancer.ingress[0].ip}'])
                ext_ip = r.stdout.strip() if r.returncode == 0 else ''
                if ext_ip and ext_ip != '<pending>':
                    service_url = f"http://{ext_ip}:5000"
                    break
                r2 = _kubectl(['get', 'svc', f'{deployment_name}-ui', '-n', namespace,
                               '-o', 'jsonpath={.status.loadBalancer.ingress[0].hostname}'])
                ext_host = r2.stdout.strip() if r2.returncode == 0 else ''
                if ext_host:
                    service_url = f"http://{ext_host}:5000"
                    break
                time.sleep(2)
            if not service_url:
                r = _kubectl(['get', 'svc', f'{deployment_name}-ui', '-n', namespace,
                              '-o', 'jsonpath={.spec.ports[0].nodePort}'])
                node_port = r.stdout.strip() if r.returncode == 0 else ''
                if node_port:
                    service_url = f"http://localhost:{node_port}"
                else:
                    service_url = f"http://{service_name}.{namespace}.svc.cluster.local:5000"

        with get_db() as conn:
            conn.execute('UPDATE instances SET status = ?, service_url = ? WHERE id = ?',
                         ('running', service_url, instance_id))

        return {
            'id': instance_id, 'name': name, 'deployment': deployment_name,
            'service_url': service_url, 'target_cluster': target_cluster,
            'status': 'running',
        }

    except Exception:
        with get_db() as conn:
            conn.execute("DELETE FROM instances WHERE id = ?", (instance_id,))
        _kubectl(['delete', 'deployment', deployment_name, '-n', namespace, '--ignore-not-found=true'])
        _kubectl(['delete', 'svc', f'{deployment_name}-ui', '-n', namespace, '--ignore-not-found=true'])
        _kubectl(['delete', 'pvc', pvc_name, '-n', namespace, '--ignore-not-found=true'])
        if kubeconfig_secret:
            _kubectl(['delete', 'secret', kubeconfig_secret, '-n', namespace, '--ignore-not-found=true'])
        raise


def delete_instance(instance_id: int, owner_id: int) -> bool:
    with get_db() as conn:
        row = conn.execute(
            'SELECT * FROM instances WHERE id = ? AND owner_id = ?',
            (instance_id, owner_id)
        ).fetchone()
        if not row:
            return False
        row = dict(row)

    ns = row['namespace']
    for resource in [
        f"deployment/{row['deployment_name']}",
        f"service/{row['service_name']}",
        f"pvc/{row['pvc_name']}",
    ]:
        _kubectl(['delete', resource, '-n', ns, '--ignore-not-found=true'])

    if row.get('kubeconfig_secret'):
        _kubectl(['delete', 'secret', row['kubeconfig_secret'], '-n', ns, '--ignore-not-found=true'])
    if _is_oc():
        _kubectl(['delete', 'route', f"{row['deployment_name']}-ui", '-n', ns, '--ignore-not-found=true'])

    wl_ns = row.get('workload_namespace', '')
    if wl_ns and wl_ns != ns:
        _kubectl(['delete', 'namespace', wl_ns, '--ignore-not-found=true'])

    with get_db() as conn:
        conn.execute('DELETE FROM instances WHERE id = ?', (instance_id,))
    return True


def list_instances(owner_id: int, group_id: int = None) -> List[Dict]:
    with get_db() as conn:
        if group_id is not None:
            rows = conn.execute(
                'SELECT * FROM instances WHERE owner_id = ? AND group_id = ? ORDER BY created_at DESC',
                (owner_id, group_id)
            ).fetchall()
        else:
            rows = conn.execute(
                'SELECT * FROM instances WHERE owner_id = ? ORDER BY created_at DESC',
                (owner_id,)
            ).fetchall()

    instances = []
    for row in rows:
        inst = dict(row)
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
