"""Unit tests for LoRA / routing metrics collection and summarization.

Covers:
1. metrics_collector.collect_lora_metrics — emits the lora_requests_info,
   per-pod TTFT quantile, and waiting-CV queries.
2. metrics_analyzer.extract_prometheus_summaries — label-based extraction of
   vllm:lora_requests_info (adapter residency, churn, affinity), per-pod
   quantile naming (must not collide with namespace-aggregated quantiles),
   and the waiting-queue CV computed query.
"""

import json
import tempfile
from pathlib import Path

from core.metrics_analyzer import MetricsAnalyzer
from core.metrics_collector import MetricsCollector, MetricsConfig


class _FakeCollector(MetricsCollector):
    """Collector with a stubbed Prometheus endpoint."""

    def __init__(self, responses):
        cfg = MetricsConfig(thanos_url='http://fake:9090', namespace='test-ns',
                            pod_name_pattern='agg-')
        super().__init__(cfg)
        self._responses = responses
        self.queries = []

    def _query_prometheus(self, query, start_time, end_time):
        self.queries.append(query)
        return self._responses.get(query, {'data': {'result': []}})


class TestLoraCollection:
    def test_lora_queries_emitted(self):
        c = _FakeCollector({})
        c.collect_lora_metrics(1000, 2000, '120s')
        assert len(c.queries) == 4
        assert any('vllm:lora_requests_info' in q for q in c.queries)
        assert any('by(pod, le)' in q and '0.99' in q for q in c.queries)
        assert any('by(pod, le)' in q and '0.50' in q for q in c.queries)
        assert any('stddev(' in q and 'num_requests_waiting' in q for q in c.queries)

    def test_lora_metrics_in_collect_all(self):
        c = _FakeCollector({})
        c.collect_gpu_metrics = lambda *a: {}
        c.collect_pod_metrics = lambda *a: {}
        c.collect_infiniband_metrics = lambda *a: {}
        c.collect_vllm_metrics = lambda *a: {}
        c.collect_inference_metrics = lambda *a: {}
        c.collect_gateway_network_metrics = lambda *a: {}
        from datetime import datetime
        with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as f:
            out = f.name
        c.collect_all_metrics(datetime(2026, 1, 1), datetime(2026, 1, 1, 0, 10), out)
        assert any('vllm:lora_requests_info' in q for q in c.queries)
        Path(out).unlink()


def _lora_result():
    """Two pods; pod-a has adapter churn (two distinct residency sets)."""
    return {
        'data': {'result': [
            {
                'metric': {'pod': 'agg-0', 'max_lora': '4',
                           'running_lora_adapters': 'agent-0001,agent-0002',
                           'waiting_lora_adapters': ''},
                'values': [['1000', '1760000000'], ['1015', '1760000015']],
            },
            {
                'metric': {'pod': 'agg-0', 'max_lora': '4',
                           'running_lora_adapters': 'agent-0001,agent-0003',
                           'waiting_lora_adapters': 'agent-0002'},
                'values': [['1030', '1760000030']],
            },
            {
                'metric': {'pod': 'agg-1', 'max_lora': '4',
                           'running_lora_adapters': 'agent-0004',
                           'waiting_lora_adapters': ''},
                'values': [['1000', '1760000000'], ['1030', '1760000030']],
            },
        ]}
    }


def _write_metrics_file(raw_metrics):
    data = {'benchmark_start': 'x', 'benchmark_end': 'y', 'metrics': raw_metrics}
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(data, f)
        return Path(f.name)


class TestLoraSummaries:
    def test_adapter_info_extraction(self):
        p = _write_metrics_file({
            'vllm:lora_requests_info{namespace="test-ns"}': _lora_result(),
        })
        try:
            s = MetricsAnalyzer.extract_prometheus_summaries(p)
        finally:
            p.unlink()
        info = s['vllm_lora_adapter_info']
        assert info is not None

        pods = info['pods']
        assert set(pods) == {'agg-0', 'agg-1'}
        assert pods['agg-0']['max_lora'] == 4
        # Two distinct residency sets on agg-0 = churn (LRU eviction / swap)
        assert pods['agg-0']['residency_set_changes'] == 2
        assert pods['agg-1']['residency_set_changes'] == 1
        # Union of adapters seen on agg-0
        assert pods['agg-0']['running_adapters_distinct'] == [
            'agent-0001', 'agent-0002', 'agent-0003']
        # Latest series labels
        assert pods['agg-0']['running_adapters_last'] == ['agent-0001', 'agent-0003']
        assert pods['agg-0']['waiting_adapters_last'] == ['agent-0002']

        # Affinity: which pods each adapter ran on
        aff = info['adapter_pod_affinity']
        assert aff['agent-0001'] == ['agg-0']
        assert aff['agent-0004'] == ['agg-1']

    def test_adapter_info_empty(self):
        p = _write_metrics_file({
            'vllm:lora_requests_info{namespace="test-ns"}': {'data': {'result': []}},
        })
        try:
            s = MetricsAnalyzer.extract_prometheus_summaries(p)
        finally:
            p.unlink()
        assert s['vllm_lora_adapter_info'] is None

    def test_per_pod_quantile_named_separately(self):
        agg_query = 'histogram_quantile(0.99, sum by(le) (rate(vllm:time_to_first_token_seconds_bucket{namespace="n"}[120s])))'
        pod_query = 'histogram_quantile(0.99, sum by(pod, le) (rate(vllm:time_to_first_token_seconds_bucket{namespace="n"}[120s])))'
        result = {'data': {'result': [
            {'metric': {'pod': 'agg-0'}, 'values': [['1', '0.05']]},
            {'metric': {'pod': 'agg-1'}, 'values': [['1', '0.25']]},
        ]}}
        p = _write_metrics_file({agg_query: result, pod_query: result})
        try:
            s = MetricsAnalyzer.extract_prometheus_summaries(p)
        finally:
            p.unlink()
        assert 'vllm_ttft_p99' in s
        assert 'vllm_ttft_p99_per_pod' in s
        assert s['vllm_ttft_p99_per_pod']['min'] == 0.05
        assert s['vllm_ttft_p99_per_pod']['max'] == 0.25

    def test_waiting_cv_query(self):
        q = 'stddev(vllm:num_requests_waiting{namespace="n"}) / clamp_min(avg(vllm:num_requests_waiting{namespace="n"}), 0.01)'
        raw_q = 'vllm:num_requests_waiting{namespace="n"}'
        result = {'data': {'result': [
            {'metric': {'pod': 'agg-0'}, 'values': [['1', '0.5']]},
        ]}}
        p = _write_metrics_file({q: result, raw_q: result})
        try:
            s = MetricsAnalyzer.extract_prometheus_summaries(p)
        finally:
            p.unlink()
        assert s['vllm_waiting_cv'] is not None
        assert s['vllm_waiting_cv']['avg'] == 0.5
        # Raw waiting query still gets its generic name
        assert s['vllm_requests_waiting'] is not None
