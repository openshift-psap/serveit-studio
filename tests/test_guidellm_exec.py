"""Unit tests for detached guidellm execution and the gateway smoke test.

Covers the two robustness fixes for long-running benchmarks on remote
clusters:
1. guidellm runs detached on the workload pod (no long-lived kubectl exec
   stream that proxies can drop after ~30 minutes) with exit-code polling
   and stale-process cleanup between attempts.
2. A tiny completion is sent through the gateway before each load-test
   attempt so a wedged EPP fails fast instead of producing a dead run.
"""

import inspect
import json
import types

import core.orchestrator.guidellm as guidellm_module
from core.orchestrator.guidellm import GuidellmMixin
from core.config_generator import TestConfig


class _CP:
    """CompletedProcess stand-in."""

    def __init__(self, returncode=0, stdout='', stderr=''):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeKubectl:
    """Scripted kubectl runner for workload-pod exec calls."""

    kubectl_cmd = 'kubectl'
    kubeconfig = '~/.kube/test-config'

    def __init__(self, poll_results=None, curl_results=None, tail_stdout=''):
        self.calls = []  # (args, input_data)
        self.script = None
        self.poll_results = list(poll_results or ['RUNNING', 'RUNNING', '0'])
        self.curl_results = list(curl_results or ['200'])
        self.tail_stdout = tail_stdout

    def run(self, args, input_data=None, check=True, timeout=60):
        args = list(args)
        self.calls.append((args, input_data))
        joined = ' '.join(args)

        if 'pkill' in joined:
            return _CP(0, '')
        if input_data is not None:
            self.script = input_data
            return _CP(0, '')
        if 'nohup' in joined:
            return _CP(0, 'launched')
        if 'echo RUNNING' in joined:
            return _CP(0, self.poll_results.pop(0) if self.poll_results else '0')
        if 'tail' in joined:
            return _CP(0, self.tail_stdout)
        if 'curl' in joined:
            result = self.curl_results.pop(0) if self.curl_results else '200'
            if result == 'ERR':
                return _CP(28, '000')
            return _CP(0, result)
        return _CP(0, '')


class Harness(GuidellmMixin):
    """Minimal host for the mixin — no cluster access."""

    def __init__(self, kubectl):
        self.namespace = 'test-ns'
        self._guidellm_pod_name = 'workload-pod'
        self.deployment_manager = types.SimpleNamespace(kubectl=kubectl)

    def ensure_guidellm_pod(self, config, log_callback=None):
        return True

    def _monitor_pods_during_benchmark(self, *args, **kwargs):
        return None


def make_config(**overrides):
    defaults = dict(
        test_id='unit-run', architecture='aggregated',
        model_name='Qwen/Qwen3-32B', namespace='test-ns',
        isl=128, osl=32, num_users=4, tensor_parallelism=1, replicas=1,
        test_duration=60,
    )
    defaults.update(overrides)
    return TestConfig(**defaults)


# ── Gateway smoke test ────────────────────────────────────────────────────

def test_smoke_test_gateway_passes_on_200():
    k = FakeKubectl(curl_results=['200'])
    h = Harness(k)
    logs = []
    assert h._smoke_test_gateway('http://gw.example', make_config(),
                                 log_callback=logs.append) is True
    assert any('smoke test passed' in l for l in logs)

    curl_calls = [c for c in k.calls if 'curl' in ' '.join(c[0])]
    assert len(curl_calls) == 1  # passed on first attempt, no retries
    cmd = ' '.join(curl_calls[0][0])
    assert '--max-time' in cmd  # total-time cap: hung gateway fails fast
    assert '-X POST' in cmd
    assert '/v1/completions' in cmd  # single-turn workload


def test_smoke_test_gateway_uses_chat_completions_for_multiturn():
    k = FakeKubectl(curl_results=['200'])
    h = Harness(k)
    assert h._smoke_test_gateway('http://gw.example', make_config(turns=2),
                                 log_callback=None) is True
    cmd = ' '.join(k.calls[0][0])
    assert '/v1/chat/completions' in cmd


def test_smoke_test_gateway_hang_fails_after_retries(monkeypatch):
    monkeypatch.setattr(guidellm_module.time, 'sleep', lambda s: None)
    k = FakeKubectl(curl_results=['000'] * 5)
    h = Harness(k)
    logs = []
    assert h._smoke_test_gateway('http://gw.example', make_config(),
                                 log_callback=logs.append) is False
    curl_calls = [c for c in k.calls if 'curl' in ' '.join(c[0])]
    assert len(curl_calls) == 3  # default attempts before giving up
    assert any('hung' in l for l in logs)


def test_smoke_test_gateway_curl_error_counts_as_failure(monkeypatch):
    monkeypatch.setattr(guidellm_module.time, 'sleep', lambda s: None)
    k = FakeKubectl(curl_results=['ERR'] * 5)  # curl exit 28 (timeout)
    h = Harness(k)
    assert h._smoke_test_gateway('http://gw.example', make_config(),
                                 log_callback=None) is False


# ── Detached guidellm execution ───────────────────────────────────────────

def test_guidellm_job_runs_detached_and_polls(monkeypatch):
    # The core regression: guidellm must NOT run under a long-lived
    # kubectl exec stream (websocket drops after ~30 min fake a failure).
    def _no_popen(*args, **kwargs):
        raise AssertionError('subprocess.Popen used — exec must be detached')
    monkeypatch.setattr(guidellm_module.subprocess, 'Popen', _no_popen)

    # Result extraction (parse_guidellm) goes through subprocess.run
    def _fake_run(cmd, **kwargs):
        return _CP(0, json.dumps({
            'ttft_ms': {'p50': 12.0},
            'request_totals': {'successful': 5},
        }))
    monkeypatch.setattr(guidellm_module.subprocess, 'run', _fake_run)
    monkeypatch.setattr(guidellm_module.time, 'sleep', lambda s: None)

    k = FakeKubectl(poll_results=['RUNNING', 'RUNNING', '0'])
    h = Harness(k)
    logs = []
    ok, out, metrics = h._run_guidellm_job(
        'http://gw.example', make_config(), log_callback=logs.append,
        collect_metrics=False)
    assert ok is True
    assert out  # extracted result file path
    assert any('launched detached' in l for l in logs)
    assert any('Guidellm completed' in l for l in logs)

    # First remote call must kill stale guidellm from previous attempts
    first_cmd = ' '.join(k.calls[0][0])
    assert 'pkill' in first_cmd
    assert 'guidellm[ ]run' in first_cmd

    # Script staged via stdin runs guidellm and records its exit code
    assert k.script and 'guidellm run' in k.script
    assert k.script.rstrip().endswith(
        'echo $? > /mnt/storage/tmp/guidellm-unit-run.exit')

    # Completion was detected via short-lived status polls
    poll_calls = [c for c in k.calls if 'echo RUNNING' in ' '.join(c[0])]
    assert len(poll_calls) == 3

    # Launch was detached (nohup) with a fresh log
    launch_calls = [c for c in k.calls if 'nohup' in ' '.join(c[0])]
    assert len(launch_calls) == 1
    assert 'rm -f' in ' '.join(launch_calls[0][0])


def test_guidellm_job_reports_nonzero_exit(monkeypatch):
    monkeypatch.setattr(guidellm_module.time, 'sleep', lambda s: None)
    k = FakeKubectl(poll_results=['RUNNING', '1'],
                    tail_stdout='ERROR: backend unreachable')
    h = Harness(k)
    logs = []
    ok, out, metrics = h._run_guidellm_job(
        'http://gw.example', make_config(), log_callback=logs.append,
        collect_metrics=False)
    assert ok is False
    assert out is None
    assert any('exited with code 1' in l for l in logs)
    assert any('backend unreachable' in l for l in logs)  # log tail surfaced


def test_guidellm_job_stops_and_kills_remote_on_stop_check(monkeypatch):
    monkeypatch.setattr(guidellm_module.time, 'sleep', lambda s: None)
    k = FakeKubectl(poll_results=['RUNNING'] * 50)
    h = Harness(k)
    logs = []

    ok, out, metrics = h._run_guidellm_job(
        'http://gw.example', make_config(), log_callback=logs.append,
        collect_metrics=False, stop_check=lambda: True)
    assert ok is False
    # Stop path must kill the remote process (bracket pattern, no self-match)
    kill_calls = [c for c in k.calls
                  if 'pkill' in ' '.join(c[0]) and 'nohup' not in ' '.join(c[0])]
    assert len(kill_calls) >= 2  # initial stale kill + stop kill
    assert any('Stopping guidellm' in l for l in logs)


def test_pkill_pattern_avoids_self_and_script_match():
    """The wrapper shell's own cmdline contains the pattern string, and the
    staged script path contains 'guidellm-run' — the bracket form must only
    match real 'guidellm run' processes."""
    import re
    pattern = 'guidellm[ ]run'
    assert re.search(pattern, 'python3 /usr/local/bin/guidellm run --backend x')
    assert not re.search(pattern, "pkill -f 'guidellm[ ]run' 2>/dev/null")
    assert not re.search(pattern, 'bash /mnt/storage/tmp/guidellm-run-unit-run.sh')


def test_runner_smokes_gateway_before_guidellm():
    """Step 5 must smoke-test the gateway before launching guidellm."""
    from core.orchestrator import runner
    src = inspect.getsource(runner)
    smoke_pos = src.index('self._smoke_test_gateway(')
    run_pos = src.index('self._run_guidellm_job(')
    assert smoke_pos < run_pos, 'smoke test must run before guidellm launch'
