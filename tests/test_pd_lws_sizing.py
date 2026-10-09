"""Unit tests for per-role LWS group sizing in PD/EP architectures.

Covers the fix for asymmetric TP pairs (e.g. prefill TP8 + decode TP16):
the decode role legitimately needs multi-pod LWS groups (TP16 spans 2 nodes),
but the prefill role must NOT inherit that group size — a TP8 prefill group
with size=2 spawns a worker pod that can never schedule (full pod memory
request) while the leader hangs in --nnodes rendezvous waiting for it.
"""


from core.config_generator import TestConfig
from core.template_manager import TemplateManager


def _pd_config(ptp, dtp, prefill_lws_size=None, decode_lws_size=None, lws_size=None):
    big = max(
        max(1, ptp // 8) if ptp > 8 else 1,
        max(1, dtp // 8) if dtp > 8 else 1,
    )
    return TestConfig(
        test_id=f'test-{ptp}p{dtp}d',
        architecture='pd',
        model_name='test/model',
        namespace='test-ns',
        isl=1000,
        osl=100,
        num_users=5,
        tensor_parallelism=ptp,
        replicas=4,
        prefill_replicas=2,
        decode_replicas=2,
        prefill_tp=ptp,
        decode_tp=dtp,
        lws_size=lws_size if lws_size is not None else (big if big > 1 else None),
        prefill_lws_size=prefill_lws_size,
        decode_lws_size=decode_lws_size,
    )


def _render(cfg):
    tm = TemplateManager()
    return tm.render_pd(cfg)


def _lws_size_of(yaml_text):
    import re
    m = re.search(r'^\s*size:\s*(\d+)', yaml_text, re.MULTILINE)
    return int(m.group(1))


class TestPerRoleLwsSizing:
    def test_ptp8_dtp16_prefill_single_node(self):
        """TP8 prefill must stay 1 pod/group even when decode TP16 needs 2."""
        cfg = _pd_config(8, 16, prefill_lws_size=1, decode_lws_size=2)
        rendered = _render(cfg)
        assert _lws_size_of(rendered['prefill']) == 1
        assert _lws_size_of(rendered['decode']) == 2
        assert '--nnodes' not in rendered['prefill']
        assert '--nnodes 2' in rendered['decode']

    def test_ptp16_dtp8_decode_single_node(self):
        """Mirror case: decode TP8 must not inherit prefill TP16 group size."""
        cfg = _pd_config(16, 8, prefill_lws_size=2, decode_lws_size=1)
        rendered = _render(cfg)
        assert _lws_size_of(rendered['prefill']) == 2
        assert _lws_size_of(rendered['decode']) == 1
        assert '--nnodes 2' in rendered['prefill']
        assert '--nnodes' not in rendered['decode']

    def test_symmetric_tp16_both_multi_node(self):
        cfg = _pd_config(16, 16, prefill_lws_size=2, decode_lws_size=2)
        rendered = _render(cfg)
        assert _lws_size_of(rendered['prefill']) == 2
        assert _lws_size_of(rendered['decode']) == 2
        assert '--nnodes 2' in rendered['prefill']
        assert '--nnodes 2' in rendered['decode']

    def test_symmetric_tp8_both_single_node(self):
        cfg = _pd_config(8, 8, prefill_lws_size=1, decode_lws_size=1)
        rendered = _render(cfg)
        assert _lws_size_of(rendered['prefill']) == 1
        assert _lws_size_of(rendered['decode']) == 1
        assert '--nnodes' not in rendered['prefill']
        assert '--nnodes' not in rendered['decode']

    def test_backward_compat_shared_lws_size(self):
        """Configs without per-role fields fall back to the shared lws_size."""
        cfg = _pd_config(8, 16, lws_size=2)  # legacy: no per-role fields
        rendered = _render(cfg)
        assert _lws_size_of(rendered['prefill']) == 2
        assert _lws_size_of(rendered['decode']) == 2

    def test_no_multi_node_at_all(self):
        """No lws fields set anywhere: both roles render size 1, no --nnodes."""
        cfg = _pd_config(8, 8, lws_size=None)
        rendered = _render(cfg)
        assert _lws_size_of(rendered['prefill']) == 1
        assert _lws_size_of(rendered['decode']) == 1
        assert '--nnodes' not in rendered['prefill']
        assert '--nnodes' not in rendered['decode']


class TestWorkerTemplateFixGating:
    def test_probe_strip_only_for_multi_node_role(self):
        """_fix_worker_template must only touch the role whose size > 1."""
        import re
        cfg = _pd_config(8, 16, prefill_lws_size=1, decode_lws_size=2)
        rendered = _render(cfg)
        # Multi-node decode: worker template's startupProbe (template source
        # line ~419) is stripped and leader memory is floored to 200Gi
        decode_worker = re.search(
            r'workerTemplate:.*?(startupProbe:)', rendered['decode'], re.DOTALL)
        assert decode_worker is None
        assert re.search(r'leaderTemplate:.*?memory: 200Gi', rendered['decode'], re.DOTALL)
        # Single-node prefill: never passed through _fix_worker_template, so
        # its leader memory is NOT floored to the multi-node 200Gi minimum
        assert not re.search(r'leaderTemplate:.*?memory: 200Gi', rendered['prefill'], re.DOTALL)


class TestBidirectionalKvGuard:
    """Bidirectional NIXL + multi-node LWS groups hangs the first request
    (validated live on kermit: TP16/TP16 PD hangs with true, serves in ~1.6s
    with false). Auto-enable must skip multi-node configs."""

    def _make_stub(self, turns, **cfg_kwargs):
        import types
        from core.optimizer.config_builder import ConfigBuilderMixin
        stub = types.SimpleNamespace(
            config=types.SimpleNamespace(turns=turns),
            logs=[],
        )
        stub.log = lambda msg, level='info': stub.logs.append(msg)
        stub._auto_enable_bidirectional_kv = (
            ConfigBuilderMixin._auto_enable_bidirectional_kv.__get__(stub))
        from core.config_generator import TestConfig
        cfg = TestConfig(
            test_id='t', architecture='pd', model_name='m', namespace='ns',
            isl=1000, osl=100, num_users=5, tensor_parallelism=8, replicas=2,
            prefill_replicas=1, decode_replicas=1, prefill_tp=8, decode_tp=8,
            **cfg_kwargs)
        return stub, cfg

    def test_single_node_multi_turn_auto_enables(self):
        stub, cfg = self._make_stub(turns=300)
        cfg = stub._auto_enable_bidirectional_kv(cfg)
        assert cfg.enable_bidirectional_kv is True

    def test_multi_node_prefill_blocks_auto_enable(self):
        stub, cfg = self._make_stub(turns=300, prefill_lws_size=2, decode_lws_size=1)
        cfg = stub._auto_enable_bidirectional_kv(cfg)
        assert not cfg.enable_bidirectional_kv
        assert any('NOT auto-enabled' in m for m in stub.logs)

    def test_multi_node_decode_blocks_auto_enable(self):
        stub, cfg = self._make_stub(turns=300, prefill_lws_size=1, decode_lws_size=2)
        cfg = stub._auto_enable_bidirectional_kv(cfg)
        assert not cfg.enable_bidirectional_kv

    def test_single_turn_not_enabled(self):
        stub, cfg = self._make_stub(turns=1)
        cfg = stub._auto_enable_bidirectional_kv(cfg)
        assert not cfg.enable_bidirectional_kv

    def test_explicit_setting_preserved(self):
        stub, cfg = self._make_stub(turns=300, enable_bidirectional_kv=True,
                                    prefill_lws_size=2, decode_lws_size=2)
        cfg = stub._auto_enable_bidirectional_kv(cfg)
        assert cfg.enable_bidirectional_kv is True
