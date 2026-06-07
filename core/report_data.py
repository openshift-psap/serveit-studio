"""
Report data models and database loader.

Contains TestResult and ParetoPoint dataclasses, and ReportDataLoader
for reading test results from the SQLite database.
"""

import sqlite3
import logging
from typing import List, Optional
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class TestResult:
    """Individual test result from database."""
    id: int
    config_name: str
    architecture: str
    prefill_pods: int
    decode_pods: int
    tensor_parallelism: int
    prefill_tp: Optional[int]
    decode_tp: Optional[int]
    status: str

    # Performance metrics
    ttft_p50: Optional[float]
    ttft_p90: Optional[float]
    ttft_p95: Optional[float]
    ttft_p99: Optional[float]
    itl_p50: Optional[float]
    itl_p90: Optional[float]
    itl_p95: Optional[float]
    itl_p99: Optional[float]
    throughput_p50: Optional[float]
    throughput_p90: Optional[float]
    throughput_p95: Optional[float]
    throughput_p99: Optional[float]

    # Resource metrics
    gpu_utilization: Optional[float]
    kv_cache_usage: Optional[float]

    # Timing
    started_at: Optional[str]
    completed_at: Optional[str]

    # Raw data
    metrics_json: Optional[str]
    manifests_yaml: Optional[str]
    test_config_json: Optional[str] = None

    @property
    def throughput_mean(self) -> Optional[float]:
        """Actual requests per second (request_successful / test_duration)."""
        if self.metrics_json and self.test_config_json:
            try:
                import json
                m = json.loads(self.metrics_json)
                tc = json.loads(self.test_config_json)
                req_ok = m.get('request_successful', 0)
                duration = tc.get('test_duration', 0)
                if req_ok > 0 and duration > 0:
                    return round(req_ok / duration, 2)
            except Exception:
                pass
        # Fallback to guidellm metric if no request count available
        if self.metrics_json:
            try:
                import json
                return json.loads(self.metrics_json).get('throughput_mean')
            except Exception:
                pass
        return None

    @property
    def total_gpus(self) -> int:
        """Calculate total GPUs used."""
        if self.architecture == 'pd':
            prefill_gpus = self.prefill_pods * (self.prefill_tp or self.tensor_parallelism)
            decode_gpus = self.decode_pods * (self.decode_tp or self.tensor_parallelism)
            return prefill_gpus + decode_gpus
        else:  # aggregated or ep
            total_pods = self.prefill_pods + self.decode_pods
            return total_pods * self.tensor_parallelism

    @property
    def display_label(self) -> str:
        """Human-readable label for charts (e.g. '3P×TP8 + 1D×TP8')."""
        if self.architecture == 'pd':
            ptp = self.prefill_tp or self.tensor_parallelism
            dtp = self.decode_tp or self.tensor_parallelism
            return f"{self.prefill_pods}P×TP{ptp} + {self.decode_pods}D×TP{dtp}"
        else:
            total_pods = self.prefill_pods + self.decode_pods
            arch = self.architecture or 'aggregated'
            return f"{total_pods}×TP{self.tensor_parallelism} ({arch})"

    @property
    def is_successful(self) -> bool:
        """Check if test completed successfully with valid metrics."""
        return (self.status == 'completed' and
                self.ttft_p90 is not None and
                self.throughput_p90 is not None and
                self.ttft_p90 >= 0 and
                self.throughput_p90 > 0 and
                self.ttft_p90 < 1000000)  # Filter out penalty values


@dataclass
class ParetoPoint:
    """Point on Pareto frontier."""
    config: TestResult
    ttft: float  # ms
    throughput: float  # req/s
    cost: int  # Total GPUs
    efficiency: float  # throughput / GPU

    def __str__(self) -> str:
        return (f"{self.config.config_name}: "
                f"TTFT={self.ttft:.1f}ms, "
                f"Throughput={self.throughput:.2f}req/s, "
                f"GPUs={self.cost}, "
                f"Efficiency={self.efficiency:.3f}req/s/GPU")


class ReportDataLoader:
    """Load test results from the SQLite database."""

    def __init__(self, db_path: str = '/mnt/storage/serveit.db'):
        self.db_path = db_path
        self.conn: Optional[sqlite3.Connection] = None

    def __enter__(self):
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.conn:
            self.conn.close()

    def get_all_test_results(self, run_id: Optional[int] = None) -> List[TestResult]:
        """Load all test results from database."""
        query = """
            SELECT
                tc.*,
                CASE
                    WHEN tc.prefill_pods > 0 AND tc.decode_pods > 0 THEN 'pd'
                    ELSE 'aggregated'
                END as architecture
            FROM test_configurations tc
        """

        params = []
        if run_id is not None:
            query += " WHERE tc.run_id = ?"
            params.append(run_id)

        query += " ORDER BY tc.id"

        cursor = self.conn.cursor()
        cursor.execute(query, params)

        results = []
        for row in cursor.fetchall():
            config_name = row['config_name']
            prefill_tp = None
            decode_tp = None

            # Try to extract ptp value (e.g., "ptp4" -> 4)
            if 'ptp' in config_name:
                try:
                    start = config_name.index('ptp') + 3
                    end = start
                    while end < len(config_name) and config_name[end].isdigit():
                        end += 1
                    if end > start:
                        prefill_tp = int(config_name[start:end])
                except (ValueError, IndexError):
                    pass

            # Try to extract dtp value (e.g., "dtp2" -> 2)
            if 'dtp' in config_name:
                try:
                    start = config_name.index('dtp') + 3
                    end = start
                    while end < len(config_name) and config_name[end].isdigit():
                        end += 1
                    if end > start:
                        decode_tp = int(config_name[start:end])
                except (ValueError, IndexError):
                    pass

            # Safely read optional fields (may not exist in older DBs)
            try:
                manifests_yaml = row['manifests_yaml']
            except (IndexError, KeyError):
                manifests_yaml = None
            try:
                test_config_json = row['test_config_json']
            except (IndexError, KeyError):
                test_config_json = None

            results.append(TestResult(
                id=row['id'],
                config_name=config_name,
                architecture=row['architecture'],
                prefill_pods=row['prefill_pods'],
                decode_pods=row['decode_pods'],
                tensor_parallelism=row['tensor_parallelism'],
                prefill_tp=prefill_tp,
                decode_tp=decode_tp,
                status=row['status'],
                ttft_p50=row['ttft_p50'],
                ttft_p90=row['ttft_p90'],
                ttft_p95=row['ttft_p95'],
                ttft_p99=row['ttft_p99'],
                itl_p50=row['itl_p50'],
                itl_p90=row['itl_p90'],
                itl_p95=row['itl_p95'],
                itl_p99=row['itl_p99'],
                throughput_p50=row['throughput_p50'],
                throughput_p90=row['throughput_p90'],
                throughput_p95=row['throughput_p95'],
                throughput_p99=row['throughput_p99'],
                gpu_utilization=row['gpu_utilization'],
                kv_cache_usage=row['kv_cache_usage'],
                started_at=row['started_at'],
                completed_at=row['completed_at'],
                metrics_json=row['metrics_json'],
                manifests_yaml=manifests_yaml,
                test_config_json=test_config_json
            ))

        return results
