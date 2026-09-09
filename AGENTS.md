# ServeIt Studio — Operations Memory

## Cluster topology

Two clusters are involved:

| Role | Kubeconfig | Namespace |
|------|-----------|-----------|
| UI (ServeIt Studio web app / admin panels) | `~/.kube/ocp4-intlab.kubeconfig` (context `ocp4-intlab`, "cp" cluster) | `inftune` |
| Workload (actual inference tests / ModelCar / deployment runs) | `~/.kube/a100` (context `serveit-admin-blog-a100/...`) | `serveit-admin-blog-a100` |

- The **UI runs on the cp cluster** (`ocp4-intlab`). Sync code changes there.
- The **workload runs on the a100 cluster** (`~/.kube/a100`). That's where test pods like `aggregated-tp1-0`, `serveit-workload-*`, and gateway/infra pods appear, and where scheduling/taint errors surface.

## Sync workflow

UI code is synced to the admin pods on the cp cluster:

```bash
export KUBECONFIG=~/.kube/ocp4-intlab.kubeconfig
python3 deployment/deploy.py --sync-all -n inftune --mode launcher
```

`sync_code` (via deploy.py / direct python) does a pod-side `git fetch origin && git reset --hard origin/main` under `/mnt/storage/app` (fallback `/app`), then `pkill -f 'python.*server.py'` and waits for port 5000.

## Networking / DRA

- DRA network detection is real (cluster-based). Priority: `('dra','shared_device','sriov_multinic','nad','nmstate','eth0')` in `core/networking/__init__.py` (`pick_best_network_type()`).
- `config_generator.py::_detect_network_type` is dead code (CLI-only); no longer used by the UI.

## Known gotchas

- `deploy.py --sync-all` only sees pods on the cp cluster that are in the namespace. On intlab the pods are under `inftune`; different admin pods may track different commits.
- Local pytest is not installed (`No module named pytest`); verify Python with `py_compile` and JS with `node --check`.