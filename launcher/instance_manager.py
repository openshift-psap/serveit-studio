"""Instance lifecycle management — create, delete, list ServeIt Studio instances.

UI pods live in the shared launcher namespace (e.g. 'serveit').
Workloads (LWS, guidellm, EPP) get their own per-instance namespace.
Instances are organized into clusters (local or remote).
"""

import json
import logging
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict

logger = logging.getLogger(__name__)

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
    s = re.sub(r'[^a-z0-9-]', '-', name.lower())
    s = re.sub(r'-+', '-', s).strip('-')[:40].strip('-')
    return s or 'unnamed'


def _validate_kubeconfig(kubeconfig_data: str) -> tuple:
    """Validate kubeconfig connectivity and return (cluster_url, cleaned_kubeconfig).

    Tests each context in the kubeconfig to find one that connects to a
    remote cluster (not the launcher's own cluster). Sets it as current-context
    so the stored Secret always uses the correct context.
    """
    import yaml
    try:
        kc = yaml.safe_load(kubeconfig_data)
    except Exception:
        kc = None

    # Detect the launcher's own cluster URL to exclude it
    local_server = None
    try:
        r = subprocess.run(['kubectl', 'cluster-info'], capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            for line in r.stdout.split('\n'):
                if 'is running at' in line:
                    local_server = line.split('is running at')[-1].strip().split()[0]
                    # Strip ANSI color codes
                    import re
                    local_server = re.sub(r'\x1b\[[0-9;]*m', '', local_server).strip()
                    break
    except Exception:
        pass

    target = None

    if kc:
        # Build a map of cluster name → server URL
        cluster_servers = {}
        for cl in kc.get('clusters', []):
            cluster_servers[cl.get('name', '')] = cl.get('cluster', {}).get('server', '')

        # Find the first context pointing to a non-local cluster
        for ctx in kc.get('contexts', []):
            ctx_cluster = ctx.get('context', {}).get('cluster', '')
            server = cluster_servers.get(ctx_cluster, '')
            if not server:
                continue
            # Skip if it's the launcher's own cluster
            if local_server and server.rstrip('/') == local_server.rstrip('/'):
                continue
            # Found a remote cluster context
            kc['current-context'] = ctx.get('name')
            target = server
            kubeconfig_data = yaml.dump(kc, default_flow_style=False)
            break

    if not target:
        target = 'remote'

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
                f"Cannot connect to cluster {target}. "
                f"Verify the kubeconfig is correct and the cluster is reachable.\n"
                f"Error: {r.stderr.strip()[:200]}")
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"Connection to {target} timed out. "
            f"Verify the cluster is reachable from this network.")
    finally:
        os.unlink(tmp_path)

    return target, kubeconfig_data


# ── Cluster CRUD ─────────────────────────────────────────────────────────────

def create_cluster(owner_id: int, name: str, icon: str = '🖥️',
                   namespace: str = 'serveit',
                   kubeconfig_data: str = None,
                   storage_class: str = None) -> Dict:
    """Create a cluster entry. If kubeconfig is provided, validates it and stores as K8s Secret."""
    target_cluster = 'local'
    kubeconfig_secret = None

    if kubeconfig_data:
        target_cluster, kubeconfig_data = _validate_kubeconfig(kubeconfig_data)

        # Check duplicate cluster URL
        with get_db() as conn:
            existing = conn.execute(
                'SELECT id FROM clusters WHERE owner_id = ? AND target_cluster = ?',
                (owner_id, target_cluster)
            ).fetchone()
            if existing:
                raise RuntimeError(f'You already have a cluster targeting {target_cluster}')

        with get_db() as conn:
            user_row = conn.execute('SELECT username FROM users WHERE id = ?', (owner_id,)).fetchone()
        owner_name = _sanitize(user_row['username']) if user_row else str(owner_id)
        safe = _sanitize(name)
        kubeconfig_secret = f"serveit-kubeconfig-{owner_name}-{safe}"
        secret_yaml = json.dumps({
            "apiVersion": "v1", "kind": "Secret",
            "metadata": {"name": kubeconfig_secret, "namespace": namespace},
            "type": "Opaque",
            "stringData": {"kubeconfig": kubeconfig_data}
        })
        r = _kubectl(['apply', '-f', '-', '-n', namespace], input_data=secret_yaml)
        if r.returncode != 0:
            raise RuntimeError(f"Failed to create kubeconfig Secret: {r.stderr}")
    else:
        # Check: only one local cluster per user
        with get_db() as conn:
            existing = conn.execute(
                "SELECT id FROM clusters WHERE owner_id = ? AND target_cluster = 'local'",
                (owner_id,)
            ).fetchone()
            if existing:
                raise RuntimeError('You already have a local cluster')

    with get_db() as conn:
        conn.execute(
            "INSERT INTO clusters (name, icon, owner_id, kubeconfig_secret, target_cluster, storage_class, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (name, icon, owner_id, kubeconfig_secret, target_cluster, storage_class, datetime.now().isoformat())
        )
        cid = conn.execute('SELECT last_insert_rowid()').fetchone()[0]

    return {'id': cid, 'name': name, 'icon': icon, 'target_cluster': target_cluster}


def delete_cluster(cluster_id: int, owner_id: int) -> bool:
    with get_db() as conn:
        row = conn.execute(
            'SELECT * FROM clusters WHERE id = ? AND owner_id = ?',
            (cluster_id, owner_id)
        ).fetchone()
        if not row:
            return False
        row = dict(row)
        instances = conn.execute(
            'SELECT id FROM instances WHERE cluster_id = ? AND owner_id = ?',
            (cluster_id, owner_id)
        ).fetchall()

    for inst in instances:
        delete_instance(inst['id'], owner_id)

    # Delete kubeconfig secret if it exists
    if row.get('kubeconfig_secret'):
        _kubectl(['delete', 'secret', row['kubeconfig_secret'], '-n', 'serveit', '--ignore-not-found=true'])

    with get_db() as conn:
        conn.execute('DELETE FROM clusters WHERE id = ?', (cluster_id,))
    return True


def list_clusters(owner_id: int) -> List[Dict]:
    with get_db() as conn:
        rows = conn.execute('''
            SELECT c.*,
                   COUNT(DISTINCT CASE WHEN i.owner_id = ? OR ia.user_id IS NOT NULL THEN i.id END) as instance_count,
                   CASE WHEN c.owner_id = ? THEN 1 ELSE 0 END as is_cluster_owner
            FROM clusters c
            LEFT JOIN instances i ON c.id = i.cluster_id
            LEFT JOIN instance_access ia ON i.id = ia.instance_id AND ia.user_id = ?
            WHERE c.owner_id = ?
               OR c.id IN (
                   SELECT i2.cluster_id FROM instances i2
                   JOIN instance_access ia2 ON i2.id = ia2.instance_id
                   WHERE ia2.user_id = ?
               )
            GROUP BY c.id ORDER BY c.created_at
        ''', (owner_id, owner_id, owner_id, owner_id, owner_id)).fetchall()
    return [dict(r) for r in rows]


def update_cluster(cluster_id: int, owner_id: int, name: str = None, icon: str = None) -> bool:
    with get_db() as conn:
        row = conn.execute(
            'SELECT id FROM clusters WHERE id = ? AND owner_id = ?',
            (cluster_id, owner_id)
        ).fetchone()
        if not row:
            return False
        if name is not None:
            conn.execute('UPDATE clusters SET name = ? WHERE id = ?', (name, cluster_id))
        if icon is not None:
            conn.execute('UPDATE clusters SET icon = ? WHERE id = ?', (icon, cluster_id))
    return True


def _seed_instance_user(deployment_name: str, namespace: str, username: str, password_hash: str):
    """Seed user credentials into instance DB. Retries until pod is Running and exec succeeds."""
    import base64

    b64user = base64.b64encode(username.encode()).decode()
    b64hash = base64.b64encode(password_hash.encode()).decode()
    seed_script = (
        "import sqlite3,os,base64;"
        f"u=base64.b64decode('{b64user}').decode();"
        f"h=base64.b64decode('{b64hash}').decode();"
        "db=os.environ.get('DB_PATH','/mnt/storage/serveit.db');"
        "c=sqlite3.connect(db);"
        "c.execute('CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, created_at TEXT NOT NULL)');"
        "c.execute('INSERT OR IGNORE INTO users (username, password_hash, created_at) VALUES (?, ?, datetime(\"now\"))',(u,h));"
        "c.commit();c.close()"
    )

    for attempt in range(90):
        r = _kubectl(['get', 'pod', '-l', f'app={deployment_name}', '-n', namespace,
                      '-o', 'jsonpath={.items[0].status.phase}'])
        if r.stdout.strip() != 'Running':
            time.sleep(3)
            continue

        # Wait for server to be ready (port 5000) before seeding
        r = _kubectl(['exec', '-n', namespace, f'deploy/{deployment_name}', '--',
                      'python3', '-c',
                      "import socket;s=socket.socket();s.settimeout(1);s.connect(('127.0.0.1',5000));s.close()"])
        if r.returncode != 0:
            time.sleep(3)
            continue

        r = _kubectl(['exec', '-n', namespace, f'deploy/{deployment_name}', '--',
                      'python3', '-c', seed_script])
        if r.returncode == 0:
            return
        time.sleep(2)


# ── User Management (admin) ──────────────────────────────────────────────────

def list_users() -> List[Dict]:
    with get_db() as conn:
        rows = conn.execute('''
            SELECT u.id, u.username, u.is_admin, u.created_at,
                   COUNT(DISTINCT c.id) as cluster_count,
                   COUNT(DISTINCT i.id) as instance_count,
                   COUNT(DISTINCT ia.instance_id) as assigned_count
            FROM users u
            LEFT JOIN clusters c ON u.id = c.owner_id
            LEFT JOIN instances i ON u.id = i.owner_id
            LEFT JOIN instance_access ia ON u.id = ia.user_id
            GROUP BY u.id ORDER BY u.created_at
        ''').fetchall()
    return [dict(r) for r in rows]


def delete_user(user_id: int) -> bool:
    with get_db() as conn:
        row = conn.execute('SELECT id, is_admin FROM users WHERE id = ?', (user_id,)).fetchone()
        if not row:
            return False
        if row['is_admin']:
            return False
        clusters = conn.execute('SELECT id FROM clusters WHERE owner_id = ?', (user_id,)).fetchall()

    for c in clusters:
        delete_cluster(c['id'], user_id)

    with get_db() as conn:
        conn.execute('DELETE FROM users WHERE id = ?', (user_id,))
    return True


# ── Instance Access (assignment) ─────────────────────────────────────────────

def list_instance_users(instance_id: int) -> List[Dict]:
    with get_db() as conn:
        rows = conn.execute('''
            SELECT u.id, u.username, ia.granted_at
            FROM instance_access ia
            JOIN users u ON ia.user_id = u.id
            WHERE ia.instance_id = ?
            ORDER BY ia.granted_at
        ''', (instance_id,)).fetchall()
    return [dict(r) for r in rows]


def assign_instance_user(instance_id: int, user_id: int, granted_by: int) -> bool:
    with get_db() as conn:
        inst = conn.execute('SELECT owner_id FROM instances WHERE id = ?', (instance_id,)).fetchone()
        if not inst:
            return False
        if inst['owner_id'] == user_id:
            return False
        try:
            conn.execute(
                'INSERT INTO instance_access (user_id, instance_id, granted_at, granted_by) VALUES (?, ?, ?, ?)',
                (user_id, instance_id, datetime.now().isoformat(), granted_by)
            )
            return True
        except Exception:
            return False


def revoke_instance_user(instance_id: int, user_id: int) -> bool:
    with get_db() as conn:
        cursor = conn.execute(
            'DELETE FROM instance_access WHERE user_id = ? AND instance_id = ?',
            (user_id, instance_id)
        )
        return cursor.rowcount > 0


def get_user_assigned_instances(user_id: int) -> List[Dict]:
    with get_db() as conn:
        rows = conn.execute('''
            SELECT i.id, i.name, i.display_name, i.status,
                   c.name as cluster_name, c.icon as cluster_icon,
                   u.username as owner_name, ia.granted_at
            FROM instance_access ia
            JOIN instances i ON ia.instance_id = i.id
            JOIN clusters c ON i.cluster_id = c.id
            JOIN users u ON i.owner_id = u.id
            WHERE ia.user_id = ?
            ORDER BY ia.granted_at DESC
        ''', (user_id,)).fetchall()
    return [dict(r) for r in rows]


# ── Instance CRUD ───────────────────────────────────────────────────────────

def create_instance(owner_id: int, username: str, name: str,
                    cluster_id: int = None,
                    namespace: str = 'serveit',
                    image: str = 'quay.io/bbenshab/inftune-studio:server',
                    password_hash: str = None,
                    preset_gpus: int = None,
                    preset_nodes: list = None,
                    storage_size: int = None,
                    storage_class_override: str = None) -> Dict:
    """Create an instance. Kubeconfig and storage class come from the cluster."""

    # Look up cluster for kubeconfig and storage class
    kubeconfig_secret = None
    target_cluster = 'local'
    storage_class = None
    kubeconfig_data = None

    if cluster_id:
        with get_db() as conn:
            cluster = conn.execute('SELECT * FROM clusters WHERE id = ?', (cluster_id,)).fetchone()
            if cluster:
                cluster = dict(cluster)
                kubeconfig_secret = cluster.get('kubeconfig_secret')
                target_cluster = cluster.get('target_cluster', 'local')
                storage_class = cluster.get('storage_class')
                # Retrieve kubeconfig data from K8s secret if needed for remote setup
                if kubeconfig_secret:
                    import base64
                    r = _kubectl(['get', 'secret', kubeconfig_secret, '-n', namespace,
                                  '-o', 'jsonpath={.data.kubeconfig}'])
                    if r.returncode == 0 and r.stdout.strip():
                        try:
                            kubeconfig_data = base64.b64decode(r.stdout.strip()).decode()
                        except Exception:
                            pass

    # Include cluster name in resource names to avoid collisions across clusters
    cluster_suffix = ''
    if cluster_id:
        with get_db() as conn:
            cr = conn.execute('SELECT name FROM clusters WHERE id = ?', (cluster_id,)).fetchone()
            if cr:
                cluster_suffix = f"-{_sanitize(cr['name'])}"
    safe_name = _sanitize(f"{username}-{name}{cluster_suffix}")
    workload_namespace = f"serveit-{safe_name}"
    deployment_name = f"serveit-{safe_name}"
    pvc_name = f"serveit-{safe_name}-storage"
    service_name = f"serveit-{safe_name}-ui"

    # Set up remote workload namespace if remote cluster
    if kubeconfig_data:
        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', suffix='.kubeconfig', delete=False) as tmp:
            tmp.write(kubeconfig_data)
            tmp_path = tmp.name
        try:
            def _remote(args, input=None):
                return subprocess.run(['kubectl', '--kubeconfig', tmp_path] + args,
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
                    "metadata": {"name": f"serveit-prometheus-{safe_name}"},
                    "subjects": [{"kind": "ServiceAccount", "name": "default",
                                  "namespace": workload_namespace}],
                    "roleRef": {"kind": "ClusterRole", "name": "prometheus-k8s",
                                "apiGroup": "rbac.authorization.k8s.io"}
                })
                _remote(['apply', '-f', '-'], input=prom_yaml)

                # Create SCC for modelserver (allows IPC_LOCK, SYS_RAWIO, runAsUser:0)
                scc_name = f"llm-d-modelserver-scc-{workload_namespace}"
                scc_check = _remote(['get', 'scc', scc_name])
                if scc_check.returncode != 0:
                    scc_yaml = json.dumps({
                        "apiVersion": "security.openshift.io/v1",
                        "kind": "SecurityContextConstraints",
                        "metadata": {"name": scc_name},
                        "allowHostDirVolumePlugin": True,
                        "allowHostIPC": False,
                        "allowHostNetwork": False,
                        "allowHostPID": False,
                        "allowHostPorts": False,
                        "allowPrivilegedContainer": True,
                        "allowedCapabilities": ["IPC_LOCK", "SYS_RAWIO", "SYS_RESOURCE"],
                        "fsGroup": {"type": "RunAsAny"},
                        "runAsUser": {"type": "RunAsAny"},
                        "seLinuxContext": {"type": "RunAsAny"},
                        "supplementalGroups": {"type": "RunAsAny"},
                        "users": [f"system:serviceaccount:{workload_namespace}:llm-d-modelserver"],
                        "volumes": ["*"]
                    })
                    r_scc = _remote(['apply', '-f', '-'], input=scc_yaml)
                    if r_scc.returncode == 0:
                        logger.info(f"Created SCC {scc_name} for modelserver")

            remote_rbac = json.dumps({
                "apiVersion": "v1", "kind": "List", "items": [
                    {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role",
                     "metadata": {"name": "serveit-full-access", "namespace": workload_namespace},
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
                     "metadata": {"name": "serveit-access", "namespace": workload_namespace},
                     "subjects": [{"kind": "ServiceAccount", "name": "default", "namespace": workload_namespace}],
                     "roleRef": {"kind": "Role", "name": "serveit-full-access",
                                 "apiGroup": "rbac.authorization.k8s.io"}},
                ]
            })
            _remote(['apply', '-f', '-'], input=remote_rbac)
        finally:
            os.unlink(tmp_path)

    import secrets
    auto_login_token = secrets.token_urlsafe(32)

    with get_db() as conn:
        conn.execute('''
            INSERT INTO instances (name, owner_id, cluster_id, display_name, status, namespace,
                                   workload_namespace, deployment_name, pvc_name, service_name,
                                   kubeconfig_secret, target_cluster, auto_login_token,
                                   preset_gpus, preset_nodes, storage_class, storage_size, created_at)
            VALUES (?, ?, ?, ?, 'creating', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (safe_name, owner_id, cluster_id, name, namespace, workload_namespace,
              deployment_name, pvc_name, service_name,
              kubeconfig_secret, target_cluster, auto_login_token,
              preset_gpus, ','.join(preset_nodes) if preset_nodes else None,
              storage_class_override or storage_class or '',
              f'{storage_size or 5}Gi',
              datetime.now().isoformat()))
        instance_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]

    try:
        pvc_yaml = _render('pvc.yaml.j2',
            pvc_name=pvc_name, namespace=namespace,
            storage_size=f'{storage_size or 5}Gi', storage_class=storage_class_override or storage_class or '')
        r = _kubectl(['apply', '-f', '-', '-n', namespace], input_data=pvc_yaml)
        if r.returncode != 0:
            raise RuntimeError(f"PVC creation failed: {r.stderr}")

        deploy_yaml = _render('instance-deployment.yaml.j2',
            name=deployment_name, namespace=namespace, image=image,
            pvc_name=pvc_name, code_pvc_name='',
            workload_namespace=workload_namespace,
            dev_mode='false', force_nad='false',
            auth_disabled='false',
            kubeconfig_secret=kubeconfig_secret or '',
            has_kubeconfig='true' if kubeconfig_secret else 'false',
            preset_gpus=preset_gpus or '',
            preset_nodes=','.join(preset_nodes) if preset_nodes else '',
            auto_login_token=auto_login_token)

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

        if password_hash:
            _seed_instance_user(deployment_name, namespace, username, password_hash)

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
        raise


def _backup_instance_db(deployment_name: str, namespace: str, instance_name: str):
    """Copy instance database to launcher storage before deletion."""
    backup_dir = Path('/mnt/storage/backups') / instance_name
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
        r = _kubectl(['get', 'pod', '-l', f'app={deployment_name}', '-n', namespace,
                      '-o', 'jsonpath={.items[0].metadata.name}'])
        pod_name = r.stdout.strip() if r.returncode == 0 else ''
        if not pod_name:
            return
        dest = str(backup_dir / 'serveit.db')
        cmd = 'oc' if _is_oc() else 'kubectl'
        subprocess.run(
            [cmd, 'cp', f'{namespace}/{pod_name}:/mnt/storage/serveit.db', dest],
            capture_output=True, timeout=60
        )
        ts_file = backup_dir / 'backup_info.txt'
        ts_file.write_text(f"Instance: {instance_name}\nBackup: {datetime.now().isoformat()}\nPod: {pod_name}\n")
    except Exception:
        pass


def delete_instance(instance_id: int, owner_id: int, backup: bool = True) -> bool:
    with get_db() as conn:
        row = conn.execute(
            'SELECT * FROM instances WHERE id = ? AND owner_id = ?',
            (instance_id, owner_id)
        ).fetchone()
        if not row:
            return False
        row = dict(row)
        conn.execute("UPDATE instances SET status = 'deleting' WHERE id = ?", (instance_id,))

    import threading
    threading.Thread(target=_delete_instance_async, args=(instance_id, row, backup), daemon=True).start()
    return True


def _delete_instance_async(instance_id: int, row: dict, backup: bool):
    ns = row['namespace']

    try:
        if backup:
            try:
                _backup_instance_db(row['deployment_name'], ns, row.get('name', str(instance_id)))
            except Exception:
                pass

        for resource in [
            f"deployment/{row['deployment_name']}",
            f"service/{row['service_name']}",
            f"pvc/{row['pvc_name']}",
        ]:
            _kubectl(['delete', resource, '-n', ns, '--ignore-not-found=true'])

        if _is_oc():
            _kubectl(['delete', 'route', f"{row['deployment_name']}-ui", '-n', ns, '--ignore-not-found=true'])

        if row.get('kubeconfig_secret'):
            try:
                _cleanup_remote_cluster(row['kubeconfig_secret'], ns, row.get('workload_namespace', ''))
            except Exception:
                logger.warning(f"Remote cleanup failed for instance {instance_id}, continuing with deletion")

        wl_ns = row.get('workload_namespace', '')
        if wl_ns and wl_ns != ns and not row.get('kubeconfig_secret'):
            _kubectl(['delete', 'namespace', wl_ns, '--ignore-not-found=true'])
    except Exception as e:
        logger.error(f"Error during async deletion of instance {instance_id}: {e}")
    finally:
        with get_db() as conn:
            conn.execute('DELETE FROM instances WHERE id = ?', (instance_id,))


def _cleanup_remote_cluster(kubeconfig_secret: str, namespace: str, workload_namespace: str):
    if not workload_namespace:
        return
    import tempfile, base64
    r = _kubectl(['get', 'secret', kubeconfig_secret, '-n', namespace,
                  '-o', 'jsonpath={.data.kubeconfig}'])
    if r.returncode != 0 or not r.stdout.strip():
        return
    try:
        kubeconfig_data = base64.b64decode(r.stdout.strip()).decode()
    except Exception:
        return
    with tempfile.NamedTemporaryFile(mode='w', suffix='.kubeconfig', delete=False) as tmp:
        tmp.write(kubeconfig_data)
        tmp_path = tmp.name
    try:
        subprocess.run(['kubectl', '--kubeconfig', tmp_path, 'delete', 'namespace',
                        workload_namespace, '--ignore-not-found=true'],
                       capture_output=True, text=True, timeout=60)
    except Exception:
        pass
    finally:
        os.unlink(tmp_path)


def list_instances(owner_id: int, cluster_id: int = None) -> List[Dict]:
    with get_db() as conn:
        if cluster_id is not None:
            rows = conn.execute('''
                SELECT DISTINCT i.*,
                    CASE WHEN i.owner_id = ? THEN 1 ELSE 0 END as is_owner
                FROM instances i
                LEFT JOIN instance_access ia ON i.id = ia.instance_id AND ia.user_id = ?
                WHERE (i.owner_id = ? OR ia.user_id = ?)
                  AND i.cluster_id = ?
                ORDER BY i.created_at DESC
            ''', (owner_id, owner_id, owner_id, owner_id, cluster_id)).fetchall()
        else:
            rows = conn.execute('''
                SELECT DISTINCT i.*,
                    CASE WHEN i.owner_id = ? THEN 1 ELSE 0 END as is_owner
                FROM instances i
                LEFT JOIN instance_access ia ON i.id = ia.instance_id AND ia.user_id = ?
                WHERE (i.owner_id = ? OR ia.user_id = ?)
                ORDER BY i.created_at DESC
            ''', (owner_id, owner_id, owner_id, owner_id)).fetchall()

    # Fallback: look up storage class from cluster for old instances without it
    cluster_sc = {}
    with get_db() as conn:
        for cr in conn.execute('SELECT id, storage_class FROM clusters').fetchall():
            cluster_sc[cr['id']] = cr['storage_class'] or ''

    instances = []
    for row in rows:
        inst = dict(row)
        if not inst.get('storage_class'):
            try:
                r = _kubectl(['get', 'pvc', inst.get('pvc_name', ''), '-n', inst['namespace'],
                              '-o', 'jsonpath={.spec.storageClassName}'])
                if r.returncode == 0 and r.stdout.strip():
                    inst['storage_class'] = r.stdout.strip()
                    with get_db() as conn:
                        conn.execute('UPDATE instances SET storage_class = ? WHERE id = ?',
                                     (inst['storage_class'], inst['id']))
            except Exception:
                inst['storage_class'] = cluster_sc.get(inst.get('cluster_id'), '')
        if inst.get('status') == 'deleting':
            inst['pod_status'] = 'Deleting'
            instances.append(inst)
            continue
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
        # If service_url is missing, try to get it from the Route (OpenShift)
        if not inst.get('service_url') and inst['pod_status'] == 'Running':
            try:
                r = _kubectl(['get', 'route', f"{inst['deployment_name']}-ui", '-n', inst['namespace'],
                              '-o', 'jsonpath={.spec.host}'])
                if r.returncode == 0 and r.stdout.strip():
                    inst['service_url'] = f"https://{r.stdout.strip()}"
                    with get_db() as conn:
                        conn.execute('UPDATE instances SET service_url = ? WHERE id = ?',
                                     (inst['service_url'], inst['id']))
            except Exception:
                pass

        instances.append(inst)

    return instances
