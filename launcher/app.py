"""ServeIt Studio Launcher — multi-user control plane."""

import os
import json
from datetime import timedelta
from functools import wraps
from flask import Flask, render_template, jsonify, request, session

from launcher.database import init_db, get_db
from launcher.auth import register_auth_routes, get_user_id, get_username, is_admin, create_user, reset_password
from launcher import instance_manager


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not is_admin():
            return jsonify({'error': 'Admin access required'}), 403
        return f(*args, **kwargs)
    return decorated


def create_app():
    app = Flask(__name__, template_folder='templates', static_folder='static')

    key_file = os.path.join(os.environ.get('HOME_STORAGE_DIR', '/mnt/storage'), '.launcher_secret')
    if os.path.exists(key_file):
        app.config['SECRET_KEY'] = open(key_file).read().strip()
    else:
        key = os.urandom(32).hex()
        os.makedirs(os.path.dirname(key_file), exist_ok=True)
        with open(key_file, 'w') as f:
            f.write(key)
        app.config['SECRET_KEY'] = key

    app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=24)

    register_auth_routes(app)

    namespace = os.environ.get('TARGET_NAMESPACE', 'serveit')
    image = os.environ.get('INFTUNE_IMAGE', 'quay.io/bbenshab/serveit-studio:server')

    # ── Dashboard ──

    @app.route('/')
    def dashboard():
        clusters = instance_manager.list_clusters(get_user_id())
        return render_template('dashboard.html',
                               username=get_username(),
                               is_admin=is_admin(),
                               clusters=clusters)

    # ── User API (admin only) ──

    @app.route('/api/users', methods=['GET'])
    @admin_required
    def api_list_users():
        return jsonify(instance_manager.list_users())

    @app.route('/api/users', methods=['POST'])
    @admin_required
    def api_create_user():
        data = request.get_json() or {}
        username = data.get('username', '').strip()
        password = data.get('password', '')
        if not username or len(username) < 3:
            return jsonify({'error': 'Username must be at least 3 characters'}), 400
        if not password or len(password) < 6:
            return jsonify({'error': 'Password must be at least 6 characters'}), 400
        try:
            create_user(username, password)
            return jsonify({'ok': True, 'username': username})
        except Exception as e:
            if 'UNIQUE' in str(e):
                return jsonify({'error': f'User "{username}" already exists'}), 409
            return jsonify({'error': str(e)}), 500

    @app.route('/api/users/<int:user_id>', methods=['DELETE'])
    @admin_required
    def api_delete_user(user_id):
        if user_id == get_user_id():
            return jsonify({'error': 'Cannot delete yourself'}), 400
        success = instance_manager.delete_user(user_id)
        if success:
            return jsonify({'ok': True})
        return jsonify({'error': 'User not found or is admin'}), 404

    @app.route('/api/users/<int:user_id>/reset-password', methods=['POST'])
    @admin_required
    def api_reset_password(user_id):
        data = request.get_json() or {}
        password = data.get('password', '')
        if not password or len(password) < 6:
            return jsonify({'error': 'Password must be at least 6 characters'}), 400
        reset_password(user_id, password)
        return jsonify({'ok': True})

    # ── Cluster API ──

    @app.route('/api/clusters', methods=['GET'])
    def api_list_clusters():
        uid = request.args.get('user_id', type=int)
        if uid and is_admin():
            return jsonify(instance_manager.list_clusters(uid))
        return jsonify(instance_manager.list_clusters(get_user_id()))

    @app.route('/api/clusters', methods=['POST'])
    def api_create_cluster():
        data = request.get_json() or {}
        name = data.get('name', '').strip()
        icon = data.get('icon', '🖥️').strip()
        if not name:
            return jsonify({'error': 'Cluster name is required'}), 400

        kubeconfig_data = data.get('kubeconfig')
        storage_class = data.get('storage_class') or os.environ.get('STORAGE_CLASS')
        proxy = data.get('proxy') or None
        api_server_ip = data.get('api_server_ip') or None
        description = data.get('description') or None

        try:
            result = instance_manager.create_cluster(
                get_user_id(), name, icon,
                namespace=namespace,
                kubeconfig_data=kubeconfig_data,
                storage_class=storage_class,
                proxy=proxy,
                api_server_ip=api_server_ip,
                description=description)
            return jsonify(result)
        except Exception as e:
            if 'UNIQUE' in str(e):
                return jsonify({'error': f'Cluster "{name}" already exists'}), 409
            return jsonify({'error': str(e)}), 500

    @app.route('/api/clusters/<int:cluster_id>', methods=['DELETE'])
    def api_delete_cluster(cluster_id):
        user_id = get_user_id()
        success = instance_manager.delete_cluster(cluster_id, user_id, is_admin=is_admin())
        if success:
            return jsonify({'ok': True})
        return jsonify({'error': 'Cluster not found'}), 404

    @app.route('/api/clusters/<int:cluster_id>', methods=['PUT'])
    def api_update_cluster(cluster_id):
        data = request.get_json() or {}
        success = instance_manager.update_cluster(
            cluster_id, get_user_id(),
            name=data.get('name'), icon=data.get('icon'),
            description=data.get('description'))
        if success:
            return jsonify({'ok': True})
        return jsonify({'error': 'Cluster not found'}), 404

    @app.route('/api/clusters/<int:cluster_id>/scan', methods=['GET'])
    def api_get_cluster_scan(cluster_id):
        """Get cached cluster scan results."""
        with get_db() as conn:
            row = conn.execute(
                'SELECT scan_data FROM clusters WHERE id = ? AND owner_id = ?',
                (cluster_id, get_user_id())
            ).fetchone()
        if not row:
            return jsonify({'error': 'Cluster not found'}), 404
        if row['scan_data']:
            return jsonify(json.loads(row['scan_data']))
        return jsonify({'not_scanned': True})

    @app.route('/api/clusters/<int:cluster_id>/scan', methods=['POST'])
    def api_scan_cluster(cluster_id):
        """Scan cluster resources and save to DB."""
        from launcher.cluster_scanner import scan_cluster_resources
        from datetime import datetime
        with get_db() as conn:
            cluster = conn.execute(
                'SELECT * FROM clusters WHERE id = ? AND owner_id = ?',
                (cluster_id, get_user_id())
            ).fetchone()
        if not cluster:
            return jsonify({'error': 'Cluster not found'}), 404
        try:
            result = scan_cluster_resources(dict(cluster), namespace)
            with get_db() as conn:
                conn.execute(
                    'UPDATE clusters SET scan_data = ?, scanned_at = ? WHERE id = ?',
                    (json.dumps(result), datetime.now().isoformat(), cluster_id)
                )
            return jsonify(result)
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    # ── Instance API ──

    @app.route('/api/instances', methods=['GET'])
    def api_list_instances():
        cluster_id = request.args.get('cluster_id', type=int)
        uid = request.args.get('user_id', type=int)
        if uid and is_admin():
            instances = instance_manager.list_instances(uid, cluster_id=cluster_id)
        else:
            instances = instance_manager.list_instances(get_user_id(), cluster_id=cluster_id)
        return jsonify(instances)

    @app.route('/api/instances', methods=['POST'])
    def api_create_instance():
        data = request.get_json() or {}
        name = data.get('name', '').strip()
        cluster_id = data.get('cluster_id')
        if not name:
            return jsonify({'error': 'Name is required'}), 400
        if not cluster_id:
            return jsonify({'error': 'Cluster is required'}), 400

        with get_db() as conn:
            existing = conn.execute(
                'SELECT id FROM instances WHERE owner_id = ? AND name = ? AND cluster_id = ?',
                (get_user_id(), instance_manager._sanitize(f"{get_username()}-{name}"), cluster_id)
            ).fetchone()
            if existing:
                return jsonify({'error': f'You already have an instance named "{name}" on this cluster.'}), 409

        with get_db() as conn:
            user_row = conn.execute('SELECT password_hash FROM users WHERE id = ?', (get_user_id(),)).fetchone()
        pwd_hash = user_row['password_hash'] if user_row else None

        preset_gpus = data.get('preset_gpus')
        preset_nodes = data.get('preset_nodes')
        storage_size = data.get('storage_size')
        storage_class = data.get('storage_class')

        import threading

        owner_id = get_user_id()
        username = get_username()

        def _create():
            try:
                instance_manager.create_instance(
                    owner_id=owner_id,
                    username=username,
                    name=name,
                    cluster_id=int(cluster_id),
                    namespace=namespace,
                    image=image,
                    password_hash=pwd_hash,
                    preset_gpus=int(preset_gpus) if preset_gpus else None,
                    preset_nodes=preset_nodes if preset_nodes else None,
                    storage_size=int(storage_size) if storage_size else None,
                    storage_class_override=storage_class,
                )
            except Exception as e:
                print(f"Instance creation failed: {e}")
                with get_db() as conn:
                    safe = instance_manager._sanitize(f"{username}-{name}")
                    conn.execute("UPDATE instances SET status = 'error' WHERE name = ? AND owner_id = ?",
                                 (safe, owner_id))

        threading.Thread(target=_create, daemon=True).start()
        return jsonify({'ok': True, 'status': 'creating', 'name': name})

    @app.route('/api/storage_classes', methods=['GET'])
    def api_storage_classes():
        import subprocess
        try:
            cmd = 'oc' if os.path.exists('/usr/local/bin/oc') else 'kubectl'
            r = subprocess.run([cmd, 'get', 'sc', '-o', 'json'],
                               capture_output=True, text=True, timeout=15)
            if r.returncode != 0:
                return jsonify([])
            import json as _json
            data = _json.loads(r.stdout)
            classes = []
            excluded_names = ('rbd', 'snapshot', 'rgw', 'noobaa', 's3', 'cos')
            excluded_provisioners = ('rbd', 'no-provisioner', '/obc', '/bucket')
            for item in data.get('items', []):
                sc_name = item['metadata']['name']
                provisioner = item.get('provisioner', '')
                if any(x in sc_name.lower() for x in excluded_names):
                    continue
                if any(x in provisioner.lower() for x in excluded_provisioners):
                    continue
                is_default = item['metadata'].get('annotations', {}).get(
                    'storageclass.kubernetes.io/is-default-class', 'false') == 'true'
                classes.append({'name': sc_name, 'provisioner': provisioner, 'is_default': is_default})
            classes.sort(key=lambda x: (not x['is_default'], x['name']))
            return jsonify(classes)
        except Exception:
            return jsonify([])

    @app.route('/api/instances/<int:instance_id>', methods=['DELETE'])
    def api_delete_instance(instance_id):
        backup = request.args.get('backup', '1') == '1'
        success = instance_manager.delete_instance(instance_id, get_user_id(), backup=backup)
        if success:
            return jsonify({'ok': True})
        with get_db() as conn:
            access = conn.execute(
                'SELECT 1 FROM instance_access WHERE instance_id = ? AND user_id = ?',
                (instance_id, get_user_id())
            ).fetchone()
        if access:
            return jsonify({'error': 'Only the instance owner can delete it'}), 403
        return jsonify({'error': 'Instance not found or not owned by you'}), 404

    @app.route('/api/instances/<int:instance_id>/backup', methods=['POST'])
    def api_backup_instance(instance_id):
        result = instance_manager.backup_instance(instance_id, get_user_id())
        if result.get('ok'):
            return jsonify(result)
        return jsonify(result), 400

    @app.route('/api/backups', methods=['GET'])
    def api_list_backups():
        backups = instance_manager.list_backups()
        # For backups without cluster_details in metadata, try live scan_data
        cluster_cache = {}
        for b in backups:
            if b.get('cluster_details'):
                continue
            cname = b.get('cluster', '')
            if not cname:
                continue
            if cname not in cluster_cache:
                try:
                    with get_db() as conn:
                        row = conn.execute('SELECT scan_data FROM clusters WHERE name = ?', (cname,)).fetchone()
                        if row and row['scan_data']:
                            sd = json.loads(row['scan_data'])
                            s = sd.get('summary', sd)
                            cluster_cache[cname] = {
                                'gpu_node_count': s.get('gpu_node_count'),
                                'total_gpus': s.get('total_gpus'),
                                'gpu_model': s.get('gpu_model'),
                                'ocp_version': s.get('ocp_version'),
                            }
                except Exception:
                    pass
            b['cluster_details'] = cluster_cache.get(cname)
        return jsonify(backups)

    @app.route('/api/backups/download')
    def api_download_backup():
        import tarfile
        import io
        from flask import Response
        backup_path = request.args.get('path', '')
        if not backup_path or not backup_path.startswith('/mnt/storage/backups/'):
            return jsonify({'error': 'Invalid path'}), 400
        from pathlib import Path
        backup_dir = Path(backup_path)
        if not backup_dir.exists():
            return jsonify({'error': 'Backup not found'}), 404
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode='w:gz', compresslevel=6) as tar:
            for f in backup_dir.rglob('*'):
                if f.is_file():
                    tar.add(str(f), arcname=f.relative_to(backup_dir))
        data = buf.getvalue()
        name = f"{backup_dir.parent.name}-{backup_dir.name}.tar.gz"
        return Response(data, mimetype='application/gzip',
                        headers={'Content-Disposition': f'attachment; filename={name}',
                                 'Content-Length': str(len(data))})

    @app.route('/api/backups/delete', methods=['POST'])
    def api_delete_backup():
        data = request.json or {}
        backup_path = data.get('path')
        if not backup_path:
            return jsonify({'ok': False, 'error': 'path required'}), 400
        result = instance_manager.delete_backup(backup_path)
        if result.get('ok'):
            return jsonify(result)
        return jsonify(result), 400

    @app.route('/api/backups/restore', methods=['POST'])
    def api_restore_backup():
        data = request.json or {}
        backup_path = data.get('backup_path')
        target_instance_id = data.get('target_instance_id')
        restore_db = data.get('restore_db', True)
        restore_artifacts = data.get('restore_artifacts', True)
        if not backup_path or not target_instance_id:
            return jsonify({'ok': False, 'error': 'backup_path and target_instance_id required'}), 400
        result = instance_manager.restore_backup(backup_path, int(target_instance_id), get_user_id(),
                                                  restore_db=restore_db, restore_artifacts=restore_artifacts)
        if result.get('ok'):
            return jsonify(result)
        return jsonify(result), 400

    # ── Instance access (assignment) endpoints ──

    @app.route('/api/instances/<int:instance_id>/users', methods=['GET'])
    @admin_required
    def api_list_instance_users(instance_id):
        users = instance_manager.list_instance_users(instance_id)
        return jsonify(users)

    @app.route('/api/instances/<int:instance_id>/users', methods=['POST'])
    @admin_required
    def api_assign_instance_users(instance_id):
        data = request.get_json() or {}
        user_ids = data.get('user_ids', [])
        if not user_ids:
            return jsonify({'error': 'No users specified'}), 400
        results = []
        for uid in user_ids:
            ok = instance_manager.assign_instance_user(instance_id, uid, get_user_id())
            results.append({'user_id': uid, 'assigned': ok})
        return jsonify({'ok': True, 'results': results})

    @app.route('/api/instances/<int:instance_id>/users/<int:user_id>', methods=['DELETE'])
    @admin_required
    def api_revoke_instance_user(instance_id, user_id):
        success = instance_manager.revoke_instance_user(instance_id, user_id)
        if success:
            return jsonify({'ok': True})
        return jsonify({'error': 'Assignment not found'}), 404

    @app.route('/api/users/<int:user_id>/assigned-instances', methods=['GET'])
    @admin_required
    def api_user_assigned_instances(user_id):
        instances = instance_manager.get_user_assigned_instances(user_id)
        return jsonify(instances)

    # ── Settings API ──

    @app.route('/api/settings', methods=['GET'])
    @admin_required
    def api_get_settings():
        with get_db() as conn:
            rows = conn.execute('SELECT key, value FROM settings').fetchall()
            return jsonify({r['key']: r['value'] for r in rows})

    @app.route('/api/settings', methods=['POST'])
    @admin_required
    def api_save_settings():
        data = request.get_json()
        with get_db() as conn:
            for key, value in data.items():
                conn.execute(
                    'INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)',
                    (key, str(value))
                )
        return jsonify({'success': True})

    return app


_scan_lock = __import__('threading').Lock()

def _auto_rescan_loop(app, namespace):
    """Background thread: periodically rescan all clusters."""
    import time
    from datetime import datetime
    from launcher.cluster_scanner import scan_cluster_resources

    with app.app_context():
        time.sleep(30)
        while True:
            try:
                with get_db() as conn:
                    rows = conn.execute('SELECT key, value FROM settings').fetchall()
                    settings = {r['key']: r['value'] for r in rows}

                enabled = settings.get('auto_rescan', 'true').lower() == 'true'
                interval = max(1, int(settings.get('rescan_interval_min', '10'))) * 60

                if not enabled:
                    time.sleep(60)
                    continue

                if not _scan_lock.acquire(blocking=False):
                    time.sleep(interval)
                    continue
                try:
                    with get_db() as conn:
                        clusters = conn.execute('SELECT * FROM clusters').fetchall()

                    for cluster in clusters:
                        try:
                            result = scan_cluster_resources(dict(cluster), namespace)
                            with get_db() as conn:
                                conn.execute(
                                    'UPDATE clusters SET scan_data = ?, scanned_at = ? WHERE id = ?',
                                    (json.dumps(result), datetime.now().isoformat(), cluster['id'])
                                )
                        except Exception as e:
                            print(f"  Auto-rescan failed for cluster {cluster['name']}: {e}")
                finally:
                    _scan_lock.release()

                time.sleep(interval)
            except Exception as e:
                print(f"  Auto-rescan loop error: {e}")
                time.sleep(60)


def main():
    print("=" * 60)
    print("ServeIt Studio Launcher — Multi-User Control Plane")
    print("=" * 60)

    init_db()

    app = create_app()

    import threading
    namespace = os.environ.get('NAMESPACE', 'inftune')
    t = threading.Thread(target=_auto_rescan_loop, args=(app, namespace), daemon=True)
    t.start()
    print(f"  Auto-rescan thread started (default: every 10 min)")

    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)


if __name__ == '__main__':
    main()
