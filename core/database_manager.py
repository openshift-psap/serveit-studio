"""
Database manager for ServeIt Studio optimization results.
Provides immediate persistence of test results to SQLite database.
"""

import sqlite3
import json
from contextlib import contextmanager
from datetime import datetime
from typing import Optional, Dict, Any, List
from pathlib import Path

from .config_generator import TestConfig
from .test_orchestrator import TestResult
from .metrics_analyzer import MetricsAnalyzer


class DatabaseManager:
    """
    Manages database operations for ServeIt Studio optimization runs.
    Supports immediate test result persistence.
    """

    def __init__(self, db_path: str = '/mnt/storage/serveit.db'):
        """
        Initialize database manager.

        Args:
            db_path: Path to SQLite database file
        """
        self.db_path = db_path
        self._ensure_tables()

    @contextmanager
    def get_connection(self):
        """Get database connection with automatic commit/rollback."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _ensure_tables(self):
        """Ensure required tables exist."""
        with self.get_connection() as conn:
            cursor = conn.cursor()

            # Create optimization_runs table if not exists
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
                    pods_deployed TEXT,
                    goal TEXT,
                    test_duration INTEGER DEFAULT 300,
                    max_gpus INTEGER DEFAULT 16,
                    use_achievable_qps INTEGER DEFAULT 0,
                    isl_stdev INTEGER,
                    osl_stdev INTEGER,
                    turns INTEGER DEFAULT 1,
                    config_json TEXT,
                    constraint_notes TEXT,
                    latency_constraint_enabled INTEGER DEFAULT 0,
                    latency_constraint_ms INTEGER DEFAULT 500,
                    latency_constraint_percentile TEXT DEFAULT 'p99',
                    workload_mode TEXT DEFAULT 'synthetic',
                    dataset_source TEXT,
                    dataset_column TEXT,
                    dataset_max_output INTEGER DEFAULT 256,
                    rate_type TEXT DEFAULT 'concurrent',
                    prefix_cache_hit_pct INTEGER DEFAULT 0,
                    prefix_cache_seed INTEGER
                )
            ''')

            # Create test_configurations table if not exists
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
                    manifests_yaml TEXT,
                    architecture TEXT,
                    decode_tp INTEGER,
                    guidellm_raw_json TEXT,
                    FOREIGN KEY (run_id) REFERENCES optimization_runs (id),
                    UNIQUE(run_id, config_name)
                )
            ''')

            # Migrations: add columns to existing tables (no-op on fresh DBs)
            _migrations = [
                ('optimization_runs', 'goal', 'TEXT'),
                ('optimization_runs', 'test_duration', 'INTEGER DEFAULT 300'),
                ('optimization_runs', 'max_gpus', 'INTEGER DEFAULT 16'),
                ('optimization_runs', 'use_achievable_qps', 'INTEGER DEFAULT 0'),
                ('optimization_runs', 'isl_stdev', 'INTEGER'),
                ('optimization_runs', 'osl_stdev', 'INTEGER'),
                ('optimization_runs', 'turns', 'INTEGER DEFAULT 1'),
                ('optimization_runs', 'config_json', 'TEXT'),
                ('optimization_runs', 'constraint_notes', 'TEXT'),
                ('optimization_runs', 'latency_constraint_enabled', 'INTEGER DEFAULT 0'),
                ('optimization_runs', 'latency_constraint_ms', 'INTEGER DEFAULT 500'),
                ('optimization_runs', 'latency_constraint_percentile', "TEXT DEFAULT 'p99'"),
                ('optimization_runs', 'workload_mode', "TEXT DEFAULT 'synthetic'"),
                ('optimization_runs', 'dataset_source', 'TEXT'),
                ('optimization_runs', 'dataset_column', 'TEXT'),
                ('optimization_runs', 'dataset_max_output', 'INTEGER DEFAULT 256'),
                ('optimization_runs', 'rate_type', "TEXT DEFAULT 'concurrent'"),
                ('optimization_runs', 'prefix_cache_hit_pct', 'INTEGER DEFAULT 0'),
                ('optimization_runs', 'prefix_cache_seed', 'INTEGER'),
                ('test_configurations', 'manifests_yaml', 'TEXT'),
                ('test_configurations', 'architecture', 'TEXT'),
                ('test_configurations', 'decode_tp', 'INTEGER'),
                ('test_configurations', 'guidellm_raw_json', 'TEXT'),
                ('test_configurations', 'test_config_json', 'TEXT'),
            ]
            for table, col, col_type in _migrations:
                try:
                    cursor.execute(f'ALTER TABLE {table} ADD COLUMN {col} {col_type}')
                except sqlite3.OperationalError:
                    pass

            # Create optuna_trials table for per-trial Optuna search results
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

            # Create optuna_studies table for study-level summaries
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

            # Create latency_search_trials table for binary search results
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
                CREATE TABLE IF NOT EXISTS pod_error_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL,
                    test_id TEXT NOT NULL,
                    architecture TEXT,
                    summary TEXT,
                    errors_json TEXT NOT NULL,
                    pod_count INTEGER,
                    error_count INTEGER,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (run_id) REFERENCES optimization_runs (id)
                )
            ''')

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS mlflow_config (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    tracking_uri TEXT NOT NULL,
                    username TEXT,
                    password TEXT,
                    experiment_name TEXT,
                    updated_at TEXT
                )
            ''')

    def get_mlflow_config(self) -> Optional[Dict]:
        with self.get_connection() as conn:
            row = conn.execute('SELECT * FROM mlflow_config WHERE id = 1').fetchone()
            if row:
                return dict(row)
            return None

    def save_mlflow_config(self, tracking_uri: str, username: str = None,
                           password: str = None, experiment_name: str = None,
                           insecure_tls: bool = True):
        with self.get_connection() as conn:
            # Add insecure_tls column if missing
            try:
                conn.execute('ALTER TABLE mlflow_config ADD COLUMN insecure_tls INTEGER DEFAULT 1')
            except Exception:
                pass
            if password is None:
                existing = conn.execute('SELECT password FROM mlflow_config WHERE id = 1').fetchone()
                if existing:
                    password = existing['password']
            conn.execute('''
                INSERT INTO mlflow_config (id, tracking_uri, username, password, experiment_name, insecure_tls, updated_at)
                VALUES (1, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    tracking_uri=excluded.tracking_uri,
                    username=excluded.username,
                    password=excluded.password,
                    experiment_name=excluded.experiment_name,
                    insecure_tls=excluded.insecure_tls,
                    updated_at=excluded.updated_at
            ''', (tracking_uri, username, password, experiment_name, 1 if insecure_tls else 0, datetime.now().isoformat()))

    def save_pod_errors(self, run_id: int, test_id: str, errors_json: str,
                        architecture: Optional[str] = None):
        """Save pod error scan results."""
        scan_data = json.loads(errors_json)
        with self.get_connection() as conn:
            conn.execute('''
                INSERT INTO pod_error_logs
                (run_id, test_id, architecture, summary, errors_json,
                 pod_count, error_count, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                run_id, test_id, architecture,
                scan_data.get('summary', ''),
                errors_json,
                len(scan_data.get('pod_reports', [])),
                sum(len(r.get('errors', [])) for r in scan_data.get('pod_reports', [])),
                datetime.now().isoformat()
            ))

    def get_pod_errors(self, run_id: int) -> List[Dict[str, Any]]:
        """Get all pod error logs for a run."""
        with self.get_connection() as conn:
            rows = conn.execute(
                'SELECT * FROM pod_error_logs WHERE run_id = ? ORDER BY created_at DESC',
                (run_id,)
            ).fetchall()
            return [dict(row) for row in rows]

    def create_optimization_run(
        self,
        run_name: str,
        model: str,
        isl: int,
        osl: int,
        num_users: int,
        notes: Optional[str] = None,
        config_dict: Optional[Dict[str, Any]] = None
    ) -> int:
        """
        Create a new optimization run record.

        Args:
            run_name: Unique name for the run
            model: Model name/path
            isl: Input sequence length
            osl: Output sequence length
            num_users: Number of concurrent users
            notes: Optional notes
            config_dict: Full RecipeOptimizerConfig as dict (from config.to_dict())

        Returns:
            run_id: ID of created run
        """
        config_json = json.dumps(config_dict) if config_dict else None

        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO optimization_runs
                (run_name, model, isl, osl, num_users, status, created_at, notes,
                 goal, test_duration, max_gpus, use_achievable_qps,
                 isl_stdev, osl_stdev, turns, config_json,
                 latency_constraint_enabled, latency_constraint_ms,
                 latency_constraint_percentile,
                 workload_mode, dataset_source, dataset_column, dataset_max_output,
                 rate_type, prefix_cache_hit_pct, prefix_cache_seed)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                run_name,
                model,
                isl,
                osl,
                num_users,
                'running',
                datetime.now().isoformat(),
                notes,
                config_dict.get('objective') if config_dict else None,
                config_dict.get('test_duration', 300) if config_dict else 300,
                config_dict.get('total_gpus', 16) if config_dict else 16,
                1 if (config_dict or {}).get('use_achievable_qps') else 0,
                config_dict.get('isl_stdev') if config_dict else None,
                config_dict.get('osl_stdev') if config_dict else None,
                config_dict.get('turns', 1) if config_dict else 1,
                config_json,
                1 if (config_dict or {}).get('latency_constraint_enabled') else 0,
                config_dict.get('latency_constraint_ms', 500) if config_dict else 500,
                config_dict.get('latency_constraint_percentile', 'p99') if config_dict else 'p99',
                config_dict.get('workload_mode', 'synthetic') if config_dict else 'synthetic',
                config_dict.get('dataset_source') if config_dict else None,
                config_dict.get('dataset_column') if config_dict else None,
                config_dict.get('dataset_max_output', 256) if config_dict else 256,
                config_dict.get('rate_type', 'concurrent') if config_dict else 'concurrent',
                config_dict.get('prefix_cache_hit_pct', 0) if config_dict else 0,
                config_dict.get('prefix_cache_seed') if config_dict else None,
            ))
            return cursor.lastrowid

    def insert_test_result(
        self,
        run_id: int,
        test_config: TestConfig,
        test_result: TestResult,
        metrics: Optional[Dict[str, Any]] = None,
        manifests_yaml: Optional[str] = None
    ):
        """
        Insert test result immediately after test completes.

        Analyzes metrics.json to fill in missing data and calculate per-GPU breakdowns.

        Args:
            run_id: ID of the optimization run
            test_config: Test configuration
            test_result: Test result from orchestrator
            metrics: Additional metrics (guidellm + Thanos)
        """
        # Extract architecture info
        architecture = test_config.architecture or 'aggregated'

        # For pd/ep, use prefill/decode counts; for aggregated, use replicas
        if architecture in ('pd', 'ep'):
            prefill_pods = test_config.prefill_replicas or 0
            decode_pods = test_config.decode_replicas or 0
        else:
            prefill_pods = test_config.replicas or 0
            decode_pods = 0

        # Default metrics if not provided
        if metrics is None:
            metrics = {}

        # Extract metrics from test_result if available
        status = 'completed' if test_result.guidellm_success else 'failed'

        # Analyze metrics.json file to extract missing data (GPU util, KV cache, per-GPU throughput)
        gpu_utilization = test_result.gpu_utilization
        kv_cache_usage = test_result.kv_cache_usage
        per_gpu_throughput = None
        analyzed_data = {}

        metrics_file_path = getattr(test_result, 'metrics_file', None) or getattr(test_result, 'metrics_output', None)
        if metrics_file_path:
            try:
                metrics_path = Path(metrics_file_path)
                if metrics_path.exists():
                    analyzer = MetricsAnalyzer()

                    # Build guidellm result dict for analyzer
                    guidellm_data = {
                        'throughput_p90': test_result.throughput_p90,
                        'output_tokens': test_config.osl if hasattr(test_config, 'osl') else 1000
                    }

                    analyzed = analyzer.analyze_metrics_file(
                        metrics_path,
                        guidellm_data,
                        test_config.tensor_parallelism
                    )

                    # Fill in missing GPU utilization from DCGM
                    if analyzed.avg_gpu_utilization is not None and gpu_utilization is None:
                        gpu_utilization = analyzed.avg_gpu_utilization

                    # Fill in missing KV cache from vLLM or inference pool
                    if analyzed.kv_cache_usage_pct is not None and kv_cache_usage is None:
                        kv_cache_usage = analyzed.kv_cache_usage_pct

                    # Get per-GPU throughput
                    if analyzed.avg_throughput_per_gpu is not None:
                        per_gpu_throughput = analyzed.avg_throughput_per_gpu

                    # Store analyzed data for metrics_json
                    analyzed_data = {
                        'avg_gpu_memory_gb': analyzed.avg_gpu_memory_used_gb,
                        'peak_gpu_memory_gb': analyzed.peak_gpu_memory_used_gb,
                        'avg_power_watts': analyzed.avg_power_watts,
                        'peak_power_watts': analyzed.peak_power_watts,
                        'per_pod_metrics': [
                            {
                                'pod_name': p.pod_name,
                                'gpu_count': p.gpu_count,
                                'avg_gpu_util': p.avg_gpu_utilization,
                                'throughput_tokens_sec': p.avg_throughput_tokens_per_sec,
                                'throughput_per_gpu': p.avg_throughput_per_gpu
                            }
                            for p in (analyzed.pods or [])
                        ],
                        'missing_vllm_metrics': analyzed.missing_vllm_metrics,
                        'data_sources': analyzed.data_sources
                    }

            except Exception as e:
                # Log but don't fail the insert
                import logging
                logging.getLogger(__name__).warning(f"Failed to analyze metrics: {e}")

        # Extract Prometheus metric summaries from the raw metrics file
        prometheus_summaries = {}
        if metrics_file_path:
            try:
                prometheus_summaries = MetricsAnalyzer.extract_prometheus_summaries(Path(metrics_file_path))
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning(f"Failed to extract Prometheus summaries: {e}")

        # Serialize full metrics as JSON (includes all guidellm + Prometheus data)
        metrics_json = json.dumps({
            'success': test_result.guidellm_success,
            'deployment_success': test_result.deployment_success,
            'deployment_ready': test_result.deployment_ready,
            # TTFT (ms)
            'ttft_p50': test_result.ttft_p50,
            'ttft_p90': test_result.ttft_p90,
            'ttft_p95': test_result.ttft_p95,
            'ttft_p99': test_result.ttft_p99,
            'ttft_mean': getattr(test_result, 'ttft_mean', None),
            'ttft_min': getattr(test_result, 'ttft_min', None),
            'ttft_max': getattr(test_result, 'ttft_max', None),
            'ttft_std_dev': getattr(test_result, 'ttft_std_dev', None),
            'ttft_p25': getattr(test_result, 'ttft_p25', None),
            'ttft_p75': getattr(test_result, 'ttft_p75', None),
            # ITL (ms)
            'itl_p50': test_result.itl_p50,
            'itl_p90': test_result.itl_p90,
            'itl_p95': test_result.itl_p95,
            'itl_p99': test_result.itl_p99,
            'itl_mean': getattr(test_result, 'itl_mean', None),
            'itl_min': getattr(test_result, 'itl_min', None),
            'itl_max': getattr(test_result, 'itl_max', None),
            'itl_std_dev': getattr(test_result, 'itl_std_dev', None),
            # Throughput (req/s)
            'throughput_p50': test_result.throughput_p50,
            'throughput_p90': test_result.throughput_p90,
            'throughput_p95': test_result.throughput_p95,
            'throughput_p99': test_result.throughput_p99,
            'throughput_mean': getattr(test_result, 'throughput_mean', None),
            # TPOT (ms)
            'tpot_mean': getattr(test_result, 'tpot_mean', None),
            'tpot_p50': getattr(test_result, 'tpot_p50', None),
            'tpot_p90': getattr(test_result, 'tpot_p90', None),
            'tpot_p95': getattr(test_result, 'tpot_p95', None),
            'tpot_p99': getattr(test_result, 'tpot_p99', None),
            # E2E request latency (seconds)
            'e2e_latency_mean': getattr(test_result, 'e2e_latency_mean', None),
            'e2e_latency_p50': getattr(test_result, 'e2e_latency_p50', None),
            'e2e_latency_p90': getattr(test_result, 'e2e_latency_p90', None),
            'e2e_latency_p95': getattr(test_result, 'e2e_latency_p95', None),
            'e2e_latency_p99': getattr(test_result, 'e2e_latency_p99', None),
            # Output tokens/sec (decode throughput)
            'output_tps_mean': getattr(test_result, 'output_tps_mean', None),
            'output_tps_p50': getattr(test_result, 'output_tps_p50', None),
            'output_tps_p90': getattr(test_result, 'output_tps_p90', None),
            'output_tps_p95': getattr(test_result, 'output_tps_p95', None),
            'output_tps_p99': getattr(test_result, 'output_tps_p99', None),
            # Token counts
            'prompt_tokens_mean': getattr(test_result, 'prompt_tokens_mean', None),
            'output_tokens_mean': getattr(test_result, 'output_tokens_mean', None),
            # Concurrency
            'concurrency_mean': getattr(test_result, 'concurrency_mean', None),
            'concurrency_p50': getattr(test_result, 'concurrency_p50', None),
            'concurrency_p90': getattr(test_result, 'concurrency_p90', None),
            # Request totals
            'request_total': getattr(test_result, 'request_total', None),
            'request_successful': getattr(test_result, 'request_successful', None),
            'request_incomplete': getattr(test_result, 'request_incomplete', None),
            'request_errored': getattr(test_result, 'request_errored', None),
            # Benchmark timing
            'benchmark_duration_s': getattr(test_result, 'benchmark_duration_s', None),
            'warmup_duration_s': getattr(test_result, 'warmup_duration_s', None),
            # Infrastructure metrics
            'gpu_utilization': gpu_utilization,
            'kv_cache_usage': kv_cache_usage,
            'per_gpu_throughput_tokens_sec': per_gpu_throughput,
            'error_message': test_result.error_message,
            'analyzed_metrics': analyzed_data,
            'prometheus_metrics': prometheus_summaries,
            'additional_metrics': metrics,
            'vllm_available_kv_gb': getattr(test_result, 'vllm_available_kv_gb', None),
            'vllm_fixed_overhead_gb': getattr(test_result, 'vllm_fixed_overhead_gb', None),
            'vllm_gpu_blocks': getattr(test_result, 'vllm_gpu_blocks', None),
            'nixl_errors': getattr(test_result, 'nixl_errors', 0),
        })

        # Serialize full TestConfig for report detail view
        from dataclasses import asdict
        tc_dict = asdict(test_config)
        for key in ('hf_token', 'selected_nodes', 'rdma_device_resources'):
            tc_dict.pop(key, None)
        test_config_json = json.dumps(tc_dict)

        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO test_configurations
                (run_id, config_name, prefill_pods, decode_pods, tensor_parallelism,
                 status, ttft_p50, ttft_p90, ttft_p95, ttft_p99,
                 itl_p50, itl_p90, itl_p95, itl_p99,
                 throughput_p50, throughput_p90, throughput_p95, throughput_p99,
                 gpu_utilization, kv_cache_usage, started_at, completed_at, metrics_json,
                 manifests_yaml, architecture, decode_tp, test_config_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                run_id,
                test_config.test_id,
                prefill_pods,
                decode_pods,
                test_config.tensor_parallelism,
                status,
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
                gpu_utilization,
                kv_cache_usage,
                datetime.now().isoformat(),
                datetime.now().isoformat(),
                metrics_json,
                manifests_yaml,
                architecture,
                getattr(test_config, 'decode_tp', None),
                test_config_json,
            ))

    def update_run_status(
        self,
        run_id: int,
        status: str,
        optimal_config: Optional[str] = None,
        notes: Optional[str] = None
    ):
        """
        Update optimization run status.

        Args:
            run_id: ID of the run
            status: Status ('running', 'completed', 'failed')
            optimal_config: JSON string of optimal configuration
            notes: Additional notes
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()

            if status in ('completed', 'failed'):
                cursor.execute('''
                    UPDATE optimization_runs
                    SET status = ?, completed_at = ?, optimal_config = ?, notes = ?
                    WHERE id = ?
                ''', (status, datetime.now().isoformat(), optimal_config, notes, run_id))
            else:
                cursor.execute('''
                    UPDATE optimization_runs
                    SET status = ?, notes = ?
                    WHERE id = ?
                ''', (status, notes, run_id))

    def get_test_results(self, run_id: int) -> List[Dict[str, Any]]:
        """
        Get all test results for a run.

        Args:
            run_id: ID of the run

        Returns:
            List of test result dictionaries
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT * FROM test_configurations
                WHERE run_id = ?
                ORDER BY id ASC
            ''', (run_id,))

            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def get_run_info(self, run_id: int) -> Optional[Dict[str, Any]]:
        """
        Get optimization run information.

        Args:
            run_id: ID of the run

        Returns:
            Run information dictionary or None
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT * FROM optimization_runs
                WHERE id = ?
            ''', (run_id,))

            row = cursor.fetchone()
            return dict(row) if row else None

    def insert_optuna_trial(
        self,
        run_id: int,
        optimization_step: str,
        trial_number: int,
        trial_params: Dict[str, Any],
        test_id: str,
        guidellm_success: bool,
        ttft_ms: Optional[float],
        throughput: Optional[float],
        target_percentile: str,
        constraint_target_ms: float,
        meets_constraint: bool,
        objective_value: float,
        trial_state: str = 'COMPLETE',
        metrics: Optional[Dict[str, Any]] = None,
    ):
        """Insert a single Optuna trial result."""
        import math
        obj_val = None if (objective_value is None or math.isinf(objective_value)) else objective_value
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO optuna_trials
                (run_id, optimization_step, trial_number, trial_params_json,
                 test_id, guidellm_success, ttft_ms, throughput,
                 target_percentile, constraint_target_ms, meets_constraint,
                 objective_value, trial_state, created_at, metrics_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                run_id, optimization_step, trial_number,
                json.dumps(trial_params),
                test_id,
                1 if guidellm_success else 0,
                ttft_ms, throughput,
                target_percentile, constraint_target_ms,
                1 if meets_constraint else 0,
                obj_val, trial_state,
                datetime.now().isoformat(),
                json.dumps(metrics) if metrics else None,
            ))

    def insert_optuna_study(
        self,
        run_id: int,
        optimization_step: str,
        constraint_config: Dict[str, Any],
        search_range: Dict[str, Any],
        total_trials: int,
        feasible_trials: int,
        best_trial_number: Optional[int],
        best_params: Optional[Dict[str, Any]],
        best_throughput: Optional[float],
        best_latency_ms: Optional[float],
        best_config_source: str,
        study_status: str,
    ):
        """Insert or update an Optuna study summary."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO optuna_studies
                (run_id, optimization_step, constraint_config_json,
                 search_range_json, total_trials, feasible_trials,
                 best_trial_number, best_params_json, best_throughput,
                 best_latency_ms, best_config_source, study_status,
                 created_at, completed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                run_id, optimization_step,
                json.dumps(constraint_config),
                json.dumps(search_range),
                total_trials, feasible_trials,
                best_trial_number,
                json.dumps(best_params) if best_params else None,
                best_throughput, best_latency_ms,
                best_config_source, study_status,
                datetime.now().isoformat(),
                datetime.now().isoformat(),
            ))

    def get_optuna_trials(self, run_id: int, optimization_step: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get all Optuna trials for a run, optionally filtered by step."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            if optimization_step:
                cursor.execute('''
                    SELECT * FROM optuna_trials
                    WHERE run_id = ? AND optimization_step = ?
                    ORDER BY trial_number ASC
                ''', (run_id, optimization_step))
            else:
                cursor.execute('''
                    SELECT * FROM optuna_trials
                    WHERE run_id = ?
                    ORDER BY optimization_step, trial_number ASC
                ''', (run_id,))
            return [dict(row) for row in cursor.fetchall()]

    def get_optuna_study(self, run_id: int, optimization_step: str) -> Optional[Dict[str, Any]]:
        """Get Optuna study summary for a run and step."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT * FROM optuna_studies
                WHERE run_id = ? AND optimization_step = ?
            ''', (run_id, optimization_step))
            row = cursor.fetchone()
            return dict(row) if row else None

    def insert_latency_search_trial(
        self,
        run_id: int,
        architecture: str,
        trial_number: int,
        search_phase: str,
        concurrency: int,
        test_id: str,
        guidellm_success: bool,
        meets_sla: bool,
        result=None,
        target_ms: float = 0,
        target_percentile: str = '',
    ):
        """Insert a latency binary search trial with full percentile data."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO latency_search_trials
                (run_id, architecture, trial_number, search_phase,
                 concurrency, test_id, guidellm_success, meets_sla,
                 ttft_p50, ttft_p90, ttft_p95, ttft_p99,
                 itl_p50, itl_p90, itl_p95, itl_p99,
                 throughput_p50, throughput_p90, throughput_p95, throughput_p99,
                 target_ms, target_percentile, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                run_id, architecture, trial_number, search_phase,
                concurrency, test_id,
                1 if guidellm_success else 0,
                1 if meets_sla else 0,
                getattr(result, 'ttft_p50', None) if result else None,
                getattr(result, 'ttft_p90', None) if result else None,
                getattr(result, 'ttft_p95', None) if result else None,
                getattr(result, 'ttft_p99', None) if result else None,
                getattr(result, 'itl_p50', None) if result else None,
                getattr(result, 'itl_p90', None) if result else None,
                getattr(result, 'itl_p95', None) if result else None,
                getattr(result, 'itl_p99', None) if result else None,
                getattr(result, 'throughput_p50', None) if result else None,
                getattr(result, 'throughput_p90', None) if result else None,
                getattr(result, 'throughput_p95', None) if result else None,
                getattr(result, 'throughput_p99', None) if result else None,
                target_ms, target_percentile,
                datetime.now().isoformat(),
            ))

    def get_latency_search_trials(
        self, run_id: int, architecture: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Get all latency search trials for a run, optionally filtered by architecture."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            if architecture:
                cursor.execute('''
                    SELECT * FROM latency_search_trials
                    WHERE run_id = ? AND architecture = ?
                    ORDER BY trial_number ASC
                ''', (run_id, architecture))
            else:
                cursor.execute('''
                    SELECT * FROM latency_search_trials
                    WHERE run_id = ?
                    ORDER BY architecture, trial_number ASC
                ''', (run_id,))
            return [dict(row) for row in cursor.fetchall()]
