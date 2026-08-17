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
        model_name='test', pvc_name='my-pvc', hf_token='test')
    assert '|| true' in result or '2>/dev/null' in result
