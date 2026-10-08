"""Verify all Jinja2 templates render without errors and produce valid YAML."""

import yaml
import pytest
from core.template_manager import TemplateManager


@pytest.fixture
def tm():
    return TemplateManager()


MOCK_VARS = {
    'test_id': 'test-tp1',
    'namespace': 'serveit',
    'model_name': 'Qwen/Qwen3-32B',
    'tensor_parallelism': 8,
    'gpu_count': 8,
    'max_model_len': 8192,
    'gpu_memory_utilization': 0.9,
    'max_num_seqs': 64,
    'block_size': 128,
    'image': 'vllm/vllm-openai:v0.26.0',
    'scheduler_image': 'ghcr.io/llm-d/llm-d-router-endpoint-picker:main',
    'pvc_name': 'serveit-cache',
    'memory_limit': '64Gi',
    'cpu_limit': '16',
    'nccl_ib_hca': 'mlx',
    'gpu_resource_key': 'nvidia.com/gpu',
    'extra_device_resources': [],
    'selected_dra_classes': [],
    'rdma_network_annotation': None,
    'rdma_nics_per_node': 0,
    'enable_prefix_caching': True,
    'enable_expert_parallel': False,
    'enable_dbo': False,
    'enable_eplb': False,
    'use_deep_gemm': None,
    'moe_dp_chunk_size': None,
    'nvshmem_symmetric_size': None,
    'disable_log_requests': True,
    'vllm_debug_logs': False,
    'trust_remote_code': True,
    'dtype': 'auto',
    'local_disk_path': '/mnt/local',
    'per_node_storage': True,
    'node_nfs_pvcs': [],
    'lws_size': 1,
    'hf_token': 'test-token',
    'kv_cache_dtype': None,
    'http_timeout_keep_alive': None,
    'prefix_cache_retention': None,
    'ssm_conv_state_layout': None,
    'model_loader_extra_config': None,
    'disk_offload_kv_path': None,
    'data_parallelism': 1,
    'enable_auto_tool_choice': False,
    'tool_call_parser': None,
    'reasoning_parser': None,
    'chat_template_content_format': None,
    'max_num_batched_tokens': None,
    'enable_chunked_prefill': False,
    'pipeline_parallel_size': 1,
    'disable_custom_all_reduce': False,
}

PD_VARS = {
    **MOCK_VARS,
    'prefill_tp': 8,
    'decode_tp': 8,
    'prefill_gpu_memory_utilization': 0.8,
    'decode_gpu_memory_utilization': 0.9,
    'prefill_max_num_seqs': 32,
    'decode_max_num_seqs': 64,
    'prefill_enable_expert_parallel': False,
    'decode_enable_expert_parallel': False,
    'prefill_enable_dbo': False,
    'decode_enable_dbo': False,
    'prefill_enable_eplb': False,
    'decode_enable_eplb': False,
    'prefill_moe_dp_chunk_size': None,
    'decode_moe_dp_chunk_size': None,
    'prefill_nvshmem_symmetric_size': None,
    'decode_nvshmem_symmetric_size': None,
    'prefill_use_deep_gemm': None,
    'decode_use_deep_gemm': None,
    'prefill_extra_device_resources': [],
    'decode_extra_device_resources': [],
    'prefill_max_num_batched_tokens': None,
    'decode_max_num_batched_tokens': None,
    'sidecar_image': 'ghcr.io/llm-d/llm-d-router-disagg-sidecar:main',
    'sidecar_connector_flag': '--kv-connector',
    'kv_transfer_config': None,
    'prefill_kv_transfer_config': None,
    'decode_kv_transfer_config': None,
    'cpu_offload_gb': None,
}


AGGREGATED_TEMPLATES = [
    'aggregated/lws.yaml.j2',
    'aggregated/service.yaml.j2',
]

PD_TEMPLATES = [
    'pd/prefill-lws.yaml.j2',
    'pd/decode-lws.yaml.j2',
    'pd/prefill-service.yaml.j2',
    'pd/decode-service.yaml.j2',
]

PREREQ_TEMPLATES = [
    ('prereq/model-cache-pvc.yaml.j2', {
        'pvc_name': 'serveit-cache', 'namespace': 'serveit',
        'test_id': 'test', 'model_name': 'test',
        'storage_class': 'gp2', 'storage_size': 50,
        'pvc_access_mode': 'ReadWriteMany',
    }),
    ('prereq/model-download-job.yaml.j2', {
        'job_name': 'test-download', 'namespace': 'serveit',
        'test_id': 'test', 'model_name': 'Qwen/Qwen3-32B',
        'pvc_name': 'serveit-cache', 'hf_token': 'test',
    }),
    ('prereq/model-download-job.yaml.j2', {
        'job_name': 'test-download-local', 'namespace': 'serveit',
        'test_id': 'test', 'model_name': 'Qwen/Qwen3-32B',
        'hf_token': 'test', 'local_disk_path': '/mnt/local',
        'target_node': 'worker-0',
    }),
    ('prereq/gaie-configmap-default.yaml.j2', {
        'namespace': 'serveit', 'test_id': 'test',
    }),
    ('prereq/gaie-serviceaccount.yaml.j2', {
        'namespace': 'serveit', 'test_id': 'test',
    }),
    ('prereq/gateway.yaml.j2', {
        'namespace': 'serveit', 'test_id': 'test',
        'gateway_class': 'istio',
    }),
]


@pytest.mark.parametrize('template', AGGREGATED_TEMPLATES)
def test_aggregated_template_renders(tm, template):
    result = tm.render_template(template, **MOCK_VARS)
    assert result
    docs = list(yaml.safe_load_all(result))
    assert len(docs) >= 1
    for doc in docs:
        assert doc is not None
        assert 'kind' in doc or 'apiVersion' in doc


@pytest.mark.parametrize('template', PD_TEMPLATES)
def test_pd_template_renders(tm, template):
    result = tm.render_template(template, **PD_VARS)
    assert result
    docs = list(yaml.safe_load_all(result))
    assert len(docs) >= 1
    for doc in docs:
        assert doc is not None
        assert 'kind' in doc or 'apiVersion' in doc


@pytest.mark.parametrize('template,vars', PREREQ_TEMPLATES,
                         ids=[t[0].split('/')[-1] + ('-local' if t[1].get('local_disk_path') else '') for t in PREREQ_TEMPLATES])
def test_prereq_template_renders(tm, template, vars):
    result = tm.render_template(template, **vars)
    assert result
    docs = list(yaml.safe_load_all(result))
    assert len(docs) >= 1


def test_aggregated_has_rdma_discovery(tm):
    """Aggregated template should source RDMA discovery script."""
    result = tm.render_template('aggregated/lws.yaml.j2', **MOCK_VARS)
    assert 'discover_ib_hca.sh' in result
    assert 'rdma-script' in result


def test_aggregated_pipeline_parallelism_renders(tm):
    """PP > 1 must emit --pipeline-parallel-size and --nnodes."""
    vars_pp = {**MOCK_VARS, 'pipeline_parallel_size': 2, 'lws_size': 2, 'nnodes': 2}
    result = tm.render_template('aggregated/lws.yaml.j2', **vars_pp)
    assert '--pipeline-parallel-size 2' in result
    assert '--nnodes 2' in result

    vars_pp1 = {**MOCK_VARS, 'pipeline_parallel_size': 1}
    result_pp1 = tm.render_template('aggregated/lws.yaml.j2', **vars_pp1)
    assert '--pipeline-parallel-size' not in result_pp1


def test_download_job_local_disk_uses_hostpath(tm):
    """Download job with local_disk_path should use hostPath, not PVC."""
    result = tm.render_template('prereq/model-download-job.yaml.j2',
        job_name='test', namespace='serveit', test_id='test',
        model_name='test', hf_token='test',
        local_disk_path='/mnt/local', target_node='node-0')
    assert 'hostPath' in result
    assert '/mnt/local/model-cache' in result


def test_download_job_pvc_without_local_disk(tm):
    """Download job without local_disk_path should use PVC."""
    result = tm.render_template('prereq/model-download-job.yaml.j2',
        job_name='test', namespace='serveit', test_id='test',
        model_name='test', pvc_name='my-pvc', hf_token='test')
    assert 'persistentVolumeClaim' in result
    assert 'my-pvc' in result
    assert 'hostPath' not in result


def test_du_does_not_crash_on_permission_errors(tm):
    """Download job du command should have || true to avoid crash."""
    result = tm.render_template('prereq/model-download-job.yaml.j2',
        job_name='test', namespace='serveit', test_id='test',
        model_name='test', hf_token='test', pvc_name='my-pvc')
    assert '|| true' in result or '2>/dev/null' in result


# ---- Multi-node (lws_size > 1) regression tests ---------------------------
# All validated live on the kermit cluster (TP16 GLM-5.3 across 2 LWS pods,
# H200, driver 595.71.05, NCCL 2.28.3). See _fix_worker_template docstring.

NCCL_WORKAROUNDS = {
    'NCCL_ALGO': 'Ring',
    'NCCL_PROTO': 'Simple',
    'NCCL_IB_GDR': '0',
}


def _assert_no_launch_blocking(container, role, side):
    """CUDA_LAUNCH_BLOCKING must NOT be set — eager mode makes it unnecessary
    and it serializes every kernel launch (validated combo didn't use it)."""
    assert _env_map(container).get('CUDA_LAUNCH_BLOCKING') is None, (role, side)


def _assert_cuda_graphs_enabled(container, role, side):
    """--enforce-eager must NEVER be applied: this product's core is multi-node
    serving, and disabling CUDA graphs there would wreck decode performance.
    The capture-phase hangs observed during debugging predated the worker
    config-parity fix; single-node capture works on the same driver."""
    assert '--enforce-eager' not in container['args'][0], (role, side)


def _make_config(**over):
    from core.config_generator import TestConfig
    base = dict(
        test_id='tptest',
        architecture='pd',
        model_name='Qwen/Qwen3-32B',
        namespace='serveit',
        tensor_parallelism=16,
        isl=8192,
        osl=1024,
        num_users=16,
        replicas=1,
        prefill_tp=16,
        decode_tp=16,
        lws_size=2,
        block_size=128,
        kv_cache_dtype='fp8',
        prefill_attention_config='{"sparse_mla_force_mqa":true}',
        decode_attention_config='{"sparse_mla_force_mqa":true}',
        memory_limit='40Gi',
        network_type='eth0',
    )
    base.update(over)
    return TestConfig(**base)


def _lws_doc(yaml_str):
    return next(d for d in yaml.safe_load_all(yaml_str)
                if d and d.get('kind') == 'LeaderWorkerSet')


def _vllm_container(pod_spec):
    for c in pod_spec.get('containers', []):
        if c.get('name') == 'vllm':
            return c
    raise AssertionError('no vllm container in pod spec')


def _env_map(container):
    return {e['name']: e.get('value') for e in container.get('env', [])}


def test_multinode_pd_renders_all_fixes(tm):
    """PD multi-node render must carry every validated fix on both sides."""
    out = tm.render_pd(_make_config())
    for role in ('prefill', 'decode'):
        lwt = _lws_doc(out[role])['spec']['leaderWorkerTemplate']
        leader = _vllm_container(lwt['leaderTemplate']['spec'])
        worker = _vllm_container(lwt['workerTemplate']['spec'])

        for cont, side in ((leader, 'leader'), (worker, 'worker')):
            # NCCL workaround env
            for k, v in NCCL_WORKAROUNDS.items():
                assert _env_map(cont).get(k) == v, (role, side, k)
            # no synchronous-launch penalty in the serving config
            _assert_no_launch_blocking(cont, role, side)
            # CUDA graphs stay enabled — never enforce eager
            _assert_cuda_graphs_enabled(cont, role, side)
            # flashinfer allreduce disable + spawn guard
            # (worker printf quotes are backslash-escaped, so match the prefix)
            assert '_fia.fi_ar_available=False' in cont['args'][0], (role, side)
            assert 'if __name__==' in cont['args'][0], (role, side)
            # memory floor
            assert cont['resources']['requests']['memory'] == '200Gi', (role, side)
            assert cont['resources']['limits']['memory'] == '200Gi', (role, side)

        # Worker is headless but keeps the leader's config flags (KV spec parity)
        wargs = worker['args'][0]
        assert '--headless' in wargs
        assert '--block-size 128' in wargs
        assert '--kv-cache-dtype fp8' in wargs
        assert 'sparse_mla_force_mqa' in wargs
        assert '${TP_SIZE}' in wargs
        assert '${LWS_LEADER_ADDRESS}' in wargs
        # Worker pods don't serve HTTP — no probes
        assert 'startupProbe' not in worker
        assert 'readinessProbe' not in worker


def test_multinode_pd_memory_floor_never_shrinks(tm):
    """A deliberately larger memory_limit must survive the multi-node floor."""
    out = tm.render_pd(_make_config(memory_limit='512Gi'))
    lwt = _lws_doc(out['prefill'])['spec']['leaderWorkerTemplate']
    leader = _vllm_container(lwt['leaderTemplate']['spec'])
    worker = _vllm_container(lwt['workerTemplate']['spec'])
    assert leader['resources']['requests']['memory'] == '512Gi'
    assert worker['resources']['requests']['memory'] == '512Gi'


def test_multinode_worker_flags_keep_json_quoting(tm):
    """Worker serve command must keep shell-quoting on JSON-valued flags.

    _fix_worker_template rebuilds the worker flags via shlex.split + join;
    shlex CONSUMES the single quotes around --kv-transfer-config '{"..."}',
    and rejoining bare tokens let bash brace-expand the JSON — vLLM died at
    worker startup with "Invalid JSON ... kv_connector:NixlConnector"
    (TP16 run, 2026-10-08). Regression guard: the JSON must arrive
    single-quoted AND round-trip through a real shell parse as valid JSON.
    """
    import json as _json
    import re as _re
    import shlex as _shlex

    out = tm.render_pd(_make_config())
    for role in ('prefill', 'decode'):
        lwt = _lws_doc(out[role])['spec']['leaderWorkerTemplate']
        worker = _vllm_container(lwt['workerTemplate']['spec'])
        a = worker['args'][0]
        # JSON value must be single-quoted in the bash script...
        assert "--kv-transfer-config '{\"kv_connector\":" in a, role
        # ...and must survive a real shell tokenization as ONE valid JSON arg
        m = _re.search(r'--kv-transfer-config (\S+)', a)
        toks = _shlex.split(m.group(0))
        parsed = _json.loads(toks[1])  # raises if quoting was mangled
        assert parsed.get('kv_connector') == 'NixlConnector', role
        # bash would still expand $VAR references (not quoted away)
        assert '${TP_SIZE}' in a and '${LWS_LEADER_ADDRESS}' in a, role


def test_single_node_pd_has_no_multinode_workarounds(tm):
    """Single-node keeps the fast path: no eager, no NCCL env, no worker."""
    cfg = _make_config(lws_size=1, tensor_parallelism=8,
                       prefill_tp=8, decode_tp=8)
    out = tm.render_pd(cfg)
    for role in ('prefill', 'decode'):
        lwt = _lws_doc(out[role])['spec']['leaderWorkerTemplate']
        leader = _vllm_container(lwt['leaderTemplate']['spec'])
        assert not lwt['workerTemplate']['spec'].get('containers')
        env = _env_map(leader)
        assert 'NCCL_ALGO' not in env
        assert 'CUDA_LAUNCH_BLOCKING' not in env
        assert '--enforce-eager' not in leader['args'][0]
        assert '_fia.fi_ar_available' not in leader['args'][0]
        assert leader['resources']['requests']['memory'] == '40Gi'


def test_multinode_aggregated_renders_all_fixes(tm):
    """Aggregated multi-node renders the same fixes natively in-template."""
    doc = _lws_doc(tm.render_aggregated(
        _make_config(architecture='aggregated')))
    lwt = doc['spec']['leaderWorkerTemplate']
    leader = _vllm_container(lwt['leaderTemplate']['spec'])
    worker = _vllm_container(lwt['workerTemplate']['spec'])
    for cont, side in ((leader, 'leader'), (worker, 'worker')):
        for k, v in NCCL_WORKAROUNDS.items():
            assert _env_map(cont).get(k) == v, (side, k)
        _assert_no_launch_blocking(cont, role='aggregated', side=side)
        _assert_cuda_graphs_enabled(cont, role='aggregated', side=side)
        assert '_fia.fi_ar_available=False' in cont['args'][0], side
        assert cont['resources']['requests']['memory'] == '200Gi', side
    # Worker keeps config flags for KV spec parity
    wargs = worker['args'][0]
    assert '--headless' in wargs
    assert '--block-size 128' in wargs
    assert '--kv-cache-dtype fp8' in wargs


def test_bump_memory_floor():
    from core.template_manager import _bump_memory_floor
    assert _bump_memory_floor('40Gi', 200) == '200Gi'
    assert _bump_memory_floor('64Gi', 200) == '200Gi'
    assert _bump_memory_floor('200Gi', 200) == '200Gi'
    assert _bump_memory_floor('512Gi', 200) == '512Gi'
    assert _bump_memory_floor('204800Mi', 200) == '204800Mi'  # exactly 200Gi
    assert _bump_memory_floor('102400Mi', 200) == '200Gi'     # 100Gi
    assert _bump_memory_floor('1Ti', 200) == '1Ti'
    assert _bump_memory_floor(None, 200) is None
    assert _bump_memory_floor('weird', 200) == 'weird'


# ---- Skip TP16 option ------------------------------------------------------

def _make_optimizer_stub(skip_tp16=True, allow_asymmetric=True):
    """Minimal RecipeOptimizer stand-in for exercising PDSearchMixin."""
    from types import SimpleNamespace
    from core.optimizer.pipeline import RecipeOptimizer

    opt = RecipeOptimizer.__new__(RecipeOptimizer)
    opt.config = SimpleNamespace(
        objective='balanced',
        tp_pair_top_n=4,
        allow_asymmetric_tp=allow_asymmetric,
        asymmetric_allow_decode_gt_prefill=True,
        asymmetric_allow_prefill_gt_decode=True,
        skip_tp16=skip_tp16,
    )
    opt.prefill_tp_results = [{'tp': 16, 'tpsg': 9.0, 'ttft_p90': 1.0},
                              {'tp': 8, 'tpsg': 5.0, 'ttft_p90': 2.0}]
    opt.decode_tp_results = [{'tp': 16, 'tpsg': 9.0},
                             {'tp': 8, 'tpsg': 5.0}]
    opt._logs = []
    opt.log = lambda msg, level='info', **kw: opt._logs.append(msg)
    return opt


def test_select_tp_pairs_skips_tp16_when_enabled():
    opt = _make_optimizer_stub(skip_tp16=True)
    opt._select_tp_pairs()
    pairs = opt._selected_tp_pairs
    assert pairs, 'expected at least one pair'
    assert all(ptp != 16 and dtp != 16 for ptp, dtp in pairs)
    assert (8, 8) in pairs
    assert any('TP16' in m or 'TP pairs' in m for m in opt._logs)


def test_select_tp_pairs_includes_tp16_when_disabled():
    opt = _make_optimizer_stub(skip_tp16=False)
    opt._select_tp_pairs()
    assert (16, 16) in opt._selected_tp_pairs


def test_get_valid_tp_options_skips_tp16_when_enabled():
    from types import SimpleNamespace
    from core.optimizer.pipeline import RecipeOptimizer

    opt = RecipeOptimizer.__new__(RecipeOptimizer)
    opt.config = SimpleNamespace(tp_options=[1, 2, 4, 8, 16], skip_tp16=True)
    opt.cluster_resources = None
    opt._model_config = None
    opt.log = lambda *a, **k: None
    tps = opt._get_valid_tp_options()
    assert 16 not in tps
    assert 8 in tps

    opt.config.skip_tp16 = False
    tps = opt._get_valid_tp_options()
    assert 16 in tps


# ---- LoRA serving -----------------------------------------------------------

def _lora_flags(args_str):
    """Extract the contiguous LoRA flag block from a serve command string."""
    import re
    m = re.search(r'--enable-lora.*?(?=\\?\n)', args_str, re.S)
    return m.group(0) if m else ''


def _has_volume(pod_spec, name, mount_path=None):
    vols = pod_spec.get('volumes', [])
    if not any(v.get('name') == name for v in vols):
        return False
    if mount_path:
        for c in pod_spec.get('containers', []):
            for m in c.get('volumeMounts', []):
                if m.get('name') == name and m.get('mountPath') == mount_path:
                    return True
        return False
    return True


def test_lora_disabled_by_default(tm):
    """Default config renders no LoRA flags, volumes, or mounts anywhere."""
    out = tm.render_pd(_make_config())
    doc = _lws_doc(tm.render_aggregated(
        _make_config(architecture='aggregated')))
    renders = [out['prefill'], out['decode'], _render_doc(doc)]
    for r in renders:
        assert '--enable-lora' not in r
        assert 'lora-adapters' not in r
        assert '/adapters' not in r


def _render_doc(doc):
    return yaml.safe_dump(doc)


def test_lora_renders_module_list_and_parity(tm):
    """Enabled LoRA renders agent-01..agent-N identically on all sides."""
    cfg = _make_config(lora_enabled=True, lora_num_adapters=20)
    out = tm.render_pd(cfg)
    lora_blocks = {}
    for role in ('prefill', 'decode'):
        lwt = _lws_doc(out[role])['spec']['leaderWorkerTemplate']
        leader = _vllm_container(lwt['leaderTemplate']['spec'])
        worker = _vllm_container(lwt['workerTemplate']['spec'])
        for cont, side in ((leader, 'leader'), (worker, 'worker')):
            a = cont['args'][0]
            assert '--enable-lora' in a, (role, side)
            assert '--max-lora-rank 16' in a, (role, side)
            assert '--max-loras 4' in a, (role, side)
            assert '--max-cpu-loras 20' in a, (role, side)
            assert 'agent-01=/adapters/agent-01' in a, (role, side)
            assert 'agent-20=/adapters/agent-20' in a, (role, side)
            assert 'agent-21' not in a, (role, side)
            # volume + mount present on both leader and worker pods
            pod_spec = (lwt['leaderTemplate'] if side == 'leader'
                        else lwt['workerTemplate'])['spec']
            assert _has_volume(pod_spec, 'lora-adapters', '/adapters'), (role, side)
            lora_blocks[(role, side)] = _lora_flags(a)
    # PD parity: prefill and decode serve the IDENTICAL adapter set
    assert lora_blocks[('prefill', 'leader')] == lora_blocks[('decode', 'leader')]

    # Aggregated multi-node: leader and worker both carry the adapter set
    doc = _lws_doc(tm.render_aggregated(
        _make_config(architecture='aggregated',
                     lora_enabled=True, lora_num_adapters=20)))
    lwt = doc['spec']['leaderWorkerTemplate']
    for tpl, side in ((lwt['leaderTemplate'], 'leader'),
                      (lwt['workerTemplate'], 'worker')):
        cont = _vllm_container(tpl['spec'])
        a = cont['args'][0]
        assert '--enable-lora' in a and 'agent-20=/adapters/agent-20' in a, side
        assert _has_volume(tpl['spec'], 'lora-adapters', '/adapters'), side


def test_lora_max_loras_capped_at_module_count(tm):
    """Warm cap can't exceed the number of declared adapters."""
    cfg = _make_config(lora_enabled=True, lora_num_adapters=2)
    out = tm.render_pd(cfg)
    lwt = _lws_doc(out['prefill'])['spec']['leaderWorkerTemplate']
    a = _vllm_container(lwt['leaderTemplate']['spec'])['args'][0]
    assert '--max-loras 2' in a
    assert '--max-cpu-loras 2' in a
    assert 'agent-02=/adapters/agent-02' in a
    assert 'agent-03' not in a


def test_lora_cpu_staging_memory_accounting(tm):
    """Pod memory grows with adapter staging; never reduced; override respected."""
    # Single-node baseline is 40Gi; staging = ceil(20*320/1024)+4 = 11Gi
    base = dict(lws_size=1, tensor_parallelism=8, prefill_tp=8, decode_tp=8)
    out = tm.render_pd(_make_config(**base))
    lwt = _lws_doc(out['prefill'])['spec']['leaderWorkerTemplate']
    mem = _vllm_container(lwt['leaderTemplate']['spec'])['resources']['requests']['memory']
    assert mem == '40Gi'

    out = tm.render_pd(_make_config(lora_enabled=True, lora_num_adapters=20, **base))
    lwt = _lws_doc(out['prefill'])['spec']['leaderWorkerTemplate']
    mem = _vllm_container(lwt['leaderTemplate']['spec'])['resources']['requests']['memory']
    assert mem == '51Gi', '40Gi base + 11Gi staging (20 x ~320MB + headroom)'

    # Per-adapter size override shrinks the estimate: ceil(20*100/1024)+4 = 6Gi
    out = tm.render_pd(_make_config(lora_enabled=True, lora_num_adapters=20,
                                    lora_adapter_size_mb=100, **base))
    lwt = _lws_doc(out['prefill'])['spec']['leaderWorkerTemplate']
    mem = _vllm_container(lwt['leaderTemplate']['spec'])['resources']['requests']['memory']
    assert mem == '46Gi'

    # Multi-node: floor (200Gi) applies first, staging adds on top (211Gi)
    out = tm.render_pd(_make_config(lora_enabled=True, lora_num_adapters=20))
    lwt = _lws_doc(out['prefill'])['spec']['leaderWorkerTemplate']
    mem = _vllm_container(lwt['leaderTemplate']['spec'])['resources']['requests']['memory']
    assert mem == '211Gi'


def test_add_memory_gib():
    from core.template_manager import _add_memory_gib
    assert _add_memory_gib('40Gi', 11) == '51Gi'
    assert _add_memory_gib('512Gi', 11) == '523Gi'
    assert _add_memory_gib('40Gi', 0) == '40Gi'
    assert _add_memory_gib(None, 11) is None
    assert _add_memory_gib('weird', 11) == 'weird'
    assert _add_memory_gib('2048Mi', 1) == '3Gi'  # 2Gi + 1Gi
