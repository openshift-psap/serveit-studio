"""Database initialization, state persistence, and deployment template storage."""

import os
import json
import sqlite3
import logging
from datetime import datetime
from typing import Optional, Dict, List

from web.app_context import DB_PATH, STATE_DIR, STATE_FILE, TARGET_NAMESPACE, get_db, state, state_lock

logger = logging.getLogger(__name__)

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
            namespace TEXT DEFAULT 'serveit',

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
            nics_json TEXT,
            versions_json TEXT
        )
    ''')

    # Create index for faster hardware scan queries
    cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_hw_scans_timestamp ON hardware_scans(scan_timestamp DESC)
    ''')

    # Migration: add versions_json column if missing (existing databases)
    try:
        cursor.execute('ALTER TABLE hardware_scans ADD COLUMN versions_json TEXT')
    except Exception:
        pass

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

    try:
        cursor.execute('ALTER TABLE test_configurations ADD COLUMN test_config_json TEXT')
    except sqlite3.OperationalError:
        pass

    for col, typ in [
        ('workload_mode', 'TEXT'),
        ('dataset_source', 'TEXT'),
        ('dataset_column', 'TEXT'),
        ('dataset_max_output', 'INTEGER'),
        ('rate_type', 'TEXT'),
        ('prefix_cache_hit_pct', 'INTEGER'),
        ('prefix_cache_seed', 'INTEGER'),
        ('stop_mode', 'TEXT'),
        ('max_requests', 'INTEGER'),
        ('speculative_method', 'TEXT'),
        ('speculative_num_tokens', 'INTEGER'),
    ]:
        try:
            cursor.execute(f'ALTER TABLE optimization_runs ADD COLUMN {col} {typ}')
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


# --- State Management ---

def save_state():
    """Save current application state to JSON file."""
    with state_lock:
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            with open(STATE_FILE, 'w') as f:
                json.dump({
                    'running': state['optimization_running'],
                    'config': state['current_config']
                }, f, indent=2)
        except Exception as e:
            print(f"Error saving state: {e}")

def load_state():
    """Load application state from JSON file and database."""
    with state_lock:
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, 'r') as f:
                    saved = json.load(f)
                state['optimization_running'] = saved.get('running', False)
                state['current_config'] = saved.get('config', {})

                # Reset running state on app restart
                if state['optimization_running']:
                    state['optimization_running'] = False
                    save_state()
            except Exception as e:
                print(f"Error loading state: {e}")
                state['optimization_running'] = False
                state['current_config'] = get_default_config()
        else:
            state['current_config'] = get_default_config()
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

                            state['current_test_plan'] = test_plan
                            print(f"✅ Restored test plan from database ({len(tests)} tests)")
        except Exception as e:
            print(f"Warning: Failed to restore test plan from database: {e}")
            state['current_test_plan'] = None

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
    image: str = 'ghcr.io/llm-d/llm-d-cuda:v0.8.0',
    pvc_name: str = 'serveit-cache',
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


def cleanup_stale_optimizations():
    """Clean up stale 'running' optimization runs on server startup."""
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
            print("  Optimizer process still running - skipping stale run cleanup")
            return

        with get_db() as conn:
            cursor = conn.cursor()
            stale_runs = cursor.execute('''
                SELECT id, run_name FROM optimization_runs
                WHERE status = 'running'
            ''').fetchall()

            if stale_runs:
                print(f"  Found {len(stale_runs)} stale 'running' optimization(s)")
                for run_id, run_name in stale_runs:
                    existing = cursor.execute('SELECT constraint_notes FROM optimization_runs WHERE id = ?', (run_id,)).fetchone()
                    sys_notes = []
                    if existing and existing[0]:
                        try:
                            sys_notes = json.loads(existing[0])
                        except Exception:
                            sys_notes = []
                    sys_notes.append('Server restarted while optimization was running')
                    cursor.execute('''
                        UPDATE optimization_runs
                        SET status = ?, completed_at = ?, constraint_notes = ?
                        WHERE id = ?
                    ''', ('interrupted', datetime.now().isoformat(), json.dumps(sys_notes), run_id))
                    print(f"   Marked {run_name} as interrupted")

            cursor.execute('''
                UPDATE ui_session_state
                SET optimization_running = 0, updated_at = ?
                WHERE id = 1 AND optimization_running = 1
            ''', (datetime.now().isoformat(),))

    except Exception as e:
        print(f"Warning: Failed to cleanup stale optimizations: {e}")
