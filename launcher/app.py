"""InfeRecipe Launcher — multi-user control plane."""

import os
import json
from datetime import timedelta
from flask import Flask, render_template, jsonify, request, session

from launcher.database import init_db, get_db
from launcher.auth import register_auth_routes, get_user_id, get_username
from launcher import instance_manager


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

    namespace = os.environ.get('TARGET_NAMESPACE', 'inferecipe')
    image = os.environ.get('INFERECIPE_IMAGE', 'quay.io/bbenshab/vllm:inferecipe')

    # ── Dashboard ──

    @app.route('/')
    def dashboard():
        groups = instance_manager.list_groups(get_user_id())
        return render_template('dashboard.html',
                               username=get_username(),
                               groups=groups)

    # ── Group API ──

    @app.route('/api/groups', methods=['GET'])
    def api_list_groups():
        return jsonify(instance_manager.list_groups(get_user_id()))

    @app.route('/api/groups', methods=['POST'])
    def api_create_group():
        data = request.get_json() or {}
        name = data.get('name', '').strip()
        icon = data.get('icon', '📦').strip()
        if not name:
            return jsonify({'error': 'Group name is required'}), 400
        try:
            result = instance_manager.create_group(get_user_id(), name, icon)
            return jsonify(result)
        except Exception as e:
            if 'UNIQUE' in str(e):
                return jsonify({'error': f'Group "{name}" already exists'}), 409
            return jsonify({'error': str(e)}), 500

    @app.route('/api/groups/<int:group_id>', methods=['DELETE'])
    def api_delete_group(group_id):
        success = instance_manager.delete_group(group_id, get_user_id())
        if success:
            return jsonify({'ok': True})
        return jsonify({'error': 'Group not found'}), 404

    @app.route('/api/groups/<int:group_id>', methods=['PUT'])
    def api_update_group(group_id):
        data = request.get_json() or {}
        success = instance_manager.update_group(
            group_id, get_user_id(),
            name=data.get('name'), icon=data.get('icon'))
        if success:
            return jsonify({'ok': True})
        return jsonify({'error': 'Group not found'}), 404

    # ── Instance API ──

    @app.route('/api/instances', methods=['GET'])
    def api_list_instances():
        group_id = request.args.get('group_id', type=int)
        instances = instance_manager.list_instances(get_user_id(), group_id=group_id)
        return jsonify(instances)

    @app.route('/api/instances', methods=['POST'])
    def api_create_instance():
        data = request.form if request.form else request.get_json()
        name = data.get('name', '').strip()
        group_id = data.get('group_id')
        if not name:
            return jsonify({'error': 'Name is required'}), 400
        if not group_id:
            return jsonify({'error': 'Group is required'}), 400

        with get_db() as conn:
            existing = conn.execute(
                'SELECT id FROM instances WHERE owner_id = ? AND name = ?',
                (get_user_id(), instance_manager._sanitize(f"{get_username()}-{name}"))
            ).fetchone()
            if existing:
                return jsonify({'error': f'You already have an instance named "{name}". Please choose a different name.'}), 409

        kubeconfig_data = None
        if 'kubeconfig' in request.files:
            kubeconfig_data = request.files['kubeconfig'].read().decode('utf-8')
        elif data.get('kubeconfig'):
            kubeconfig_data = data['kubeconfig']

        storage_class = data.get('storage_class') or os.environ.get('STORAGE_CLASS')

        try:
            result = instance_manager.create_instance(
                owner_id=get_user_id(),
                username=get_username(),
                name=name,
                group_id=int(group_id),
                namespace=namespace,
                kubeconfig_data=kubeconfig_data,
                storage_class=storage_class,
                image=image,
            )
            return jsonify(result)
        except Exception as e:
            return jsonify({'error': str(e)}), 500

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
            for item in data.get('items', []):
                sc_name = item['metadata']['name']
                is_default = item['metadata'].get('annotations', {}).get(
                    'storageclass.kubernetes.io/is-default-class', 'false') == 'true'
                classes.append({'name': sc_name, 'is_default': is_default})
            classes.sort(key=lambda x: (not x['is_default'], x['name']))
            return jsonify(classes)
        except Exception:
            return jsonify([])

    @app.route('/api/instances/<int:instance_id>', methods=['DELETE'])
    def api_delete_instance(instance_id):
        success = instance_manager.delete_instance(instance_id, get_user_id())
        if success:
            return jsonify({'ok': True})
        return jsonify({'error': 'Instance not found or not owned by you'}), 404

    return app


def main():
    print("=" * 60)
    print("InfeRecipe Launcher — Multi-User Control Plane")
    print("=" * 60)

    init_db()

    app = create_app()
    app.run(host='0.0.0.0', port=5000, debug=False)


if __name__ == '__main__':
    main()
