"""
ServeIt Studio Test Orchestrator

Orchestrates the complete test flow: deploy → run guidellm → collect metrics → cleanup.
Coordinates between deployment_manager, metrics_collector, and database.
"""

import os
import time
import json
import logging
import subprocess
from typing import List, Optional, Callable
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path

from core.config_generator import TestConfig, OptimizationPlan
from core.deployment_manager import DeploymentManager
from core.metrics_collector import MetricsCollector, MetricsConfig

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


@dataclass
class TestResult:
    """Result of a single test run."""
    test_id: str
    architecture: str
    deployment_success: bool
    deployment_ready: bool
    guidellm_success: bool
    metrics_collected: bool

    # Timing
    deployment_start_time: str
    deployment_ready_time: Optional[str] = None
    test_start_time: Optional[str] = None
    test_end_time: Optional[str] = None
    cleanup_time: Optional[str] = None

    # Results
    service_endpoint: Optional[str] = None
    guidellm_output: Optional[str] = None
    metrics_file: Optional[str] = None
    error_message: Optional[str] = None

    # Parsed metrics from guidellm results (percentiles)
    ttft_p50: Optional[float] = None
    ttft_p90: Optional[float] = None
    ttft_p95: Optional[float] = None
    ttft_p99: Optional[float] = None
    itl_p50: Optional[float] = None
    itl_p90: Optional[float] = None
    itl_p95: Optional[float] = None
    itl_p99: Optional[float] = None
    throughput_p50: Optional[float] = None
    throughput_p90: Optional[float] = None
    throughput_p95: Optional[float] = None
    throughput_p99: Optional[float] = None

    # Extended guidellm metrics
    guidellm_raw_json: Optional[str] = None

    # Request counts
    request_total: Optional[int] = None
    request_successful: Optional[int] = None
    request_incomplete: Optional[int] = None
    request_errored: Optional[int] = None

    # Benchmark timing
    benchmark_duration_s: Optional[float] = None
    warmup_duration_s: Optional[float] = None

    # TPOT (Time Per Output Token) — avg time per decoded token
    tpot_mean: Optional[float] = None
    tpot_p50: Optional[float] = None
    tpot_p90: Optional[float] = None
    tpot_p95: Optional[float] = None
    tpot_p99: Optional[float] = None

    # E2E request latency (seconds)
    e2e_latency_mean: Optional[float] = None
    e2e_latency_p50: Optional[float] = None
    e2e_latency_p90: Optional[float] = None
    e2e_latency_p95: Optional[float] = None
    e2e_latency_p99: Optional[float] = None

    # Token throughput (tokens/sec)
    output_tps_mean: Optional[float] = None
    output_tps_p50: Optional[float] = None
    output_tps_p90: Optional[float] = None
    output_tps_p95: Optional[float] = None
    output_tps_p99: Optional[float] = None

    # Token counts
    prompt_tokens_mean: Optional[float] = None
    output_tokens_mean: Optional[float] = None

    # Concurrency
    concurrency_mean: Optional[float] = None
    concurrency_p50: Optional[float] = None
    concurrency_p90: Optional[float] = None

    # Extended TTFT/ITL percentiles (full distribution)
    ttft_mean: Optional[float] = None
    ttft_min: Optional[float] = None
    ttft_max: Optional[float] = None
    ttft_std_dev: Optional[float] = None
    ttft_p25: Optional[float] = None
    ttft_p75: Optional[float] = None
    itl_mean: Optional[float] = None
    itl_min: Optional[float] = None
    itl_max: Optional[float] = None
    itl_std_dev: Optional[float] = None

    # Throughput (req/s) extended stats
    throughput_mean: Optional[float] = None

    # Metrics from Prometheus/Thanos
    gpu_utilization: Optional[float] = None
    kv_cache_usage: Optional[float] = None

    # vLLM memory profiling (measured from pod logs after startup)
    vllm_gpu_blocks: Optional[int] = None
    vllm_available_kv_gb: Optional[float] = None
    vllm_fixed_overhead_gb: Optional[float] = None

    # Pod error scanning
    pod_errors_detected: bool = False
    pod_errors_json: Optional[str] = None
    nixl_errors: int = 0


