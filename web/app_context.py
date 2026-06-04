"""
Shared application context — the single source of truth for Flask app,
SocketIO, database access, and mutable global state.

Every web module imports from here. Never define app/socketio/state elsewhere.
"""

# IMPORTANT: gevent monkey patch must happen before this module is imported.
# server.py handles this before importing anything.

import os
import json
import sqlite3
import logging
from contextlib import contextmanager
from threading import RLock
from datetime import timedelta

from flask import Flask
from flask_socketio import SocketIO

logger = logging.getLogger(__name__)

# ── Path Constants ───────────────────────────────────────────────────────────
APP_PATH = os.environ.get('INFTUNE_PATH', '/opt/serveit')
STATE_DIR = '/tmp/infe_recipe_state'
STATE_FILE = os.path.join(STATE_DIR, 'state.json')
DB_PATH = os.environ.get('DB_PATH', '/mnt/storage/serveit.db')
OPTIMIZATION_OUTPUT_DIR = os.environ.get('OPTIMIZATION_OUTPUT_DIR', '/mnt/storage/optimization-runs')

if not os.environ.get('HF_HOME'):
    os.environ['HF_HOME'] = os.path.join(os.path.dirname(DB_PATH), '.cache', 'huggingface')
TARGET_NAMESPACE = os.environ.get('TARGET_NAMESPACE', 'serveit')


# ── Mutable Global State (shared dict so mutations are visible everywhere) ──
state = {
    'optimization_running': False,
    'current_config': {},
    'current_test_plan': None,
    'active_ui_session': None,
}
state_lock = RLock()
_session_lock = RLock()
_SESSION_TIMEOUT_SECS = 15


# ── Flask & SocketIO ────────────────────────────────────────────────────────
def _get_secret_key():
    if os.environ.get('SECRET_KEY'):
        return os.environ['SECRET_KEY']
    key_file = os.path.join(os.path.dirname(DB_PATH), '.flask_secret_key')
    if os.path.exists(key_file):
        return open(key_file).read().strip()
    key = os.urandom(32).hex()
    os.makedirs(os.path.dirname(key_file), exist_ok=True)
    with open(key_file, 'w') as f:
        f.write(key)
    return key


app = Flask(__name__, template_folder='templates')
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.config['SECRET_KEY'] = _get_secret_key()
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=24)

socketio = SocketIO(app,
                    async_mode='gevent',
                    cors_allowed_origins="*",
                    logger=True,
                    engineio_logger=True,
                    ping_timeout=60,
                    ping_interval=25)


# ── Database ────────────────────────────────────────────────────────────────
@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
