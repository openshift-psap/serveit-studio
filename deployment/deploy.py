#!/usr/bin/env python3
"""
ServeIt Studio Deployer — deploy, sync, and manage ServeIt Studio on Kubernetes.

Replaces deploy.sh with a cleaner Python implementation using Jinja2 templates.

Examples:
    # Deploy with new PVC
    python3 deployment/deploy.py --storage-class shared-vast

    # Deploy with existing PVC
    python3 deployment/deploy.py --pvc-name my-pvc

    # Dev mode (auto-sync code)
    python3 deployment/deploy.py --dev --storage-class shared-vast

    # Sync code to launcher pod
    python3 deployment/deploy.py --sync

    # Sync code to ALL serveit pods (launcher + wizards)
    python3 deployment/deploy.py --sync-all

    # Restart server
    python3 deployment/deploy.py --restart-server

    # Port-forward
    python3 deployment/deploy.py --port-forward

    # Generate YAML only
    python3 deployment/deploy.py --just-yaml --storage-class shared-vast
"""

import argparse
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
PF_PID_FILE = Path('/tmp/serveit-port-forward.pid')


# ── Kubectl ──────────────────────────────────────────────────────────────────

def detect_kubectl() -> str:
    # Check if cluster is OpenShift (not just if oc binary exists)
    try:
        r = subprocess.run(['kubectl', 'api-resources', '--api-group=route.openshift.io'],
                          capture_output=True, text=True, timeout=15)
        if r.returncode == 0 and 'Route' in r.stdout:
            return 'oc'
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    try:
        subprocess.run(['kubectl', 'version', '--client'], capture_output=True, timeout=10)
        return 'kubectl'
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    print("Error: kubectl not found.", file=sys.stderr)
    sys.exit(1)


def kubectl_run(cmd: str, args: list, input_data: str = None, check: bool = True) -> subprocess.CompletedProcess:
    full_cmd = [cmd] + args
    return subprocess.run(full_cmd, input=input_data, capture_output=True, text=True, timeout=60)


def is_openshift(cmd: str) -> bool:
    r = subprocess.run([cmd, 'api-resources', '--api-group=route.openshift.io'],
                       capture_output=True, text=True, timeout=15)
    return r.returncode == 0 and 'Route' in r.stdout


# ── Jinja2 Rendering ────────────────────────────────────────────────────────

def render_template(template_name: str, **ctx) -> str:
    from jinja2 import Environment, FileSystemLoader
    env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)), keep_trailing_newline=True)
    tmpl = env.get_template(template_name)
    return tmpl.render(**ctx)


# ── YAML Generation ─────────────────────────────────────────────────────────

def generate_yaml(namespace: str, image: str, pvc_name: str,
                  storage_class: str, storage_size: str,
                  create_pvc: bool, dev_mode: bool, force_nad: bool,
                  openshift: bool, name: str = 'serveit-optimizer',
                  mode: str = 'local') -> str:
    parts = []

    # RBAC — launcher mode uses minimal permissions (no LWS/Istio/DRA)
    rbac_template = 'rbac-launcher.yaml.j2' if mode == 'launcher' else 'rbac.yaml.j2'
    parts.append(render_template(rbac_template, namespace=namespace))

    # PVC (conditional) — launcher only needs RWO, optimizer needs RWX for shared workload pod access
    if create_pvc:
        access_mode = 'ReadWriteOnce' if mode == 'launcher' else 'ReadWriteMany'
        parts.append(render_template('pvc.yaml.j2',
            pvc_name=pvc_name, namespace=namespace,
            storage_size=storage_size, storage_class=storage_class,
            access_mode=access_mode))

    # Deployment
    parts.append(render_template('deployment.yaml.j2',
        name=name, namespace=namespace, image=image,
        pvc_name=pvc_name, dev_mode='true' if dev_mode else 'false',
        force_nad='true' if force_nad else 'false',
        serveit_mode='launcher' if mode == 'launcher' else ''))

    # Service + Route
    parts.append(render_template('service.yaml.j2',
        name=name, namespace=namespace, is_openshift=openshift))

    return '\n---\n'.join(parts)


# ── Sync ─────────────────────────────────────────────────────────────────────

def sync_code(cmd: str, namespace: str, pod_name: str):
    print(f"📦 Syncing code to pod {pod_name}...", file=sys.stderr)

    # Try git pull on PVC first (/mnt/storage/app/), then legacy /app/
    r = kubectl_run(cmd, ['exec', '-n', namespace, pod_name, '--',
                          'bash', '-c', 'cd /mnt/storage/app 2>/dev/null && git pull --ff-only 2>&1 || cd /app && git pull --ff-only 2>&1'])
    if r.returncode == 0:
        print(f"   {r.stdout.strip()}", file=sys.stderr)
    else:
        print(f"   git pull failed: {r.stderr.strip()[:200]}", file=sys.stderr)
        print("   Falling back to tar sync...", file=sys.stderr)
        _tar_sync(cmd, namespace, pod_name)

    # Restart server so new code takes effect (restart loop picks it back up)
    print("   Restarting server...", file=sys.stderr)
    kubectl_run(cmd, ['exec', '-n', namespace, pod_name, '--',
                      'bash', '-c', "pkill -f 'python.*server.py' || true"])
    time.sleep(5)
    for _ in range(20):
        r = kubectl_run(cmd, ['exec', '-n', namespace, pod_name, '--', 'python3', '-c',
            "import socket; s=socket.socket(); s.settimeout(1); s.connect(('127.0.0.1',5000)); s.close()"])
        if r.returncode == 0:
            print("   ✅ Server restarted", file=sys.stderr)
            return
        time.sleep(1)
    print("   ⚠️  Server may not have restarted", file=sys.stderr)


def _tar_sync(cmd: str, namespace: str, pod_name: str):
    kubectl_run(cmd, ['exec', '-n', namespace, pod_name, '--',
                      'mkdir', '-p', '/mnt/storage/app'])
    env = os.environ.copy()
    env['COPYFILE_DISABLE'] = '1'
    tar_cmd = ['tar', 'cf', '-',
               '--exclude=.git', '--exclude=.claude', '--exclude=__pycache__',
               '--exclude=.DS_Store', '--exclude=*.pyc', '--exclude=._*',
               '-C', str(REPO_ROOT), '.']
    untar_cmd = [cmd, 'exec', '-i', '-n', namespace, pod_name, '--',
                 'tar', 'xf', '-', '--overwrite', '-C', '/mnt/storage/app/']
    tar_proc = subprocess.Popen(tar_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
    untar_proc = subprocess.Popen(untar_cmd, stdin=tar_proc.stdout,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    tar_proc.stdout.close()
    _, stderr = untar_proc.communicate(timeout=120)
    if untar_proc.returncode == 0:
        print("   Code synced (tar).", file=sys.stderr)
    else:
        print(f"   Tar sync failed: {stderr.decode()[:200]}", file=sys.stderr)


# ── Port Forward ─────────────────────────────────────────────────────────────

def start_port_forward(cmd: str, namespace: str, port: int, svc_name: str = 'serveit-optimizer-ui'):
    stop_port_forward(quiet=True)
    print(f"🔌 Starting port-forward (localhost:{port} -> svc/{svc_name}:5000)...", file=sys.stderr)

    proc = subprocess.Popen(
        [cmd, 'port-forward', '-n', namespace, f'svc/{svc_name}', f'{port}:5000'],
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
    # Find any serveit pod (optimizer or launcher)
    r = kubectl_run(cmd, ['get', 'pod', '-n', namespace,
                          '-l', 'app in (serveit-optimizer,serveit-launcher)',
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

def preflight_checks(cmd: str, namespace: str, mode: str = 'local'):
    # Launcher-only mode skips LWS and Istio checks — those are needed
    # on the target clusters, not on the launcher's cluster
    if mode != 'launcher':
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

def check_dependencies():
    missing = []

    try:
        import jinja2
    except ImportError:
        missing.append(('jinja2', 'pip3 install jinja2'))

    try:
        subprocess.run(['kubectl', 'version', '--client'], capture_output=True, timeout=10)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        missing.append(('kubectl', 'https://kubernetes.io/docs/tasks/tools/install-kubectl/'))

    if not missing:
        return

    print("❌ Missing dependencies:", file=sys.stderr)
    for name, fix in missing:
        print(f"   • {name} — install with: {fix}", file=sys.stderr)

    pip_deps = [name for name, fix in missing if fix.startswith('pip')]
    if pip_deps:
        install_cmd = f"pip3 install {' '.join(pip_deps)}"
        answer = input(f"\nInstall Python dependencies now? ({install_cmd}) [y/N] ").strip().lower()
        if answer == 'y':
            subprocess.run([sys.executable, '-m', 'pip', 'install'] + pip_deps)
            print("✅ Dependencies installed. Re-run the deploy command.", file=sys.stderr)
        sys.exit(1)

    sys.exit(1)


def main():
    check_dependencies()

    p = argparse.ArgumentParser(
        prog='deploy.py',
        description='ServeIt Studio Deployer — deploy, sync, and manage on Kubernetes',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    p.add_argument('--mode', choices=['local', 'launcher'], default='launcher',
                   help='Deploy mode: local (single instance) or launcher (multi-user control plane)')
    p.add_argument('-n', '--namespace', default='serveit', help='Kubernetes namespace (default: llm-d)')
    p.add_argument('-i', '--image', default='quay.io/bbenshab/serveit-studio:server', help='Container image')

    sg = p.add_argument_group('Storage')
    sg.add_argument('-p', '--pvc-name', help='Use existing PVC (skips PVC creation)')
    sg.add_argument('-s', '--storage-class', help='Create new PVC with this storage class')
    sg.add_argument('--storage-size', default='10Gi', help='PVC size (default: 10Gi)')

    dg = p.add_argument_group('Dev Options')
    dg.add_argument('--dev', action='store_true', help='Dev mode (auto-sync code, auto-restart)')
    dg.add_argument('--sync', action='store_true', help='Sync local code to running pod')
    dg.add_argument('--sync-all', action='store_true', help='Sync code to ALL serveit pods in the namespace')
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
        svc = 'serveit-launcher-ui' if args.mode == 'launcher' else 'serveit-optimizer-ui'
        start_port_forward(cmd, args.namespace, args.local_port, svc_name=svc)
        return

    if args.restart_server:
        restart_server(cmd, args.namespace, args.local_port)
        return

    # ── Sync only ──
    if (args.sync or args.sync_all) and not args.storage_class and not args.pvc_name:
        if args.sync_all:
            r = kubectl_run(cmd, ['get', 'pod', '-n', args.namespace,
                                  '-o', 'jsonpath={range .items[*]}{.metadata.name}{"\\n"}{end}'])
            all_pods = [p for p in r.stdout.strip().split('\n')
                        if (p.startswith('serveit-') or p.startswith('inftune-')) and p]
            if not all_pods:
                print("❌ No ServeIt Studio pods found.", file=sys.stderr)
                sys.exit(1)
            print(f"📦 Found {len(all_pods)} serveit pods to sync", file=sys.stderr)
            for pod in all_pods:
                sync_code(cmd, args.namespace, pod)
            print(f"\n   ✅ All {len(all_pods)} pods synced and restarted.", file=sys.stderr)
            return

        # --sync: single pod (launcher or optimizer)
        app_label = 'serveit-launcher' if args.mode == 'launcher' else 'serveit-optimizer'
        r = kubectl_run(cmd, ['get', 'pod', '-n', args.namespace, '-l', f'app={app_label}',
                              '-o', 'jsonpath={.items[0].metadata.name}'])
        pod = r.stdout.strip()
        if not pod:
            r = kubectl_run(cmd, ['get', 'pod', '-n', args.namespace,
                                  '-l', 'app in (serveit-optimizer,serveit-launcher,inftune-optimizer,inftune-launcher)',
                                  '-o', 'jsonpath={.items[0].metadata.name}'])
            pod = r.stdout.strip()
        if not pod:
            print("❌ No ServeIt Studio pod found. Deploy first.", file=sys.stderr)
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
        pvc_name = 'serveit-storage'
    else:
        if args.just_yaml:
            print("Error: --storage-class or --pvc-name required for YAML generation", file=sys.stderr)
            sys.exit(1)
        print("Error: Specify --storage-class (create new PVC) or --pvc-name (use existing)", file=sys.stderr)
        p.print_help()
        sys.exit(1)

    # ── Generate YAML ──
    deploy_name = 'serveit-launcher' if args.mode == 'launcher' else 'serveit-optimizer'
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
        name=deploy_name,
        mode=args.mode,
    )

    if args.just_yaml:
        print(yaml)
        return

    # ── Deploy ──
    preflight_checks(cmd, args.namespace, mode=args.mode)

    print(f"🚀 Deploying ServeIt Studio to namespace: {args.namespace}", file=sys.stderr)
    r = kubectl_run(cmd, ['apply', '-f', '-'], input_data=yaml)
    if r.returncode != 0:
        print(f"❌ Deployment failed:\n{r.stderr}", file=sys.stderr)
        sys.exit(1)

    print("✅ Manifests applied", file=sys.stderr)

    # Wait for pod
    print("⏳ Waiting for deployment to be ready...", file=sys.stderr)
    subprocess.run([cmd, 'wait', '--for=condition=available',
                    f'deployment/{deploy_name}', '-n', args.namespace,
                    '--timeout=300s'], capture_output=True)

    print("\n" + "=" * 60, file=sys.stderr)
    print("✅ ServeIt Studio deployment complete!", file=sys.stderr)
    print("=" * 60, file=sys.stderr)

    # Sync code in dev mode
    if args.dev or args.sync:
        r = kubectl_run(cmd, ['get', 'pod', '-n', args.namespace, '-l', f'app={deploy_name}',
                              '-o', 'jsonpath={.items[0].metadata.name}'])
        pod = r.stdout.strip()
        if pod:
            sync_code(cmd, args.namespace, pod)
            if args.restart_server:
                restart_server(cmd, args.namespace, args.local_port)
            else:
                print("\n   Server will auto-start on crash.", file=sys.stderr)

    # Port-forward (vanilla K8s)
    svc_name = f'{deploy_name}-ui'
    if openshift:
        r = kubectl_run(cmd, ['get', 'route', svc_name, '-n', args.namespace,
                              '-o', 'jsonpath={.spec.host}'])
        if r.stdout.strip():
            print(f"\n🌐 Web UI: https://{r.stdout.strip()}", file=sys.stderr)
    else:
        start_port_forward(cmd, args.namespace, args.local_port, svc_name=svc_name)

    print("=" * 60, file=sys.stderr)


if __name__ == '__main__':
    main()
