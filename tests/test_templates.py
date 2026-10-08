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
            # eager mode + flashinfer allreduce disable + spawn guard
            # (worker printf quotes are backslash-escaped, so match the prefix)
            assert '--enforce-eager' in cont['args'][0], (role, side)
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
        assert '--enforce-eager' in cont['args'][0], side
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
