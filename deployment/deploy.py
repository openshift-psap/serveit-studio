#!/usr/bin/env python3
"""
InfeRecipe Deployer — deploy, sync, and manage InfeRecipe on Kubernetes.

Replaces deploy.sh with a cleaner Python implementation using Jinja2 templates.

Examples:
    # Deploy with new PVC
    python3 deployment/deploy.py --storage-class shared-vast

    # Deploy with existing PVC
    python3 deployment/deploy.py --pvc-name my-pvc

    # Dev mode (auto-sync code)
    python3 deployment/deploy.py --dev --storage-class shared-vast

    # Sync code to running pod
    python3 deployment/deploy.py --sync

    # Restart server
    python3 deployment/deploy.py --restart-server

    # Port-forward
    python3 deployment/deploy.py --port-forward

    # Generate YAML only
    python3 deployment/deploy.py --just-yaml --storage-class shared-vast
"""

import argparse
import hashlib
import os
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

SCRIPT_DIR = Path(__file__).parent.resolve()
REPO_ROOT = SCRIPT_DIR.parent
TEMPLATES_DIR = SCRIPT_DIR / 'templates'
PF_PID_FILE = Path('/tmp/inferecipe-port-forward.pid')


# ── Kubectl ──────────────────────────────────────────────────────────────────

def detect_kubectl() -> str:
    for cmd in ['oc', 'kubectl']:
        try:
            subprocess.run([cmd, 'version', '--client'], capture_output=True, timeout=10)
            return cmd
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    print("Error: Neither kubectl nor oc found.", file=sys.stderr)
    sys.exit(1)


def kubectl_run(cmd: str, args: list, input_data: str = None, check: bool = True) -> subprocess.CompletedProcess:
    full_cmd = [cmd] + args
    return subprocess.run(full_cmd, input=input_data, capture_output=True, text=True, timeout=60)


def is_openshift(cmd: str) -> bool:
    r = subprocess.run([cmd, 'api-resources', '--api-group=route.openshift.io'],
                       capture_output=True, text=True, timeout=15)
    return r.returncode == 0 and 'Route' in r.stdout


# ── Jinja2 Rendering ────────────────────────────────────────────────────────

def render_template(name: str, **ctx) -> str:
    try:
        from jinja2 import Environment, FileSystemLoader
    except ImportError:
        # Fallback: simple string replacement
        path = TEMPLATES_DIR / name
        content = path.read_text()
        for key, val in ctx.items():
            content = content.replace('{{ ' + key + ' }}', str(val))
        return content

    env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)), keep_trailing_newline=True)
    tmpl = env.get_template(name)
    return tmpl.render(**ctx)


# ── YAML Generation ─────────────────────────────────────────────────────────

def generate_yaml(namespace: str, image: str, pvc_name: str,
                  storage_class: str, storage_size: str,
                  create_pvc: bool, dev_mode: bool, force_nad: bool,
                  openshift: bool, name: str = 'inferecipe-optimizer') -> str:
    parts = []

    # RBAC
    parts.append(render_template('rbac.yaml.j2', namespace=namespace))

    # PVC (conditional)
    if create_pvc:
        parts.append(render_template('pvc.yaml.j2',
            pvc_name=pvc_name, namespace=namespace,
            storage_size=storage_size, storage_class=storage_class))

    # Deployment
    parts.append(render_template('deployment.yaml.j2',
        name=name, namespace=namespace, image=image,
        pvc_name=pvc_name, dev_mode='true' if dev_mode else 'false',
        force_nad='true' if force_nad else 'false'))

    # Service + Route
    parts.append(render_template('service.yaml.j2',
        name=name, namespace=namespace, is_openshift=openshift))

    return '\n---\n'.join(parts)


# ── Sync ─────────────────────────────────────────────────────────────────────

def sync_code(cmd: str, namespace: str, pod_name: str):
    print(f"📦 Syncing code to pod {pod_name}...", file=sys.stderr)

    kubectl_run(cmd, ['exec', '-n', namespace, pod_name, '--',
                      'mkdir', '-p', '/mnt/storage/app'])

    # Build local manifest
    local_files = {}
    for path in REPO_ROOT.rglob('*'):
        if not path.is_file():
            continue
        rel = str(path.relative_to(REPO_ROOT))
        if any(skip in rel for skip in ['.git/', '.claude/', '__pycache__/', '.DS_Store']):
            continue
        md5 = hashlib.md5(path.read_bytes()).hexdigest()
        local_files['./' + rel] = md5

    # Build remote manifest
    remote_files = {}
    r = kubectl_run(cmd, ['exec', '-n', namespace, pod_name, '--', 'bash', '-c',
        'cd /mnt/storage/app 2>/dev/null && find . -type f -not -path "*/__pycache__/*" -not -name ".DS_Store" -print0 | xargs -0 md5sum 2>/dev/null || true'
    ])
    if r.returncode == 0:
        for line in r.stdout.strip().split('\n'):
            parts = line.strip().split(None, 1)
            if len(parts) == 2:
                remote_files[parts[1]] = parts[0]

    # Diff
    to_copy = [f for f, h in local_files.items() if remote_files.get(f) != h]
    to_delete = [f for f in remote_files if f not in local_files]

    if not to_copy and not to_delete:
        print("   Already up to date.", file=sys.stderr)
        return

    # Create directories
    if to_copy:
        dirs = sorted(set(str(Path(f).parent) for f in to_copy if str(Path(f).parent) != '.'))
        if dirs:
            dir_cmd = ' && '.join(f'mkdir -p {d}' for d in dirs[:50])
            kubectl_run(cmd, ['exec', '-n', namespace, pod_name, '--', 'bash', '-c',
                             f'cd /mnt/storage/app && {dir_cmd}'])

    # Copy files
    for f in to_copy:
        rel = f.lstrip('./')
        src = str(REPO_ROOT / rel)
        dst = f'{namespace}/{pod_name}:/mnt/storage/app/{rel}'
        subprocess.run([cmd, 'cp', src, dst], capture_output=True, timeout=30)

    # Delete stale files
    if to_delete:
        del_paths = ' '.join(f'/mnt/storage/app/{f.lstrip("./")}' for f in to_delete[:100])
        kubectl_run(cmd, ['exec', '-n', namespace, pod_name, '--', 'bash', '-c',
                         f'rm -f {del_paths}'])

    print(f"   {len(to_copy)} file(s) updated, {len(to_delete)} file(s) removed.", file=sys.stderr)


# ── Port Forward ─────────────────────────────────────────────────────────────

def start_port_forward(cmd: str, namespace: str, port: int):
    stop_port_forward(quiet=True)
    print(f"🔌 Starting port-forward (localhost:{port} -> svc/inferecipe-optimizer-ui:5000)...", file=sys.stderr)

    proc = subprocess.Popen(
        [cmd, 'port-forward', '-n', namespace, 'svc/inferecipe-optimizer-ui', f'{port}:5000'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    PF_PID_FILE.write_text(str(proc.pid))
    time.sleep(2)

    if proc.poll() is None:
        print(f"✅ Port-forward running (PID: {proc.pid})", file=sys.stderr)
        print(f"   Web UI: http://localhost:{port}", file=sys.stderr)
        print(f"   Stop with: python3 deployment/deploy.py --stop-port-forward", file=sys.stderr)
    else:
        print("⚠️  Port-forward failed to start", file=sys.stderr)
        PF_PID_FILE.unlink(missing_ok=True)


def stop_port_forward(quiet: bool = False):
    if PF_PID_FILE.exists():
        pid = int(PF_PID_FILE.read_text().strip())
        try:
            os.kill(pid, 9)
            if not quiet:
                print(f"✅ Port-forward stopped (PID: {pid})", file=sys.stderr)
        except ProcessLookupError:
            if not quiet:
                print("ℹ️  Port-forward was not running", file=sys.stderr)
        PF_PID_FILE.unlink(missing_ok=True)
    elif not quiet:
        print("ℹ️  No port-forward running", file=sys.stderr)


# ── Restart Server ───────────────────────────────────────────────────────────

def restart_server(cmd: str, namespace: str, port: int):
    r = kubectl_run(cmd, ['get', 'pod', '-n', namespace, '-l', 'app=inferecipe-optimizer',
                          '-o', 'jsonpath={.items[0].metadata.name}'])
    pod = r.stdout.strip()
    if not pod:
        print("❌ No optimizer pod found", file=sys.stderr)
        return

    print(f"🔄 Restarting server in pod: {pod}", file=sys.stderr)
    stop_port_forward(quiet=True)

    # Kill server — the dev mode entrypoint loop will auto-restart it
    kubectl_run(cmd, ['exec', '-n', namespace, pod, '--', 'bash', '-c',
                      "pkill -f 'python.*server.py' || true"])

    # Wait for auto-restart
    print("   Waiting for server to restart...", file=sys.stderr)
    time.sleep(5)
    for i in range(20):
        r = kubectl_run(cmd, ['exec', '-n', namespace, pod, '--', 'python3', '-c',
            "import socket; s=socket.socket(); s.settimeout(1); s.connect(('127.0.0.1',5000)); s.close()"])
        if r.returncode == 0:
            break
        time.sleep(2)

    print("   ✅ Server restarted", file=sys.stderr)

    if cmd != 'oc':
        start_port_forward(cmd, namespace, port)


# ── Preflight Checks ────────────────────────────────────────────────────────

def preflight_checks(cmd: str, namespace: str):
    # LWS CRD
    r = subprocess.run([cmd, 'api-resources'], capture_output=True, text=True, timeout=15)
    if 'leaderworkerset' not in r.stdout:
        print("❌ LeaderWorkerSet (LWS) CRD not found.", file=sys.stderr)
        print("   Install: kubectl apply --server-side -f https://github.com/kubernetes-sigs/lws/releases/latest/download/manifests.yaml", file=sys.stderr)
        sys.exit(1)

    # Istio
    r = kubectl_run(cmd, ['get', 'namespace', 'istio-system'])
    if r.returncode != 0:
        print("❌ istio-system namespace not found. Install Istio first.", file=sys.stderr)
        sys.exit(1)

    # Create namespace
    r = kubectl_run(cmd, ['get', 'namespace', namespace])
    if r.returncode != 0:
        print(f"📦 Creating namespace: {namespace}", file=sys.stderr)
        kubectl_run(cmd, ['create', 'namespace', namespace])

    # Copy Istio pull secret on vanilla K8s
    if cmd != 'oc':
        r = kubectl_run(cmd, ['get', 'secret', 'rhaii-pull-secret', '-n', 'istio-system'])
        if r.returncode == 0:
            r2 = kubectl_run(cmd, ['get', 'secret', 'rhaii-pull-secret', '-n', namespace])
            if r2.returncode != 0:
                print(f"🔑 Copying Istio pull secret to {namespace}...", file=sys.stderr)
                r3 = kubectl_run(cmd, ['get', 'secret', 'rhaii-pull-secret', '-n', 'istio-system', '-o', 'json'])
                if r3.returncode == 0:
                    secret = json.loads(r3.stdout)
                    secret['metadata'] = {'name': 'rhaii-pull-secret', 'namespace': namespace}
                    kubectl_run(cmd, ['apply', '-f', '-'], input_data=json.dumps(secret))


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(
        prog='deploy.py',
        description='InfeRecipe Deployer — deploy, sync, and manage on Kubernetes',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    p.add_argument('-n', '--namespace', default='llm-d', help='Kubernetes namespace (default: llm-d)')
    p.add_argument('-i', '--image', default='quay.io/bbenshab/vllm:inferecipe', help='Container image')

    sg = p.add_argument_group('Storage')
    sg.add_argument('-p', '--pvc-name', help='Use existing PVC (skips PVC creation)')
    sg.add_argument('-s', '--storage-class', help='Create new PVC with this storage class')
    sg.add_argument('--storage-size', default='100Gi', help='PVC size (default: 100Gi)')

    dg = p.add_argument_group('Dev Options')
    dg.add_argument('--dev', action='store_true', help='Dev mode (auto-sync code, auto-restart)')
    dg.add_argument('--sync', action='store_true', help='Sync local code to running pod')
    dg.add_argument('--force-nad', action='store_true', help='Force NAD mode instead of DRA')

    pg = p.add_argument_group('Port Forward & Server')
    pg.add_argument('--port-forward', action='store_true', help='Start port-forward')
    pg.add_argument('--stop-port-forward', action='store_true', help='Stop port-forward')
    pg.add_argument('--restart-server', action='store_true', help='Restart server in pod')
    pg.add_argument('--local-port', type=int, default=8080, help='Local port (default: 8080)')

    p.add_argument('--just-yaml', action='store_true', help='Output YAML only, do not deploy')

    args = p.parse_args()

    # ── Quick actions (no deploy) ──
    if args.stop_port_forward:
        stop_port_forward()
        return

    cmd = detect_kubectl()
    openshift = is_openshift(cmd)

    if args.port_forward:
        start_port_forward(cmd, args.namespace, args.local_port)
        return

    if args.restart_server:
        restart_server(cmd, args.namespace, args.local_port)
        return

    # ── Sync only ──
    if args.sync and not args.storage_class and not args.pvc_name:
        r = kubectl_run(cmd, ['get', 'pod', '-n', args.namespace, '-l', 'app=inferecipe-optimizer',
                              '-o', 'jsonpath={.items[0].metadata.name}'])
        pod = r.stdout.strip()
        if not pod:
            print("❌ No optimizer pod found. Deploy first.", file=sys.stderr)
            sys.exit(1)
        sync_code(cmd, args.namespace, pod)
        print("\n   Code synced. Server will auto-restart and pick up changes.", file=sys.stderr)
        print("   To force restart: python3 deployment/deploy.py --restart-server", file=sys.stderr)
        return

    # ── Determine PVC strategy ──
    if args.pvc_name:
        create_pvc = False
        pvc_name = args.pvc_name
    elif args.storage_class:
        create_pvc = True
        pvc_name = 'inferecipe-storage'
    else:
        if args.just_yaml:
            print("Error: --storage-class or --pvc-name required for YAML generation", file=sys.stderr)
            sys.exit(1)
        print("Error: Specify --storage-class (create new PVC) or --pvc-name (use existing)", file=sys.stderr)
        p.print_help()
        sys.exit(1)

    # ── Generate YAML ──
    yaml = generate_yaml(
        namespace=args.namespace,
        image=args.image,
        pvc_name=pvc_name,
        storage_class=args.storage_class or '',
        storage_size=args.storage_size,
        create_pvc=create_pvc,
        dev_mode=args.dev,
        force_nad=args.force_nad,
        openshift=openshift,
    )

    if args.just_yaml:
        print(yaml)
        return

    # ── Deploy ──
    preflight_checks(cmd, args.namespace)

    print(f"🚀 Deploying InfeRecipe to namespace: {args.namespace}", file=sys.stderr)
    r = kubectl_run(cmd, ['apply', '-f', '-'], input_data=yaml)
    if r.returncode != 0:
        print(f"❌ Deployment failed:\n{r.stderr}", file=sys.stderr)
        sys.exit(1)

    print("✅ Manifests applied", file=sys.stderr)

    # Wait for pod
    print("⏳ Waiting for deployment to be ready...", file=sys.stderr)
    subprocess.run([cmd, 'wait', '--for=condition=available',
                    'deployment/inferecipe-optimizer', '-n', args.namespace,
                    '--timeout=300s'], capture_output=True)

    print("\n" + "=" * 60, file=sys.stderr)
    print("✅ InfeRecipe deployment complete!", file=sys.stderr)
    print("=" * 60, file=sys.stderr)

    # Sync code in dev mode
    if args.dev or args.sync:
        r = kubectl_run(cmd, ['get', 'pod', '-n', args.namespace, '-l', 'app=inferecipe-optimizer',
                              '-o', 'jsonpath={.items[0].metadata.name}'])
        pod = r.stdout.strip()
        if pod:
            sync_code(cmd, args.namespace, pod)
            if args.restart_server:
                restart_server(cmd, args.namespace, args.local_port)
            else:
                print("\n   Server will auto-start on crash.", file=sys.stderr)

    # Port-forward (vanilla K8s)
    if openshift:
        r = kubectl_run(cmd, ['get', 'route', 'inferecipe-optimizer-ui', '-n', args.namespace,
                              '-o', 'jsonpath={.spec.host}'])
        if r.stdout.strip():
            print(f"\n🌐 Web UI: https://{r.stdout.strip()}", file=sys.stderr)
    else:
        start_port_forward(cmd, args.namespace, args.local_port)

    print("=" * 60, file=sys.stderr)


if __name__ == '__main__':
    main()
