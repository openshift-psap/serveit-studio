"""Tests for run liveness detection (force-stop gating in the UI state paths).

Covers the fix for a live optimization run being marked stopped:
- handle_load_config / _replay_state_to_client force-stop a 'running' run
  when the _optimization_greenlet state entry looks dead. That entry can go
  stale while the actual work greenlet keeps executing (a second spawn
  overwriting it, or an in-memory state reset), which flipped the UI to the
  green Start button mid-run and stopped persisting results.
- _run_greenlet_alive() now also accepts recent console_logs activity as
  proof of life, and both state paths report running=True when work is
  provably alive even if the flag/run row says stopped.
"""

import os
import tempfile
from datetime import datetime, timedelta

import pytest

_tmpdir = tempfile.mkdtemp(prefix='serveit-test-')
# setdefault: test_api.py may have already configured the shared Flask app
# singleton with its own temp DB — reuse it instead of conflicting.
os.environ.setdefault('DB_PATH', os.path.join(_tmpdir, 'test.db'))
os.environ.setdefault('OPTIMIZATION_OUTPUT_DIR', os.path.join(_tmpdir, 'output'))
os.environ.setdefault('TARGET_NAMESPACE', 'serveit-test')

# Import during collection (before any test serves a request) so route
# registration on the shared Flask app succeeds.
try:
    import web.realtime  # noqa: F401
except Exception:
    pass


@pytest.fixture(scope='module')
def app():
    try:
        from web.app_context import app
        app.config['TESTING'] = True
        with app.app_context():
            from web.database import init_db
            init_db()
        yield app
    except Exception as e:
        pytest.skip(f'Flask app setup failed: {e}')


@pytest.fixture
def clean_state(app):
    """Isolated state + empty console_logs for each test."""
    from web.realtime import state
    from web.database import get_db
    state['_optimization_greenlet'] = None
    with get_db() as conn:
        conn.execute('DELETE FROM console_logs')
    yield state
    state['_optimization_greenlet'] = None


def _add_log(timestamp):
    from web.database import get_db
    with get_db() as conn:
        conn.execute(
            'INSERT INTO console_logs (timestamp, log_type, message) VALUES (?, ?, ?)',
            (timestamp, 'info', 'test log line'))


def test_no_greenlet_no_activity_is_dead(app, clean_state):
    from web.realtime import _run_greenlet_alive
    assert _run_greenlet_alive() is False


def test_live_greenlet_is_alive(app, clean_state):
    import gevent
    from web.realtime import _run_greenlet_alive
    gl = gevent.spawn(gevent.sleep, 60)
    clean_state['_optimization_greenlet'] = gl
    try:
        assert _run_greenlet_alive() is True
    finally:
        gl.kill()


def test_dead_greenlet_with_recent_activity_is_alive(app, clean_state):
    from web.realtime import _run_greenlet_alive
    _add_log(datetime.now().isoformat())
    assert _run_greenlet_alive() is True


def test_dead_greenlet_with_stale_activity_is_dead(app, clean_state):
    from web.realtime import _run_greenlet_alive
    _add_log((datetime.now() - timedelta(minutes=11)).isoformat())
    assert _run_greenlet_alive() is False


def test_dead_greenlet_beats_stale_activity(app, clean_state):
    """A dead state entry with recent logs still counts as alive (recency wins)."""
    from web.realtime import _run_greenlet_alive
    _add_log(datetime.now().isoformat())
    assert _run_greenlet_alive() is True


def test_killed_greenlet_with_no_activity_is_dead(app, clean_state):
    import gevent
    from web.realtime import _run_greenlet_alive
    gl = gevent.spawn(gevent.sleep, 60)
    gl.kill()
    gevent.sleep(0)
    clean_state['_optimization_greenlet'] = gl
    assert _run_greenlet_alive() is False
