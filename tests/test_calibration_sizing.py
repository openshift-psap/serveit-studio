"""Unit tests for calibration workload sizing (Steps 2-3).

Covers the fix for oversized calibration workloads:
1. Streams are clamped to the target pod's vLLM admission (max_num_seqs x
   1.25) instead of the raw KV-safe estimate, which could exceed admission
   by 4x and turn TTFT into pure queueing delay.
2. Calibration runs stop on duration (bounded wall time, consistent
   measured window across TP trials) instead of a request count derived
   from a latency-model guess.
3. Dataset pool sizing covers warmup + measurement window; guidellm's
   json_file loader does not cycle the pool.
"""

import types

from core.optimizer.tp_calibration import (
    CALIBRATION_DURATION_DECODE,
    CALIBRATION_DURATION_PREFILL,
    TPCalibrationMixin,
    _calibration_pool_size,
)


class _FakeResult:
    guidellm_success = True
    test_id = 'fake'
    throughput_p90 = 5.0
    throughput_p50 = 4.0
    throughput_mean = 4.5
    ttft_p90 = 100.0
    ttft_p50 = 80.0
    error_message = ''
    metrics_file = None


class _FakeOrchestrator:
    def __init__(self, owner):
        self.owner = owner
        self.run_configs = []

    def run_test(self, config, cleanup=True, log_callback=None,
                 stop_check=None, skip_deploy=False, skip_prereqs=False):
        self.run_configs.append(config)
        return _FakeResult()

    def cleanup_deployment(self, config, log_callback=None):
        pass


class _StubOptimizer(TPCalibrationMixin):
    """Minimal RecipeOptimizer stand-in exercising the calibration mixin."""

    def __init__(self, safe_c=244, admission=64, objective='pd_only',
                 max_model_len=8192, isl=1500, osl=500):
        self.config = types.SimpleNamespace(
            objective=objective, max_model_len=max_model_len,
            isl=isl, osl=osl, qps=50, model_name='test/model',
            namespace='ns')
        self._safe_c = safe_c
        self._admission = admission
        self._model_config = {'num_key_value_heads': 8, 'head_dim': 128,
                              'num_hidden_layers': 32}
        self._gpu_vram_gb = 80.0
        self.logs = []
        self.compute_calls = []
        self.mml_at_pd_config = None
        self.orchestrator = _FakeOrchestrator(self)
        self.completed_tests = {}
        self.all_test_results = []

    def log(self, msg, level='info'):
        self.logs.append(msg)

    def _should_stop(self):
        return False

    def _get_valid_tp_options(self, role='aggregated'):
        return [8]

    def _estimate_model_size_gb(self):
        return 100.0

    def _estimate_safe_concurrency(self, tp, isl=None, osl=None):
        return self._safe_c

    def _compute_gpu_mem_util(self, tp, log=True):
        return 0.90

    def _compute_max_num_seqs(self, tp, role='aggregated',
                              gpu_mem_util_override=None, num_pods=1):
        self.compute_calls.append(types.SimpleNamespace(
            tp=tp, role=role, gmu=gpu_mem_util_override,
            num_pods=num_pods, mml=self.config.max_model_len))
        return self._admission

    def _create_pd_config(self, split):
        self.mml_at_pd_config = self.config.max_model_len
        return types.SimpleNamespace(
            test_id='', isl=0, osl=0, num_users=1, request_rate=1,
            stop_mode='duration', test_duration=300, max_requests=None,
            max_model_len=None, speculative_method=None,
            speculative_num_tokens=None,
            prefill_speculative_num_tokens=None, cpu_offload_gb=None,
            weight_cpu_offload_gb=None,
        )

    def _generate_calibration_dataset(self, isl, osl, label='calibration',
                                      pool_size=0):
        return None

    def _save_test_to_database(self, config, result):
        pass

    def _check_pod_errors(self, config, result):
        pass

    def _check_request_errors(self, config, result):
        pass


def test_pool_size_covers_warmup_plus_window():
    # streams=80, ISL=1, OSL=500, 180s window:
    # req_time = 1*0.0005 + 500*0.015 = 7.5005s
    # est_rps = 80 / 7.5005; rows = (60 + 180 + 30) * est_rps
    expected = int(270 * (80 / 7.5005))
    assert _calibration_pool_size(80, 1, 500, 180) == expected
    # Pool scales with streams (more streams need more rows per second)
    assert _calibration_pool_size(160, 1, 500, 180) > expected


def test_pool_size_handles_degenerate_req_time():
    # ISL=1, OSL=1 → req_time floors at 0.1s, no division by zero
    size = _calibration_pool_size(8, 1, 1, 90)
    assert size == int(180 * (8 / 0.1))


def test_streams_clamped_to_admission():
    opt = _StubOptimizer(safe_c=244, admission=64, objective='pd_only')
    streams = opt._calibration_streams(16, isl=1, osl=500, role='decode')
    assert streams == 80  # min(244, int(64 * 1.25))
    assert any('clamped' in msg for msg in opt.logs)


def test_streams_below_admission_unchanged():
    opt = _StubOptimizer(safe_c=40, admission=64)
    streams = opt._calibration_streams(8, isl=1, osl=500, role='decode')
    assert streams == 40


def test_streams_admission_none_falls_back_to_safe():
    opt = _StubOptimizer(safe_c=48, admission=None)
    assert opt._calibration_streams(8, isl=1, osl=500, role='decode') == 48


def test_pd_only_streams_apply_calibration_max_model_len_cap():
    # In pd_only mode the admission estimate must see the calibration
    # max_model_len cap (mirrors _create_pd_config) and restore it after.
    opt = _StubOptimizer(safe_c=244, admission=64, objective='pd_only',
                         max_model_len=8192, isl=1500, osl=500)
    opt._calibration_streams(16, isl=1, osl=500, role='decode')
    assert opt.compute_calls, 'admission must be computed'
    cal_mml = 1500 + 500 + 1024
    assert all(c.mml == cal_mml for c in opt.compute_calls)
    assert opt.config.max_model_len == 8192  # restored


def test_aggregated_role_uses_uncapped_len_and_no_gmu():
    opt = _StubOptimizer(safe_c=244, admission=64, objective='balanced',
                         max_model_len=8192)
    opt._calibration_streams(8, isl=1, osl=500, role='aggregated')
    assert len(opt.compute_calls) == 1
    call = opt.compute_calls[0]
    assert call.role == 'aggregated'
    assert call.gmu is None
    assert call.mml == 8192  # no calibration cap outside pd_only


def test_decode_role_passes_nixl_adjusted_gmu():
    opt = _StubOptimizer(safe_c=244, admission=64, objective='pd_only')
    opt._calibration_streams(16, isl=1, osl=500, role='decode')
    call = opt.compute_calls[-1]
    assert call.role == 'decode'
    # 0.90 base with NIXL reserve: max(round(min(0.90, 0.85), 2), 0.80)
    assert call.gmu == 0.85


def test_apply_calibration_workload_sets_duration_stop():
    cfg = types.SimpleNamespace(
        num_users=1, request_rate=1, stop_mode='max_requests',
        test_duration=300, max_requests=3415)
    TPCalibrationMixin._apply_calibration_workload(cfg, 80, 180)
    assert cfg.num_users == 80
    assert cfg.request_rate == 80
    assert cfg.stop_mode == 'duration'
    assert cfg.test_duration == 180
    assert cfg.max_requests is None


def test_durations_are_sane():
    assert 60 <= CALIBRATION_DURATION_PREFILL < CALIBRATION_DURATION_DECODE <= 600


def test_optimize_tp_combined_wires_duration_workload():
    """End-to-end wiring: the configs handed to the orchestrator use
    admission-clamped streams and duration-based stop, not max_requests."""
    opt = _StubOptimizer(safe_c=244, admission=64, objective='pd_only',
                         max_model_len=8192, isl=1500, osl=500)
    opt._optimize_tp_combined()

    assert len(opt.orchestrator.run_configs) == 2  # decode + prefill
    decode_cfg, prefill_cfg = opt.orchestrator.run_configs

    assert decode_cfg.num_users == 80
    assert decode_cfg.request_rate == 80
    assert decode_cfg.stop_mode == 'duration'
    assert decode_cfg.test_duration == CALIBRATION_DURATION_DECODE
    assert decode_cfg.max_requests is None
    assert decode_cfg.isl == 1
    assert decode_cfg.osl == 500

    assert prefill_cfg.stop_mode == 'duration'
    assert prefill_cfg.test_duration == CALIBRATION_DURATION_PREFILL
    assert prefill_cfg.max_requests is None
    assert prefill_cfg.isl == 1500
    assert prefill_cfg.osl == 1

    # pd_only calibration len cap was in effect while configs were built
    assert opt.mml_at_pd_config == 1500 + 500 + 1024
    # and restored afterwards
    assert opt.config.max_model_len == 8192

    # Both trials completed and produced candidates
    assert any('Optimal Decode TP' in msg for msg in opt.logs)
    assert any('Optimal Prefill TP' in msg for msg in opt.logs)


def test_no_max_requests_in_guidellm_wiring():
    """The old sizing (max_requests stop) must not survive anywhere in the
    calibration flow — grep the module source as a tripwire."""
    import inspect
    import re
    src = inspect.getsource(TPCalibrationMixin)
    assert not re.search(r"stop_mode\s*=\s*'max_requests'", src)
    assert 'max_requests = ' not in src.replace('config.max_requests = None', '')
