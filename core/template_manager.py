"""
ServeIt Studio Template Manager

Renders Jinja2 templates for Kubernetes deployments based on test configurations.
Generates YAML manifests for Aggregated, PD, and EP architectures.
"""

import json
import logging
import re
from pathlib import Path
from typing import Dict, List, Optional
from dataclasses import asdict
from jinja2 import Environment, FileSystemLoader, TemplateNotFound
import yaml as _yaml

from .config_generator import TestConfig
from .networking import compute_network_values


def _bump_memory_floor(memory: Optional[str], floor_gib: int) -> Optional[str]:
    """Raise a k8s memory quantity to at least ``floor_gib`` Gi (never shrink).

    Multi-node LWS pods spawn one rank process per GPU on every replica;
    peak RSS during cold shard load (~6.5Gi per rank, 8 ranks ≈ 52Gi on a
    TP16 worker) far exceeds the single-node baseline, so multi-node pods
    need a memory floor or the workers get OOMKilled mid-load.
    """
    if not memory:
        return memory
    m = re.fullmatch(r'(\d+(?:\.\d+)?)\s*(Ki|Mi|Gi|Ti)?', str(memory).strip())
    if not m:
        return memory
    unit = m.group(2) or 'Gi'  # ServeIt configs are Gi-denominated
    gib = float(m.group(1)) * {'Ki': 1 / (1024 ** 2), 'Mi': 1 / 1024,
                               'Gi': 1, 'Ti': 1024}[unit]
    return memory if gib >= floor_gib else f'{floor_gib}Gi'


def _fix_worker_template(yaml_str: str) -> str:
    """Fix workerTemplate (and leader) for multi-node LWS pods (lws_size > 1).

    Worker pods run as headless distributed compute workers — they don't serve
    HTTP. Applied fixes, validated live on the kermit cluster (driver 595.71.05,
    NCCL 2.28.3, H200):

    1. Worker runs a guarded ``/tmp/_vllm_patched`` script (multiprocessing
       spawn re-imports it as ``__mp_main__`` in each rank process).
    2. Worker pins CUDA_VISIBLE_DEVICES and sources the RDMA discovery script —
       without it NCCL_IB_HCA stays the template default ("mlx") and cross-node
       NCCL fails with "internal error".
    3. Worker serve command carries the LEADER's full config flags (block size,
       kv-cache-dtype, attention-config, ...). The headless worker builds its
       own VllmConfig from its CLI: with defaults only, its KV cache specs
       diverge from the leader's and engine init dies with "The KV cache specs
       for the same layer are different across workers".
     4. Worker and leader memory floored at 200Gi (never shrunk): the 8
        spawned rank processes hold ~6.4Gi RSS each (~52Gi total) and get
        OOMKilled at the template's 40Gi.
    5. Flashinfer allreduce disabled on both sides: the auto-selected mnnvl
       one-shot kernel spins forever on non-NVLink fabrics (its workspace setup
       succeeds over IB but the data path never completes).
     6. NCCL forced to Ring/Simple with GPUDirect RDMA off: auto-selected
        cross-node NVLS heads hang over IB on this fabric. CUDA graphs stay
        ENABLED — no --enforce-eager and no CUDA_LAUNCH_BLOCKING: the
        multi-node capture-phase deadlocks observed during debugging all
        predated the worker config-parity fix (item 3), and single-node
        capture works on this driver. If capture hangs return on multi-node,
        debug the root cause instead of disabling graphs.

    IMPORTANT: PyYAML resolves YAML anchors (spec: *pod_spec) as shared Python
    object references, not deep copies. We must deep-copy the workerTemplate spec
    before modifying to avoid accidentally modifying the leaderTemplate too.
    """
    import copy as _copy
    import shlex as _shlex
    try:
        docs = list(_yaml.safe_load_all(yaml_str))
        for doc in docs:
            if not doc or doc.get('kind') != 'LeaderWorkerSet':
                continue
            lwt = doc.get('spec', {}).get('leaderWorkerTemplate', {})
            wt = lwt.get('workerTemplate', {})

            # ---- Leader-side surgery (before worker flag extraction) ----
            leader_containers = (lwt.get('leaderTemplate', {})
                                    .get('spec', {})
                                    .get('containers', []) or [])
            model_name = None
            for lc in leader_containers:
                largs = lc.get('args', [])
                if largs and '/tmp/_vllm_patched serve' in largs[0]:
                    lbody = largs[0]
                    _serve_at = lbody.find('/tmp/_vllm_patched serve')

                    # Disable the flashinfer fused/standalone allreduce (fix 5).
                    _fi_patch = (
                        'import vllm.distributed.device_communicators.'
                        'flashinfer_all_reduce as _fia;_fia.fi_ar_available=False\\n'
                        'import vllm.models.common.ops.fused_allreduce_rms_norm '
                        'as _far;_far.flashinfer_trtllm_fused_allreduce_norm=None;'
                        '_far.get_fi_ar_workspace=None\\n'
                    )
                    _guard = 'if __name__=="__main__":\\n'
                    if _guard in lbody and '_fia.fi_ar_available' not in lbody:
                        lbody = lbody.replace(_guard, _fi_patch + _guard, 1)

                    m = re.search(r'/tmp/_vllm_patched serve ([^\s\\]+)', lbody)
                    if m:
                        model_name = m.group(1)

                    # Extract the leader's config flags for the worker (fix 3).
                    _j = lbody.find('|| sleep infinity', _serve_at)
                    if _j < 0:
                        _j = len(lbody)
                    _block = lbody[_serve_at:_j].replace('\\\n', ' ').replace('\n', ' ')
                    try:
                        _toks = _shlex.split(_block)
                    except ValueError:
                        _toks = _block.split()
                    _skip_val = {'--port', '--tensor-parallel-size', '--nnodes',
                                 '--node-rank', '--master-addr'}
                    _skip_bool = {'--headless'}
                    _kept, _i = [], 0
                    while _i < len(_toks):
                        _t = _toks[_i]
                        if _t in _skip_val:
                            _i += 2
                            continue
                        if _t in _skip_bool:
                            _i += 1
                            continue
                        _kept.append(_t)
                        _i += 1
                    _leader_flags = ' '.join(_kept[_kept.index('serve') + 2:]) if 'serve' in _kept else ''

                    largs[0] = lbody
                    # Leader env (fix 6) on the vllm container only. No
                    # CUDA_LAUNCH_BLOCKING: eager mode makes it unnecessary and
                    # it serializes every kernel launch (heavy runtime cost).
                    _ensure_container_env(lc, 'NCCL_ALGO', 'Ring')
                    _ensure_container_env(lc, 'NCCL_PROTO', 'Simple')
                    _ensure_container_env(lc, 'NCCL_IB_GDR', '0')

                    # Leader memory floor (fix 4): the leader runs the same
                    # rank-per-GPU process tree as the worker.
                    _lres = lc.get('resources') or {}
                    for _k in ('requests', 'limits'):
                        _lside = _lres.get(_k) or {}
                        if _lside.get('memory'):
                            _lside['memory'] = _bump_memory_floor(_lside['memory'], 200)
                            _lres[_k] = _lside
                    lc['resources'] = _lres

            if not model_name:
                continue

            # Deep-copy the workerTemplate spec so we don't corrupt the leaderTemplate
            # (PyYAML aliases resolve to shared references, not copies)
            wt_spec = _copy.deepcopy(wt.get('spec', {}))
            wt['spec'] = wt_spec

            for container in (wt_spec.get('containers', []) or []):
                container.pop('startupProbe', None)
                container.pop('readinessProbe', None)
                container.pop('livenessProbe', None)

                # Worker memory floor (fix 4): 8 spawned ranks ≈ 52Gi RSS.
                # Only raise — never shrink a deliberately larger config.
                _res = container.get('resources') or {}
                for _k in ('requests', 'limits'):
                    _side = _res.get(_k) or {}
                    if _side.get('memory'):
                        _side['memory'] = _bump_memory_floor(_side['memory'], 200)
                        _res[_k] = _side
                container['resources'] = _res

                # Worker env (fix 6) — same as leader.
                _ensure_container_env(container, 'NCCL_ALGO', 'Ring')
                _ensure_container_env(container, 'NCCL_PROTO', 'Simple')
                _ensure_container_env(container, 'NCCL_IB_GDR', '0')

                container['args'] = [
                    '# Worker node: headless distributed compute (no HTTP server)\n'
                    'rm -f /dev/shm/vllm* /dev/shm/psm_* 2>/dev/null || true\n'
                    'ulimit -l unlimited || true\n'
                    # Pin to allocated GPUs (privileged pods see all host GPUs) —
                    # same block as the leader and the aggregated multi-node worker.
                    'if [ -d /var/run/nvidia-container-devices ]; then\n'
                    '  GPU_INDICES=""\n'
                    '  for uuid in $(ls /var/run/nvidia-container-devices/); do\n'
                    '    idx=$(nvidia-smi --query-gpu=index,uuid --format=csv,noheader | grep "$uuid" | cut -d\',\' -f1 | tr -d \' \')\n'
                    '    [ -n "$idx" ] && GPU_INDICES="${GPU_INDICES:+$GPU_INDICES,}$idx"\n'
                    '  done\n'
                    '  if [ -n "$GPU_INDICES" ]; then\n'
                    '    export CUDA_VISIBLE_DEVICES="$GPU_INDICES"\n'
                    '    echo "--- CUDA_VISIBLE_DEVICES=$CUDA_VISIBLE_DEVICES ---"\n'
                    '  fi\n'
                    'fi\n'
                    # Source the RDMA discovery script — REQUIRED for cross-node NCCL.
                    # Without it NCCL_IB_HCA stays the generic template value ("mlx"),
                    # which selects RoCE HCAs with no valid GIDs and the multi-node
                    # TP allreduce fails with "NCCL error: internal error".
                    'if [ -f /scripts/discover_ib_hca.sh ]; then\n'
                    '  source /scripts/discover_ib_hca.sh\n'
                    '  echo "--- NCCL_SOCKET_IFNAME: ${NCCL_SOCKET_IFNAME:-not set} ---"\n'
                    '  echo "--- UCX_NET_DEVICES: ${UCX_NET_DEVICES:-not set} ---"\n'
                    '  echo "--- Loaded RDMA environment ---"\n'
                    'fi\n'
                    '_MOE_FILE=$(python3 -c "import vllm; print(vllm.__path__[0])" 2>/dev/null)/model_executor/layers/fused_moe/moe_permute_unpermute.py\n'
                    'if [ -f "$_MOE_FILE" ] && grep -q \'a1q_scale\\[permuted_idx.clamp\' "$_MOE_FILE"; then\n'
                    '  sed -i \'s|a1q_scale = a1q_scale\\[permuted_idx.clamp(max=n_token \\* topk - 1) // topk\\]|scale_idx = permuted_idx.clamp(max=n_token * topk - 1) // topk; scale_idx = scale_idx.clamp(max=a1q_scale.shape[0] - 1); a1q_scale = a1q_scale[scale_idx]|\' "$_MOE_FILE"\n'
                    '  echo "--- Applied vllm#43396 FP8 MoE patch ---"\n'
                    'fi\n'
                    # The if __name__=="__main__" guard is REQUIRED: vLLM's
                    # MultiprocExecutor spawns per-GPU processes via multiprocessing
                    # "spawn", which re-imports this script as __mp_main__ in each
                    # child. Without the guard, children re-run main() during
                    # bootstrapping and crash with RuntimeError. The flashinfer
                    # allreduce disable above the guard is module-level so the
                    # spawned ranks apply it too.
                    "printf '#!/usr/bin/env python3\\ntry:\\n import transformers.integrations.heterogeneity.configuration_utils as _hc\\n _p=_hc.HeterogeneousConfigMixin.allow_global_per_layer_attribute_access\\n _hc.HeterogeneousConfigMixin.allow_global_per_layer_attribute_access=property(lambda s:s.__dict__.get(\\\"allow_global_per_layer_attribute_access\\\",True),_p.fset)\\nexcept Exception:\\n pass\\n"
                    'import vllm.distributed.device_communicators.flashinfer_all_reduce as _fia;_fia.fi_ar_available=False\\n'
                    'import vllm.models.common.ops.fused_allreduce_rms_norm as _far;_far.flashinfer_trtllm_fused_allreduce_norm=None;_far.get_fi_ar_workspace=None\\n'
                    "if __name__==\\\"__main__\\\":\\n import sys;sys.argv[0]=\\\"vllm\\\"\\n from vllm.entrypoints.cli.main import main;main()\\n'"
                    ' > /tmp/_vllm_patched && chmod +x /tmp/_vllm_patched\n'
                    f'/tmp/_vllm_patched serve {model_name} '
                    '--tensor-parallel-size ${TP_SIZE} '
                    '--nnodes ${LWS_GROUP_SIZE:-2} '
                    '--node-rank ${LWS_WORKER_INDEX:-0} '
                    '--master-addr ${LWS_LEADER_ADDRESS} '
                    f'--headless {_leader_flags} || sleep infinity\n'
                ]

        return _yaml.dump_all(docs, default_flow_style=False, allow_unicode=True)
    except Exception:
        return yaml_str  # fall back to original if parsing fails


def _ensure_container_env(container: dict, name: str, value: str) -> None:
    """Idempotently set an env var on a container spec (vllm containers only
    should be passed — callers pick the right container)."""
    env = container.get('env') or []
    for e in env:
        if e.get('name') == name:
            e['value'] = value
            return
    env.append({'name': name, 'value': value})
    container['env'] = env

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def resolve_vllm_log_request_flag(image: str, disabled: Optional[bool]) -> str:
    """
    Select the vLLM access-log flag that matches the runtime image.

    Upstream vLLM deprecated/removed ``--disable-log-requests`` (gone by ~v0.13);
    newer upstream and llm-d images both accept ``--disable-uvicorn-access-log``.

    Args:
        image: Container image reference (e.g. ``vllm/vllm-openai:v0.11.0``).
        disabled: Whether request logging should be suppressed.

    Returns:
        The exact CLI flag to pass, or ``''`` when request logging is enabled.
    """
    if not disabled:
        return ''

    if 'llm-d' in image:
        return '--disable-uvicorn-access-log'

    # Upstream vllm/vllm-openai — parse the version tag
    m = re.search(r':v?(\d+)\.(\d+)', image)
    if m:
        major, minor = int(m.group(1)), int(m.group(2))
        if (major, minor) >= (0, 13):
            return '--disable-uvicorn-access-log'
        return '--disable-log-requests'

    # No parseable tag — upstream `--disable-log-requests` is only valid on old
    # images; prefer the flag accepted by current upstream AND llm-d images.
    return '--disable-uvicorn-access-log'


class TemplateManager:
    """Manages Jinja2 templates for Kubernetes deployment manifests."""

    def __init__(self, templates_dir: Optional[str] = None):
        """
        Initialize TemplateManager.

        Args:
            templates_dir: Path to templates directory (default: ./templates)
        """
        if templates_dir is None:
            current_file = Path(__file__).resolve()
            templates_dir = str(current_file.parent / 'templates')

        self.templates_dir = templates_dir
        self.env = Environment(
            loader=FileSystemLoader(templates_dir),
            trim_blocks=True,
            lstrip_blocks=True
        )

        logger.info(f"TemplateManager initialized with templates_dir: {templates_dir}")

    def render_template(self, template_path: str, **kwargs) -> str:
        """
        Render any template by path with provided variables.

        Args:
            template_path: Path to template file relative to templates_dir
            **kwargs: Template variables

        Returns:
            Rendered template as string
        """
        try:
            template = self.env.get_template(template_path)
            return template.render(**kwargs)
        except TemplateNotFound:
            raise FileNotFoundError(f"Template not found: {template_path}")

    def _get_template_path(self, architecture: str) -> str:
        """
        Get the template file path for a given architecture.

        Args:
            architecture: Architecture type ('aggregated', 'pd', 'ep')

        Returns:
            Template file path relative to templates_dir
        """
        if architecture == 'aggregated':
            return 'aggregated/lws.yaml.j2'
        elif architecture in ('pd', 'ep'):
            return {
                'prefill': 'pd/prefill-lws.yaml.j2',
                'decode': 'pd/decode-lws.yaml.j2'
            }
        else:
            raise ValueError(f"Unknown architecture: {architecture}")

    def _prepare_template_vars(self, config: TestConfig) -> Dict:
        """
        Assemble fully-resolved template variables from TestConfig
        and core producers (networking, resources).

        Templates receive a flat dict with no fallback logic needed.
        """
        vars_dict = asdict(config)

        vars_dict['deployment_name'] = f"{config.architecture}-{config.test_id}"
        vars_dict['model_name_label'] = config.model_name.replace('/', '-') if config.model_name else 'unknown'
        vars_dict['nccl_ib_hca_prefix'] = config.nccl_ib_hca.split('_')[0] if config.nccl_ib_hca else 'mlx'

        # Resolve PD role-specific TP
        vars_dict['prefill_tp'] = config.prefill_tp or config.tensor_parallelism
        vars_dict['decode_tp'] = config.decode_tp or config.tensor_parallelism

        # Resolve GPU memory utilization per role
        vars_dict['prefill_gpu_memory_utilization'] = (
            config.prefill_gpu_memory_utilization or config.gpu_memory_utilization
        )
        vars_dict['decode_gpu_memory_utilization'] = (
            config.decode_gpu_memory_utilization or config.gpu_memory_utilization
        )

        # Resolve GPU count per role
        gpus_override = getattr(config, 'gpus_per_pod', None)
        vars_dict['prefill_gpu_count'] = gpus_override or vars_dict['prefill_tp']
        vars_dict['decode_gpu_count'] = gpus_override or vars_dict['decode_tp']
        vars_dict['gpu_count'] = gpus_override or config.tensor_parallelism

        # CPU limit defaults to request
        vars_dict['cpu_limit'] = config.cpu_limit or config.cpu_request

        # Multi-node / data parallelism
        vars_dict['lws_size'] = getattr(config, 'lws_size', None) or 1
        vars_dict['data_parallelism'] = getattr(config, 'data_parallel_size', None) or 1
        vars_dict['data_parallel_size_local'] = getattr(config, 'data_parallel_size_local', None) or vars_dict['data_parallelism']

        # Multi-node pods run the same rank-per-GPU process tree on every
        # replica (leader included) — raise the pod memory floor so neither
        # side gets OOMKilled during cold model loading.
        if vars_dict['lws_size'] > 1:
            vars_dict['memory_limit'] = _bump_memory_floor(
                vars_dict.get('memory_limit'), 200)

        # Pipeline parallelism: normalize None -> 0 so templates can compare safely
        vars_dict['pipeline_parallel_size'] = getattr(config, 'pipeline_parallel_size', None) or 0
        vars_dict['context_parallel_size'] = getattr(config, 'context_parallel_size', None)
        vars_dict['prefill_context_parallel_size'] = getattr(config, 'prefill_context_parallel_size', None)
        vars_dict['decode_context_parallel_size'] = getattr(config, 'decode_context_parallel_size', None)
        vars_dict['override_generation_config'] = getattr(config, 'override_generation_config', None)
        vars_dict['prefill_attention_config'] = getattr(config, 'prefill_attention_config', None)
        vars_dict['decode_attention_config'] = getattr(config, 'decode_attention_config', None)
        # aggregated template uses attention_config = decode side
        vars_dict['attention_config'] = vars_dict['decode_attention_config']

        # KV cache and weight offloading
        vars_dict['cpu_offload_gb'] = getattr(config, 'cpu_offload_gb', None)
        vars_dict['weight_cpu_offload_gb'] = getattr(config, 'weight_cpu_offload_gb', None)
        vars_dict['disk_offload_kv_path'] = getattr(config, 'disk_offload_kv_path', None)
        vars_dict['disk_offload_kv_read_threads'] = getattr(config, 'disk_offload_kv_read_threads', 32)
        vars_dict['disk_offload_kv_write_threads'] = getattr(config, 'disk_offload_kv_write_threads', 16)
        # hostIPC or /dev/shm sizing for CPU KV offload
        vars_dict['host_ipc'] = bool(getattr(config, 'host_ipc', False))
        _shm = getattr(config, 'shm_size_gb', None)
        if not vars_dict['host_ipc']:
            if not _shm and vars_dict.get('cpu_offload_gb'):
                _shm = int(vars_dict['cpu_offload_gb']) + 210  # auto: cpu_offload_gb + 210 for CUDA/NIXL overhead
            vars_dict['shm_size_gb'] = _shm or 2
        else:
            vars_dict['shm_size_gb'] = 2  # irrelevant when hostIPC is used

        # vLLM access-log flag — version-aware across upstream vllm AND llm-d images
        vars_dict['vllm_log_request_flag'] = resolve_vllm_log_request_flag(
            vars_dict.get('image') or '',
            vars_dict.get('disable_log_requests'),
        )

        # Routing proxy image — derive from scheduler image
        sched_image = vars_dict.get('scheduler_image') or getattr(config, 'scheduler_image', '') or 'ghcr.io/llm-d/llm-d-router-endpoint-picker@sha256:873179822ab0895a37ea09f2112ca39a6ae50a26612561c8bfad7f9a8c5af6f5'
        # Extract tag; digest refs (image@sha256:...) have no usable tag — fall back to 'main'
        if '@sha256:' in sched_image:
            sched_tag = 'main'
        elif ':' in sched_image:
            sched_tag = sched_image.split(':')[-1]
        else:
            sched_tag = 'main'
        vars_dict.setdefault('routing_proxy_image', f'ghcr.io/llm-d/llm-d-router-disagg-sidecar:{sched_tag}')
        vars_dict.setdefault('sidecar_connector_flag', '--kv-connector')

        # Network values from core/networking
        rdma_nics = getattr(config, 'rdma_nics_per_node', 0)
        rdma_resources = getattr(config, 'rdma_device_resources', [])
        network_vals = compute_network_values(
            config.network_type,
            rdma_resources,
            rdma_nics,
            rdma_network_annotation=getattr(config, 'rdma_network_annotation', None),
            selected_dra_classes=getattr(config, 'selected_dra_classes', None),
            dra_gpu_resource_key=getattr(config, 'dra_gpu_resource_key', None),
        )

        # All roles use the same device resources (rdma/ib: 1 is a capacity token,
        # the discovery script handles topology-aware NIC selection)
        network_vals['prefill_extra_device_resources'] = network_vals['extra_device_resources']
        network_vals['decode_extra_device_resources'] = network_vals['extra_device_resources']

        vars_dict.update(network_vals)

        # Per-role EP flags: only enable on roles with TP > 1
        prefill_tp = vars_dict['prefill_tp']
        decode_tp = vars_dict['decode_tp']
        ep = config.enable_expert_parallel
        vars_dict['prefill_enable_expert_parallel'] = ep and prefill_tp > 1
        vars_dict['decode_enable_expert_parallel'] = ep and decode_tp > 1
        vars_dict['prefill_enable_eplb'] = config.enable_eplb and prefill_tp > 1
        vars_dict['decode_enable_eplb'] = config.enable_eplb and decode_tp > 1
        vars_dict['prefill_enable_dbo'] = config.enable_dbo and prefill_tp > 1
        vars_dict['decode_enable_dbo'] = config.enable_dbo and decode_tp > 1
        vars_dict['prefill_moe_backend'] = config.moe_backend if prefill_tp > 1 else None
        vars_dict['decode_moe_backend'] = config.moe_backend if decode_tp > 1 else None
        # Upstream llm-d uses different all2all backends per role:
        # prefill = deepep_high_throughput (optimized for batch prefill)
        # decode = deepep_low_latency (optimized for per-token decode)
        if config.all2all_backend:
            vars_dict['prefill_all2all_backend'] = config.all2all_backend if prefill_tp > 1 else None
            if config.all2all_backend == 'deepep_high_throughput':
                vars_dict['decode_all2all_backend'] = 'deepep_low_latency' if decode_tp > 1 else None
            else:
                vars_dict['decode_all2all_backend'] = config.all2all_backend if decode_tp > 1 else None
        else:
            vars_dict['prefill_all2all_backend'] = None
            vars_dict['decode_all2all_backend'] = None

        vars_dict['moe_dp_chunk_size'] = getattr(config, 'moe_dp_chunk_size', None)
        vars_dict['nvshmem_symmetric_size'] = getattr(config, 'nvshmem_symmetric_size', None)
        vars_dict['num_redundant_experts'] = getattr(config, 'num_redundant_experts', None)

        # Build speculative config JSON for --speculative-config flag.
        # speculative_config_json  → decode pod (and aggregated pod)
        # prefill_speculative_config_json → prefill pod only (PD/EP split); None suppresses it
        spec = {}
        if config.speculative_num_tokens:
            spec['method'] = config.speculative_method or 'mtp'
            spec['model'] = getattr(config, 'speculative_model', None) or config.model_name
            spec['num_speculative_tokens'] = config.speculative_num_tokens
            # Inject extra speculative config fields (user-configurable, e.g. parallel_drafting for GLM MTP)
            for entry in (getattr(config, 'speculative_extra_config', None) or []):
                k, v = entry.get('key'), entry.get('value')
                if k and v is not None:
                    if isinstance(v, str):
                        if v.lower() == 'true': v = True
                        elif v.lower() == 'false': v = False
                        else:
                            try: v = int(v)
                            except ValueError: pass
                    spec[k] = v
        vars_dict['speculative_config_json'] = json.dumps(spec) if spec else None

        prefill_tokens = getattr(config, 'prefill_speculative_num_tokens', None)
        if spec and prefill_tokens:
            prefill_spec = {**spec, 'num_speculative_tokens': prefill_tokens}
            vars_dict['prefill_speculative_config_json'] = json.dumps(prefill_spec)
        else:
            vars_dict['prefill_speculative_config_json'] = None

        return vars_dict

    def render_aggregated(self, config: TestConfig) -> str:
        """
        Render aggregated architecture template.

        Args:
            config: Test configuration

        Returns:
            Rendered YAML manifest as string
        """
        if config.architecture != 'aggregated':
            raise ValueError(f"Expected aggregated architecture, got {config.architecture}")

        template_path = self._get_template_path('aggregated')
        template = self.env.get_template(template_path)

        vars_dict = self._prepare_template_vars(config)

        rendered = template.render(**vars_dict)
        logger.info(f"Rendered aggregated template for {config.test_id}")

        return rendered

    def render_pd(self, config: TestConfig) -> Dict[str, str]:
        """
        Render PD architecture templates (prefill and decode).

        IMPORTANT: Returns manifests in deployment order - pods with higher GPU
        requirements FIRST to avoid scheduling traps where larger pods can't find
        nodes after smaller pods spread out.

        Args:
            config: Test configuration

        Returns:
            Dictionary with 'prefill' and 'decode' keys containing rendered YAML
            Ordered by GPU requirement (highest TP first)
        """
        if config.architecture not in ('pd', 'ep'):
            raise ValueError(f"Expected pd or ep architecture, got {config.architecture}")

        template_paths = self._get_template_path(config.architecture)
        prefill_template = self.env.get_template(template_paths['prefill'])
        decode_template = self.env.get_template(template_paths['decode'])

        vars_dict = self._prepare_template_vars(config)

        # Prefill pods use their own speculative and attention configs.
        prefill_vars = {
            **vars_dict,
            'speculative_config_json': vars_dict['prefill_speculative_config_json'],
            'attention_config': vars_dict['prefill_attention_config'],
        }
        # Decode pod uses decode_attention_config.
        vars_dict['attention_config'] = vars_dict['decode_attention_config']

        # Render both templates
        prefill_yaml = prefill_template.render(**prefill_vars)
        decode_yaml = decode_template.render(**vars_dict)

        # For multi-node LWS (lws_size > 1), worker pods don't serve HTTP so strip
        # startup/readiness/liveness probes from the workerTemplate containers.
        lws_size = getattr(config, 'lws_size', None) or 1
        if lws_size > 1:
            prefill_yaml = _fix_worker_template(prefill_yaml)
            decode_yaml = _fix_worker_template(decode_yaml)

        # Determine deployment order based on GPU requirements
        prefill_tp = config.prefill_tp or config.tensor_parallelism
        decode_tp = config.decode_tp or config.tensor_parallelism

        # Deploy pods with HIGHER GPU requirement first to claim full nodes
        # This prevents scheduling deadlocks where small pods spread across all nodes
        # leaving no node with enough GPUs for large pods
        if decode_tp > prefill_tp:
            rendered = {
                'decode': decode_yaml,
                'prefill': prefill_yaml
            }
            logger.info(f"Rendered PD templates for {config.test_id} (decode first: {decode_tp} > {prefill_tp} GPUs)")
        else:
            rendered = {
                'prefill': prefill_yaml,
                'decode': decode_yaml
            }
            logger.info(f"Rendered PD templates for {config.test_id} (prefill first: {prefill_tp} >= {decode_tp} GPUs)")

        return rendered

    def render_ep(self, config: TestConfig) -> Dict[str, str]:
        """
        Render EP architecture templates (uses PD prefill/decode split).

        EP reuses PD templates — the EP-specific flags (expert parallel, EPLB,
        MoE backend) are set in the config and rendered via PD template conditionals.
        """
        if config.architecture != 'ep':
            raise ValueError(f"Expected ep architecture, got {config.architecture}")
        return self.render_pd(config)

    def render_config(self, config: TestConfig) -> Dict[str, str]:
        """
        Render template(s) for any architecture type.

        Args:
            config: Test configuration

        Returns:
            Dictionary of manifest_name -> rendered_yaml
        """
        if config.architecture == 'aggregated':
            # Render both LWS and Service for aggregated
            lws_yaml = self.render_aggregated(config)
            service_template = self.env.get_template('aggregated/service.yaml.j2')
            service_yaml = service_template.render(**self._prepare_template_vars(config))
            return {'lws': lws_yaml, 'service': service_yaml}
        elif config.architecture in ('pd', 'ep'):
            pd_manifests = self.render_pd(config)
            vars_dict = self._prepare_template_vars(config)
            prefill_svc = self.env.get_template('pd/prefill-service.yaml.j2').render(**vars_dict)
            decode_svc = self.env.get_template('pd/decode-service.yaml.j2').render(**vars_dict)

            prefill_tp = config.prefill_tp or config.tensor_parallelism
            decode_tp = config.decode_tp or config.tensor_parallelism

            if decode_tp > prefill_tp:
                pd_manifests['decode-service'] = decode_svc
                pd_manifests['prefill-service'] = prefill_svc
            else:
                pd_manifests['prefill-service'] = prefill_svc
                pd_manifests['decode-service'] = decode_svc

            return pd_manifests
        else:
            raise ValueError(f"Unknown architecture: {config.architecture}")

    def save_manifest(
        self,
        config: TestConfig,
        output_dir: str,
        create_dir: bool = True
    ) -> List[str]:
        """
        Render and save manifest(s) to file(s).

        Args:
            config: Test configuration
            output_dir: Directory to save manifests
            create_dir: Create output directory if it doesn't exist

        Returns:
            List of file paths that were created
        """
        output_path = Path(output_dir)

        if create_dir:
            output_path.mkdir(parents=True, exist_ok=True)

        manifests = self.render_config(config)
        saved_files = []

        for manifest_name, manifest_content in manifests.items():
            # Create filename: <test_id>-<manifest_name>.yaml
            filename = f"{config.test_id}-{manifest_name}.yaml"
            file_path = output_path / filename

            with open(file_path, 'w') as f:
                f.write(manifest_content)

            saved_files.append(str(file_path))
            logger.info(f"Saved manifest to {file_path}")

        return saved_files

    def save_all_manifests(
        self,
        configs: List[TestConfig],
        output_dir: str,
        create_dir: bool = True
    ) -> Dict[str, List[str]]:
        """
        Save manifests for multiple test configurations.

        Args:
            configs: List of test configurations
            output_dir: Directory to save manifests
            create_dir: Create output directory if it doesn't exist

        Returns:
            Dictionary mapping test_id -> list of file paths
        """
        results = {}

        for config in configs:
            saved_files = self.save_manifest(config, output_dir, create_dir)
            results[config.test_id] = saved_files

        logger.info(f"Saved manifests for {len(configs)} configurations to {output_dir}")

        return results


def main():
    """Main entry point for standalone execution."""
    import argparse
    import json
    parser = argparse.ArgumentParser(
        description='Render Kubernetes manifests from test configurations'
    )
    parser.add_argument('--config-file', required=True, help='Path to optimization plan JSON file')
    parser.add_argument('--output-dir', required=True, help='Directory to save manifests')
    parser.add_argument('--templates-dir', help='Templates directory (default: ./templates)')

    args = parser.parse_args()

    # Load optimization plan
    with open(args.config_file, 'r') as f:
        plan_dict = json.load(f)

    # Reconstruct test configs
    from .config_generator import TestConfig
    test_configs = [TestConfig(**cfg) for cfg in plan_dict['test_configs']]

    # Render and save manifests
    manager = TemplateManager(templates_dir=args.templates_dir)
    results = manager.save_all_manifests(test_configs, args.output_dir)

    # Print summary
    print(f"✓ Saved manifests for {len(test_configs)} configurations")
    print(f"✓ Output directory: {args.output_dir}")
    print("\nFiles created:")
    for test_id, files in results.items():
        print(f"  {test_id}:")
        for file_path in files:
            print(f"    - {file_path}")


if __name__ == '__main__':
    main()
