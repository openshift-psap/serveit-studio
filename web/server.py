"""
In-S8 llm-d optimizer Web Application
Main Flask application for In-S8 optimization benchmarking tool.
Uses gevent instead of deprecated eventlet.
"""

# IMPORTANT: Monkey patch MUST happen BEFORE any other imports
from gevent import monkey
monkey.patch_all()

import os
import sys
import json
import logging
import sqlite3
import subprocess
from contextlib import contextmanager
from datetime import datetime, timedelta
from threading import RLock
from typing import Optional, Dict, List
from flask import Flask, render_template, jsonify, request, Response, redirect, url_for, session
from flask_socketio import SocketIO, emit
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from gevent import spawn

logger = logging.getLogger(__name__)

# Add parent directory to path for core module imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Import core modules
from core import (
    SystemScanner,
    TestResult,
    DeploymentManager,
    TestConfig,
    TestOrchestrator
)
from core.web_deployer import DeploymentOrchestrator
from core.k8s_utils import KubectlRunner

# --- Configuration & Path Constants ---
APP_PATH = os.environ.get('IN_S8_PATH', '/opt/in-s8')
STATE_DIR = '/tmp/in_s8_state'
STATE_FILE = os.path.join(STATE_DIR, 'state.json')
DB_PATH = os.environ.get('DB_PATH', '/mnt/storage/in-s8.db')
OPTIMIZATION_OUTPUT_DIR = os.environ.get('OPTIMIZATION_OUTPUT_DIR', '/mnt/storage/optimization-runs')

if not os.environ.get('HF_HOME'):
    os.environ['HF_HOME'] = os.path.join(os.path.dirname(DB_PATH), '.cache', 'huggingface')
TARGET_NAMESPACE = os.environ.get('TARGET_NAMESPACE', 'llm-d')

# Global state
OPTIMIZATION_RUNNING = False
CURRENT_CONFIG = {}
CURRENT_TEST_PLAN = None  # Store test plan for deployment
state_lock = RLock()

# Active UI session guard — only one controlling session at a time
_active_ui_session = None  # { 'sid': str, 'username': str, 'connected_at': str }

# Initialize Flask application and SocketIO with gevent
app = Flask(__name__, template_folder='templates')
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

app.config['SECRET_KEY'] = _get_secret_key()
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=24)

# --- Session Auth with rate limiting (credentials stored in DB) ---

_login_attempts = {}  # ip -> (count, first_attempt_time)
_LOGIN_MAX_ATTEMPTS = 5
_LOGIN_WINDOW_SECONDS = 60

def _has_any_users():
    conn = sqlite3.connect(DB_PATH)
    conn.execute('CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, created_at TEXT NOT NULL)')
    count = conn.execute('SELECT COUNT(*) FROM users').fetchone()[0]
    conn.close()
    return count > 0

def _create_user(username, password):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        'INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)',
        (username, generate_password_hash(password), datetime.now().isoformat())
    )
    conn.commit()
    conn.close()

def _check_auth(username, password):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute('SELECT password_hash FROM users WHERE username = ?', (username,)).fetchone()
    conn.close()
    return row is not None and check_password_hash(row[0], password)

def _is_rate_limited(ip):
    now = datetime.now().timestamp()
    if ip in _login_attempts:
        count, first_time = _login_attempts[ip]
        if now - first_time > _LOGIN_WINDOW_SECONDS:
            del _login_attempts[ip]
            return False
        return count >= _LOGIN_MAX_ATTEMPTS
    return False

def _record_failed_attempt(ip):
    now = datetime.now().timestamp()
    if ip in _login_attempts:
        count, first_time = _login_attempts[ip]
        if now - first_time > _LOGIN_WINDOW_SECONDS:
            _login_attempts[ip] = (1, now)
        else:
            _login_attempts[ip] = (count + 1, first_time)
    else:
        _login_attempts[ip] = (1, now)

@app.route('/setup', methods=['GET', 'POST'])
def setup():
    if _has_any_users():
        return redirect(url_for('login'))
    error = None
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        confirm = request.form.get('confirm_password', '')
        if len(username) < 3:
            error = 'Username must be at least 3 characters.'
        elif len(password) < 6:
            error = 'Password must be at least 6 characters.'
        elif password != confirm:
            error = 'Passwords do not match.'
        else:
            _create_user(username, password)
            session.permanent = True
            app.permanent_session_lifetime = timedelta(hours=24)
            session['user'] = username
            session['_created'] = datetime.now().timestamp()
            return redirect(url_for('index'))
    return render_template('setup.html', error=error)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if not _has_any_users():
        return redirect(url_for('setup'))
    error = None
    if request.method == 'POST':
        client_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
        if _is_rate_limited(client_ip):
            error = 'Too many login attempts. Try again in 60 seconds.'
        else:
            username = request.form.get('username', '')
            password = request.form.get('password', '')
            if _check_auth(username, password):
                remember = request.form.get('remember')
                session.permanent = True
                if remember:
                    app.permanent_session_lifetime = timedelta(days=30)
                else:
                    app.permanent_session_lifetime = timedelta(hours=24)
                session['user'] = username
                session['_created'] = datetime.now().timestamp()
                _login_attempts.pop(client_ip, None)
                return redirect(url_for('index'))
            _record_failed_attempt(client_ip)
            error = 'Invalid username or password.'
    return render_template('login.html', error=error)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.before_request
def require_auth():
    if request.endpoint in ('login', 'setup', 'static'):
        return
    if not _has_any_users():
        return redirect(url_for('setup'))
    if 'user' not in session:
        return redirect(url_for('login'))
socketio = SocketIO(app,
                    async_mode='gevent',
                    cors_allowed_origins="*",
                    logger=True,
                    engineio_logger=True,
                    ping_timeout=60,
                    ping_interval=25)

# --- Database Functions ---

def init_db():
    """Initialize SQLite database for storing optimization results."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Create optimization_runs table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS optimization_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_name TEXT UNIQUE NOT NULL,
            model TEXT NOT NULL,
            isl INTEGER NOT NULL,
            osl INTEGER NOT NULL,
            num_users INTEGER NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            completed_at TEXT,
            optimal_config TEXT,
            notes TEXT,
            current_test_index INTEGER DEFAULT 0,
            last_deployed_config TEXT,
            deployment_status TEXT,
            pods_deployed TEXT
        )
    ''')

    # Create test_configurations table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS test_configurations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            config_name TEXT NOT NULL,
            prefill_pods INTEGER NOT NULL,
            decode_pods INTEGER NOT NULL,
            tensor_parallelism INTEGER NOT NULL,
            status TEXT NOT NULL,
            ttft_p50 REAL,
            ttft_p90 REAL,
            ttft_p95 REAL,
            ttft_p99 REAL,
            itl_p50 REAL,
            itl_p90 REAL,
            itl_p95 REAL,
            itl_p99 REAL,
            throughput_p50 REAL,
            throughput_p90 REAL,
            throughput_p95 REAL,
            throughput_p99 REAL,
            gpu_utilization REAL,
            kv_cache_usage REAL,
            started_at TEXT,
            completed_at TEXT,
            metrics_json TEXT,
            FOREIGN KEY (run_id) REFERENCES optimization_runs (id),
            UNIQUE(run_id, config_name)
        )
    ''')

    # Create console_logs table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS console_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            log_type TEXT NOT NULL,
            message TEXT NOT NULL,
            run_id INTEGER,
            job_name TEXT,
            session_id TEXT,
            FOREIGN KEY (run_id) REFERENCES optimization_runs (id)
        )
    ''')

    # Create index for faster log queries
    cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_logs_timestamp ON console_logs(timestamp DESC)
    ''')
    cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_logs_run_id ON console_logs(run_id)
    ''')
    cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_logs_job_name ON console_logs(job_name)
    ''')

    # Create deployment_templates table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS deployment_templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            model_name TEXT NOT NULL,
            architecture TEXT NOT NULL,
            role TEXT,

            -- Deployment settings
            replicas INTEGER,
            tensor_parallelism INTEGER,
            image TEXT,
            pvc_name TEXT,
            namespace TEXT DEFAULT 'llm-d',

            -- vLLM serve parameters
            port INTEGER DEFAULT 8000,
            trust_remote_code INTEGER DEFAULT 1,
            disable_log_requests INTEGER DEFAULT 1,
            disable_uvicorn_access_log INTEGER DEFAULT 1,
            max_model_len INTEGER,
            gpu_memory_utilization REAL DEFAULT 0.95,

            -- PD-specific vLLM parameters
            max_num_batched_tokens INTEGER,

            -- NCCL/Network settings
            nccl_ib_hca TEXT DEFAULT 'mlx',

            -- Resource requirements
            gpus_per_pod INTEGER,
            memory_limit TEXT DEFAULT '512Gi',
            memory_request TEXT DEFAULT '512Gi',
            cpu_request TEXT DEFAULT '32',
            cpu_limit TEXT,

            -- Workload parameters (for reference)
            isl INTEGER,
            osl INTEGER,

            -- Metadata
            created_at TEXT NOT NULL,
            updated_at TEXT,
            is_active INTEGER DEFAULT 1,

            UNIQUE(model_name, architecture, role, is_active)
        )
    ''')

    # Create index for faster template queries
    cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_templates_model ON deployment_templates(model_name, is_active)
    ''')
    cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_templates_arch ON deployment_templates(architecture, is_active)
    ''')

    # Create ui_session_state table for persistent UI configuration
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS ui_session_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            current_step INTEGER NOT NULL DEFAULT 1,
            config_json TEXT NOT NULL,
            optimization_running INTEGER DEFAULT 0,
            updated_at TEXT NOT NULL
        )
    ''')

    # Create hardware_scans table for storing hardware scan results
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS hardware_scans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_timestamp TEXT NOT NULL,
            cloud_provider TEXT,
            node_count INTEGER,
            gpu_node_count INTEGER,
            total_gpus INTEGER,
            gpu_vendor TEXT,
            gpu_model TEXT,
            gpu_memory_per_gpu_mb INTEGER,
            total_gpu_memory_gb INTEGER,
            max_gpus_per_node INTEGER,
            total_cpu_cores INTEGER,
            total_memory_gb INTEGER,
            cpu_model TEXT,
            host_model TEXT,
            has_rdma INTEGER,
            rdma_capable_nodes INTEGER,
            total_nics INTEGER,
            nodes_json TEXT,
            nics_json TEXT
        )
    ''')

    # Create index for faster hardware scan queries
    cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_hw_scans_timestamp ON hardware_scans(scan_timestamp DESC)
    ''')

    # Initialize with default state if empty
    cursor.execute('SELECT COUNT(*) FROM ui_session_state')
    if cursor.fetchone()[0] == 0:
        cursor.execute('''
            INSERT INTO ui_session_state (id, current_step, config_json, optimization_running, updated_at)
            VALUES (1, 1, '{}', 0, ?)
        ''', (datetime.now().isoformat(),))

    # Migrations: add columns to existing tables
    try:
        cursor.execute('ALTER TABLE optimization_runs ADD COLUMN goal TEXT')
    except sqlite3.OperationalError:
        pass  # Column already exists

    try:
        cursor.execute('ALTER TABLE optimization_runs ADD COLUMN test_duration INTEGER DEFAULT 300')
    except sqlite3.OperationalError:
        pass  # Column already exists

    try:
        cursor.execute('ALTER TABLE optimization_runs ADD COLUMN max_gpus INTEGER DEFAULT 16')
    except sqlite3.OperationalError:
        pass  # Column already exists

    try:
        cursor.execute('ALTER TABLE optimization_runs ADD COLUMN use_achievable_qps INTEGER DEFAULT 0')
    except sqlite3.OperationalError:
        pass  # Column already exists

    try:
        cursor.execute('ALTER TABLE test_configurations ADD COLUMN manifests_yaml TEXT')
    except sqlite3.OperationalError:
        pass  # Column already exists

    try:
        cursor.execute('ALTER TABLE optimization_runs ADD COLUMN constraint_notes TEXT')
    except sqlite3.OperationalError:
        pass  # Column already exists

    try:
        cursor.execute('ALTER TABLE optimization_runs ADD COLUMN isl_stdev INTEGER')
    except sqlite3.OperationalError:
        pass

    try:
        cursor.execute('ALTER TABLE optimization_runs ADD COLUMN osl_stdev INTEGER')
    except sqlite3.OperationalError:
        pass

    try:
        cursor.execute('ALTER TABLE optimization_runs ADD COLUMN turns INTEGER DEFAULT 1')
    except sqlite3.OperationalError:
        pass

    try:
        cursor.execute('ALTER TABLE optimization_runs ADD COLUMN config_json TEXT')
    except sqlite3.OperationalError:
        pass

    try:
        cursor.execute('ALTER TABLE optimization_runs ADD COLUMN latency_constraint_enabled INTEGER DEFAULT 0')
    except sqlite3.OperationalError:
        pass

    try:
        cursor.execute('ALTER TABLE optimization_runs ADD COLUMN latency_constraint_ms INTEGER DEFAULT 500')
    except sqlite3.OperationalError:
        pass

    try:
        cursor.execute('ALTER TABLE optimization_runs ADD COLUMN latency_constraint_percentile TEXT DEFAULT "p90"')
    except sqlite3.OperationalError:
        pass

    try:
        cursor.execute('ALTER TABLE test_configurations ADD COLUMN architecture TEXT')
    except sqlite3.OperationalError:
        pass

    try:
        cursor.execute('ALTER TABLE test_configurations ADD COLUMN decode_tp INTEGER')
    except sqlite3.OperationalError:
        pass

    try:
        cursor.execute('ALTER TABLE test_configurations ADD COLUMN guidellm_raw_json TEXT')
    except sqlite3.OperationalError:
        pass

    # Create Optuna tables if they don't exist
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS optuna_trials (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            optimization_step TEXT NOT NULL,
            trial_number INTEGER NOT NULL,
            trial_params_json TEXT NOT NULL,
            test_id TEXT NOT NULL,
            guidellm_success INTEGER,
            ttft_ms REAL,
            throughput REAL,
            target_percentile TEXT,
            constraint_target_ms REAL,
            meets_constraint INTEGER,
            objective_value REAL,
            trial_state TEXT,
            created_at TEXT NOT NULL,
            metrics_json TEXT,
            FOREIGN KEY (run_id) REFERENCES optimization_runs (id),
            UNIQUE(run_id, optimization_step, trial_number)
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS optuna_studies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            optimization_step TEXT NOT NULL,
            constraint_config_json TEXT,
            search_range_json TEXT,
            total_trials INTEGER,
            feasible_trials INTEGER,
            best_trial_number INTEGER,
            best_params_json TEXT,
            best_throughput REAL,
            best_latency_ms REAL,
            best_config_source TEXT,
            study_status TEXT,
            created_at TEXT NOT NULL,
            completed_at TEXT,
            FOREIGN KEY (run_id) REFERENCES optimization_runs (id),
            UNIQUE(run_id, optimization_step)
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS latency_search_trials (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            architecture TEXT NOT NULL,
            trial_number INTEGER NOT NULL,
            search_phase TEXT NOT NULL,
            concurrency INTEGER NOT NULL,
            test_id TEXT NOT NULL,
            guidellm_success INTEGER,
            meets_sla INTEGER,
            ttft_p50 REAL,
            ttft_p90 REAL,
            ttft_p95 REAL,
            ttft_p99 REAL,
            itl_p50 REAL,
            itl_p90 REAL,
            itl_p95 REAL,
            itl_p99 REAL,
            throughput_p50 REAL,
            throughput_p90 REAL,
            throughput_p95 REAL,
            throughput_p99 REAL,
            target_ms REAL,
            target_percentile TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (run_id) REFERENCES optimization_runs (id),
            UNIQUE(run_id, architecture, trial_number)
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    ''')

    conn.commit()
    conn.close()
    print(f"✓ Database initialized at {DB_PATH}")

@contextmanager
def get_db():
    """Context manager for database connections with automatic commit/rollback."""
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

# --- State Management ---

def save_state():
    """Save current application state to JSON file."""
    with state_lock:
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            with open(STATE_FILE, 'w') as f:
                json.dump({
                    'running': OPTIMIZATION_RUNNING,
                    'config': CURRENT_CONFIG
                }, f, indent=2)
        except Exception as e:
            print(f"Error saving state: {e}")

def load_state():
    """Load application state from JSON file and database."""
    global OPTIMIZATION_RUNNING, CURRENT_CONFIG, CURRENT_TEST_PLAN
    with state_lock:
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, 'r') as f:
                    state = json.load(f)
                OPTIMIZATION_RUNNING = state.get('running', False)
                CURRENT_CONFIG = state.get('config', {})

                # Reset running state on app restart
                if OPTIMIZATION_RUNNING:
                    OPTIMIZATION_RUNNING = False
                    save_state()
            except Exception as e:
                print(f"Error loading state: {e}")
                OPTIMIZATION_RUNNING = False
                CURRENT_CONFIG = get_default_config()
        else:
            CURRENT_CONFIG = get_default_config()
            save_state()

        # Restore test plan from database
        try:
            with get_db() as conn:
                row = conn.execute('SELECT config_json FROM ui_session_state WHERE id = 1').fetchone()
                if row and row['config_json']:
                    config = json.loads(row['config_json'])
                    if 'test_plan' in config and config['test_plan']:
                        # Restore test plan object from saved data
                        from core import TestPlan, TestConfiguration, Architecture
                        from core.test_planner import ModelRequirements

                        tp_data = config['test_plan']
                        if tp_data.get('can_proceed'):
                            # Reconstruct ModelRequirements
                            req_data = tp_data.get('model_requirements', {})
                            model_req = ModelRequirements(
                                model_size_b=req_data.get('model_size_b', 0),
                                dtype=req_data.get('dtype', 'fp16'),
                                model_weights_gb=req_data.get('model_weights_gb', 0),
                                kv_cache_gb=req_data.get('kv_cache_gb', 0),
                                activations_gb=req_data.get('activations_gb', 0),
                                cuda_overhead_gb=req_data.get('cuda_overhead_gb', 0),
                                total_vram_per_gpu_gb=req_data.get('total_vram_per_gpu_gb', 0)
                            )

                            # Reconstruct TestConfiguration objects
                            tests = []
                            for test_data in tp_data.get('tests', []):
                                test = TestConfiguration(
                                    architecture=Architecture(test_data['architecture']),
                                    gpus_required=test_data['gpus_required'],
                                    tp=test_data['tp'],
                                    prefill_pods=test_data.get('prefill_pods', 0),
                                    decode_pods=test_data.get('decode_pods', 0),
                                    ep_pods=test_data.get('ep_pods', 0),
                                    description=test_data['description']
                                )
                                tests.append(test)

                            # Reconstruct TestPlan
                            test_plan = TestPlan(
                                model_name=tp_data.get('model_name', ''),
                                optimization_goal=tp_data.get('optimization_goal', 'balanced'),
                                model_requirements=model_req,
                                tests=tests,
                                can_proceed=True,
                                error_message=None
                            )

                            CURRENT_TEST_PLAN = test_plan
                            print(f"✅ Restored test plan from database ({len(tests)} tests)")
        except Exception as e:
            print(f"Warning: Failed to restore test plan from database: {e}")
            CURRENT_TEST_PLAN = None

def get_default_config():
    """Get default configuration from environment variables."""
    return {
        'model': os.environ.get('MODEL', 'RedHatAI/Qwen3-235B-A22B-FP8-dynamic'),
        'inference_gateway': os.environ.get('INFERENCE_GATEWAY', f'http://infra-ep-inference-gateway-istio.{TARGET_NAMESPACE}.svc.cluster.local:80'),
        'isl': int(os.environ.get('ISL', '3000')),
        'osl': int(os.environ.get('OSL', '100')),
        'num_users': int(os.environ.get('NUM_USERS', '100')),
        'prefill_decode_ratios': os.environ.get('PREFILL_DECODE_RATIOS', '1:1,1:2,1:4,2:1'),
        'tp_values': os.environ.get('TP_VALUES', '1,2,4,8'),
        'optimization_metric': os.environ.get('OPTIMIZATION_METRIC', 'balanced'),
        'max_test_duration': int(os.environ.get('MAX_TEST_DURATION', '300')),
    }

# --- Deployment Template Functions ---

def save_deployment_template(
    model_name: str,
    architecture: str,
    role: Optional[str],
    tensor_parallelism: int,
    replicas: int = 1,
    max_model_len: int = 8192,
    gpu_memory_utilization: float = 0.95,
    image: str = 'ghcr.io/llm-d/llm-d-cuda:v0.5.1',
    pvc_name: str = 'in-s8-model-cache',
    nccl_ib_hca: str = 'mlx',
    isl: int = 2000,
    osl: int = 100,
    max_num_batched_tokens: Optional[int] = None,
    gpus_per_pod: Optional[int] = None,
    memory_limit: str = '512Gi',
    cpu_request: str = '32',
    namespace: Optional[str] = None
) -> int:
    """
    Save deployment template to database.

    Args:
        model_name: HuggingFace model name
        architecture: 'aggregated', 'pd', or 'ep'
        role: None for aggregated/ep, 'prefill' or 'decode' for PD
        tensor_parallelism: TP value
        replicas: Number of replica pods
        max_model_len: Max sequence length for vLLM
        gpu_memory_utilization: GPU memory utilization (0.0-1.0)
        image: Container image
        pvc_name: PVC name for model cache
        nccl_ib_hca: NCCL IB HCA prefix
        isl: Input sequence length
        osl: Output sequence length
        max_num_batched_tokens: PD decode-specific parameter
        gpus_per_pod: GPUs per pod (defaults to tensor_parallelism)
        memory_limit: Memory limit per pod
        cpu_request: CPU request per pod
        namespace: Kubernetes namespace

    Returns:
        Template ID
    """
    if gpus_per_pod is None:
        gpus_per_pod = tensor_parallelism
    if namespace is None:
        namespace = TARGET_NAMESPACE

    with get_db() as conn:
        cursor = conn.cursor()

        try:
            # Delete old templates for this model+architecture+role
            # (Deleting instead of deactivating to avoid UNIQUE constraint issues)
            if role is None:
                cursor.execute('''
                    DELETE FROM deployment_templates
                    WHERE model_name = ? AND architecture = ? AND role IS NULL
                ''', (model_name, architecture))
            else:
                cursor.execute('''
                    DELETE FROM deployment_templates
                    WHERE model_name = ? AND architecture = ? AND role = ?
                ''', (model_name, architecture, role))

            # Insert new template
            cursor.execute('''
                INSERT INTO deployment_templates (
                model_name, architecture, role,
                replicas, tensor_parallelism, image, pvc_name, namespace,
                port, trust_remote_code, disable_log_requests, disable_uvicorn_access_log,
                max_model_len, gpu_memory_utilization,
                max_num_batched_tokens,
                nccl_ib_hca,
                gpus_per_pod, memory_limit, memory_request, cpu_request,
                isl, osl,
                created_at, is_active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            model_name, architecture, role,
            replicas, tensor_parallelism, image, pvc_name, namespace,
            8000, 1, 1, 1,
            max_model_len, gpu_memory_utilization,
            max_num_batched_tokens,
            nccl_ib_hca,
            gpus_per_pod, memory_limit, memory_limit, cpu_request,
            isl, osl,
            datetime.now().isoformat(), 1
        ))

            return cursor.lastrowid

        except sqlite3.IntegrityError as _e:
            # UNIQUE constraint failed - template already exists
            # This can happen on double-clicks or race conditions
            # Return the existing active template ID
            if role is None:
                cursor.execute('''
                    SELECT id FROM deployment_templates
                    WHERE model_name = ? AND architecture = ? AND role IS NULL AND is_active = 1
                    ORDER BY created_at DESC LIMIT 1
                ''', (model_name, architecture))
            else:
                cursor.execute('''
                    SELECT id FROM deployment_templates
                    WHERE model_name = ? AND architecture = ? AND role = ? AND is_active = 1
                    ORDER BY created_at DESC LIMIT 1
                ''', (model_name, architecture, role))

            result = cursor.fetchone()
            if result:
                return result[0]
            else:
                # Re-raise if it's a different integrity error
                raise


def get_deployment_template(model_name: str, architecture: str, role: Optional[str] = None) -> Optional[Dict]:
    """
    Get the latest active deployment template from database.

    Args:
        model_name: HuggingFace model name
        architecture: 'aggregated', 'pd', or 'ep'
        role: None for aggregated/ep, 'prefill' or 'decode' for PD

    Returns:
        Template dict or None if not found
    """
    with get_db() as conn:
        if role is None:
            result = conn.execute('''
                SELECT * FROM deployment_templates
                WHERE model_name = ? AND architecture = ? AND role IS NULL AND is_active = 1
                ORDER BY created_at DESC
                LIMIT 1
            ''', (model_name, architecture)).fetchone()
        else:
            result = conn.execute('''
                SELECT * FROM deployment_templates
                WHERE model_name = ? AND architecture = ? AND role = ? AND is_active = 1
                ORDER BY created_at DESC
                LIMIT 1
            ''', (model_name, architecture, role)).fetchone()

        if result:
            return dict(result)
        return None


def save_test_state(run_id: int, test_index: int, test_config_json: str, pods_deployed: List[str], deployment_status: str = 'deploying'):
    """
    Save current test state to database for crash recovery.

    Args:
        run_id: Optimization run ID
        test_index: Index of current test in the plan
        test_config_json: JSON serialized TestConfig
        pods_deployed: List of pod names that were deployed
        deployment_status: 'deploying', 'running', 'crashed', 'completed'
    """
    with get_db() as conn:
        conn.execute('''
            UPDATE optimization_runs
            SET current_test_index = ?,
                last_deployed_config = ?,
                deployment_status = ?,
                pods_deployed = ?
            WHERE id = ?
        ''', (test_index, test_config_json, deployment_status, json.dumps(pods_deployed), run_id))


def get_resumable_run() -> Optional[Dict]:
    """
    Check if there's a run that can be resumed.

    Looks for the most recent run that has completed tests but is not
    fully done (missing step 8, or was interrupted).

    Returns:
        Dict with run info and test counts, or None
    """
    with get_db() as conn:
        # Find most recent run with any completed tests
        result = conn.execute('''
            SELECT r.*, COUNT(tc.id) as completed_tests
            FROM optimization_runs r
            LEFT JOIN test_configurations tc ON tc.run_id = r.id AND tc.status = 'completed'
            WHERE r.status IN ('running', 'completed', 'failed', 'stopped', 'error_stopped')
            GROUP BY r.id
            HAVING completed_tests > 0
            ORDER BY r.created_at DESC
            LIMIT 1
        ''').fetchone()

        if result:
            return dict(result)
        return None


def check_pods_exist(namespace: str, pod_names: List[str]) -> Dict[str, bool]:
    """
    Check which pods still exist in the cluster.

    Args:
        namespace: Kubernetes namespace
        pod_names: List of pod names to check

    Returns:
        Dict mapping pod_name -> exists (bool)
    """
    from core.k8s_utils import KubectlRunner

    kubectl = KubectlRunner(namespace=namespace)
    pod_status = {}

    for pod_name in pod_names:
        try:
            result = kubectl.run(['get', 'pod', pod_name, '-n', namespace], check=False)
            pod_status[pod_name] = result.returncode == 0
        except Exception:
            pod_status[pod_name] = False

    return pod_status

# --- Flask Routes ---

@app.route('/')
def index():
    """Serve the main UI page."""
    return render_template('index.html')

@app.route('/api/status')
def get_status():
    """Get current optimization status."""
    with state_lock:
        return jsonify({
            'running': OPTIMIZATION_RUNNING,
            'config': CURRENT_CONFIG
        })

@app.route('/api/config', methods=['GET', 'POST'])
def handle_config():
    """Get or update configuration."""
    global CURRENT_CONFIG

    if request.method == 'POST':
        with state_lock:
            CURRENT_CONFIG = request.json
            save_state()
        return jsonify({'success': True, 'config': CURRENT_CONFIG})
    else:
        with state_lock:
            return jsonify(CURRENT_CONFIG)

@app.route('/api/stop_optimization', methods=['POST'])
def api_stop_optimization():
    """Stop the running optimization (REST endpoint)."""
    global OPTIMIZATION_RUNNING

    with state_lock:
        if not OPTIMIZATION_RUNNING:
            return jsonify({'success': False, 'error': 'No optimization running'}), 400

        OPTIMIZATION_RUNNING = False
        save_state()

    # Notify all connected clients
    socketio.emit('status_update', {'running': False, 'message': 'Optimization stopped'})
    socketio.emit('console_log', {'type': 'warning', 'message': '🛑 Optimization stopped by user'})

    return jsonify({'success': True, 'message': 'Optimization stopped'})

@app.route('/api/clear_console', methods=['POST'])
def api_clear_console():
    """Clear console logs from database."""
    try:
        with get_db() as conn:
            conn.execute('DELETE FROM console_logs')
            conn.commit()

        # Notify all connected clients to clear their console
        socketio.emit('clear_console', {})

        return jsonify({'success': True, 'message': 'Console logs cleared'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/runs')
def get_runs():
    """Get all optimization runs from database."""
    try:
        with get_db() as conn:
            runs = conn.execute('SELECT * FROM optimization_runs ORDER BY created_at DESC').fetchall()
            return jsonify([dict(run) for run in runs])
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/runs_for_resume')
def get_runs_for_resume():
    """Get all optimization runs with step-level progress for resume overlay."""
    try:
        with get_db() as conn:
            runs = conn.execute('''
                SELECT r.id, r.run_name, r.model, r.isl, r.osl, r.num_users,
                       r.status, r.created_at, r.goal, r.max_gpus,
                       r.test_duration, r.notes, r.isl_stdev, r.osl_stdev,
                       r.turns, r.latency_constraint_enabled,
                       r.latency_constraint_ms, r.latency_constraint_percentile,
                       r.config_json,
                       r.workload_mode, r.dataset_source, r.dataset_column,
                       r.dataset_max_output, r.rate_type,
                       COUNT(tc.id) as completed_tests
                FROM optimization_runs r
                LEFT JOIN test_configurations tc ON tc.run_id = r.id AND tc.status = 'completed'
                GROUP BY r.id
                ORDER BY r.created_at DESC
            ''').fetchall()

            result = []
            for run in runs:
                run_dict = dict(run)
                run_id = run_dict['id']

                # Determine which steps have completed tests
                steps = conn.execute('''
                    SELECT DISTINCT
                        CASE
                            WHEN config_name LIKE 'step2-%' THEN 2
                            WHEN config_name LIKE 'step3-%' THEN 3
                            WHEN config_name LIKE 'step7-%' THEN 7
                            WHEN config_name LIKE 'step6-%' THEN 6
                            WHEN config_name LIKE 'step8-%' THEN 8
                            WHEN config_name LIKE 'step9-%' THEN 9
                            ELSE 0
                        END as step_num
                    FROM test_configurations
                    WHERE run_id = ? AND status = 'completed'
                ''', (run_id,)).fetchall()

                completed_steps = sorted([s['step_num'] for s in steps if s['step_num'] > 0])
                run_dict['completed_steps'] = completed_steps
                run_dict['last_step'] = max(completed_steps) if completed_steps else 0
                result.append(run_dict)

            return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/delete_run/<int:run_id>', methods=['DELETE'])
def delete_run(run_id):
    """Delete an optimization run and all its test results."""
    try:
        with get_db() as conn:
            status = conn.execute(
                'SELECT status FROM optimization_runs WHERE id = ?', (run_id,)
            ).fetchone()
            if not status:
                return jsonify({'success': False, 'error': f'Run #{run_id} not found'}), 404
            if status['status'] == 'running':
                return jsonify({'success': False, 'error': f'Run #{run_id} is currently running — stop it first'}), 409
            deleted_tests = conn.execute(
                'DELETE FROM test_configurations WHERE run_id = ?', (run_id,)
            ).rowcount
            conn.execute(
                'DELETE FROM console_logs WHERE run_id = ?', (run_id,)
            )
            conn.execute(
                'DELETE FROM optimization_runs WHERE id = ?', (run_id,)
            )
        return jsonify({'success': True, 'deleted_tests': deleted_tests})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/restart_run/<int:run_id>', methods=['POST'])
def restart_run(run_id):
    """Clear all test results for a run and reset it so it can be re-run from the beginning."""
    try:
        with get_db() as conn:
            row = conn.execute(
                'SELECT status FROM optimization_runs WHERE id = ?', (run_id,)
            ).fetchone()
            if not row:
                return jsonify({'success': False, 'error': f'Run #{run_id} not found'}), 404
            if row['status'] == 'running':
                return jsonify({'success': False, 'error': f'Run #{run_id} is currently running — stop it first'}), 409
            deleted_tests = conn.execute(
                'DELETE FROM test_configurations WHERE run_id = ?', (run_id,)
            ).rowcount
            conn.execute(
                'DELETE FROM console_logs WHERE run_id = ?', (run_id,)
            )
            conn.execute('''
                UPDATE optimization_runs
                SET status = 'running', completed_at = NULL, optimal_config = NULL,
                    constraint_notes = NULL, current_test_index = 0,
                    last_deployed_config = NULL, deployment_status = NULL, pods_deployed = NULL
                WHERE id = ?
            ''', (run_id,))
        return jsonify({'success': True, 'deleted_tests': deleted_tests})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/resumable_run')
def get_resumable_run_api():
    """Check if there's a resumable run."""
    try:
        run = get_resumable_run()
        if run:
            return jsonify({
                'resumable': True,
                'run': {
                    'id': run['id'],
                    'run_name': run['run_name'],
                    'model': run.get('model'),
                    'status': run['status'],
                },
                'completed_tests': run.get('completed_tests', 0),
            })
        else:
            return jsonify({'resumable': False})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/runs/<int:run_id>/pod_errors')
def get_run_pod_errors(run_id):
    """Get pod error logs for a specific run."""
    try:
        with get_db() as conn:
            errors = conn.execute(
                'SELECT * FROM pod_error_logs WHERE run_id = ? ORDER BY created_at DESC',
                (run_id,)
            ).fetchall()
            return jsonify([dict(e) for e in errors])
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/runs/<int:run_id>/configurations')
def get_run_configurations(run_id):
    """Get all test configurations for a specific run."""
    try:
        with get_db() as conn:
            configs = conn.execute(
                'SELECT * FROM test_configurations WHERE run_id = ? ORDER BY id',
                (run_id,)
            ).fetchall()
            return jsonify([dict(config) for config in configs])
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/runs/<int:run_id>/charts')
def get_run_charts(run_id):
    """Get chart data, summary stats, and recommendation for an optimization run."""
    try:
        from core.report_data import ReportDataLoader
        from core.report_analysis import ReportAnalyzer

        analyzer = ReportAnalyzer()
        with ReportDataLoader(DB_PATH) as loader:
            data = analyzer.build_full_report_data(run_id, loader)
            if not data:
                return jsonify({'error': 'No results found for this run'}), 404
            return jsonify(data)

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/api/models')
def get_models():
    """Get available Red Hat AI models from JSON file."""
    try:
        models_file = os.path.join(os.path.dirname(__file__), 'data', 'models.json')
        with open(models_file, 'r') as f:
            models = json.load(f)
        return jsonify(models)
    except FileNotFoundError:
        return jsonify({'error': 'Models file not found'}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/deployment_templates')
def get_deployment_templates():
    """Get all active deployment templates."""
    try:
        model_name = request.args.get('model_name')
        architecture = request.args.get('architecture')

        query = 'SELECT * FROM deployment_templates WHERE is_active = 1'
        params = []

        if model_name:
            query += ' AND model_name = ?'
            params.append(model_name)

        if architecture:
            query += ' AND architecture = ?'
            params.append(architecture)

        query += ' ORDER BY created_at DESC'

        with get_db() as conn:
            templates = conn.execute(query, params).fetchall()
            return jsonify({
                'success': True,
                'count': len(templates),
                'templates': [dict(t) for t in templates]
            })

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/deployment_templates/<model_name>/<architecture>', methods=['GET'])
def get_deployment_template_by_key(model_name, architecture):
    """Get specific deployment template."""
    try:
        role = request.args.get('role')  # Optional role for PD
        template = get_deployment_template(model_name, architecture, role)

        if template:
            return jsonify({'success': True, 'template': template})
        else:
            return jsonify({'success': False, 'error': 'Template not found'}), 404

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/deployment_templates', methods=['PUT'])
def update_deployment_template():
    """Update deployment template."""
    try:
        data = request.json

        # Save updated template (will deactivate old and create new)
        template_id = save_deployment_template(
            model_name=data['model_name'],
            architecture=data['architecture'],
            role=data.get('role'),
            tensor_parallelism=data['tensor_parallelism'],
            replicas=data.get('replicas', 1),
            max_model_len=data.get('max_model_len', 8192),
            gpu_memory_utilization=data.get('gpu_memory_utilization', 0.95),
            image=data.get('image', 'ghcr.io/llm-d/llm-d-cuda:v0.5.1'),
            pvc_name=data.get('pvc_name', 'in-s8-model-cache'),
            nccl_ib_hca=data.get('nccl_ib_hca', 'mlx'),
            isl=data.get('isl', 2000),
            osl=data.get('osl', 100),
            max_num_batched_tokens=data.get('max_num_batched_tokens'),
            gpus_per_pod=data.get('gpus_per_pod'),
            memory_limit=data.get('memory_limit', '512Gi'),
            cpu_request=data.get('cpu_request', '32'),
            namespace=data.get('namespace', TARGET_NAMESPACE)
        )

        return jsonify({
            'success': True,
            'template_id': template_id,
            'message': 'Template updated successfully'
        })

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/logs')
def get_console_logs():
    """Get console logs with optional filtering.

    Query parameters:
        run_id: Filter by optimization run ID
        job_name: Filter by job name
        since: ISO timestamp - get logs after this time
        limit: Maximum number of logs to return (default: 100, max: 1000)
    """
    try:
        # Parse query parameters
        run_id = request.args.get('run_id', type=int)
        job_name = request.args.get('job_name')
        since = request.args.get('since')
        limit = min(request.args.get('limit', default=100, type=int), 1000)

        # Build query
        query = 'SELECT id, timestamp, log_type, message, run_id, job_name FROM console_logs WHERE 1=1'
        params = []

        if run_id:
            query += ' AND run_id = ?'
            params.append(run_id)

        if job_name:
            query += ' AND job_name = ?'
            params.append(job_name)

        if since:
            query += ' AND timestamp > ?'
            params.append(since)

        query += ' ORDER BY id DESC LIMIT ?'
        params.append(limit)

        with get_db() as conn:
            logs = conn.execute(query, params).fetchall()
            return jsonify({
                'success': True,
                'count': len(logs),
                'logs': [dict(log) for log in reversed(logs)]  # Return in chronological order
            })

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# --- Optimization Runner ---

def log_to_ui(message: str, log_type: str = 'info', run_id: int = None, job_name: str = None, session_id: str = None):
    """Send log message to UI via SocketIO and persist to database.

    Args:
        message: Log message
        log_type: Log level (info, success, error, warning, decision)
        run_id: Optional optimization run ID
        job_name: Optional job name for filtering
        session_id: Optional session ID for tracking client sessions
    """
    # Skip empty messages
    if not message or not message.strip():
        return

    # Add timestamp prefix for info messages
    display_message = message
    if log_type == 'info':
        timestamp = datetime.now().strftime('%m/%d %H:%M:%S')
        display_message = f"[{timestamp}] {message}"

    # Emit to connected clients
    socketio.emit('console_log', {'type': log_type, 'message': display_message})

    # Persist to database
    try:
        with get_db() as conn:
            conn.execute(
                '''INSERT INTO console_logs (timestamp, log_type, message, run_id, job_name, session_id)
                   VALUES (?, ?, ?, ?, ?, ?)''',
                (datetime.now().isoformat(), log_type, message, run_id, job_name, session_id)
            )
    except Exception as e:
        # Don't fail the operation if logging fails, but print error
        print(f"Warning: Failed to persist log to database: {e}")

def stream_job_logs(job_name: str, namespace: str):
    """
    Stream Kubernetes job logs to UI in real-time.

    Args:
        job_name: Name of the job
        namespace: Kubernetes namespace
    """
    global CURRENT_TEST_PLAN
    import time

    try:
        # Determine kubectl command
        kubectl_cmd = 'oc'
        try:
            subprocess.run(['oc', 'version'], capture_output=True, timeout=5, check=True)
        except (FileNotFoundError, subprocess.CalledProcessError):
            kubectl_cmd = 'kubectl'

        # Wait for pod to be created
        time.sleep(2)

        # Get pod name from job
        cmd = [kubectl_cmd, 'get', 'pods', '-n', namespace, '-l', f'job-name={job_name}', '-o', 'jsonpath={.items[0].metadata.name}']
        pod_name = None

        # Wait up to 30 seconds for pod to appear
        for i in range(30):
            proc = subprocess.run(cmd, capture_output=True, timeout=10)
            if proc.returncode == 0:
                pod_name = proc.stdout.decode().strip()
                if pod_name:
                    break
            time.sleep(1)

        if not pod_name:
            log_to_ui('⚠️ Could not find download job pod', 'warning', job_name=job_name)
            return

        # Wait for pod to be ready with status indicator
        max_wait = 300  # 5 minutes
        last_phase = None
        logged_waiting = False

        for i in range(max_wait):
            # Get pod status
            status_cmd = [kubectl_cmd, 'get', 'pod', pod_name, '-n', namespace, '-o', 'jsonpath={.status.phase}:{.status.containerStatuses[0].state}']
            proc = subprocess.run(status_cmd, capture_output=True, timeout=10)

            if proc.returncode == 0:
                status_output = proc.stdout.decode().strip()
                phase = status_output.split(':')[0] if ':' in status_output else status_output

                if phase == 'Running':
                    log_to_ui('✅ Pod is ready', 'success', job_name=job_name)
                    break
                elif phase == 'Failed':
                    log_to_ui(f'❌ Pod {pod_name} failed to start', 'error', job_name=job_name)
                    return
                elif phase == 'Succeeded':
                    log_to_ui('✅ Job completed (no logs to stream)', 'success', job_name=job_name)
                    return
                else:
                    # Only show status when it changes
                    if phase != last_phase:
                        if phase == 'Pending' and not logged_waiting:
                            log_to_ui('⏳ Waiting for pod to be scheduled...', 'info', job_name=job_name)
                            logged_waiting = True
                        elif phase == 'ContainerCreating':
                            log_to_ui('📦 Pulling container image...', 'info', job_name=job_name)
                        else:
                            log_to_ui(f'⏳ Pod status: {phase}', 'info', job_name=job_name)
                        last_phase = phase

            time.sleep(1)

        log_to_ui(f'📡 Streaming logs from {pod_name}...', 'info', job_name=job_name)

        # Stream logs with follow
        cmd = ['oc', 'logs', '-n', namespace, '-f', pod_name]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        except FileNotFoundError:
            cmd[0] = 'kubectl'
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        # Stream lines to UI
        for line in proc.stdout:
            line = line.strip()
            if line:
                # Check if it's a progress bar line
                if '[' in line and ']' in line and '%' in line:
                    log_to_ui(line, 'info', job_name=job_name)
                elif '✅' in line or 'complete' in line.lower():
                    log_to_ui(line, 'success', job_name=job_name)
                elif '❌' in line or 'error' in line.lower():
                    log_to_ui(line, 'error', job_name=job_name)
                else:
                    log_to_ui(line, 'info', job_name=job_name)

        proc.wait()

        # Wait a moment for pod to transition to Succeeded
        time.sleep(2)

        # Check if job completed successfully
        status_cmd = [kubectl_cmd, 'get', 'pod', pod_name, '-n', namespace, '-o', 'jsonpath={.status.phase}']
        proc = subprocess.run(status_cmd, capture_output=True, timeout=10)

        pod_phase = proc.stdout.decode().strip() if proc.returncode == 0 else 'Unknown'
        log_to_ui(f'🔍 Final pod phase: {pod_phase}', 'info', job_name=job_name)

        if proc.returncode == 0 and pod_phase == 'Succeeded':
            log_to_ui('✅ Model download completed successfully!', 'success', job_name=job_name)

            # Trigger aggregated deployment
            # Try to get model name from job metadata
            model_name = None

            # Method 1: Get from job annotations
            model_cmd = [kubectl_cmd, 'get', 'job', job_name, '-n', namespace, '-o', 'jsonpath={.metadata.annotations.description}']
            proc = subprocess.run(model_cmd, capture_output=True, timeout=10)
            model_desc = proc.stdout.decode().strip()

            log_to_ui(f'🔍 Job description: "{model_desc}"', 'info', job_name=job_name)

            # Extract model name from description: "Model download job for {model_name}"
            if 'for ' in model_desc:
                model_name = model_desc.split('for ')[-1].strip()
                log_to_ui(f'🔍 Extracted model name (method 1): {model_name}', 'info', job_name=job_name)

            # Method 2: Fallback - get from CURRENT_TEST_PLAN
            if not model_name:
                with state_lock:
                    if CURRENT_TEST_PLAN:
                        model_name = CURRENT_TEST_PLAN.model_name
                        log_to_ui(f'🔍 Extracted model name (method 2 - test plan): {model_name}', 'info', job_name=job_name)

            if model_name:
                log_to_ui('', 'info', job_name=job_name)  # Blank line
                log_to_ui('🚀 Starting optimization...', 'info', job_name=job_name)

                # Get the test plan from global state, fallback to database
                test_plan = None
                with state_lock:
                    test_plan = CURRENT_TEST_PLAN

                if not test_plan:
                    # Try to restore from database
                    load_state()
                    with state_lock:
                        test_plan = CURRENT_TEST_PLAN

                # Build optimization data from test plan or saved config
                saved_config = {}
                try:
                    with get_db() as conn:
                        row = conn.execute('SELECT config_json FROM ui_session_state WHERE id = 1').fetchone()
                        if row and row['config_json']:
                            saved_config = json.loads(row['config_json'])
                except Exception:
                    pass

                optimization_data = {
                    'model': model_name,
                    'isl': test_plan.model_requirements.isl if test_plan else saved_config.get('isl', 3000),
                    'osl': test_plan.model_requirements.osl if test_plan else saved_config.get('osl', 100),
                    'isl_stdev': saved_config.get('isl_stdev'),
                    'osl_stdev': saved_config.get('osl_stdev'),
                    'turns': saved_config.get('turns', 1),
                    'num_users': saved_config.get('users', 100),
                    'optimization_metric': test_plan.optimization_goal if test_plan else saved_config.get('goal', 'ttft'),
                    'max_test_duration': saved_config.get('duration', 300),
                    'stop_mode': saved_config.get('stop_mode', 'duration'),
                    'max_requests': saved_config.get('max_requests'),
                    'max_gpus': test_plan.max_gpus_to_use if test_plan else saved_config.get('max_gpus', 16),
                    'hf_token': saved_config.get('hf_token'),
                    'selected_nodes': saved_config.get('selected_nodes', []),
                    'workload_mode': saved_config.get('workload_mode', 'synthetic'),
                    'dataset_source': saved_config.get('dataset_source'),
                    'dataset_column': saved_config.get('dataset_column'),
                    'dataset_max_output': saved_config.get('dataset_max_output', 256),
                    'rate_type': saved_config.get('rate_type', 'concurrent'),
                    'advanced_vllm': saved_config.get('advanced_vllm'),
                }

                # Start optimization in background
                spawn(run_optimization_background, optimization_data)
            else:
                log_to_ui('⚠️ Could not determine model name for deployment', 'warning', job_name=job_name)
                log_to_ui('   CURRENT_TEST_PLAN is None or has no model_name', 'warning', job_name=job_name)
        else:
            log_to_ui(f'⚠️ Pod did not reach Succeeded status (current: {pod_phase})', 'warning', job_name=job_name)
            log_to_ui('   Deployment will not be triggered automatically', 'warning', job_name=job_name)

    except Exception as e:
        log_to_ui(f'⚠️ Log streaming error: {str(e)}', 'warning', job_name=job_name)

def deploy_and_test_inference(model_name: str, namespace: str, job_name: str = None):
    """
    Deploy aggregated inference and test it using the latest template from database.

    Args:
        model_name: HuggingFace model name
        namespace: Kubernetes namespace
        job_name: Optional job name for log context
    """
    import time

    try:
        # Step 1: Deploy prerequisite infrastructure (GAIE, Gateway, etc.)
        from core import PrereqManager, CleanupManager

        log_to_ui('', 'info', job_name=job_name)
        log_to_ui('=' * 60, 'info', job_name=job_name)
        log_to_ui('📋 Step 1: Deploying Prerequisite Infrastructure', 'info', job_name=job_name)
        log_to_ui('=' * 60, 'info', job_name=job_name)

        # Deploy prerequisites (GAIE, Gateway, InferencePool)
        # This function deploys aggregated architecture
        prereq_mgr = PrereqManager(namespace=namespace)
        try:
            # Deploy prerequisites - this will create missing resources and skip existing ones
            success = prereq_mgr.deploy_prereqs(
                architecture='aggregated',
                log_callback=lambda msg: log_to_ui(msg, 'info', job_name=job_name)
            )

            if not success:
                log_to_ui('', 'error', job_name=job_name)
                log_to_ui('❌ Failed to deploy prerequisite infrastructure', 'error', job_name=job_name)
                log_to_ui('', 'error', job_name=job_name)
                return None

            log_to_ui('', 'info', job_name=job_name)
            log_to_ui('ℹ️  Note: Gateway typically takes 1-2 minutes to become fully healthy', 'info', job_name=job_name)
            log_to_ui('   Waiting for gateway to be ready before proceeding...', 'info', job_name=job_name)
        except Exception as e:
            log_to_ui('', 'error', job_name=job_name)
            log_to_ui(f'❌ Failed to deploy prerequisites: {str(e)}', 'error', job_name=job_name)
            log_to_ui('', 'error', job_name=job_name)
            import traceback
            traceback.print_exc()
            return None

        log_to_ui('', 'info', job_name=job_name)
        log_to_ui('▶️  Prerequisites ready, continuing with inference pod deployment...', 'info', job_name=job_name)
        log_to_ui('', 'info', job_name=job_name)
        log_to_ui('=' * 60, 'info', job_name=job_name)
        log_to_ui('📋 Step 2: Deploying Inference Pods', 'info', job_name=job_name)
        log_to_ui('=' * 60, 'info', job_name=job_name)
        log_to_ui('', 'info', job_name=job_name)

        # Step 2: Check if there are existing In-S8 deployments
        cleanup_mgr = CleanupManager(namespace=namespace)
        existing_resources = cleanup_mgr.get_deployed_resources()

        if existing_resources:
            log_to_ui(f'⚠️  Found {len(existing_resources)} existing deployment(s)', 'warning', job_name=job_name)
            for resource in existing_resources:
                log_to_ui(f'   • {resource}', 'info', job_name=job_name)
            log_to_ui('', 'info', job_name=job_name)
            log_to_ui('🧹 Cleaning up existing deployments first...', 'info', job_name=job_name)

            # Clean up existing deployments
            success = cleanup_mgr.cleanup_all_test_deployments(
                log_callback=lambda msg: log_to_ui(f'   {msg}', 'info', job_name=job_name)
            )

            if not success:
                log_to_ui('⚠️  Cleanup had warnings, but continuing...', 'warning', job_name=job_name)

            log_to_ui('', 'info', job_name=job_name)

        timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        test_id = f'in-s8-inference-{timestamp}'

        log_to_ui('📋 Loading deployment template from database...', 'info', job_name=job_name)

        # Get the latest aggregated template from database
        template = get_deployment_template(model_name, 'aggregated', role=None)

        if template is None:
            log_to_ui('⚠️ No deployment template found in database. Using defaults.', 'warning', job_name=job_name)
            # Fallback to defaults
            tp = 2
            isl = 2000
            osl = 100
            replicas = 1
            max_model_len = 8192
            gpu_memory_utilization = 0.95
            image = 'ghcr.io/llm-d/llm-d-cuda:v0.5.1'
            pvc_name = 'in-s8-model-cache'
            nccl_ib_hca = 'mlx'
            gpus_per_pod = tp
        else:
            # Use template from database
            tp = template['tensor_parallelism']
            isl = template['isl']
            osl = template['osl']
            replicas = template['replicas']
            max_model_len = template['max_model_len']
            gpu_memory_utilization = template['gpu_memory_utilization']
            image = template['image']
            pvc_name = template['pvc_name']
            nccl_ib_hca = template['nccl_ib_hca']
            gpus_per_pod = template['gpus_per_pod']
            log_to_ui('   ✅ Template loaded from database', 'success', job_name=job_name)
            log_to_ui('   • Architecture: aggregated', 'info', job_name=job_name)
            log_to_ui(f'   • Tensor Parallelism: TP={tp}', 'info', job_name=job_name)
            log_to_ui(f'   • GPUs per pod: {gpus_per_pod}', 'info', job_name=job_name)
            log_to_ui(f'   • Max model length: {max_model_len} tokens', 'info', job_name=job_name)
            log_to_ui(f'   • GPU memory utilization: {gpu_memory_utilization}', 'info', job_name=job_name)
            log_to_ui(f'   • ISL/OSL: {isl}/{osl} tokens', 'info', job_name=job_name)

        # Create a TestConfig for aggregated deployment
        config = TestConfig(
            test_id=test_id,
            architecture='aggregated',
            model_name=model_name,
            namespace=namespace,
            isl=isl,
            osl=osl,
            num_users=50,
            tensor_parallelism=tp,
            replicas=replicas,
            pvc_name=pvc_name,
            nccl_ib_hca=nccl_ib_hca,
            max_model_len=max_model_len,
            gpu_memory_utilization=gpu_memory_utilization,
            image=image,
            optimization_goal='balanced',
            test_duration=300
        )

        log_to_ui('', 'info', job_name=job_name)
        log_to_ui(f'📦 Deploying LeaderWorkerSet for {model_name}...', 'info', job_name=job_name)

        # Deploy using DeploymentManager
        deployment_mgr = DeploymentManager(namespace=namespace)

        def deployment_log(msg):
            log_to_ui(f'   {msg}', 'info', job_name=job_name)

        success = deployment_mgr.deploy_config(config, log_callback=deployment_log)

        if not success:
            log_to_ui('❌ Failed to deploy aggregated inference', 'error', job_name=job_name)
            return

        log_to_ui('✅ Deployment manifests applied', 'success', job_name=job_name)

        # Wait for deployment to be ready
        log_to_ui('', 'info', job_name=job_name)  # Blank line
        log_to_ui('⏳ Waiting for pods to reach inference serving state...', 'info', job_name=job_name)

        ready = deployment_mgr.wait_for_ready(
            test_id=test_id,
            architecture='aggregated',
            timeout=900,  # 15 minutes for model loading
            log_callback=deployment_log
        )

        if not ready:
            log_to_ui('⏱️ Timeout: Pods did not reach serving state', 'error', job_name=job_name)
            return

        # Pods are ready, now test inference
        log_to_ui('', 'info', job_name=job_name)  # Blank line
        log_to_ui('🧪 Testing inference endpoint...', 'info', job_name=job_name)

        # Get service endpoint
        service_name = f'{test_id}-aggregated'
        service_url = f'http://{service_name}.{namespace}.svc.cluster.local:8000'

        log_to_ui(f'   Service: {service_name}', 'info', job_name=job_name)
        log_to_ui(f'   Endpoint: {service_url}', 'info', job_name=job_name)

        # Wait a bit for service to be fully ready
        time.sleep(5)

        # Test with a simple curl to /v1/models
        try:
            # Determine kubectl command
            kubectl_cmd = 'oc'
            try:
                subprocess.run(['oc', 'version'], capture_output=True, timeout=5, check=True)
            except (FileNotFoundError, subprocess.CalledProcessError):
                kubectl_cmd = 'kubectl'

            # Run curl from within cluster (use a temporary pod)
            curl_cmd = [
                kubectl_cmd, 'run', 'in-s8-curl-test', '-n', namespace,
                '--rm', '-i', '--restart=Never',
                '--image=registry.access.redhat.com/ubi9/ubi-minimal:latest',
                '--', 'curl', '-s', '-m', '30', f'{service_url}/v1/models'
            ]

            log_to_ui('   Running curl test...', 'info', job_name=job_name)
            proc = subprocess.run(curl_cmd, capture_output=True, timeout=60)

            if proc.returncode == 0:
                output = proc.stdout.decode().strip()
                # Check if we got a valid JSON response with model info
                if 'data' in output and model_name in output:
                    log_to_ui('✅ Inference endpoint is serving!', 'success', job_name=job_name)
                    log_to_ui(f'   Response: {output[:200]}...', 'info', job_name=job_name)
                else:
                    log_to_ui('⚠️ Endpoint responded but may not be fully ready', 'warning', job_name=job_name)
                    log_to_ui(f'   Response: {output[:200]}', 'info', job_name=job_name)
            else:
                log_to_ui(f'❌ Curl test failed: {proc.stderr.decode().strip()}', 'error', job_name=job_name)

        except subprocess.TimeoutExpired:
            log_to_ui('⏱️ Curl test timeout', 'warning', job_name=job_name)
        except Exception as curl_err:
            log_to_ui(f'⚠️ Could not run curl test: {curl_err}', 'warning', job_name=job_name)

        # Final summary
        log_to_ui('', 'info', job_name=job_name)  # Blank line
        log_to_ui('=' * 60, 'info', job_name=job_name)
        log_to_ui('🎉 Aggregated inference deployment complete!', 'success', job_name=job_name)
        log_to_ui(f'   Test ID: {test_id}', 'info', job_name=job_name)
        log_to_ui(f'   Service: {service_name}', 'info', job_name=job_name)
        log_to_ui(f'   Namespace: {namespace}', 'info', job_name=job_name)
        log_to_ui('=' * 60, 'info', job_name=job_name)

    except Exception as e:
        log_to_ui(f'❌ Deployment failed: {str(e)}', 'error', job_name=job_name)
        import traceback
        traceback.print_exc()


def run_optimization_background(data):
    """Run optimization in background greenlet using recipe-based approach."""
    global OPTIMIZATION_RUNNING, CURRENT_TEST_PLAN

    # Re-assert the flag here — it may have been reset by load_state() or
    # page reload between handle_storage_setup and this greenlet starting.
    with state_lock:
        OPTIMIZATION_RUNNING = True

    resume_run_id = data.get('resume_run_id')  # If set, resume this run instead of creating new
    run_name = data.get('run_name', f"run-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    run_id = None

    try:
        # Parse configuration directly from data (no test plan needed)
        model = data.get('model')
        isl = int(data.get('isl', 3000))
        osl = int(data.get('osl', 100))
        isl_stdev = data.get('isl_stdev')  # Optional ISL std dev
        osl_stdev = data.get('osl_stdev')  # Optional OSL std dev
        turns = int(data.get('turns', 1))  # Conversation turns (1 = single-turn)
        num_users = int(data.get('num_users', 100))
        optimization_goal = data.get('optimization_metric') or 'ttft'
        stop_mode = data.get('stop_mode', 'duration')  # 'duration' or 'max_requests'
        test_duration = int(data.get('max_test_duration', 300))
        max_requests = data.get('max_requests')  # Alternative to duration
        thanos_url = data.get('thanos_url', os.environ.get('THANOS_URL'))
        hf_token = data.get('hf_token')
        max_gpus = data.get('max_gpus', 16)
        use_achievable_qps = data.get('use_achievable_qps', False)
        latency_constraint_enabled = data.get('latency_constraint_enabled', False)
        latency_constraint_ms = int(data.get('latency_constraint_ms', 500))
        latency_constraint_percentile = data.get('latency_constraint_percentile', 'p90')
        tp_pair_top_n = int(data.get('tp_pair_top_n', 2))
        selected_nodes = data.get('selected_nodes') or []
        workload_mode = data.get('workload_mode', 'synthetic')
        dataset_source = data.get('dataset_source')
        dataset_column = data.get('dataset_column')
        dataset_max_output = int(data.get('dataset_max_output', 256))
        rate_type = data.get('rate_type', 'concurrent')
        advanced_vllm = data.get('advanced_vllm')

        # Create/update HuggingFace token secret if provided
        if hf_token and hf_token.strip():
            try:
                log_to_ui("🔑 Creating HuggingFace token secret...", 'info')
                import subprocess
                import base64

                # Create secret YAML
                secret_yaml = f"""apiVersion: v1
kind: Secret
metadata:
  name: llm-d-hf-token
  namespace: {TARGET_NAMESPACE}
type: Opaque
data:
  HF_TOKEN: {base64.b64encode(hf_token.strip().encode()).decode()}
"""

                # Apply secret (try oc first, fallback to kubectl)
                cmd = ['oc', 'apply', '-f', '-']
                try:
                    proc = subprocess.run(cmd, input=secret_yaml.encode(), capture_output=True, timeout=30)
                except FileNotFoundError:
                    cmd = ['kubectl', 'apply', '-f', '-']
                    proc = subprocess.run(cmd, input=secret_yaml.encode(), capture_output=True, timeout=30)

                if proc.returncode == 0:
                    log_to_ui("✅ HuggingFace token secret created/updated", 'success')
                else:
                    log_to_ui(f"⚠️  Warning: Failed to create secret: {proc.stderr.decode()}", 'warning')
            except Exception as e:
                log_to_ui(f"⚠️  Warning: Failed to create HF token secret: {str(e)}", 'warning')

        # Log startup
        log_to_ui(f"🚀 Starting optimization run: {run_name}", 'success')
        log_to_ui(f"Model: {model}", 'info')
        isl_str = f"ISL: {isl}" + (f" (σ={isl_stdev})" if isl_stdev else "")
        osl_str = f"OSL: {osl}" + (f" (σ={osl_stdev})" if osl_stdev else "")
        turns_str = f", Turns: {turns}" if turns > 1 else ""
        log_to_ui(f"{isl_str}, {osl_str}, Users: {num_users}{turns_str}", 'info')
        log_to_ui(f"Optimization goal: {optimization_goal}", 'decision')
        log_to_ui("Using recipe-based optimization (tests generated dynamically)", 'info')

        # Create or resume database entry
        if resume_run_id:
            run_id = resume_run_id
            with get_db() as conn:
                conn.execute(
                    'UPDATE optimization_runs SET status = ? WHERE id = ?',
                    ('running', run_id)
                )
            log_to_ui(f"📋 Resuming run #{run_id} — completed tests will be skipped", 'info')
        else:
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO optimization_runs
                    (run_name, model, isl, osl, num_users, status, created_at, goal, test_duration, max_gpus, use_achievable_qps, isl_stdev, osl_stdev, turns,
                     latency_constraint_enabled, latency_constraint_ms, latency_constraint_percentile)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (run_name, model, isl, osl, num_users, 'running',
                      datetime.now().isoformat(), optimization_goal, test_duration, max_gpus,
                      1 if use_achievable_qps else 0,
                      int(isl_stdev) if isl_stdev else None,
                      int(osl_stdev) if osl_stdev else None,
                      turns,
                      1 if latency_constraint_enabled else 0,
                      latency_constraint_ms,
                      latency_constraint_percentile))
                run_id = cursor.lastrowid

        # Step 1: Choose optimization approach
        log_to_ui("\n📋 Step 1: Selecting optimization approach...", 'info')
        log_to_ui(f"   Optimization goal: {optimization_goal}", 'info')

        # Use Recipe-based optimization for all goals
        if optimization_goal in ('ttft', 'throughput', 'balanced'):
            goal_descriptions = {
                'ttft': {
                    'label': 'Response Time Priority (PD vs Aggregated)',
                    'steps': [
                        "Steps 2-3: Find optimal prefill/decode TP (exhaustive sweep)",
                        "Steps 4-5: Mathematical resource sizing",
                        "Step 7: Test feasible P/D splits near ideal ratio",
                        "Step 8: Validate best PD vs equivalent Aggregated",
                    ]
                },
                'throughput': {
                    'label': 'Throughput Priority (EP vs Aggregated)',
                    'steps': [
                        "Steps 2-3: Find optimal TP for throughput (exhaustive sweep)",
                        "Steps 4-5: EP configuration space enumeration",
                        "Step 7: Test EP configurations at full workload",
                        "Step 8: Validate best EP vs equivalent Aggregated",
                    ]
                },
                'balanced': {
                    'label': 'Balanced Performance (PD + EP + Aggregated)',
                    'steps': [
                        "Steps 2-3: Find optimal TP (exhaustive sweep)",
                        "Steps 4-5: Resource sizing for PD + EP",
                        "Step 7: Test PD splits and EP configurations",
                        "Step 8: Three-way comparison (PD vs EP vs Aggregated)",
                    ]
                },
            }
            goal_info = goal_descriptions[optimization_goal]
            log_to_ui("", 'info')
            log_to_ui(f"✨ Using Recipe-based optimization — {goal_info['label']}", 'success')
            for step_desc in goal_info['steps']:
                log_to_ui(f"   • {step_desc}", 'info')
            log_to_ui("", 'info')

            # Import Recipe optimizer
            from core.recipe_optimizer import RecipeOptimizer, RecipeOptimizerConfig
            from core.system_scanner import SystemScanner

            # Scan cluster to get valid TP options based on actual hardware
            log_to_ui("🔍 Scanning cluster for valid TP options...", 'info')
            scanner = SystemScanner(namespace=TARGET_NAMESPACE)
            cluster_resources = scanner.scan_cluster()
            tp_options = cluster_resources.get_tp_options()
            log_to_ui(f"   Valid TP options for this cluster: {tp_options}", 'info')
            log_to_ui("", 'info')

            # Create optimizer config
            recipe_config = RecipeOptimizerConfig(
                model_name=model,
                namespace=TARGET_NAMESPACE,
                isl=isl,
                osl=osl,
                isl_stdev=int(isl_stdev) if isl_stdev else None,
                osl_stdev=int(osl_stdev) if osl_stdev else None,
                turns=turns,
                qps=float(num_users),
                total_gpus=max_gpus,
                max_model_len=8192,
                test_duration=test_duration,
                stop_mode=stop_mode,
                max_requests=int(max_requests) if max_requests else None,
                # Steps 2 & 3 exhaustively test ALL valid TP values
                max_pd_splits=0,  # 0 = full coverage (test all valid splits)
                tp_pair_top_n=tp_pair_top_n,
                thanos_url=thanos_url,
                image='ghcr.io/llm-d/llm-d-cuda:v0.5.1',
                pvc_name='in-s8-model-cache',
                nccl_ib_hca='mlx',
                hf_token=hf_token,
                tp_options=tp_options,  # Dynamic based on cluster hardware
                objective=optimization_goal,  # 'ttft' selects TP by lowest latency
                use_achievable_qps=use_achievable_qps,
                latency_constraint_enabled=latency_constraint_enabled,
                latency_constraint_ms=latency_constraint_ms,
                latency_constraint_percentile=latency_constraint_percentile,
                selected_nodes=selected_nodes,
                workload_mode=workload_mode,
                dataset_source=dataset_source,
                dataset_column=dataset_column,
                dataset_max_output=dataset_max_output,
                rate_type=rate_type,
                advanced_vllm=advanced_vllm,
            )

            # Save full config to DB for resume
            with get_db() as conn:
                conn.execute(
                    'UPDATE optimization_runs SET config_json = ? WHERE id = ?',
                    (json.dumps(recipe_config.to_dict()), run_id))

            # Run optimization with database persistence
            def check_stopped():
                with state_lock:
                    return not OPTIMIZATION_RUNNING

            optimizer = RecipeOptimizer(
                config=recipe_config,
                log_callback=log_to_ui,
                run_id=run_id,
                db_path=DB_PATH,
                stop_check=check_stopped
            )

            try:
                results = optimizer.optimize(resume=bool(resume_run_id))

                # Log final results
                log_to_ui("", 'info')
                log_to_ui("=" * 80, 'success')
                log_to_ui("🎯 OPTIMIZATION COMPLETE", 'success')
                log_to_ui("=" * 80, 'success')
                log_to_ui(f"✅ Total tests run: {results['total_tests_run']}", 'success')
                log_to_ui(f"✅ Optimal decode TP: {results['optimal_decode_tp']}", 'success')
                log_to_ui(f"✅ Optimal prefill TP: {results['optimal_prefill_tp']}", 'success')

                # Goal-specific result summary
                opt_goal = results.get('optimization_goal', 'ttft')

                if results.get('pareto_configurations'):
                    log_to_ui(f"✅ PD Pareto configurations: {results['pareto_front_count']}", 'success')
                    log_to_ui("", 'info')
                    log_to_ui("📊 PD Configurations (Pareto Front):", 'decision')
                    for i, config in enumerate(results['pareto_configurations'], 1):
                        log_to_ui(f"   {i}. PD: {config['prefill_pods']}P×TP{config['prefill_tp']} + "
                                 f"{config['decode_pods']}D×TP{config['decode_tp']}", 'info')
                        log_to_ui(f"      TTFT p90: {config['ttft_p90']:.1f}ms, "
                                 f"Throughput p90: {config['throughput_p90']:.2f} req/s", 'info')

                if results.get('ep_configurations'):
                    log_to_ui("", 'info')
                    log_to_ui("📊 EP Configurations:", 'decision')
                    for i, config in enumerate(results['ep_configurations'], 1):
                        log_to_ui(f"   {i}. EP: TP{config['tp']} × {config['replicas']} replicas "
                                 f"({config['total_gpus']} GPUs)", 'info')
                        log_to_ui(f"      TTFT p90: {config['ttft_p90']:.1f}ms, "
                                 f"Throughput p90: {config['throughput_p90']:.2f} req/s", 'info')

                best_ep = results.get('best_ep')
                if best_ep:
                    log_to_ui("", 'info')
                    log_to_ui(f"✅ Best EP: TP{best_ep['tp']} × {best_ep['replicas']} replicas", 'success')

                agg = results.get('aggregated_result')
                if agg:
                    log_to_ui("", 'info')
                    log_to_ui("📊 Aggregated Baseline (Step 8):", 'decision')
                    log_to_ui(f"   Aggregated: {agg['pods']} pods × TP{agg['tp']} ({agg['gpus']} GPUs)", 'info')
                    agg_ttft = agg.get('ttft_p90')
                    agg_tput = agg.get('throughput_p90')
                    if agg_ttft is not None and agg_tput is not None:
                        log_to_ui(f"      TTFT p90: {agg_ttft:.1f}ms, "
                                 f"Throughput p90: {agg_tput:.2f} req/s", 'info')

                        # Compare based on goal
                        if opt_goal == 'ttft':
                            pareto_cfgs = results.get('pareto_configurations', [])
                            best_pd = min(pareto_cfgs, key=lambda p: p.get('ttft_p90') or 1e9) if pareto_cfgs else None
                            if best_pd:
                                pd_ttft = best_pd['ttft_p90']
                                if agg_ttft < pd_ttft:
                                    log_to_ui(f"   ⚡ Aggregated has better TTFT ({agg_ttft:.1f}ms vs {pd_ttft:.1f}ms)", 'warning')
                                else:
                                    log_to_ui(f"   ✅ PD has better TTFT ({pd_ttft:.1f}ms vs {agg_ttft:.1f}ms)", 'success')
                        elif opt_goal == 'throughput' and best_ep:
                            ep_tput = best_ep.get('throughput_p90', 0)
                            if ep_tput and agg_tput:
                                if ep_tput > agg_tput:
                                    log_to_ui(f"   ✅ EP has better throughput ({ep_tput:.2f} vs {agg_tput:.2f} req/s)", 'success')
                                else:
                                    log_to_ui(f"   ⚡ Aggregated has better throughput ({agg_tput:.2f} vs {ep_tput:.2f} req/s)", 'warning')

                # Note: RecipeOptimizer already saves each test to DB in real-time
                # via _save_test_to_database(), so no need to re-save here
                log_to_ui("", 'info')
                log_to_ui(f"💾 All {results['total_tests_run']} test results already saved to database", 'success')

                # Update run status and save constraint notes
                constraint_notes_json = None
                if results.get('constraint_notes'):
                    import json as _json
                    constraint_notes_json = _json.dumps(results['constraint_notes'])

                optimal_config_json = None
                try:
                    import json as _json2
                    serializable_keys = {
                        'optimal_decode_tp', 'optimal_prefill_tp',
                        'decode_tpsg', 'prefill_tpsg',
                        'concurrency', 'total_gpus_available',
                        'gpu_sizing', 'feasible_splits_count', 'pareto_front_count',
                        'total_tests_run', 'pareto_configurations',
                        'aggregated_result',
                        'calibrated_qps', 'calibrated_concurrency', 'sustainable_throughput_rps',
                        'calibrated_pd_result', 'calibrated_agg_result',
                        'best_ep', 'calibrated_ep_result',
                        'optimization_goal', 'stopped',
                    }
                    optimal_config_json = _json2.dumps({
                        k: v for k, v in results.items() if k in serializable_keys
                    })
                except Exception:
                    pass

                if results.get('stopped'):
                    run_status = 'stopped'
                elif results.get('total_tests_run', 0) == 0:
                    run_status = 'failed'
                else:
                    run_status = 'completed'

                with get_db() as conn:
                    conn.execute('''
                        UPDATE optimization_runs
                        SET status = ?, completed_at = ?,
                            constraint_notes = ?,
                            optimal_config = ?
                        WHERE id = ?
                    ''', (run_status, datetime.now().isoformat(), constraint_notes_json, optimal_config_json, run_id))

                log_to_ui("", 'info')
                if results.get('stopped'):
                    log_to_ui("🛑 Optimization stopped by user — can be resumed later", 'warning')
                else:
                    log_to_ui("✅ Recipe optimization completed successfully!", 'success')
                    log_to_ui("", 'info')
                    log_to_ui("📈 Click 'Report Analytics' in the toolbar to see full results and breakdown", 'info')

            except Exception as e:
                from core.pod_error_scanner import PodErrorsDetected
                if isinstance(e, PodErrorsDetected):
                    log_to_ui("", 'info')
                    log_to_ui("=" * 80, 'error')
                    log_to_ui("🚨 OPTIMIZATION STOPPED — Critical Pod Errors Detected", 'error')
                    log_to_ui("=" * 80, 'error')
                    log_to_ui(f"Test: {e.test_id}", 'error')
                    log_to_ui("Pods left running for investigation.", 'error')
                    log_to_ui("Resume from the Resume page after investigating.", 'info')

                    with get_db() as conn:
                        conn.execute('''
                            UPDATE optimization_runs
                            SET status = 'error_stopped', completed_at = ?
                            WHERE id = ?
                        ''', (datetime.now().isoformat(), run_id))
                else:
                    log_to_ui(f"❌ Recipe optimization failed: {str(e)}", 'error')
                    import traceback
                    log_to_ui(traceback.format_exc(), 'error')

                    with get_db() as conn:
                        conn.execute('''
                            UPDATE optimization_runs
                            SET status = 'failed', completed_at = ?
                            WHERE id = ?
                        ''', (datetime.now().isoformat(), run_id))

            finally:
                # Mark optimization as stopped
                with state_lock:
                    OPTIMIZATION_RUNNING = False
                    save_state()
                socketio.emit('status_update', {'running': False})

            return

        # Otherwise, use traditional exhaustive approach (requires pre-generated test plan)
        log_to_ui("", 'info')
        log_to_ui("📋 Using exhaustive test plan approach", 'info')

        # Load pre-generated test plan for exhaustive mode
        test_plan = None
        with state_lock:
            test_plan = CURRENT_TEST_PLAN

        if not test_plan:
            log_to_ui('❌ No test plan found for exhaustive mode. Use TTFT optimization instead.', 'error')
            with state_lock:
                OPTIMIZATION_RUNNING = False
                save_state()
            socketio.emit('status_update', {'running': False})
            return

        log_to_ui(f"   Recipe-based plan: {test_plan.estimated_total_tests}", 'info')
        log_to_ui("", 'info')

        from core.config_generator import TestConfig
        from core.system_scanner import SystemScanner

        # Get cluster resources for memory calculation
        scanner = SystemScanner(namespace=TARGET_NAMESPACE)
        cluster_resources = scanner.scan_cluster()
        total_memory_gb = cluster_resources.total_memory_gb
        gpu_nodes = cluster_resources.gpu_node_count
        max_gpus_per_node = cluster_resources.max_gpus_per_node
        log_to_ui(f"   Cluster: {gpu_nodes} GPU nodes, {max_gpus_per_node} GPUs/node, {total_memory_gb}GB total RAM", 'info')

        # Filter out placeholder tests and convert to TestConfig
        test_configs = []
        for idx, test in enumerate(test_plan.tests):
            # Skip placeholder tests (TP=0 or gpus_required=0)
            if test.tp == 0 or test.gpus_required == 0:
                continue

            # Generate test ID (lowercase for Kubernetes RFC 1123 compliance)
            arch_short = test.architecture.value.lower()[:3]
            test_id = f"{run_name}-{arch_short}-tp{test.tp}-{idx:02d}"

            # Get workload ISL/OSL (use override if specified, otherwise use plan defaults)
            test_isl = test.workload_isl if test.workload_isl is not None else isl
            test_osl = test.workload_osl if test.workload_osl is not None else osl

            # Calculate memory and CPU per pod based on per-node capacity
            # Memory and CPU are per-node, so calculate based on max pods that can fit on one node
            per_node_memory_gb = total_memory_gb / gpu_nodes
            per_node_usable_memory_gb = per_node_memory_gb * 0.8  # 80% usable

            # Get CPU info from GPU nodes only (filter nodes with GPUs)
            gpu_nodes_list = [n for n in cluster_resources.nodes if n.gpus > 0]
            if gpu_nodes_list:
                # Use average CPU cores from GPU nodes
                avg_cpu_per_gpu_node = sum(n.cpu_cores for n in gpu_nodes_list) / len(gpu_nodes_list)
            else:
                # Fallback: assume 48 cores per node (conservative)
                avg_cpu_per_gpu_node = 48
            per_node_usable_cpus = int(avg_cpu_per_gpu_node * 0.7)  # 70% usable

            # Max pods per node = GPUs per node / TP
            max_pods_per_node = max_gpus_per_node / test.tp
            memory_per_pod_gb = int(per_node_usable_memory_gb / max_pods_per_node)
            memory_per_pod = f"{memory_per_pod_gb}Gi"
            cpu_per_pod = max(1, int(per_node_usable_cpus / max_pods_per_node))  # At least 1 CPU
            cpu_request = str(cpu_per_pod)

            # Create TestConfig based on architecture
            if test.architecture.value == 'pd':
                config = TestConfig(
                    test_id=test_id,
                    architecture='pd',
                    model_name=model,
                    namespace=TARGET_NAMESPACE,
                    isl=test_isl,
                    osl=test_osl,
                    num_users=num_users,
                    tensor_parallelism=test.tp,
                    replicas=test.prefill_pods + test.decode_pods,
                    prefill_replicas=test.prefill_pods,
                    decode_replicas=test.decode_pods,
                    prefill_decode_ratio=f"{test.prefill_pods}:{test.decode_pods}",
                    max_model_len=8192,
                    gpu_memory_utilization=test_plan.model_requirements.gpu_memory_utilization,
                    memory_request=memory_per_pod,
                    memory_limit=memory_per_pod,
                    pvc_name='in-s8-model-cache',
                    optimization_goal=optimization_goal,
                    test_duration=test_duration,
                    cpu_request=cpu_request
                )
            else:
                # AGGREGATED or EP
                config = TestConfig(
                    test_id=test_id,
                    architecture=test.architecture.value,
                    model_name=model,
                    namespace=TARGET_NAMESPACE,
                    isl=test_isl,
                    osl=test_osl,
                    num_users=num_users,
                    tensor_parallelism=test.tp,
                    replicas=test.ep_pods,
                    max_model_len=8192,
                    gpu_memory_utilization=test_plan.model_requirements.gpu_memory_utilization,
                    memory_request=memory_per_pod,
                    memory_limit=memory_per_pod,
                    pvc_name='in-s8-model-cache',
                    optimization_goal=optimization_goal,
                    test_duration=test_duration,
                    cpu_request=cpu_request
                )

            test_configs.append(config)

        log_to_ui(f"✅ Prepared {len(test_configs)} test configurations for execution", 'success')

        # Step 2: Run tests with architecture-aware logging
        log_to_ui("\n🧪 Step 2: Running optimization tests...", 'info')
        if stop_mode == 'max_requests' and max_requests:
            log_to_ui(f"   Stop condition: {max_requests} requests per test", 'info')
        else:
            log_to_ui(f"   Test duration: {test_duration}s per test", 'info')
        log_to_ui(f"   Thanos URL: {thanos_url or 'Not configured'}", 'info')
        log_to_ui('', 'info')

        # Initialize deployment orchestrator with kubectl runner
        kubectl = KubectlRunner()
        deployment_orchestrator = DeploymentOrchestrator(kubectl)

        # Run tests one by one with detailed architecture logging
        results = []
        from core import CleanupManager

        for idx, test_config in enumerate(test_configs, 1):
            # Check if optimization was stopped
            with state_lock:
                if not OPTIMIZATION_RUNNING:
                    log_to_ui('', 'info')
                    log_to_ui('🛑 Optimization stopped by user - cancelling remaining tests', 'warning')
                    break

            log_to_ui('', 'info')
            log_to_ui('═' * 60, 'info')
            log_to_ui(f'📊 Test {idx}/{len(test_configs)}: {test_config.architecture.upper()}', 'info')
            log_to_ui('═' * 60, 'info')
            log_to_ui(f'   Architecture: {test_config.architecture.upper()}', 'info')
            log_to_ui(f'   Tensor Parallelism: TP={test_config.tensor_parallelism}', 'info')
            log_to_ui(f'   Workload: ISL={test_config.isl}, OSL={test_config.osl}', 'info')
            log_to_ui(f'   Users: {test_config.num_users}', 'info')

            if test_config.architecture == 'pd':
                log_to_ui(f'   Prefill pods: {test_config.prefill_replicas}', 'info')
                log_to_ui(f'   Decode pods: {test_config.decode_replicas}', 'info')
                log_to_ui(f'   Ratio: {test_config.prefill_decode_ratio}', 'info')
            elif test_config.architecture == 'ep':
                log_to_ui(f'   Expert pods: {test_config.replicas}', 'info')
            else:  # aggregated
                log_to_ui(f'   Replicas: {test_config.replicas}', 'info')

            log_to_ui('', 'info')

            # Create test result tracker
            test_result = TestResult(
                test_id=test_config.test_id,
                architecture=test_config.architecture,
                deployment_success=False,
                deployment_ready=False,
                guidellm_success=False,
                metrics_collected=False,
                deployment_start_time=datetime.now().isoformat()
            )

            try:
                # Convert TestConfig to dict for DeploymentOrchestrator
                deployment_config = {
                    'test_id': test_config.test_id,
                    'namespace': test_config.namespace,
                    'architecture': test_config.architecture,
                    'model_name': test_config.model_name,
                    'image': test_config.image,
                    'tensor_parallelism': test_config.tensor_parallelism,
                    'pvc_name': test_config.pvc_name,
                    'gpu_memory_utilization': test_config.gpu_memory_utilization,
                    'max_model_len': test_config.max_model_len,
                    'kv_connector': test_config.kv_connector,
                    'memory_request': test_config.memory_request,
                    'memory_limit': test_config.memory_limit,
                    'cpu_request': getattr(test_config, 'cpu_request', '32'),
                    'cpu_limit': getattr(test_config, 'cpu_limit', None),
                    'extra_env': {
                        'INPUT_SEQUENCE_LENGTH': str(test_config.isl),
                        'OUTPUT_SEQUENCE_LENGTH': str(test_config.osl),
                        'NUM_CONCURRENT_USERS': str(test_config.num_users)
                    }
                }

                # Add architecture-specific fields
                if test_config.architecture == 'pd':
                    deployment_config['prefill_pods'] = test_config.prefill_replicas
                    deployment_config['decode_pods'] = test_config.decode_replicas
                    deployment_config['prefill_tp'] = test_config.tensor_parallelism
                    deployment_config['decode_tp'] = test_config.decode_tp or test_config.tensor_parallelism
                elif test_config.architecture == 'ep':
                    deployment_config['ep_pods'] = test_config.replicas
                else:  # aggregated
                    deployment_config['agg_pods'] = test_config.replicas

                # Deploy using new orchestrator (handles provider detection, network setup, prerequisites)
                log_to_ui(f'🚀 Deploying {test_config.architecture} configuration...', 'info')
                log_to_ui('   This includes waiting for all pods to be ready...', 'info')
                deployed_test_id = deployment_orchestrator.deploy(deployment_config)

                test_result.deployment_success = True
                test_result.deployment_ready = True
                test_result.deployment_end_time = datetime.now().isoformat()
                log_to_ui('✅ Deployment successful and all pods are ready!', 'success')

                # Patch services with InferencePool selector labels (required for Istio gateway routing)
                log_to_ui('🏷️  Patching services for InferencePool discovery...', 'info')

                # Determine kubectl command
                kubectl_cmd = 'oc'
                try:
                    subprocess.run(['oc', 'version'], capture_output=True, timeout=5, check=True)
                except (FileNotFoundError, subprocess.CalledProcessError):
                    kubectl_cmd = 'kubectl'

                try:
                    if test_config.architecture == 'pd':
                        # Patch both prefill and decode services
                        for role in ['prefill', 'decode']:
                            svc_name = f'{deployed_test_id}-{role}'
                            patch_cmd = [
                                kubectl_cmd, 'patch', 'svc', svc_name, '-n', test_config.namespace,
                                '-p', f'{{"metadata":{{"labels":{{"llm-d.ai/inference-serving":"true","llm-d.ai/role":"{role}"}}}}}}'
                            ]
                            result = subprocess.run(patch_cmd, capture_output=True, timeout=10)
                            if result.returncode == 0:
                                log_to_ui(f'   ✓ Patched {svc_name} service', 'success')
                            else:
                                log_to_ui(f'   ⚠️  Could not patch {svc_name}: {result.stderr.decode()[:100]}', 'warning')
                    else:
                        # For EP/aggregated, patch the main service
                        svc_name = deployed_test_id
                        patch_cmd = [
                            kubectl_cmd, 'patch', 'svc', svc_name, '-n', test_config.namespace,
                            '-p', '{"metadata":{"labels":{"llm-d.ai/inference-serving":"true"}}}'
                        ]
                        result = subprocess.run(patch_cmd, capture_output=True, timeout=10)
                        if result.returncode == 0:
                            log_to_ui(f'   ✓ Patched {svc_name} service', 'success')
                except Exception as e:
                    log_to_ui(f'   ⚠️  Service patching failed: {str(e)[:100]}', 'warning')

                # Validate inference endpoint is serving
                # Wait for vLLM to finish loading model in ALL pods
                log_to_ui('', 'info')
                log_to_ui('🧪 Waiting for vLLM to finish loading model...', 'info')

                inference_validated = False
                import time
                import subprocess

                # Get all pod IPs for this deployment
                try:
                    # Get pods by test-id label
                    get_pods_cmd = [
                        kubectl_cmd, 'get', 'pods', '-n', test_config.namespace,
                        '-l', f'test-id={deployed_test_id}',
                        '-o', 'jsonpath={range .items[*]}{.metadata.name}:{.status.podIP}{"\\n"}{end}'
                    ]
                    result = subprocess.run(get_pods_cmd, capture_output=True, timeout=10, check=True)
                    pod_info = []
                    for line in result.stdout.decode().strip().split('\n'):
                        if ':' in line:
                            name, ip = line.split(':')
                            pod_info.append({'name': name, 'ip': ip})

                    if not pod_info:
                        log_to_ui('⚠️ No pods found for validation', 'warning')
                        test_result.guidellm_success = False
                        test_result.error_message = "No pods found for inference validation"
                        log_to_ui('❌ Validation failed - no pods found', 'error')
                    else:
                        log_to_ui(f'   Found {len(pod_info)} pods to validate', 'info')

                        # Validate ALL pods (critical for PD deployments with many pods)
                        pods_to_test = pod_info
                        log_to_ui(f'   Validating all {len(pods_to_test)} pods...', 'info')

                        # Retry logic: try for up to 10 minutes
                        max_retries = 40  # 40 retries × 15 seconds = 10 minutes
                        retry_delay = 15  # seconds

                        all_pods_serving = False
                        last_progress = -1
                        for attempt in range(max_retries):
                            if attempt > 0:
                                time.sleep(retry_delay)

                            # Check pod status directly via Kubernetes API (no temporary pods)
                            status_cmd = [
                                kubectl_cmd, 'get', 'pods', '-n', test_config.namespace,
                                '-l', f'test-id={deployed_test_id}',
                                '-o', 'jsonpath={range .items[*]}{.metadata.name}:{.status.phase}:{.status.containerStatuses[0].ready}:{.status.containerStatuses[0].restartCount}{"\\n"}{end}'
                            ]

                            pods_ready = 0
                            pod_statuses = []

                            try:
                                result = subprocess.run(status_cmd, capture_output=True, timeout=10)
                                if result.returncode == 0:
                                    for line in result.stdout.decode().strip().split('\n'):
                                        if line and ':' in line:
                                            parts = line.split(':')
                                            if len(parts) >= 4:
                                                pod_name, phase, ready, _restart_count = parts[0], parts[1], parts[2], parts[3]

                                                # Pod is ready if: Running + container ready + no restarts
                                                if phase == 'Running' and ready == 'true':
                                                    # Check logs for "Application startup complete" to ensure vLLM is serving
                                                    log_cmd = [kubectl_cmd, 'logs', pod_name, '-n', test_config.namespace, '--tail=50']
                                                    log_result = subprocess.run(log_cmd, capture_output=True, timeout=10)

                                                    if log_result.returncode == 0:
                                                        logs = log_result.stdout.decode()
                                                        if 'Application startup complete' in logs:
                                                            pods_ready += 1
                                                            pod_statuses.append(f"{pod_name[:20]}: ✓")
                                                        else:
                                                            pod_statuses.append(f"{pod_name[:20]}: starting")
                                                    else:
                                                        pod_statuses.append(f"{pod_name[:20]}: log check failed")
                                                elif phase == 'Running' and ready == 'false':
                                                    pod_statuses.append(f"{pod_name[:20]}: not ready")
                                                elif phase != 'Running':
                                                    pod_statuses.append(f"{pod_name[:20]}: {phase}")
                                            else:
                                                # Fallback if containerStatuses not available yet
                                                pod_statuses.append("pod: initializing")
                            except Exception as e:
                                log_to_ui(f'⚠️  Status check error: {str(e)[:100]}', 'warning')

                            if pods_ready == len(pods_to_test):
                                all_pods_serving = True
                                log_to_ui(f'✅ All {pods_ready}/{len(pods_to_test)} pods are serving!', 'success')
                                break
                            else:
                                # Calculate progress percentage
                                progress = int((pods_ready / len(pods_to_test)) * 100)
                                elapsed = attempt * retry_delay

                                # Only log every 3rd attempt to reduce spam, or when progress changes
                                if attempt % 3 == 0 or progress != last_progress or attempt == 0:
                                    log_to_ui(f'   [{progress}%] {pods_ready}/{len(pods_to_test)} pods ready - {elapsed}s elapsed', 'info')
                                    # Show failed/loading pods only (not all pods, too verbose)
                                    failed = [s for s in pod_statuses if '✓' not in s]
                                    if failed and len(failed) <= 5:
                                        for status in failed:
                                            log_to_ui(f'      {status}', 'info')
                                    elif failed:
                                        log_to_ui(f'      {len(failed)} pods still loading...', 'info')
                                    last_progress = progress

                        if all_pods_serving:
                            # Pods are validated (Running + Ready + Application startup complete)
                            inference_validated = True
                            log_to_ui('✅ All pods validated - vLLM application started successfully!', 'success')
                        else:
                            log_to_ui(f'⏱️ Timeout: Pods did not finish loading model after {max_retries * retry_delay}s', 'error')

                except Exception as e:
                    log_to_ui(f'❌ Validation error: {str(e)[:200]}', 'error')
                    import traceback
                    traceback.print_exc()

                # Mark test as successful only if inference validated
                if not inference_validated:
                    test_result.guidellm_success = False
                    test_result.error_message = "Inference endpoint validation failed - model did not load"
                    log_to_ui('❌ Validation failed - model not serving properly', 'error')
                    log_to_ui('🛑 Stopping test - deployment not healthy', 'error')
                else:
                    log_to_ui('✅ Validation complete - all pods serving inference!', 'success')

                    # Run benchmark with crash detection using core module
                    log_to_ui('', 'info')

                    # Create test orchestrator instance
                    test_orchestrator = TestOrchestrator(
                        namespace=test_config.namespace,
                        deployment_timeout=3600,
                        test_duration=test_duration
                    )

                    # Run guidellm test with Istio gateway discovery, pod monitoring, and metrics collection
                    benchmark_success, result_file, metrics_file = test_orchestrator._run_guidellm_test(
                        endpoint=None,  # Auto-discover Istio gateway
                        config=test_config,
                        log_callback=log_to_ui,
                        monitor_pods=True,
                        expected_pod_count=len(pod_info),
                        collect_metrics=True  # Collect Prometheus/Thanos metrics
                    )

                    # Update test result
                    test_result.guidellm_success = benchmark_success
                    if metrics_file:
                        test_result.metrics_collected = True

                    if benchmark_success and result_file:
                        log_to_ui(f'   Results saved to: {result_file}', 'info')

                        # Parse guidellm results and extract metrics
                        try:
                            import json as _json3
                            with open(result_file, 'r') as f:
                                results = _json3.load(f)
                                if 'benchmarks' in results and len(results['benchmarks']) > 0:
                                    bench = results['benchmarks'][0]
                                    if 'summary' in bench:
                                        summary = bench['summary']

                                        # Extract TTFT percentiles (time_to_first_token in ms)
                                        if 'time_to_first_token' in summary:
                                            ttft = summary['time_to_first_token']
                                            test_result.ttft_p50 = ttft.get('p50')
                                            test_result.ttft_p90 = ttft.get('p90')
                                            test_result.ttft_p95 = ttft.get('p95')
                                            test_result.ttft_p99 = ttft.get('p99')

                                        # Extract ITL percentiles (inter_token_latency in ms)
                                        if 'inter_token_latency' in summary:
                                            itl = summary['inter_token_latency']
                                            test_result.itl_p50 = itl.get('p50')
                                            test_result.itl_p90 = itl.get('p90')
                                            test_result.itl_p95 = itl.get('p95')
                                            test_result.itl_p99 = itl.get('p99')

                                        # Extract throughput percentiles (req/s)
                                        if 'throughput' in summary:
                                            # guidellm may provide throughput as a single value or percentiles
                                            throughput_data = summary['throughput']
                                            if isinstance(throughput_data, dict):
                                                # If throughput has percentiles
                                                test_result.throughput_p50 = throughput_data.get('p50')
                                                test_result.throughput_p90 = throughput_data.get('p90')
                                                test_result.throughput_p95 = throughput_data.get('p95')
                                                test_result.throughput_p99 = throughput_data.get('p99')
                                            else:
                                                # If throughput is a single value (mean), store as p50
                                                test_result.throughput_p50 = throughput_data

                                        # Log summary
                                        log_to_ui(f'   Requests: {summary.get("total_count", "N/A")}', 'info')
                                        log_to_ui(f'   Success: {summary.get("success_count", "N/A")}', 'info')
                                        log_to_ui(f'   Errors: {summary.get("error_count", "N/A")}', 'info')
                                        if test_result.throughput_p50:
                                            log_to_ui(f'   Throughput p50: {test_result.throughput_p50:.2f} req/s', 'info')
                                            if test_result.throughput_p90:
                                                log_to_ui(f'   Throughput p90: {test_result.throughput_p90:.2f} req/s', 'info')
                                        if test_result.ttft_p50:
                                            log_to_ui(f'   TTFT p50: {test_result.ttft_p50:.2f}ms, p90: {test_result.ttft_p90:.2f}ms', 'info')
                                        if test_result.itl_p50:
                                            log_to_ui(f'   ITL p50: {test_result.itl_p50:.2f}ms, p90: {test_result.itl_p90:.2f}ms', 'info')
                        except Exception as e:
                            log_to_ui(f'   (Could not parse guidellm results: {str(e)[:50]})', 'info')

                    # Parse metrics file and extract GPU utilization
                    if metrics_file and os.path.exists(metrics_file):
                        try:
                            with open(metrics_file, 'r') as f:
                                metrics_data = _json3.load(f)
                                # Store raw JSON content for database
                                test_result.metrics_json_content = _json3.dumps(metrics_data)

                                # Extract GPU utilization (average across all GPUs)
                                if 'metrics' in metrics_data and 'gpu' in metrics_data['metrics']:
                                    gpu_metrics = metrics_data['metrics']['gpu']
                                    # Look for average utilization across all samples
                                    utilizations = []
                                    for gpu_data in gpu_metrics:
                                        if 'values' in gpu_data and len(gpu_data['values']) > 0:
                                            # Each value is [timestamp, utilization_percentage]
                                            for val in gpu_data['values']:
                                                if len(val) >= 2 and val[1] is not None:
                                                    utilizations.append(float(val[1]))

                                    if utilizations:
                                        test_result.gpu_utilization = sum(utilizations) / len(utilizations)
                                        log_to_ui(f'   GPU utilization: {test_result.gpu_utilization:.1f}%', 'info')

                                # Extract KV cache usage (average across all samples)
                                if 'metrics' in metrics_data and 'vllm' in metrics_data['metrics']:
                                    vllm_metrics = metrics_data['metrics']['vllm']
                                    # Look for kv_cache_usage_perc metric
                                    kv_cache_key = f'vllm:kv_cache_usage_perc{{namespace="{TARGET_NAMESPACE}"}}'
                                    if kv_cache_key in vllm_metrics:
                                        kv_cache_data = vllm_metrics[kv_cache_key]
                                        kv_values = []
                                        if 'prefill' in kv_cache_data:
                                            for val in kv_cache_data['prefill']:
                                                if 'values' in val and len(val['values']) > 0:
                                                    for v in val['values']:
                                                        if len(v) >= 2 and v[1] is not None:
                                                            kv_values.append(float(v[1]))
                                        if 'decode' in kv_cache_data:
                                            for val in kv_cache_data['decode']:
                                                if 'values' in val and len(val['values']) > 0:
                                                    for v in val['values']:
                                                        if len(v) >= 2 and v[1] is not None:
                                                            kv_values.append(float(v[1]))

                                        if kv_values:
                                            test_result.kv_cache_usage = sum(kv_values) / len(kv_values)
                                            log_to_ui(f'   KV cache usage: {test_result.kv_cache_usage:.1f}%', 'info')
                        except Exception as e:
                            log_to_ui(f'   (Could not parse metrics file: {str(e)[:50]})', 'info')
                    else:
                        if not test_result.error_message:
                            test_result.error_message = "Benchmark failed"
                        log_to_ui(f'❌ Benchmark failed: {test_result.error_message}', 'error')

            except Exception as e:
                test_result.deployment_success = False
                test_result.error_message = str(e)
                log_to_ui(f'❌ Deployment failed: {str(e)}', 'error')
                import traceback
                tb_str = ''.join(traceback.format_exception(type(e), e, e.__traceback__))
                print(f"Deployment traceback:\n{tb_str}")
                # Log first few lines of traceback to UI for debugging
                tb_lines = tb_str.split('\n')
                for line in tb_lines[-10:]:  # Last 10 lines
                    if line.strip():
                        print(line)
                traceback.print_exc()

            # Store result with config reference for later database storage
            results.append((test_config, test_result))

            # Cleanup deployment (always cleanup, whether success or failure)
            log_to_ui('🧹 Cleaning up deployment...', 'info')
            cleanup_mgr = CleanupManager(namespace=TARGET_NAMESPACE)
            cleanup_mgr.cleanup_test(test_config.test_id)
            log_to_ui('✅ Cleanup complete', 'success')

            # Log test result
            if test_result.deployment_success:
                log_to_ui(f'✅ Test {idx} completed successfully', 'success')
            else:
                log_to_ui(f'⚠️  Test {idx} failed: {test_result.error_message}', 'warning')

        log_to_ui('', 'info')
        log_to_ui(f'✅ All {len(results)} tests completed', 'success')

        # Step 3: Store results in database
        log_to_ui("\n💾 Step 3: Storing results in database...", 'info')

        # Prepare batch insert data
        test_config_rows = []
        for test_config, test_result in results:
            # Use test_config fields instead of parsing test_id
            tp = test_config.tensor_parallelism

            # Determine config name based on architecture
            # Include workload (ISL/OSL) to make name unique
            if test_config.architecture == 'aggregated':
                config_name = f"aggregated-tp{tp}-isl{test_config.isl}-osl{test_config.osl}"
                prefill_pods = 0
                decode_pods = 0
            elif test_config.architecture == 'pd':
                config_name = f"pd-{test_config.prefill_decode_ratio}-tp{tp}-isl{test_config.isl}-osl{test_config.osl}"
                prefill_pods = test_config.prefill_replicas
                decode_pods = test_config.decode_replicas
            else:  # ep
                config_name = f"ep-tp{tp}-isl{test_config.isl}-osl{test_config.osl}"
                prefill_pods = 0
                decode_pods = 0

            test_config_rows.append((
                run_id,
                config_name,
                prefill_pods,
                decode_pods,
                tp,
                'completed' if test_result.guidellm_success else 'failed',
                test_result.ttft_p50,
                test_result.ttft_p90,
                test_result.ttft_p95,
                test_result.ttft_p99,
                test_result.itl_p50,
                test_result.itl_p90,
                test_result.itl_p95,
                test_result.itl_p99,
                test_result.throughput_p50,
                test_result.throughput_p90,
                test_result.throughput_p95,
                test_result.throughput_p99,
                test_result.gpu_utilization,
                test_result.kv_cache_usage,
                test_result.test_start_time,
                test_result.test_end_time,
                test_result.metrics_json_content
            ))

        with get_db() as conn:
            cursor = conn.cursor()

            # Batch insert test configurations
            cursor.executemany('''
                INSERT INTO test_configurations
                (run_id, config_name, prefill_pods, decode_pods, tensor_parallelism,
                 status, ttft_p50, ttft_p90, ttft_p95, ttft_p99,
                 itl_p50, itl_p90, itl_p95, itl_p99,
                 throughput_p50, throughput_p90, throughput_p95, throughput_p99,
                 gpu_utilization, kv_cache_usage, started_at, completed_at, metrics_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', test_config_rows)

            # Update run status
            cursor.execute('''
                UPDATE optimization_runs
                SET status = ?, completed_at = ?
                WHERE id = ?
            ''', ('completed', datetime.now().isoformat(), run_id))

        log_to_ui("\n✅ Optimization run completed successfully!", 'success')
        log_to_ui(f"Results saved to database (run_id: {run_id})", 'success')

    except Exception as e:
        error_msg = f"Optimization failed: {str(e)}"
        log_to_ui(f"\n❌ {error_msg}", 'error')
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()

        # Update database if run was created
        if run_id:
            try:
                with get_db() as conn:
                    cursor = conn.cursor()
                    cursor.execute('''
                        UPDATE optimization_runs
                        SET status = ?, completed_at = ?, notes = ?
                        WHERE id = ?
                    ''', ('failed', datetime.now().isoformat(), error_msg, run_id))
            except Exception:
                pass

    finally:
        with state_lock:
            OPTIMIZATION_RUNNING = False
            save_state()

            # Update database to reflect optimization finished
            try:
                with get_db() as conn:
                    conn.execute('''
                        UPDATE ui_session_state
                        SET optimization_running = 0,
                            updated_at = ?
                        WHERE id = 1
                    ''', (datetime.now().isoformat(),))
            except Exception as e:
                print(f"Warning: Failed to update optimization_running in database: {e}")

        socketio.emit('status_update', {'running': False, 'message': 'Optimization finished'})

# --- SocketIO Events ---

@socketio.on('connect')
def handle_connect():
    """Handle client connection — enforce single active UI tab."""
    global _active_ui_session
    from flask import request as flask_request
    sid = flask_request.sid
    username = session.get('user', 'unknown')

    tab_id = flask_request.args.get('tab_id', '')

    if _active_ui_session and _active_ui_session['sid'] != sid:
        # Same tab reconnecting after socket drop — just update the SID
        if tab_id and _active_ui_session.get('tab_id') == tab_id:
            _active_ui_session['sid'] = sid
            print(f'Client {sid} ({username}) reconnected same tab')
            return

        emit('session_locked', {
            'username': _active_ui_session['username'],
            'connected_at': _active_ui_session['connected_at'],
        })
        print(f'Client {sid} ({username}) blocked — UI in use by {_active_ui_session["username"]}')
        return

    _active_ui_session = {
        'sid': sid,
        'tab_id': tab_id,
        'username': username,
        'connected_at': datetime.now().strftime('%H:%M:%S'),
    }
    print(f'Client {sid} ({username}) is now the active UI session')
    _replay_state_to_client()


def _replay_state_to_client():
    """Send current optimization status and recent logs to the calling client."""
    optimization_running = OPTIMIZATION_RUNNING
    try:
        with get_db() as conn:
            row = conn.execute('''
                SELECT optimization_running
                FROM ui_session_state
                WHERE id = 1
            ''').fetchone()
            if row:
                optimization_running = bool(row['optimization_running'])
    except Exception as e:
        print(f"Warning: Could not load optimization_running from database: {e}")

    emit('status_update', {
        'running': optimization_running,
        'config': CURRENT_CONFIG
    })

    try:
        with get_db() as conn:
            logs = conn.execute(
                '''SELECT timestamp, log_type, message
                   FROM console_logs
                   ORDER BY id DESC
                   LIMIT 100'''
            ).fetchall()
            for log in reversed(logs):
                emit('console_log', {
                    'type': log['log_type'],
                    'message': log['message'],
                    'replayed': True,
                })
            if len(logs) > 0:
                emit('console_log', {
                    'type': 'info',
                    'message': f'Replayed {len(logs)} recent log messages',
                    'replayed': False,
                })
    except Exception as e:
        print(f"Warning: Could not replay logs: {e}")


@socketio.on('take_over')
def handle_take_over():
    """New client takes over — kick old session, keep optimization running."""
    global _active_ui_session
    from flask import request as flask_request
    sid = flask_request.sid
    username = session.get('user', 'unknown')

    tab_id = flask_request.args.get('tab_id', '')

    if _active_ui_session and _active_ui_session['sid'] != sid:
        old_sid = _active_ui_session['sid']
        old_user = _active_ui_session['username']
        socketio.emit('session_kicked', {
            'taken_by': username,
        }, to=old_sid)
        try:
            socketio.server.disconnect(old_sid, namespace='/')
        except Exception:
            pass
        print(f'Session takeover: {username} kicked {old_user}')

    _active_ui_session = {
        'sid': sid,
        'tab_id': tab_id,
        'username': username,
        'connected_at': datetime.now().strftime('%H:%M:%S'),
    }
    emit('session_granted')


@socketio.on('disconnect')
def handle_disconnect():
    """Clear active session if the disconnecting client was the active one."""
    global _active_ui_session
    from flask import request as flask_request
    sid = flask_request.sid
    if _active_ui_session and _active_ui_session['sid'] == sid:
        print(f'Active UI session disconnected ({_active_ui_session["username"]})')
        _active_ui_session = None
    else:
        print(f'Non-active client disconnected ({sid})')

@socketio.on('save_config')
def handle_save_config(data):
    """Save UI configuration and step to database.

    Args:
        data: Dictionary containing:
            - config: User configuration (model, isl, osl, etc.)
            - current_step: Current wizard step (0-4)
    """
    try:
        config = data.get('config', {})
        current_step = data.get('current_step', 0)

        with get_db() as conn:
            conn.execute('''
                UPDATE ui_session_state
                SET config_json = ?,
                    current_step = ?,
                    updated_at = ?
                WHERE id = 1
            ''', (json.dumps(config), current_step, datetime.now().isoformat()))

        # Broadcast config update to all connected clients (except sender)
        socketio.emit('config_updated', {
            'config': config,
            'current_step': current_step
        }, skip_sid=request.sid)

        emit('save_config_result', {'success': True})

    except Exception as e:
        print(f"Error saving config: {e}")
        emit('save_config_result', {'success': False, 'error': str(e)})

@socketio.on('load_config')
def handle_load_config():
    """Load UI configuration and step from database."""
    try:
        with get_db() as conn:
            row = conn.execute('''
                SELECT config_json, current_step, optimization_running
                FROM ui_session_state
                WHERE id = 1
            ''').fetchone()

            # Check if there's a running optimization
            running_opt = conn.execute('''
                SELECT id FROM optimization_runs
                WHERE status = 'running'
                LIMIT 1
            ''').fetchone()

            is_running = bool(running_opt)

            if row:
                config = json.loads(row['config_json']) if row['config_json'] else {}
                current_step = row['current_step']

                # If there's a running optimization, force step to 6 (monitoring)
                if is_running:
                    current_step = 6

                emit('load_config_result', {
                    'success': True,
                    'config': config,
                    'current_step': current_step,
                    'optimization_running': is_running,
                    'namespace': TARGET_NAMESPACE
                })
            else:
                # No saved session - check if there's a running optimization
                current_step = 6 if is_running else 1

                emit('load_config_result', {
                    'success': True,
                    'config': {},
                    'current_step': current_step,
                    'optimization_running': is_running,
                    'namespace': TARGET_NAMESPACE
                })

            # Emit button state update to ensure UI is in sync
            emit('status_update', {
                'running': is_running,
                'message': 'Optimization running' if is_running else 'Ready to start'
            })

    except Exception as e:
        print(f"Error loading config: {e}")
        emit('load_config_result', {'success': False, 'error': str(e)})

@socketio.on('start_optimization')
def handle_start_optimization(data):
    """Start an optimization run."""
    global OPTIMIZATION_RUNNING, CURRENT_CONFIG, CURRENT_TEST_PLAN

    with state_lock:
        if OPTIMIZATION_RUNNING:
            emit('error', {'message': 'Optimization already running'})
            return

        # Validate that test plan exists and is ready
        if not CURRENT_TEST_PLAN or not CURRENT_TEST_PLAN.can_proceed:
            error_msg = 'Cannot start: No valid test plan. Please generate a test plan first.'
            if CURRENT_TEST_PLAN and CURRENT_TEST_PLAN.error_message:
                error_msg = f'Cannot start: {CURRENT_TEST_PLAN.error_message}'
            log_to_ui(f'❌ {error_msg}', 'error')
            emit('error', {'message': error_msg})
            return

        OPTIMIZATION_RUNNING = True
        CURRENT_CONFIG = data
        save_state()

        # Update database to reflect optimization running
        try:
            with get_db() as conn:
                conn.execute('''
                    UPDATE ui_session_state
                    SET optimization_running = 1,
                        updated_at = ?
                    WHERE id = 1
                ''', (datetime.now().isoformat(),))
        except Exception as e:
            print(f"Warning: Failed to update optimization_running in database: {e}")

    # Broadcast to all clients that optimization started
    socketio.emit('status_update', {'running': True, 'message': 'Optimization started'})

    # Start optimization in background greenlet
    spawn(run_optimization_background, data)

@socketio.on('resume_optimization')
def handle_resume_optimization(data):
    """Resume a previous optimization run directly from DB state."""
    global OPTIMIZATION_RUNNING

    with state_lock:
        if OPTIMIZATION_RUNNING:
            emit('error', {'message': 'Optimization already running. Stop it first before resuming another run.'})
            return

    run_id = data.get('run_id')
    if not run_id:
        emit('error', {'message': 'No run_id provided for resume'})
        return

    try:
        # Load run info from database
        with get_db() as conn:
            run = conn.execute(
                'SELECT * FROM optimization_runs WHERE id = ?', (run_id,)
            ).fetchone()

        if not run:
            log_to_ui(f'❌ Run #{run_id} not found in database', 'error')
            emit('status_update', {'running': False})
            return

        run = dict(run)

        # Set optimization as running
        with state_lock:
            OPTIMIZATION_RUNNING = True
            save_state()

        # Update database to reflect optimization running
        try:
            with get_db() as conn:
                conn.execute('''
                    UPDATE ui_session_state
                    SET optimization_running = 1,
                        updated_at = ?
                    WHERE id = 1
                ''', (datetime.now().isoformat(),))
        except Exception as e:
            print(f"Warning: Failed to update optimization_running in database: {e}")

        socketio.emit('status_update', {'running': True, 'message': 'Optimization resumed'})

        log_to_ui(f'📦 Resuming optimization run #{run_id}...', 'info')
        log_to_ui(f'   Model: {run["model"]}', 'info')
        isl_str = f'ISL: {run["isl"]}' + (f' (σ={run["isl_stdev"]})' if run.get("isl_stdev") else '')
        osl_str = f'OSL: {run["osl"]}' + (f' (σ={run["osl_stdev"]})' if run.get("osl_stdev") else '')
        turns_str = f', Turns: {run["turns"]}' if run.get("turns", 1) > 1 else ''
        log_to_ui(f'   {isl_str}, {osl_str}, Users: {run["num_users"]}{turns_str}', 'info')
        if run.get('workload_mode') == 'dataset' and run.get('dataset_source'):
            log_to_ui(f'   Dataset: {run["dataset_source"]}', 'info')

        # Restore selected_nodes and advanced settings from saved config_json
        saved_selected_nodes = []
        saved_advanced_vllm = None
        if run.get('config_json'):
            try:
                import json as _json
                saved_cfg = _json.loads(run['config_json'])
                saved_selected_nodes = saved_cfg.get('selected_nodes', [])
                saved_advanced_vllm = saved_cfg.get('advanced_vllm')
            except Exception:
                pass

        # Read dataset fields from dedicated columns (fall back to config_json for old runs)
        saved_workload_mode = run.get('workload_mode') or 'synthetic'
        saved_dataset_source = run.get('dataset_source')
        saved_dataset_column = run.get('dataset_column')
        saved_dataset_max_output = run.get('dataset_max_output') or 256

        # Build optimization data from DB run record
        optimization_data = {
            'model': run['model'],
            'isl': run['isl'],
            'osl': run['osl'],
            'isl_stdev': run.get('isl_stdev'),
            'osl_stdev': run.get('osl_stdev'),
            'turns': run.get('turns', 1),
            'num_users': run['num_users'],
            'optimization_metric': run.get('goal') or 'ttft',
            'max_test_duration': run.get('test_duration') or 300,
            'stop_mode': run.get('stop_mode', 'duration'),
            'max_requests': run.get('max_requests'),
            'hf_token': data.get('hf_token'),
            'max_gpus': run.get('max_gpus') or 16,
            'use_achievable_qps': bool(run.get('use_achievable_qps', 0)),
            'selected_nodes': saved_selected_nodes,
            'workload_mode': saved_workload_mode,
            'dataset_source': saved_dataset_source,
            'dataset_column': saved_dataset_column,
            'dataset_max_output': saved_dataset_max_output,
            'rate_type': run.get('rate_type') or 'concurrent',
            'latency_constraint_enabled': bool(run.get('latency_constraint_enabled', 0)),
            'latency_constraint_ms': run.get('latency_constraint_ms', 500),
            'latency_constraint_percentile': run.get('latency_constraint_percentile', 'p90'),
            'advanced_vllm': saved_advanced_vllm,
            'resume_run_id': run_id
        }

        # Start optimization in background
        spawn(run_optimization_background, optimization_data)

    except Exception as e:
        log_to_ui(f'❌ Failed to resume run #{run_id}: {str(e)}', 'error')
        with state_lock:
            OPTIMIZATION_RUNNING = False
            save_state()
        emit('status_update', {'running': False})


@socketio.on('stop_optimization')
def handle_stop_optimization():
    """Stop the running optimization."""
    global OPTIMIZATION_RUNNING

    with state_lock:
        if not OPTIMIZATION_RUNNING:
            emit('error', {'message': 'No optimization running'})
            return

        OPTIMIZATION_RUNNING = False
        save_state()

        # Update database to reflect optimization stopped
        try:
            with get_db() as conn:
                conn.execute('''
                    UPDATE ui_session_state
                    SET optimization_running = 0,
                        updated_at = ?
                    WHERE id = 1
                ''', (datetime.now().isoformat(),))
        except Exception as e:
            print(f"Warning: Failed to update optimization_running in database: {e}")

    # Broadcast to all clients that optimization stopped
    socketio.emit('status_update', {'running': False, 'message': 'Optimization stopped'})
    socketio.emit('console_log', {'type': 'warning', 'message': '🛑 Optimization stopped by user'})

@socketio.on('cleanup_deployment')
def handle_cleanup_deployment(data):
    """Clean up last deployed test configuration."""
    try:
        log_to_ui('🧹 Checking for deployments to clean up...', 'info')

        # Get resumable run to find what to clean up
        run = get_resumable_run()
        if not run:
            log_to_ui('ℹ️  No previous deployments found to clean up', 'info')
            emit('cleanup_result', {'success': True, 'message': 'Nothing to clean up'})
            return

        log_to_ui(f'✅ Found deployment to clean up: {run["run_name"]}', 'success')

        # Get pods to clean up
        pods_to_cleanup = []
        if run['pods_deployed']:
            pods_to_cleanup = json.loads(run['pods_deployed'])
            log_to_ui(f'   Pods to remove: {len(pods_to_cleanup)}', 'info')

        if run['last_deployed_config']:
            config_dict = json.loads(run['last_deployed_config'])
            log_to_ui(f'   Configuration: {config_dict.get("architecture")} (TP={config_dict.get("tensor_parallelism")})', 'info')

        # Perform cleanup
        from core import CleanupManager
        cleanup_mgr = CleanupManager(namespace=TARGET_NAMESPACE)

        def cleanup_log(msg):
            log_to_ui(f'   {msg}', 'info')

        if pods_to_cleanup:
            success = cleanup_mgr.cleanup_last_deployment(pods_to_cleanup, log_callback=cleanup_log)
        else:
            # No specific pods, try cleaning up all In-S8 test deployments
            success = cleanup_mgr.cleanup_all_test_deployments(log_callback=cleanup_log)

        if success:
            # Update database to mark as cleaned up
            with get_db() as conn:
                conn.execute('''
                    UPDATE optimization_runs
                    SET deployment_status = 'cleaned_up',
                        pods_deployed = NULL
                    WHERE id = ?
                ''', (run['id'],))

            log_to_ui('', 'info')
            log_to_ui('✅ Cleanup completed successfully!', 'success')
            emit('cleanup_result', {'success': True, 'message': 'Cleanup successful'})
        else:
            log_to_ui('⚠️  Cleanup completed with warnings', 'warning')
            emit('cleanup_result', {'success': False, 'error': 'Cleanup had warnings'})

    except Exception as e:
        error_msg = f"Cleanup failed: {str(e)}"
        log_to_ui(f'❌ {error_msg}', 'error')
        import traceback
        traceback.print_exc()
        emit('cleanup_result', {'success': False, 'error': error_msg})


@socketio.on('scan_cluster')
def handle_scan_cluster(data):
    """Scan cluster resources (GPUs, CPUs, RAM, storage classes)."""
    try:
        log_to_ui('🔍 Scanning cluster resources...', 'info')

        # Initialize system scanner
        scanner = SystemScanner(namespace=TARGET_NAMESPACE)

        # Scan cluster
        resources = scanner.scan_cluster()

        # Calculate currently used GPUs
        gpus_in_use = 0
        try:
            import json
            pods_result = scanner.kubectl.run(['get', 'pods', '--all-namespaces', '-o', 'json'], check=False)
            if pods_result.returncode == 0:
                pods_data = json.loads(pods_result.stdout)
                for pod in pods_data.get('items', []):
                    # Only count Running pods (skip Pending, Succeeded, Failed, etc.)
                    if pod.get('status', {}).get('phase') != 'Running':
                        continue
                    # Sum GPU requests from all containers
                    for container in pod.get('spec', {}).get('containers', []):
                        container_resources = container.get('resources', {})
                        if container_resources:
                            requests = container_resources.get('requests', {})
                            if requests and 'nvidia.com/gpu' in requests:
                                gpu_count = requests['nvidia.com/gpu']
                                # Handle both string and int values
                                if gpu_count and str(gpu_count) != '0':
                                    gpus_in_use += int(gpu_count)
        except Exception as e:
            log_to_ui(f'⚠️ Could not calculate GPU usage: {e}', 'warning')
            print(f"GPU usage calculation error: {e}")
            import traceback
            traceback.print_exc()

        # Detect provider and network type
        provider_name = 'unknown'
        network_type = 'nad'  # default
        dranet_available = False

        try:
            from core.providers import ProviderRegistry
            from core.web_deployer import NetworkIntegrator

            provider = ProviderRegistry.detect_provider(kubectl_runner=scanner.kubectl)
            provider_name = provider.get_provider_id()

            # Detect network type
            integrator = NetworkIntegrator(provider, scanner.kubectl)
            selected_network = integrator._select_network_type()
            network_type = selected_network.value
            dranet_available = (network_type == 'dra')

            log_to_ui(f'   Provider: {provider.get_display_name()}', 'info')
            log_to_ui(f'   Network type: {network_type.upper()}', 'info')
        except Exception as e:
            print(f"Could not detect provider/network: {e}")

        # Build per-node NIC details for database and UI
        nodes_detail = []
        all_nics_detail = []
        for node in resources.nodes:
            node_nics = []
            for nic in node.network_interfaces:
                nic_entry = {
                    'name': nic.name,
                    'type': nic.type,
                    'vendor': nic.vendor,
                    'model': nic.model,
                    'speed_gbps': nic.speed_gbps,
                    'count': nic.count,
                }
                node_nics.append(nic_entry)
                all_nics_detail.append({**nic_entry, 'node': node.name})
            nodes_detail.append({
                'name': node.name,
                'gpus': node.gpus,
                'gpu_vendor': node.gpu_vendor,
                'gpu_model': node.gpu_model,
                'gpu_memory_mb': node.gpu_memory_mb,
                'cpu_cores': node.cpu_cores,
                'cpu_model': node.cpu_model,
                'memory_gb': node.memory_gb,
                'has_rdma': node.has_rdma,
                'nics': node_nics
            })

        # Save hardware scan to database
        try:
            with get_db() as conn:
                conn.execute('''
                    INSERT INTO hardware_scans
                    (scan_timestamp, cloud_provider, node_count, gpu_node_count,
                     total_gpus, gpu_vendor, gpu_model, gpu_memory_per_gpu_mb,
                     total_gpu_memory_gb, max_gpus_per_node, total_cpu_cores,
                     total_memory_gb, cpu_model, host_model, has_rdma,
                     rdma_capable_nodes, total_nics, nodes_json, nics_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    datetime.now().isoformat(),
                    provider_name,
                    resources.node_count,
                    resources.gpu_node_count,
                    resources.total_gpus,
                    resources.gpu_vendor,
                    resources.gpu_model,
                    resources.gpu_memory_per_gpu_mb,
                    resources.total_gpu_memory_gb,
                    resources.max_gpus_per_node,
                    resources.total_cpu_cores,
                    resources.total_memory_gb,
                    resources.cpu_model,
                    resources.host_model,
                    1 if resources.has_rdma else 0,
                    resources.rdma_capable_nodes,
                    resources.total_network_interfaces,
                    json.dumps(nodes_detail),
                    json.dumps(all_nics_detail)
                ))
            log_to_ui('💾 Hardware scan saved to database', 'info')
        except Exception as db_err:
            log_to_ui(f'⚠️ Could not save hardware scan to database: {db_err}', 'warning')
            print(f"Hardware DB save error: {db_err}")

        # Convert to dict for JSON serialization
        result = {
            'total_gpus': resources.total_gpus,
            'gpus_in_use': gpus_in_use,
            'gpus_available': resources.total_gpus - gpus_in_use,
            'gpus_per_node': resources.gpus_per_node,
            'max_gpus_per_node': resources.max_gpus_per_node,
            'min_gpus_per_node': resources.min_gpus_per_node,
            'total_gpu_memory_gb': resources.total_gpu_memory_gb,
            'gpu_memory_per_gpu_mb': resources.gpu_memory_per_gpu_mb,
            'total_cpu_cores': resources.total_cpu_cores,
            'total_memory_gb': resources.total_memory_gb,
            'node_count': resources.node_count,
            'gpu_node_count': resources.gpu_node_count,
            'has_rdma': resources.has_rdma,
            'rdma_capable_nodes': resources.rdma_capable_nodes,
            'gpu_type': resources.gpu_type,
            'gpu_vendor': resources.gpu_vendor,
            'gpu_model': resources.gpu_model,
            'total_network_interfaces': resources.total_network_interfaces,
            'network_interfaces_by_type': resources.network_interfaces_by_type,
            'network_interfaces_by_vendor': resources.network_interfaces_by_vendor,
            'tp_options': resources.get_tp_options(),
            'cpu_model': resources.cpu_model,
            'host_model': resources.host_model,
            'nic_models': resources.nic_models if resources.nic_models else [],
            'nic_speeds': resources.nic_speeds if resources.nic_speeds else {},
            'nodes_detail': nodes_detail,
            'storage_classes': [
                {
                    'name': sc.name,
                    'provisioner': sc.provisioner,
                    'reclaim_policy': sc.reclaim_policy,
                    'volume_binding_mode': sc.volume_binding_mode,
                    'allow_volume_expansion': sc.allow_volume_expansion
                }
                for sc in resources.storage_classes
            ],
            # Provider and network information
            'provider': provider_name,
            'network_type': network_type,
            'dranet_available': dranet_available
        }

        emit('cluster_scan_result', result)
        log_to_ui('✅ Cluster scan complete', 'success')

    except Exception as e:
        error_msg = f"Cluster scan failed: {str(e)}"
        log_to_ui(f'❌ {error_msg}', 'error')
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
        emit('error', {'message': error_msg})

def read_config_from_pvc(pvc_name: str, model_name: str, namespace: Optional[str] = None) -> Optional[Dict]:
    """
    Read model config.json from existing PVC by deploying a temporary pod.

    Args:
        pvc_name: Name of the PVC containing the model
        model_name: HuggingFace model name (e.g., "Qwen/Qwen2.5-72B-Instruct")
        namespace: Kubernetes namespace

    Returns:
        Model config dict, or None if read fails
    """
    if namespace is None:
        namespace = TARGET_NAMESPACE
    import json
    import time
    from core.k8s_utils import KubectlRunner

    kubectl = KubectlRunner(namespace=namespace)
    pod_name = f"config-reader-{int(time.time())}"

    try:
        log_to_ui(f'📖 Reading model config from PVC: {pvc_name}', 'info')

        # Create temporary pod manifest
        pod_manifest = f"""
apiVersion: v1
kind: Pod
metadata:
  name: {pod_name}
  namespace: {namespace}
spec:
  restartPolicy: Never
  containers:
  - name: reader
    image: registry.access.redhat.com/ubi9/ubi:9.4
    command: ['sh', '-c', 'sleep 3600']
    volumeMounts:
    - name: model-storage
      mountPath: /models
      readOnly: true
  volumes:
  - name: model-storage
    persistentVolumeClaim:
      claimName: {pvc_name}
"""

        # Deploy pod
        result = kubectl.run(['apply', '-f', '-', '-n', namespace], input_data=pod_manifest)
        if result.returncode != 0:
            log_to_ui(f'❌ Failed to create config reader pod: {result.stderr}', 'error')
            return None

        log_to_ui('⏳ Waiting for config reader pod to be ready...', 'info')

        # Wait for pod to be ready (max 180 seconds)
        pod_ready = False
        for i in range(180):
            result = kubectl.run_json(['get', 'pod', pod_name, '-n', namespace])
            status = result.get('status', {})
            phase = status.get('phase')

            # Check container ready state, not just pod phase
            container_statuses = status.get('containerStatuses', [])
            if phase == 'Running' and container_statuses:
                if all(cs.get('ready', False) for cs in container_statuses):
                    log_to_ui('✅ Config reader pod ready', 'success')
                    pod_ready = True
                    break
            time.sleep(1)

        if not pod_ready:
            log_to_ui('❌ Config reader pod did not become ready', 'error')
            kubectl.run(['delete', 'pod', pod_name, '-n', namespace], check=False)
            return None

        # Give filesystem a moment to fully mount
        time.sleep(2)

        # First check what's in /models directory
        ls_result = kubectl.run(
            ['exec', pod_name, '-n', namespace, '--', 'ls', '-la', '/models'],
            check=False
        )
        if ls_result.returncode == 0:
            log_to_ui('📂 /models directory contents:', 'info')
            for line in ls_result.stdout.strip().split('\n')[:10]:  # Show first 10 items
                log_to_ui(f'   {line}', 'info')
        else:
            log_to_ui(f'⚠️  Could not list /models directory: {ls_result.stderr}', 'warning')

        # Search for config.json files in the PVC with retry logic
        find_result = None
        for attempt in range(3):
            find_result = kubectl.run(
                ['exec', pod_name, '-n', namespace, '--', 'find', '/models', '-name', 'config.json'],
                check=False
            )

            if find_result.returncode == 0 and find_result.stdout.strip():
                break

            if attempt < 2:
                log_to_ui(f'   Retry {attempt + 1}/3: Searching for config.json files...', 'info')
                time.sleep(2)

        if not find_result or find_result.returncode != 0 or not find_result.stdout.strip():
            log_to_ui('❌ No config.json files found in PVC', 'error')
            if find_result and find_result.stderr:
                log_to_ui(f'   Find error: {find_result.stderr}', 'error')
            kubectl.run(['delete', 'pod', pod_name, '-n', namespace], check=False)
            return None

        # Get all config.json paths
        all_config_paths = find_result.stdout.strip().split('\n')

        # Filter for paths containing the model name
        # Support both formats: "RedHatAI/Model-Name" and "RedHatAI--Model-Name"
        model_name_normalized = model_name.replace('/', '--')
        model_name_parts = model_name.split('/')

        matching_paths = []
        for path in all_config_paths:
            # Check if path contains model name in any format
            if (model_name in path or
                model_name_normalized in path or
                all(part in path for part in model_name_parts)):
                matching_paths.append(path)

        if not matching_paths:
            log_to_ui(f'⚠️  No config.json found for model "{model_name}"', 'warning')
            log_to_ui(f'   Available configs: {", ".join(all_config_paths)}', 'info')
            kubectl.run(['delete', 'pod', pod_name, '-n', namespace], check=False)
            return None

        # Use the first matching path
        config_path = matching_paths[0]
        log_to_ui(f'✅ Found config.json for {model_name}', 'success')

        # Read the config file
        config_content = None
        result = kubectl.run(
            ['exec', pod_name, '-n', namespace, '--', 'cat', config_path],
            check=False
        )
        if result.returncode == 0:
            config_content = result.stdout
            log_to_ui('✅ Successfully read config.json', 'success')
        else:
            log_to_ui(f'❌ Failed to read {config_path}', 'error')

        # Cleanup pod
        log_to_ui('🧹 Cleaning up config reader pod...', 'info')
        kubectl.run(['delete', 'pod', pod_name, '-n', namespace], check=False)

        if not config_content:
            log_to_ui('❌ Could not read config.json from PVC', 'error')
            return None

        # Parse JSON
        config = json.loads(config_content)
        log_to_ui('✅ Successfully read model config from PVC', 'success')
        return config

    except Exception as e:
        log_to_ui(f'❌ Failed to read config from PVC: {str(e)}', 'error')
        # Cleanup pod if it exists
        kubectl.run(['delete', 'pod', pod_name, '-n', namespace], check=False)
        return None

@socketio.on('generate_test_plan')
def handle_generate_test_plan(data):
    """Generate test plan based on model and resources."""
    global CURRENT_TEST_PLAN

    try:
        from core import TestPlanner
        from core.system_scanner import SystemScanner

        model = data.get('model')
        optimization_goal = data.get('optimization_goal')
        max_gpus = data.get('max_gpus')
        isl = data.get('isl', 2048)
        osl = data.get('osl', 512)
        num_users = data.get('num_users', 100)
        use_existing_pvc = data.get('use_existing_pvc', False)
        existing_pvc_name = data.get('existing_pvc_name')
        hf_token = data.get('hf_token')

        # Get cluster info to cap TP at max_gpus_per_node
        scanner = SystemScanner(namespace=TARGET_NAMESPACE)
        cluster_resources = scanner.scan_cluster()
        max_gpus_per_node = cluster_resources.max_gpus_per_node

        # Use VRAM from cluster scan instead of UI default
        gpu_vram_gb = cluster_resources.gpu_memory_per_gpu_mb / 1024 if cluster_resources.gpu_memory_per_gpu_mb > 0 else 80
        log_to_ui(f'   GPU VRAM per GPU: {gpu_vram_gb:.0f} GB (from cluster scan)', 'info')

        log_to_ui(f'🧪 Generating test plan for {model}...', 'info')
        if hf_token:
            log_to_ui('🔑 HuggingFace token provided for authentication', 'info')

        # ALWAYS try to read config from PVC first (if available)
        # This ensures we use accurate architecture details for gpu_memory_utilization
        model_config = None

        # Priority 1: Use explicitly specified PVC
        if use_existing_pvc and existing_pvc_name:
            log_to_ui(f'📦 Using existing PVC: {existing_pvc_name}', 'info')
            model_config = read_config_from_pvc(existing_pvc_name, model)
            if model_config:
                log_to_ui('✅ Model config loaded from PVC', 'success')
            else:
                log_to_ui('⚠️  Could not read config from PVC, using fallback estimates', 'warning')

        # Priority 2: Auto-detect PVC from session state
        if not model_config:
            try:
                with get_db() as conn:
                    row = conn.execute('SELECT config_json FROM ui_session_state WHERE id = 1').fetchone()
                    if row and row['config_json']:
                        session_config = json.loads(row['config_json'])
                        pvc_name = session_config.get('pvc_name') or session_config.get('existing_pvc_name')
                        if pvc_name:
                            log_to_ui(f'📦 Auto-detected PVC from session: {pvc_name}', 'info')
                            model_config = read_config_from_pvc(pvc_name, model)
                            if model_config:
                                log_to_ui('✅ Model config loaded from PVC', 'success')
            except Exception as _e:
                # If PVC auto-detection fails, continue without it
                pass

        # Priority 3: List all PVCs and try to find one with the model
        if not model_config:
            try:
                log_to_ui('📦 Searching for PVCs with model cache...', 'info')
                scanner_ns = TARGET_NAMESPACE
                pvcs = scanner.kubectl.run(['get', 'pvc', '-n', scanner_ns, '-o', 'name'], check=False)
                if pvcs.returncode == 0 and pvcs.stdout:
                    for pvc_line in pvcs.stdout.strip().split('\n'):
                        pvc_name = pvc_line.replace('persistentvolumeclaim/', '').strip()
                        if pvc_name:
                            # Try to read config from this PVC
                            temp_config = read_config_from_pvc(pvc_name, model, namespace=scanner_ns)
                            if temp_config:
                                log_to_ui(f'📦 Found model in PVC: {pvc_name}', 'info')
                                model_config = temp_config
                                log_to_ui('✅ Model config loaded from PVC', 'success')
                                break
            except Exception as _e:
                # If PVC listing fails, continue without it
                pass

        # If still no model_config, proceed with HuggingFace fallback
        if not model_config:
            log_to_ui('ℹ️  No PVC with model found - will use HuggingFace config', 'info')

        # Check if test plan parameters have changed (avoid duplicate generation)
        test_plan_params = {
            'model': model,
            'optimization_goal': optimization_goal,
            'max_gpus': max_gpus,
            'gpu_vram_gb': gpu_vram_gb,
            'isl': isl,
            'osl': osl,
            'num_users': num_users,
            'has_model_config': bool(model_config),
            'model_config_vram': model_config.get('vram_gb') if model_config else None
        }

        # Check if we already have a cached test plan with same parameters
        with state_lock:
            if CURRENT_TEST_PLAN is not None:
                cached_params = getattr(CURRENT_TEST_PLAN, '_params', None)
                if cached_params == test_plan_params:
                    log_to_ui('✅ Using cached test plan (parameters unchanged)', 'success')
                    socketio.emit('test_plan_ready', {
                        'test_plan': CURRENT_TEST_PLAN.to_dict(),
                        'can_proceed': CURRENT_TEST_PLAN.can_proceed,
                        'estimated_total_tests': CURRENT_TEST_PLAN.estimated_total_tests,
                        'model_requirements': CURRENT_TEST_PLAN.model_requirements.__dict__
                    })
                    return

        log_to_ui('🔧 Calculating VRAM requirements and planning tests...', 'info')

        planner = TestPlanner()

        # Pass model_config to planner if available
        if model_config:
            # We need to pass this to calculate_model_requirements
            # For now, we'll set it on the planner instance
            planner._pvc_model_config = model_config

        test_plan = planner.plan_tests(
            model_name=model,
            optimization_goal=optimization_goal,
            max_gpus_to_use=max_gpus,
            gpu_vram_gb=gpu_vram_gb,
            isl=isl,
            osl=osl,
            num_users=num_users,
            hf_token=hf_token,
            max_gpus_per_node=max_gpus_per_node,
            cloud_provider=cluster_resources.cloud_provider,
            node_count=cluster_resources.gpu_node_count,
            kubectl_runner=scanner.kubectl
        )

        if test_plan.can_proceed:
            # Log VRAM calculation breakdown to console FIRST (before showing tests)
            req = test_plan.model_requirements
            bytes_per_param = {'fp16': 2, 'fp8': 1, 'int8': 1, 'int4': 0.5, 'fp32': 4}
            bytes = bytes_per_param.get(req.dtype, 2)
            model_weights_gb = (req.model_size_b * 1e9 * bytes) / (1024**3)
            kv_cache_percent = (req.kv_cache_gb / model_weights_gb * 100) if model_weights_gb > 0 else 0
            activations_percent = (req.activations_gb / model_weights_gb * 100) if model_weights_gb > 0 else 0
            cuda_percent = (req.cuda_overhead_gb / model_weights_gb * 100) if model_weights_gb > 0 else 0

            log_to_ui('', 'info')  # Empty line for spacing
            log_to_ui('═══════════════════════════════════════════════════════════', 'info')
            log_to_ui('📊 VRAM CALCULATION BREAKDOWN', 'info')
            log_to_ui('═══════════════════════════════════════════════════════════', 'info')
            log_to_ui('', 'info')

            # Section 1: Model Information
            log_to_ui('🔹 MODEL INFORMATION', 'info')
            log_to_ui(f'   Model Name: {req.model_name}', 'info')
            log_to_ui(f'   Total Parameters: {req.model_size_b}B ({req.model_size_b * 1e9:,.0f})', 'info')
            log_to_ui(f'   Data Type: {req.dtype.upper()} ({bytes} bytes per parameter)', 'info')
            log_to_ui(f'   Why {req.dtype.upper()}? Detected from model name/config', 'info')
            log_to_ui('', 'info')

            # Section 2: Workload Configuration
            log_to_ui('🔹 WORKLOAD CONFIGURATION', 'info')
            log_to_ui(f'   Input Sequence Length (ISL): {req.isl} tokens', 'info')
            log_to_ui(f'   Output Sequence Length (OSL): {req.osl} tokens', 'info')
            log_to_ui(f'   Total Sequence Length: {req.isl + req.osl} tokens', 'info')
            log_to_ui('   Why this matters? KV cache scales with sequence length', 'info')
            log_to_ui('', 'info')

            # Section 3: Architecture Details (if config was loaded)
            # Check both PVC config and HuggingFace config
            actual_config = model_config or getattr(planner, '_last_model_config', None)
            if actual_config:
                num_layers = actual_config.get('num_hidden_layers', 'unknown')
                num_kv_heads = actual_config.get('num_key_value_heads', actual_config.get('num_attention_heads', 'unknown'))
                num_attn_heads = actual_config.get('num_attention_heads', 'unknown')
                hidden_size = actual_config.get('hidden_size', 'unknown')
                config_source = 'PVC' if model_config else 'HuggingFace'

                log_to_ui(f'🔹 MODEL ARCHITECTURE (from {config_source})', 'info')
                log_to_ui(f'   Layers: {num_layers} (transformer blocks stacked sequentially)', 'info')
                log_to_ui(f'   Attention Heads: {num_attn_heads} (parallel attention computations)', 'info')
                log_to_ui(f'   KV Heads: {num_kv_heads} (cached key/value attention heads)', 'info')
                if num_kv_heads != 'unknown' and num_attn_heads != 'unknown':
                    if num_kv_heads < num_attn_heads:
                        log_to_ui('   Strategy: GQA (Grouped Query Attention)', 'info')
                        log_to_ui(f'   Efficiency: {num_attn_heads // num_kv_heads} queries share 1 KV head = Lower memory', 'info')
                    else:
                        log_to_ui('   Strategy: MHA (Multi-Head Attention)', 'info')
                        log_to_ui('   Efficiency: 1:1 query to KV ratio = Higher memory', 'info')
                log_to_ui(f'   Hidden Size: {hidden_size} (dimension of internal representations)', 'info')
                log_to_ui('', 'info')
            else:
                log_to_ui('🔹 MODEL ARCHITECTURE', 'warning')
                log_to_ui('   ⚠️  WARNING: Could not fetch model config!', 'error')
                log_to_ui('', 'warning')

                # Get specific error from planner
                fetch_error = getattr(planner, '_config_fetch_error', 'unknown')

                if fetch_error == 'auth':
                    log_to_ui('   Reason: Authentication required (401/403)', 'error')
                    log_to_ui('   This is a PRIVATE model requiring HuggingFace token', 'error')
                    log_to_ui('', 'warning')
                    log_to_ui('   💡 Solution:', 'warning')
                    log_to_ui('   1. Go back to Step 2', 'warning')
                    log_to_ui('   2. Enter your HuggingFace token (HF_TOKEN)', 'warning')
                    log_to_ui('   3. Get token from: https://huggingface.co/settings/tokens', 'warning')
                elif fetch_error == 'connection':
                    log_to_ui('   Reason: No internet connectivity', 'error')
                    log_to_ui('   Cannot reach HuggingFace servers', 'error')
                    log_to_ui('', 'warning')
                    log_to_ui('   💡 Solution:', 'warning')
                    log_to_ui('   1. Check network connectivity', 'warning')
                    log_to_ui('   2. OR use "Existing PVC" mode in Step 5 (air-gapped)', 'warning')
                elif fetch_error == 'timeout':
                    log_to_ui('   Reason: Request timeout (>10 seconds)', 'error')
                    log_to_ui('   HuggingFace may be slow or unreachable', 'error')
                    log_to_ui('', 'warning')
                    log_to_ui('   💡 Solution:', 'warning')
                    log_to_ui('   1. Try again in a few moments', 'warning')
                    log_to_ui('   2. Check network connectivity', 'warning')
                elif fetch_error == 'not_found':
                    log_to_ui('   Reason: Model or config.json not found (404)', 'error')
                    log_to_ui('   Model name may be incorrect', 'error')
                    log_to_ui('', 'warning')
                    log_to_ui('   💡 Solution:', 'warning')
                    log_to_ui('   1. Verify model name in Step 2', 'warning')
                    log_to_ui('   2. Check model exists at: https://huggingface.co/{model}', 'warning')
                else:
                    log_to_ui(f'   Reason: {fetch_error}', 'error')
                    log_to_ui('', 'warning')
                    log_to_ui('   💡 Possible solutions:', 'warning')
                    log_to_ui('   1. If private model: Set HF_TOKEN in Step 2', 'warning')
                    log_to_ui('   2. If air-gapped: Use "Existing PVC" mode in Step 5', 'warning')
                    log_to_ui('   3. Check internet connectivity', 'warning')

                log_to_ui('', 'warning')
                log_to_ui('   Using FALLBACK estimates (may be inaccurate):', 'warning')
                log_to_ui('   Layers: ~40 (transformer blocks stacked sequentially)', 'warning')
                log_to_ui('   KV Heads: ~8 (cached key/value attention heads)', 'warning')
                log_to_ui('   Head Dim: ~128 (dimension of each attention head)', 'warning')
                log_to_ui('', 'warning')
                log_to_ui('   ⚠️  VRAM calculation may be INACCURATE!', 'error')
                log_to_ui('   Real model architecture may differ significantly.', 'error')
                log_to_ui('', 'info')

            # Section 4: VRAM Components
            log_to_ui('🔹 VRAM COMPONENTS', 'info')
            log_to_ui('', 'info')
            log_to_ui(f'   1️⃣  MODEL WEIGHTS: {model_weights_gb:.1f} GB', 'info')
            log_to_ui(f'       Formula: {req.model_size_b}B params × {bytes} bytes/param ÷ 1024³', 'info')
            log_to_ui('       Purpose: Store model parameters in GPU memory', 'info')
            log_to_ui('', 'info')

            log_to_ui(f'   2️⃣  KV CACHE (per request): {req.kv_cache_gb:.1f} GB ({kv_cache_percent:.1f}% of weights)', 'info')
            log_to_ui('       Formula: 2 × layers × kv_heads × head_dim × seq_len × bytes', 'info')
            log_to_ui('       Purpose: Store attention key/value tensors for ONE request', 'info')
            log_to_ui(f'       Note: Scales with sequence length ({req.isl + req.osl} tokens)', 'info')
            log_to_ui(f'       Note: Actual cache = {req.kv_cache_gb:.1f} GB × concurrent users', 'info')
            log_to_ui('', 'info')

            log_to_ui(f'   3️⃣  ACTIVATIONS: {req.activations_gb:.1f} GB ({activations_percent:.1f}% of weights)', 'info')
            log_to_ui('       Formula: Model weights × 15%', 'info')
            log_to_ui('       Purpose: Intermediate computations during inference', 'info')
            log_to_ui('       Note: Conservative estimate for batch inference', 'info')
            log_to_ui('', 'info')

            log_to_ui(f'   4️⃣  CUDA OVERHEAD: {req.cuda_overhead_gb:.1f} GB ({cuda_percent:.1f}% of weights)', 'info')
            log_to_ui('       Formula: Model weights × 5%', 'info')
            log_to_ui('       Purpose: CUDA kernels, framework overhead, buffers', 'info')
            log_to_ui('', 'info')

            log_to_ui('   ─────────────────────────────────────────────────────', 'info')
            log_to_ui(f'   TOTAL VRAM REQUIRED: {req.total_vram_gb:.1f} GB', 'info')
            log_to_ui(f'   Breakdown: {model_weights_gb:.1f} + {req.kv_cache_gb:.1f} + {req.activations_gb:.1f} + {req.cuda_overhead_gb:.1f} GB', 'info')
            log_to_ui('', 'info')

            # Section 5: GPU Requirements
            log_to_ui('🔹 GPU REQUIREMENTS', 'info')
            log_to_ui(f'   Available VRAM per GPU: {req.gpu_vram_gb:.0f} GB', 'info')
            log_to_ui(f'   Total VRAM Needed: {req.total_vram_gb:.1f} GB', 'info')
            log_to_ui('', 'info')
            log_to_ui(f'   Minimum GPUs = ⌈{req.total_vram_gb:.1f} ÷ {req.gpu_vram_gb:.0f}⌉ = {req.min_gpus} GPUs', 'info')
            log_to_ui('   Why? Model won\'t fit on fewer GPUs', 'info')
            log_to_ui('', 'info')

            # Section 6: Tensor Parallelism
            log_to_ui('🔹 TENSOR PARALLELISM (TP)', 'info')
            log_to_ui(f'   Minimum TP: {req.min_tp}', 'info')
            log_to_ui(f'   Why {req.min_tp}? TP must be power of 2 (≥ min GPUs)', 'info')
            if req.min_tp != req.min_gpus:
                log_to_ui(f'   Note: Rounded up from {req.min_gpus} to nearest power of 2', 'info')
            log_to_ui(f'   Recommended TP options: {", ".join(map(str, req.recommended_tp_options))}', 'info')
            log_to_ui('', 'info')

            # Section 7: GPU Memory Utilization
            log_to_ui('🔹 GPU MEMORY UTILIZATION (vLLM Setting)', 'info')

            # Calculate components for display
            base_gb = model_weights_gb + req.cuda_overhead_gb + req.activations_gb
            safety_buffer_gb = req.gpu_vram_gb * 0.05
            concurrent_users = num_users  # Get from user input
            total_cache_gb = concurrent_users * req.kv_cache_gb
            _total_target_gb = base_gb + total_cache_gb + safety_buffer_gb
            gpu_mem_util = req.gpu_memory_utilization

            log_to_ui('   Dynamic Setting (Capacity-Based Formula):', 'info')
            log_to_ui(f'   gpu_memory_utilization = {gpu_mem_util:.2f} ({gpu_mem_util*100:.0f}%)', 'info')
            log_to_ui('', 'info')
            log_to_ui('   Formula Breakdown:', 'info')
            log_to_ui('   Setting = (Base + Cache + Buffer) / Total_VRAM', 'info')
            log_to_ui('', 'info')
            log_to_ui('   Components:', 'info')
            log_to_ui(f'   • Base (weights + overhead + activations): {base_gb:.1f} GB', 'info')
            log_to_ui(f'   • Cache (per request): {req.kv_cache_gb:.2f} GB', 'info')
            log_to_ui(f'   • Concurrent users: {concurrent_users}', 'info')
            log_to_ui(f'   • Total cache ({concurrent_users} users × {req.kv_cache_gb:.2f}): {total_cache_gb:.1f} GB', 'info')
            log_to_ui(f'   • Safety buffer (5% of {req.gpu_vram_gb:.0f}GB): {safety_buffer_gb:.1f} GB', 'info')
            log_to_ui('', 'info')
            log_to_ui('   Calculation:', 'info')
            log_to_ui(f'   ({base_gb:.1f} + {total_cache_gb:.1f} + {safety_buffer_gb:.1f}) / {req.gpu_vram_gb:.0f} = {gpu_mem_util:.2f}', 'info')
            log_to_ui('', 'info')
            log_to_ui('   Why this matters?', 'info')
            log_to_ui('   This tells vLLM what % of GPU memory to use for the model.', 'info')
            log_to_ui('   Setting too high → OOM errors with concurrent requests', 'info')
            log_to_ui('   Setting too low → Wasted GPU memory capacity', 'info')
            log_to_ui('', 'info')

            # Section 8: Final Allocation
            log_to_ui('🔹 MINIMUM RESOURCE REQUIREMENTS', 'info')
            log_to_ui(f'   Minimum Configuration: TP={req.min_tp} (using {req.min_tp} GPUs)', 'info')
            log_to_ui(f'   VRAM per GPU: {req.estimated_vram_gb:.1f} GB / {req.gpu_vram_gb:.0f} GB available', 'info')
            utilization = (req.estimated_vram_gb / req.gpu_vram_gb * 100) if req.gpu_vram_gb > 0 else 0
            log_to_ui(f'   GPU Memory Utilization: {utilization:.1f}%', 'info')
            log_to_ui('', 'info')
            log_to_ui('🔹 OPTIMIZATION STRATEGY', 'info')
            log_to_ui('   In-S8 will now test multiple configurations:', 'info')
            log_to_ui('   • Different GPU counts (scaling up from minimum)', 'info')
            log_to_ui('   • Different architectures (Aggregated, EP, PD)', 'info')
            log_to_ui('   • Different pod ratios (prefill/decode balance)', 'info')
            log_to_ui('   The optimal configuration will be determined through actual', 'info')
            log_to_ui('   performance testing, not just memory calculations.', 'info')
            log_to_ui('', 'info')
            log_to_ui('═══════════════════════════════════════════════════════════', 'info')
            log_to_ui('', 'info')
            log_to_ui(f'✅ Test plan ready: {len(test_plan.tests)} tests planned', 'success')
            log_to_ui('', 'info')

            # Display cloud provider constraint warning if tests were filtered
            if test_plan.cloud_filtered_count > 0:
                total_generated = len(test_plan.tests) + test_plan.cloud_filtered_count
                log_to_ui('=' * 80, 'warning')
                log_to_ui(f'⚠️  CLOUD PROVIDER CONSTRAINT: {test_plan.cloud_provider.upper()}', 'warning')
                log_to_ui('=' * 80, 'warning')
                log_to_ui(f'{total_generated} tests were generated, but {test_plan.cloud_filtered_count} tests', 'warning')
                log_to_ui('were FILTERED OUT due to cloud provider constraints:', 'warning')
                log_to_ui('', 'warning')
                log_to_ui(f'  {test_plan.cloud_filter_reason}', 'warning')
                log_to_ui('', 'warning')
                log_to_ui(f'Tests after filtering: {len(test_plan.tests)}', 'warning')
                log_to_ui('=' * 80, 'warning')
                log_to_ui('', 'info')

            # Display detailed test plan in console
            log_to_ui('📋 Planned Tests', 'info')
            log_to_ui('', 'info')
            for idx, test in enumerate(test_plan.tests, 1):
                first_test_label = ' (First Test)' if idx == 1 else ''
                log_to_ui(f'{idx}. {test.test_name}{first_test_label}', 'info')
                log_to_ui(f'   {test.description}', 'info')
                log_to_ui(f'   Architecture: {test.architecture.value.upper()} | GPUs: {test.gpus_required} | TP: {test.tp}', 'info')
                log_to_ui('', 'info')

            log_to_ui(f'📝 Preparing test configurations with gpu_memory_utilization={req.gpu_memory_utilization:.2f}...', 'info')
            log_to_ui('', 'info')
        else:
            log_to_ui('❌ Resource validation failed', 'error')

        # Convert to dict for JSON serialization
        result = {
            'model_name': test_plan.model_name,
            'total_gpus_available': test_plan.total_gpus_available,
            'max_gpus_to_use': test_plan.max_gpus_to_use,
            'optimization_goal': test_plan.optimization_goal,
            'can_proceed': test_plan.can_proceed,
            'error_message': test_plan.error_message,
            'model_requirements': {
                'model_name': test_plan.model_requirements.model_name,
                'estimated_vram_gb': test_plan.model_requirements.estimated_vram_gb,
                'min_gpus': test_plan.model_requirements.min_gpus,
                'min_tp': test_plan.model_requirements.min_tp,
                'recommended_tp_options': test_plan.model_requirements.recommended_tp_options,
                'model_size_b': test_plan.model_requirements.model_size_b,
                'dtype': test_plan.model_requirements.dtype,
                'total_vram_gb': test_plan.model_requirements.total_vram_gb,
                'gpu_vram_gb': test_plan.model_requirements.gpu_vram_gb,
                'kv_cache_gb': test_plan.model_requirements.kv_cache_gb,
                'activations_gb': test_plan.model_requirements.activations_gb,
                'cuda_overhead_gb': test_plan.model_requirements.cuda_overhead_gb,
                'isl': test_plan.model_requirements.isl,
                'osl': test_plan.model_requirements.osl,
                'gpu_memory_utilization': test_plan.model_requirements.gpu_memory_utilization
            },
            'tests': [
                {
                    'test_name': test.test_name,
                    'architecture': test.architecture.value,
                    'gpus_required': test.gpus_required,
                    'tp': test.tp,
                    'prefill_pods': test.prefill_pods,
                    'decode_pods': test.decode_pods,
                    'ep_pods': test.ep_pods,
                    'description': test.description
                }
                for test in test_plan.tests
            ]
        }

        # Store test plan globally for later use during deployment
        with state_lock:
            CURRENT_TEST_PLAN = test_plan
            # Cache parameters to avoid duplicate generation
            CURRENT_TEST_PLAN._params = test_plan_params

        # Persist test plan to database so it survives server restarts
        try:
            with get_db() as conn:
                # Get current config
                row = conn.execute('SELECT config_json FROM ui_session_state WHERE id = 1').fetchone()
                config = json.loads(row['config_json']) if row and row['config_json'] else {}

                # Add test plan to config
                config['test_plan'] = result

                # Save back to database
                conn.execute('''
                    UPDATE ui_session_state
                    SET config_json = ?, updated_at = ?
                    WHERE id = 1
                ''', (json.dumps(config), datetime.now().isoformat()))

                print("✅ Test plan persisted to database")
        except Exception as e:
            print(f"Warning: Failed to persist test plan to database: {e}")

        # Emit test plan result early so UI can display structure
        emit('test_plan_result', result)

        # Save deployment templates to database
        if test_plan.can_proceed:
            log_to_ui('', 'info')
            log_to_ui('💾 Saving deployment templates to database...', 'info')

            # Scan cluster to get resource information
            from core import SystemScanner
            scanner = SystemScanner(namespace=TARGET_NAMESPACE)
            cluster_resources = scanner.scan_cluster()

            # Calculate per-node resources based on cluster scan
            max_gpus_per_node = cluster_resources.max_gpus_per_node

            # Get CPU info from GPU nodes only (not all cluster nodes)
            gpu_nodes_list = [n for n in cluster_resources.nodes if n.gpus > 0]
            if gpu_nodes_list:
                # Use average from actual GPU nodes
                avg_memory_per_gpu_node_gb = sum(n.memory_gb for n in gpu_nodes_list) / len(gpu_nodes_list)
                avg_cpu_per_gpu_node = sum(n.cpu_cores for n in gpu_nodes_list) / len(gpu_nodes_list)
            else:
                # Fallback if no GPU nodes detected
                avg_memory_per_gpu_node_gb = 512
                avg_cpu_per_gpu_node = 48

            for test in test_plan.tests:
                # Skip placeholder tests (TP=0 or gpus_required=0)
                if test.tp == 0 or test.gpus_required == 0:
                    continue

                arch = test.architecture.value

                # Calculate resource requests PER POD based on how many pods fit on one node
                # Max pods per node = GPUs per node / TP
                max_pods_per_node = max_gpus_per_node / test.tp

                # Usable resources per node (leave headroom for system)
                usable_memory_gb = avg_memory_per_gpu_node_gb * 0.80  # 80% usable
                usable_cpus = int(avg_cpu_per_gpu_node * 0.70)  # 70% usable

                # Resources per pod = node resources / pods per node
                memory_request_gb = int(usable_memory_gb / max_pods_per_node)
                cpu_request = max(1, int(usable_cpus / max_pods_per_node))

                memory_limit = f'{memory_request_gb}Gi'
                cpu_request_str = str(cpu_request)

                if arch == 'aggregated' or arch == 'ep':
                    # Single template for aggregated/EP
                    save_deployment_template(
                        model_name=test_plan.model_name,
                        architecture=arch,
                        role=None,
                        tensor_parallelism=test.tp,
                        replicas=1,  # Single replica for initial deployment
                        max_model_len=8192,
                        gpu_memory_utilization=0.95,
                        isl=test_plan.model_requirements.isl,
                        osl=test_plan.model_requirements.osl,
                        gpus_per_pod=test.tp,
                        memory_limit=memory_limit,
                        cpu_request=cpu_request_str
                    )
                    log_to_ui(f'   ✅ Saved {arch.upper()} template (TP={test.tp}, CPU={cpu_request}, Memory={memory_limit})', 'success')

                elif arch == 'pd':
                    # Two templates for PD: prefill and decode
                    # Prefill template
                    save_deployment_template(
                        model_name=test_plan.model_name,
                        architecture='pd',
                        role='prefill',
                        tensor_parallelism=test.tp,
                        replicas=test.prefill_pods,
                        max_model_len=8192,
                        gpu_memory_utilization=0.95,
                        isl=test_plan.model_requirements.isl,
                        osl=test_plan.model_requirements.osl,
                        gpus_per_pod=test.tp,
                        memory_limit=memory_limit,
                        cpu_request=cpu_request_str
                    )

                    # Decode template (with max_num_batched_tokens)
                    save_deployment_template(
                        model_name=test_plan.model_name,
                        architecture='pd',
                        role='decode',
                        tensor_parallelism=test.tp,
                        replicas=test.decode_pods,
                        max_model_len=8192,
                        gpu_memory_utilization=0.95,
                        isl=test_plan.model_requirements.isl,
                        osl=test_plan.model_requirements.osl,
                        max_num_batched_tokens=8192,  # Decode-specific
                        gpus_per_pod=test.tp,
                        memory_limit=memory_limit,
                        cpu_request=cpu_request_str
                    )
                    log_to_ui(f'   ✅ Saved PD templates (TP={test.tp}, Prefill={test.prefill_pods}, Decode={test.decode_pods}, CPU={cpu_request}, Memory={memory_limit})', 'success')

            log_to_ui('', 'info')
            log_to_ui('🚀 All deployment templates saved - ready to deploy!', 'success')
            log_to_ui('', 'info')

            # Signal that everything is ready
            emit('test_plan_ready', {'ready': True})

    except Exception as e:
        error_msg = f"Failed to generate test plan: {str(e)}"
        log_to_ui(f'❌ {error_msg}', 'error')
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
        emit('test_plan_result', {
            'can_proceed': False,
            'error_message': error_msg
        })

@socketio.on('list_pvcs')
def handle_list_pvcs(data):
    """List all PVCs in the namespace."""
    try:
        from core.k8s_utils import KubectlRunner

        namespace = TARGET_NAMESPACE
        kubectl = KubectlRunner(namespace=namespace)

        logger.debug(f'Listing PVCs in namespace: {namespace}')

        # Get PVCs as JSON
        result = kubectl.run_json(['get', 'pvc', '-n', namespace])

        pvcs = []
        if result and 'items' in result:
            for pvc in result['items']:
                metadata = pvc.get('metadata', {})
                spec = pvc.get('spec', {})
                status = pvc.get('status', {})

                pvc_info = {
                    'name': metadata.get('name', 'unknown'),
                    'size': status.get('capacity', {}).get('storage', 'unknown'),
                    'storage_class': spec.get('storageClassName', 'unknown'),
                    'status': status.get('phase', 'unknown')
                }
                pvcs.append(pvc_info)

        # Log PVC names
        pvc_names = ', '.join([p['name'] for p in pvcs])
        logger.debug(f'Found {len(pvcs)} PVC(s): {pvc_names}')

        emit('pvc_list_result', {
            'success': True,
            'pvcs': pvcs
        })

    except Exception as e:
        error_msg = f'Failed to list PVCs: {str(e)}'
        log_to_ui(f'❌ {error_msg}', 'error')
        emit('pvc_list_result', {
            'success': False,
            'error': error_msg,
            'pvcs': []
        })

@socketio.on('setup_storage')
def handle_setup_storage(data):
    """Create PVC and start model download job."""
    global OPTIMIZATION_RUNNING

    try:
        from core import TemplateManager

        existing_pvc = data.get('existing_pvc')
        storage_class = data.get('storage_class')
        pvc_size = data.get('pvc_size', 256)
        model = data.get('model')
        hf_token = data.get('hf_token')
        namespace = TARGET_NAMESPACE

        # Fallback to saved config if frontend didn't send existing_pvc
        if not existing_pvc:
            try:
                with get_db() as conn:
                    row = conn.execute('SELECT config_json FROM ui_session_state WHERE id = 1').fetchone()
                    if row and row['config_json']:
                        saved = json.loads(row['config_json'])
                        if saved.get('use_existing_pvc') and saved.get('existing_pvc_name'):
                            existing_pvc = saved['existing_pvc_name']
                            log_to_ui(f'📦 Using saved PVC config: {existing_pvc}', 'info')
                        if not model and saved.get('model'):
                            model = saved['model']
            except Exception:
                pass

        log_to_ui('📦 Setting up persistent model cache...', 'info')

        # Set optimization as running
        with state_lock:
            OPTIMIZATION_RUNNING = True
            save_state()

        # Update database to reflect optimization running
        try:
            with get_db() as conn:
                conn.execute('''
                    UPDATE ui_session_state
                    SET optimization_running = 1,
                        updated_at = ?
                    WHERE id = 1
                ''', (datetime.now().isoformat(),))
        except Exception as e:
            print(f"Warning: Failed to update optimization_running in database: {e}")

        socketio.emit('status_update', {'running': True})

        # Check if user specified an existing PVC
        if existing_pvc:
            log_to_ui(f'✅ Using existing PVC: {existing_pvc}', 'success')
            emit('storage_setup_result', {
                'success': True,
                'pvc_name': existing_pvc,
                'pvc_size': 'existing',
                'storage_class': 'existing',
                'model': model,
                'existing': True
            })

            # Skip model download and go straight to optimization
            log_to_ui('✅ Model already available in PVC', 'success')
            log_to_ui('', 'info')
            log_to_ui('🚀 Starting optimization...', 'info')

            # Prepare optimization data from client config
            resume_run_id = data.get('resume_run_id')
            optimization_data = {
                'model': model,
                'isl': data.get('isl', 3000),
                'osl': data.get('osl', 100),
                'isl_stdev': data.get('isl_stdev'),
                'osl_stdev': data.get('osl_stdev'),
                'turns': data.get('turns', 1),
                'num_users': data.get('num_users', 100),
                'optimization_metric': data.get('optimization_goal', 'ttft'),
                'max_test_duration': data.get('duration', 300),
                'stop_mode': data.get('stop_mode', 'duration'),
                'max_requests': data.get('max_requests'),
                'hf_token': hf_token,
                'max_gpus': data.get('max_gpus', 16),
                'use_achievable_qps': data.get('use_achievable_qps', False),
                'latency_constraint_enabled': data.get('latency_constraint_enabled', False),
                'latency_constraint_ms': data.get('latency_constraint_ms', 500),
                'latency_constraint_percentile': data.get('latency_constraint_percentile', 'p90'),
                'tp_pair_top_n': data.get('tp_pair_top_n', 2),
                'selected_nodes': data.get('selected_nodes') or [],
                'workload_mode': data.get('workload_mode', 'synthetic'),
                'dataset_source': data.get('dataset_source'),
                'dataset_column': data.get('dataset_column'),
                'dataset_max_output': int(data.get('dataset_max_output', 256)),
                'rate_type': data.get('rate_type', 'concurrent'),
                'advanced_vllm': data.get('advanced_vllm'),
            }
            if resume_run_id:
                optimization_data['resume_run_id'] = resume_run_id

            # Start optimization in background
            spawn(run_optimization_background, optimization_data)
            return

        # Create new PVC and download model
        pvc_name = 'in-s8-model-cache'
        timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        job_name = f'in-s8-model-download-{timestamp}'
        test_id = f'in-s8-setup-{timestamp}'

        # Initialize TemplateManager
        template_mgr = TemplateManager()

        # Check if PVC already exists
        check_cmd = ['kubectl', 'get', 'pvc', pvc_name, '-n', namespace]
        proc = subprocess.run(check_cmd, capture_output=True, timeout=10)
        pvc_exists = proc.returncode == 0

        if not pvc_exists:
            log_to_ui(f'📦 Creating PVC {pvc_name} ({pvc_size}Gi)...', 'info')

            # Render PVC template
            pvc_yaml = template_mgr.render_template(
                'prereq/model-cache-pvc.yaml.j2',
                pvc_name=pvc_name,
                namespace=namespace,
                test_id=test_id,
                model_name=model,
                storage_class=storage_class,
                storage_size=pvc_size
            )

            cmd = ['kubectl', 'apply', '-f', '-']
            proc = subprocess.run(cmd, input=pvc_yaml.encode(), capture_output=True, timeout=30)

            if proc.returncode != 0:
                raise Exception(f"PVC creation failed: {proc.stderr.decode()}")

            log_to_ui(f'✅ PVC {pvc_name} created', 'success')
        else:
            log_to_ui(f'✅ PVC {pvc_name} already exists (reusing)', 'success')

        # Create model download job
        log_to_ui(f'📥 Starting model download: {model}', 'info')

        # Render job template (uses lightweight Red Hat UBI Python image by default)
        job_yaml = template_mgr.render_template(
            'prereq/model-download-job.yaml.j2',
            job_name=job_name,
            namespace=namespace,
            test_id=test_id,
            model_name=model,
            pvc_name=pvc_name,
            hf_token=hf_token
        )

        cmd = ['kubectl', 'apply', '-f', '-']
        proc = subprocess.run(cmd, input=job_yaml.encode(), capture_output=True, timeout=30)

        if proc.returncode != 0:
            raise Exception(f"Job creation failed: {proc.stderr.decode()}")

        log_to_ui(f'✅ Model download job {job_name} started', 'success')
        log_to_ui('⏳ Streaming download progress...', 'info')

        emit('storage_setup_result', {
            'success': True,
            'pvc_name': pvc_name,
            'pvc_size': pvc_size,
            'storage_class': storage_class,
            'model': model,
            'job_name': job_name,
            'existing': False
        })

        # Start background task to stream job logs
        spawn(stream_job_logs, job_name, namespace)

    except Exception as e:
        error_msg = f"Storage setup failed: {str(e)}"
        log_to_ui(f'❌ {error_msg}', 'error')
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()

        # Reset optimization state on error
        with state_lock:
            OPTIMIZATION_RUNNING = False
            save_state()
        emit('status_update', {'running': False})
        emit('storage_setup_result', {'success': False, 'error': str(e)})

@socketio.on('reset_database')
def handle_reset_database(data):
    """Reset database and all settings."""
    try:
        log_to_ui('🔄 Resetting database...', 'warning')

        # Close all existing database connections
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        # Delete all data from tables
        cursor.execute('DELETE FROM console_logs')
        cursor.execute('DELETE FROM test_configurations')
        cursor.execute('DELETE FROM optimization_runs')
        cursor.execute('DELETE FROM deployment_templates')
        cursor.execute('DELETE FROM ui_session_state')

        conn.commit()
        conn.close()

        # Clear state file
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)

        # Reset global variables
        global OPTIMIZATION_RUNNING, CURRENT_CONFIG, CURRENT_TEST_PLAN
        with state_lock:
            OPTIMIZATION_RUNNING = False
            CURRENT_CONFIG = {}
            CURRENT_TEST_PLAN = None
            save_state()

        log_to_ui('✅ Database reset complete', 'success')
        emit('reset_complete', {'success': True})

    except Exception as e:
        error_msg = f"Reset failed: {str(e)}"
        log_to_ui(f'❌ {error_msg}', 'error')
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
        emit('reset_complete', {'success': False, 'error': str(e)})

@app.route('/api/optuna_trials/<int:run_id>')
def get_optuna_trials(run_id):
    """Get Optuna trial history for a run."""
    try:
        step = request.args.get('step', 'step9_latency_bounded')
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute('''
            SELECT * FROM optuna_trials
            WHERE run_id = ? AND optimization_step = ?
            ORDER BY trial_number ASC
        ''', (run_id, step))
        trials = [dict(row) for row in cursor.fetchall()]

        cursor.execute('''
            SELECT * FROM optuna_studies
            WHERE run_id = ? AND optimization_step = ?
        ''', (run_id, step))
        study_row = cursor.fetchone()
        study = dict(study_row) if study_row else None

        conn.close()
        return jsonify({'trials': trials, 'study': study})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/latency_search/<int:run_id>')
def get_latency_search_trials(run_id):
    """Get latency binary search trials for a run — used for cost-of-latency charting."""
    try:
        arch_filter = request.args.get('architecture')
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        if arch_filter:
            cursor.execute('''
                SELECT * FROM latency_search_trials
                WHERE run_id = ? AND architecture = ?
                ORDER BY trial_number ASC
            ''', (run_id, arch_filter))
        else:
            cursor.execute('''
                SELECT * FROM latency_search_trials
                WHERE run_id = ?
                ORDER BY architecture, trial_number ASC
            ''', (run_id,))

        trials = [dict(row) for row in cursor.fetchall()]
        conn.close()

        # Group by architecture for charting
        by_arch = {}
        for t in trials:
            arch = t['architecture']
            by_arch.setdefault(arch, []).append(t)

        return jsonify({'trials': trials, 'by_architecture': by_arch})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@socketio.on('compress_database')
def handle_compress_database():
    """Compress the database file and emit progress events."""
    try:
        import gzip as gzip_mod

        if not os.path.exists(DB_PATH):
            emit('compression_error', {'error': 'Database file not found'})
            return

        total_size = os.path.getsize(DB_PATH)
        compressed_path = '/tmp/in-s8-optimizer.db.gz'

        emit('compression_progress', {'percent': 0, 'status': 'Compressing...',
             'original_size': total_size})

        chunk_size = 256 * 1024
        bytes_read = 0

        with open(DB_PATH, 'rb') as f_in, gzip_mod.open(compressed_path, 'wb', compresslevel=6) as f_out:
            while True:
                chunk = f_in.read(chunk_size)
                if not chunk:
                    break
                f_out.write(chunk)
                bytes_read += len(chunk)
                percent = min(int((bytes_read / total_size) * 100), 99)
                emit('compression_progress', {'percent': percent, 'status': 'Compressing...'})
                socketio.sleep(0)

        compressed_size = os.path.getsize(compressed_path)
        ratio = ((total_size - compressed_size) / total_size) * 100

        emit('compression_progress', {'percent': 100, 'status': 'Done!'})
        emit('compression_complete', {
            'original_size': total_size,
            'compressed_size': compressed_size,
            'ratio': round(ratio, 1)
        })

    except Exception as e:
        print(f"ERROR compressing database: {e}")
        import traceback
        traceback.print_exc()
        emit('compression_error', {'error': str(e)})

@app.route('/api/download_database')
def download_database():
    """Download the compressed database file."""
    try:
        from flask import send_file, after_this_request

        compressed_path = '/tmp/in-s8-optimizer.db.gz'
        if os.path.exists(compressed_path):
            @after_this_request
            def cleanup(response):
                try:
                    os.remove(compressed_path)
                except Exception:
                    pass
                return response

            return send_file(
                compressed_path,
                mimetype='application/gzip',
                as_attachment=True,
                download_name='in-s8-optimizer.db.gz'
            )

        if not os.path.exists(DB_PATH):
            return jsonify({'error': 'Database file not found'}), 404

        return send_file(
            DB_PATH,
            mimetype='application/x-sqlite3',
            as_attachment=True,
            download_name='in-s8-optimizer.db'
        )
    except Exception as e:
        print(f"ERROR downloading database: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/api/upload-dataset', methods=['POST'])
def upload_dataset():
    """Upload a custom dataset file for benchmarking."""
    try:
        if 'file' not in request.files:
            return jsonify({'success': False, 'error': 'No file provided'})
        f = request.files['file']
        if not f.filename:
            return jsonify({'success': False, 'error': 'Empty filename'})
        allowed = {'.csv', '.json', '.jsonl', '.txt'}
        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in allowed:
            return jsonify({'success': False, 'error': f'Unsupported file type: {ext}'})
        dataset_dir = os.path.join(IN_S8_PATH, 'data', 'datasets')
        os.makedirs(dataset_dir, exist_ok=True)
        safe_name = f.filename.replace('/', '_').replace('\\', '_')
        dest = os.path.join(dataset_dir, safe_name)
        f.save(dest)
        return jsonify({'success': True, 'path': dest, 'filename': safe_name})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/upload_database', methods=['POST'])
def upload_database():
    """Import optimization runs and test results from an uploaded database file."""
    import tempfile
    try:
        if 'database' not in request.files:
            return jsonify({'success': False, 'error': 'No database file provided'}), 400

        file = request.files['database']
        if not file.filename:
            return jsonify({'success': False, 'error': 'Empty filename'}), 400

        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as tmp:
            tmp_path = tmp.name
            file.save(tmp_path)

        # Decompress .gz files
        if file.filename.endswith('.gz'):
            import gzip as gzip_mod
            decompressed_path = tmp_path + '.decompressed.db'
            try:
                with gzip_mod.open(tmp_path, 'rb') as gz_in, open(decompressed_path, 'wb') as db_out:
                    while True:
                        chunk = gz_in.read(256 * 1024)
                        if not chunk:
                            break
                        db_out.write(chunk)
                os.remove(tmp_path)
                tmp_path = decompressed_path
            except Exception as e:
                os.remove(tmp_path)
                if os.path.exists(decompressed_path):
                    os.remove(decompressed_path)
                return jsonify({'success': False, 'error': f'Failed to decompress .gz file: {e}'}), 400

        try:
            src_conn = sqlite3.connect(tmp_path)
            src_conn.row_factory = sqlite3.Row

            src_tables = [r[0] for r in src_conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()]
            if 'optimization_runs' not in src_tables or 'test_configurations' not in src_tables:
                src_conn.close()
                return jsonify({'success': False, 'error': 'Not a valid In-S8 database (missing required tables)'}), 400

            src_runs = src_conn.execute('SELECT * FROM optimization_runs ORDER BY id').fetchall()
            if not src_runs:
                src_conn.close()
                return jsonify({'success': False, 'error': 'Database contains no optimization runs'}), 400

            imported_runs = 0
            imported_tests = 0
            skipped_runs = 0

            source_label = os.path.splitext(file.filename)[0]

            with get_db() as dst_conn:
                dst_cols = [r[1] for r in dst_conn.execute('PRAGMA table_info(optimization_runs)').fetchall()]
                src_cols = [r['name'] for r in src_conn.execute('PRAGMA table_info(optimization_runs)').fetchall()]
                common_run_cols = [c for c in src_cols if c in dst_cols and c != 'id']

                dst_test_cols = [r[1] for r in dst_conn.execute('PRAGMA table_info(test_configurations)').fetchall()]
                src_test_cols = [r['name'] for r in src_conn.execute('PRAGMA table_info(test_configurations)').fetchall()]
                common_test_cols = [c for c in src_test_cols if c in dst_test_cols and c not in ('id', 'run_id')]

                for src_run in src_runs:
                    src_run_dict = dict(src_run)
                    old_run_id = src_run_dict['id']

                    orig_name = src_run_dict.get('run_name', f'run-{old_run_id}')
                    new_name = f"[{source_label}] {orig_name}"

                    existing = dst_conn.execute(
                        'SELECT id FROM optimization_runs WHERE run_name = ?', (new_name,)
                    ).fetchone()
                    if existing:
                        skipped_runs += 1
                        continue

                    col_names = [c for c in common_run_cols if c != 'run_name']
                    placeholders = ', '.join(['?'] * (len(col_names) + 1))
                    col_str = 'run_name, ' + ', '.join(col_names)
                    values = [new_name] + [src_run_dict.get(c) for c in col_names]

                    cursor = dst_conn.execute(
                        f'INSERT INTO optimization_runs ({col_str}) VALUES ({placeholders})',
                        values
                    )
                    new_run_id = cursor.lastrowid
                    imported_runs += 1

                    src_tests = src_conn.execute(
                        'SELECT * FROM test_configurations WHERE run_id = ? ORDER BY id',
                        (old_run_id,)
                    ).fetchall()

                    for src_test in src_tests:
                        src_test_dict = dict(src_test)
                        test_col_str = 'run_id, ' + ', '.join(common_test_cols)
                        test_placeholders = ', '.join(['?'] * (len(common_test_cols) + 1))
                        test_values = [new_run_id] + [src_test_dict.get(c) for c in common_test_cols]

                        dst_conn.execute(
                            f'INSERT INTO test_configurations ({test_col_str}) VALUES ({test_placeholders})',
                            test_values
                        )
                        imported_tests += 1

            src_conn.close()
            return jsonify({
                'success': True,
                'imported_runs': imported_runs,
                'imported_tests': imported_tests,
                'skipped_runs': skipped_runs,
                'source': file.filename,
            })

        finally:
            os.unlink(tmp_path)

    except Exception as e:
        print(f"ERROR uploading database: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/run/<int:run_id>/config/<config_name>/manifest/<manifest_type>')
def download_manifest(run_id, config_name, manifest_type):
    """Download a rendered LWS YAML manifest for a specific test configuration.

    Args:
        run_id: Optimization run ID
        config_name: Test config name (e.g. step7-pd-p2d6-ptp4-dtp2)
        manifest_type: Manifest key (e.g. lws, service, prefill, decode, prefill-service, decode-service)
    """
    try:
        import json
        from flask import Response

        with get_db() as conn:
            row = conn.execute(
                'SELECT manifests_yaml FROM test_configurations WHERE run_id = ? AND config_name = ?',
                (run_id, config_name)
            ).fetchone()

        if not row or not row['manifests_yaml']:
            return jsonify({'error': 'No manifests found for this configuration'}), 404

        manifests = json.loads(row['manifests_yaml'])

        if manifest_type not in manifests:
            return jsonify({
                'error': f'Manifest type "{manifest_type}" not found',
                'available': list(manifests.keys())
            }), 404

        yaml_content = manifests[manifest_type]
        filename = f"{config_name}-{manifest_type}.yaml"

        return Response(
            yaml_content,
            mimetype='application/x-yaml',
            headers={'Content-Disposition': f'attachment; filename="{filename}"'}
        )

    except Exception as e:
        print(f"ERROR downloading manifest: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/run/<int:run_id>/config/<config_name>/manifests')
def get_manifest_list(run_id, config_name):
    """List available manifests for a test configuration."""
    try:
        import json

        with get_db() as conn:
            row = conn.execute(
                'SELECT manifests_yaml FROM test_configurations WHERE run_id = ? AND config_name = ?',
                (run_id, config_name)
            ).fetchone()

        if not row or not row['manifests_yaml']:
            return jsonify({'available': []})

        manifests = json.loads(row['manifests_yaml'])
        return jsonify({'available': list(manifests.keys())})

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# --- Application Entry Point ---

def cleanup_stale_optimizations():
    """
    Clean up stale optimization runs on server startup.
    Marks any 'running' optimizations as 'interrupted' — but only if
    no optimizer process is still alive (e.g. a CLI run).
    """
    try:
        import subprocess
        optimizer_alive = False
        try:
            result = subprocess.run(
                ['pgrep', '-f', 'recipe_optimizer|run_optimization_cli|resume_latest'],
                capture_output=True, timeout=5)
            optimizer_alive = result.returncode == 0
        except Exception:
            pass

        if optimizer_alive:
            print("ℹ️  Optimizer process still running — skipping stale run cleanup")
            return

        with get_db() as conn:
            cursor = conn.cursor()

            # Find all 'running' optimizations
            stale_runs = cursor.execute('''
                SELECT id, run_name FROM optimization_runs
                WHERE status = 'running'
            ''').fetchall()

            if stale_runs:
                print(f"⚠️  Found {len(stale_runs)} stale 'running' optimization(s)")

                # Mark them as interrupted
                for run_id, run_name in stale_runs:
                    cursor.execute('''
                        UPDATE optimization_runs
                        SET status = ?, completed_at = ?, notes = ?
                        WHERE id = ?
                    ''', ('interrupted', datetime.now().isoformat(),
                          'Server restarted while optimization was running', run_id))
                    print(f"   ✓ Marked {run_name} as interrupted")

                print(f"✓ Cleaned up {len(stale_runs)} stale optimization(s)")

            # Also ensure ui_session_state.optimization_running is cleared
            cursor.execute('''
                UPDATE ui_session_state
                SET optimization_running = 0,
                    updated_at = ?
                WHERE id = 1 AND optimization_running = 1
            ''', (datetime.now().isoformat(),))

    except Exception as e:
        print(f"Warning: Failed to cleanup stale optimizations: {e}")


def main():
    """Main application entry point."""
    print("=" * 60)
    print("In-S8 llm-d optimizer - Intelligent Search for Optimal llm-d Inference Configuration")
    print("=" * 60)

    # Initialize database
    init_db()

    # Load saved state
    load_state()

    # Clean up any stale 'running' optimizations from previous server instance
    cleanup_stale_optimizations()

    # Create output directory
    os.makedirs(OPTIMIZATION_OUTPUT_DIR, exist_ok=True)

    print(f"✓ Output directory: {OPTIMIZATION_OUTPUT_DIR}")
    print(f"✓ Database: {DB_PATH}")
    print(f"✓ State directory: {STATE_DIR}")
    print("✓ Starting web server on port 5000...")
    print("=" * 60)

    # Start the Flask-SocketIO server
    socketio.run(app, host='0.0.0.0', port=5000, debug=False)

if __name__ == '__main__':
    main()
