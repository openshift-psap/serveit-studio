"""Guidellm result parsing and static helpers."""

import logging
import subprocess
from typing import Optional, Callable

from core.orchestrator.result import TestResult

logger = logging.getLogger(__name__)


class ParserMixin:
    """Mixin providing parsing and static helper methods."""

    @staticmethod
    def _parse_guidellm_results(guidellm_output: str, result: TestResult) -> None:
        """
        Parse guidellm JSON output and populate ALL TestResult metrics.

        Extracts the complete set of metrics from guidellm output so the
        raw JSON file is not needed after parsing.
        """
        try:
            import json
            from pathlib import Path

            if not guidellm_output or not Path(guidellm_output).exists():
                logger.warning(f"guidellm output file not found: {guidellm_output}")
                return

            with open(guidellm_output, "r") as f:
                raw_content = f.read()

            result.guidellm_raw_json = raw_content
            data = json.loads(raw_content)

            benchmarks = data.get("benchmarks", [])
            if not benchmarks:
                logger.warning("No benchmarks found in guidellm output")
                return

            if len(benchmarks) > 1:
                # Parallel workloads: one benchmark entry per workload shape.
                # Per-workload numbers live in result.workloads; the main
                # fields carry the request-weighted combined view (an
                # approximation of the pooled distribution — the true
                # system-wide numbers come from Prometheus, which sees all
                # traffic through the gateway).
                ParserMixin._parse_parallel_benchmarks(benchmarks, result)
                return

            ParserMixin._parse_single_benchmark(benchmarks[0], result)

        except Exception as e:
            logger.error(f"Failed to parse guidellm results: {e}")

    @staticmethod
    def _parse_parallel_benchmarks(benchmarks, result):
        """Aggregate parallel workload benchmarks into one TestResult.

        Each benchmark entry is parsed into a throwaway TestResult, then:
        - per-workload summaries land in result.workloads
        - percentile fields become request-count-weighted averages
        - throughput and request totals sum across workloads
        """
        from core.orchestrator.result import TestResult as _TR

        subs = []
        for i, bench in enumerate(benchmarks):
            sub = _TR(
                test_id=f"{result.test_id}-w{i}",
                architecture=result.architecture,
                deployment_success=True,
                deployment_ready=True,
                guidellm_success=True,
                metrics_collected=False,
                deployment_start_time="",
            )
            ParserMixin._parse_single_benchmark(bench, sub)
            subs.append(sub)

        def _wavg(vals_weights):
            total_w = sum(w for _, w in vals_weights if w)
            if not total_w:
                return None
            return sum(v * w for v, w in vals_weights if v and w) / total_w

        def _n(sub):
            return sub.request_successful or sub.request_total or 0

        for field in (
            "ttft_p50",
            "ttft_p90",
            "ttft_p95",
            "ttft_p99",
            "ttft_mean",
            "itl_p50",
            "itl_p90",
            "itl_p95",
            "itl_p99",
            "itl_mean",
            "e2e_latency_p50",
            "e2e_latency_p90",
            "e2e_latency_p95",
            "e2e_latency_p99",
            "e2e_latency_mean",
            "tpot_mean",
            "tpot_p90",
        ):
            setattr(result, field, _wavg([(getattr(s, field), _n(s)) for s in subs]))
        for field in ("throughput_mean", "throughput_p90", "output_tps_mean"):
            setattr(result, field, sum(getattr(s, field) or 0 for s in subs))
        result.request_total = sum(s.request_total or 0 for s in subs)
        result.request_successful = sum(s.request_successful or 0 for s in subs)
        result.request_errored = sum(s.request_errored or 0 for s in subs)
        result.request_incomplete = sum(s.request_incomplete or 0 for s in subs)
        result.benchmark_duration_s = max(s.benchmark_duration_s or 0 for s in subs)
        # Window-exact combined tok/s: parallel workloads share the wall
        # clock, so sum their token totals and divide by the longest
        # elapsed window (NOT the sum of per-workload rates).
        _total_tokens = 0.0
        for bench, sub in zip(benchmarks, subs):
            try:
                _total_tokens += (
                    bench.get("metrics", {})
                    .get("total_token_count", {})
                    .get("total", {})
                    .get("total_sum")
                    or 0
                )
            except Exception:
                pass
        if _total_tokens and result.benchmark_duration_s:
            result.tokens_per_second = _total_tokens / result.benchmark_duration_s
        result.guidellm_success = all(s.guidellm_success for s in subs)
        if not result.guidellm_success:
            result.error_message = "; ".join(s.error_message for s in subs if s.error_message)

        result.workloads = [
            {
                "name": (bench.get("workload_name") or f"workload-{i}"),
                "isl": (bench.get("metrics", {}).get("prompt_token_count", {}).get("successful", {}).get("mean")),
                "osl": (bench.get("metrics", {}).get("output_token_count", {}).get("successful", {}).get("mean")),
                "request_successful": sub.request_successful,
                "request_total": sub.request_total,
                "tokens": (
                    bench.get("metrics", {}).get("total_token_count", {}).get("total", {}).get("total_sum")
                ),
                "tokens_per_second": sub.tokens_per_second,
                "ttft_p50": sub.ttft_p50,
                "ttft_p90": sub.ttft_p90,
                "ttft_p99": sub.ttft_p99,
                "itl_p90": sub.itl_p90,
                "e2e_latency_p50": sub.e2e_latency_p50,
                "e2e_latency_p90": sub.e2e_latency_p90,
                "e2e_latency_p99": sub.e2e_latency_p99,
                "throughput_mean": sub.throughput_mean,
            }
            for i, (bench, sub) in enumerate(zip(benchmarks, subs))
        ]
        logger.info(
            f"Parsed {len(benchmarks)} parallel workloads: "
            f"combined TTFT p90={result.ttft_p90}ms, "
            f"{result.request_successful}/{result.request_total} requests ok"
        )

    @staticmethod
    def _parse_single_benchmark(bench, result):
        """Parse one guidellm benchmark entry into a TestResult."""
        try:
            metrics = bench.get("metrics", {})

            def _get_dist(metric_key):
                """Get the successful DistributionSummary for a metric."""
                return metrics.get(metric_key, {}).get("successful", {})

            def _pcts(dist):
                """Extract percentiles dict from a DistributionSummary."""
                return dist.get("percentiles", {})

            # --- TTFT (Time to First Token, ms) ---
            # TTFT is valid the moment the first token arrives — a request
            # cut off at window end ('incomplete') still has a real TTFT.
            # guidellm files those under the 'total' bucket only, and in
            # short screen runs the slow long-output workloads never
            # complete, silently censoring their (slowest) TTFTs out of
            # the 'successful' dists. Fall back to 'total' when successful
            # has no samples.
            ttft_metric = metrics.get("time_to_first_token_ms", {})
            ttft_dist = ttft_metric.get("successful", {})
            if not (ttft_dist or {}).get("count"):
                _total_dist = ttft_metric.get("total", {}) or {}
                if _total_dist.get("count"):
                    ttft_dist = _total_dist
            ttft_p = _pcts(ttft_dist)
            result.ttft_p50 = ttft_p.get("p50")
            result.ttft_p90 = ttft_p.get("p90")
            result.ttft_p95 = ttft_p.get("p95")
            result.ttft_p99 = ttft_p.get("p999")
            result.ttft_mean = ttft_dist.get("mean")
            result.ttft_min = ttft_dist.get("min")
            result.ttft_max = ttft_dist.get("max")
            result.ttft_std_dev = ttft_dist.get("std_dev")
            result.ttft_p25 = ttft_p.get("p25")
            result.ttft_p75 = ttft_p.get("p75")

            # --- ITL (Inter-Token Latency, ms) ---
            itl_dist = _get_dist("inter_token_latency_ms")
            itl_p = _pcts(itl_dist)
            result.itl_p50 = itl_p.get("p50")
            result.itl_p90 = itl_p.get("p90")
            result.itl_p95 = itl_p.get("p95")
            result.itl_p99 = itl_p.get("p999")
            result.itl_mean = itl_dist.get("mean")
            result.itl_min = itl_dist.get("min")
            result.itl_max = itl_dist.get("max")
            result.itl_std_dev = itl_dist.get("std_dev")

            # --- Throughput (requests/sec) ---
            # guidellm 0.7.x: percentiles are per-second snapshot rates (not aggregate)
            # Use mean for all throughput values — it's the true aggregate req/s
            # Also compute from request_totals/duration as a fallback
            rps_dist = _get_dist("requests_per_second")
            rps_mean = rps_dist.get("mean", 0)

            # Fallback: calculate from totals
            request_totals = metrics.get("request_totals", {})
            total_reqs = request_totals.get("successful", request_totals.get("total", 0))
            duration = bench.get("duration", 0)
            if not rps_mean and total_reqs and duration:
                rps_mean = total_reqs / duration

            result.throughput_mean = rps_mean
            result.throughput_p50 = rps_mean
            result.throughput_p90 = rps_mean
            result.throughput_p95 = rps_mean
            result.throughput_p99 = rps_mean

            # --- TPOT (Time Per Output Token, ms) ---
            tpot_dist = _get_dist("time_per_output_token_ms")
            tpot_p = _pcts(tpot_dist)
            result.tpot_mean = tpot_dist.get("mean")
            result.tpot_p50 = tpot_p.get("p50")
            result.tpot_p90 = tpot_p.get("p90")
            result.tpot_p95 = tpot_p.get("p95")
            result.tpot_p99 = tpot_p.get("p999")

            # --- E2E Request Latency (seconds) ---
            lat_dist = _get_dist("request_latency")
            lat_p = _pcts(lat_dist)
            result.e2e_latency_mean = lat_dist.get("mean")
            result.e2e_latency_p50 = lat_p.get("p50")
            result.e2e_latency_p90 = lat_p.get("p90")
            result.e2e_latency_p95 = lat_p.get("p95")
            result.e2e_latency_p99 = lat_p.get("p999")

            # --- Output Tokens Per Second (decode throughput) ---
            otps_dist = _get_dist("output_tokens_per_second")
            otps_p = _pcts(otps_dist)
            result.output_tps_mean = otps_dist.get("mean")
            result.output_tps_p50 = otps_p.get("p50")
            result.output_tps_p90 = otps_p.get("p90")
            result.output_tps_p95 = otps_p.get("p95")
            result.output_tps_p99 = otps_p.get("p999")

            # --- Token Counts ---
            prompt_dist = _get_dist("prompt_token_count")
            result.prompt_tokens_mean = prompt_dist.get("mean")
            output_dist = _get_dist("output_token_count")
            result.output_tokens_mean = output_dist.get("mean")

            # --- Concurrency ---
            conc_dist = _get_dist("request_concurrency")
            conc_p = _pcts(conc_dist)
            result.concurrency_mean = conc_dist.get("mean")
            result.concurrency_p50 = conc_p.get("p50")
            result.concurrency_p90 = conc_p.get("p90")

            # --- Request Totals ---
            totals = metrics.get("request_totals", {})
            result.request_total = totals.get("total")
            result.request_successful = totals.get("successful")
            result.request_incomplete = totals.get("incomplete")
            result.request_errored = totals.get("errored")

            # --- Benchmark Timing ---
            result.benchmark_duration_s = bench.get("duration")
            result.warmup_duration_s = bench.get("warmup_duration")

            # --- Window-exact token throughput ---
            # Total tokens served (prompt+output, all outcomes) over the
            # benchmark's own elapsed window. Unlike Prometheus window
            # averages — which dilute by deploy/model-load idle time and
            # vary with when collection started — this is comparable
            # across single tests and short screen runs.
            try:
                _tt = (
                    metrics.get("total_token_count", {})
                    .get("total", {})
                    .get("total_sum")
                )
                _elapsed = None
                if bench.get("start_time") and bench.get("end_time"):
                    _elapsed = bench["end_time"] - bench["start_time"]
                _elapsed = _elapsed or bench.get("duration")
                if _tt and _elapsed and _elapsed > 0:
                    result.tokens_per_second = _tt / _elapsed
            except Exception:
                pass

            if not result.request_successful or result.request_successful == 0:
                logger.warning(
                    f"No successful requests in guidellm output ({result.request_errored} errored, {result.request_incomplete} incomplete)"
                )
                result.guidellm_success = False
                result.error_message = f"All requests failed ({result.request_errored or 0} errored, {result.request_incomplete or 0} incomplete)"
            else:
                logger.info(
                    f"Parsed guidellm metrics: TTFT p90={result.ttft_p90}ms, "
                    f"Throughput p90={result.throughput_p90} req/s, "
                    f"TPOT p90={result.tpot_p90}ms, "
                    f"Requests: {result.request_successful}/{result.request_total} ok"
                )

        except Exception as e:
            logger.error(f"Failed to parse guidellm benchmark: {e}")

    @staticmethod
    def _get_thanos_url() -> Optional[str]:
        """
        Dynamically fetch Thanos URL from OpenShift cluster.

        Returns:
            Thanos URL or None if not found
        """
        try:
            # Try kubectl (works in both OpenShift and vanilla k8s)
            cmd = [
                "kubectl",
                "get",
                "route",
                "thanos-querier",
                "-n",
                "openshift-monitoring",
                "-o",
                "jsonpath={.spec.host}",
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=False)

            if result.returncode == 0 and result.stdout.strip():
                host = result.stdout.strip()
                thanos_url = f"https://{host}"
                logger.info(f"Discovered Thanos URL: {thanos_url}")
                return thanos_url
            else:
                logger.warning(f"Could not discover Thanos URL from cluster: {result.stderr}")
                return None
        except Exception as e:
            logger.error(f"Failed to get Thanos URL: {e}")
            return None

    @staticmethod
    def _enable_namespace_monitoring(namespace: str) -> bool:
        """
        Enable User Workload Monitoring on OpenShift namespace.

        This labels the namespace with openshift.io/user-monitoring=true
        which allows Prometheus to scrape metrics from pods in the namespace.

        Args:
            namespace: Kubernetes namespace to label

        Returns:
            True if successful or not on OpenShift, False if failed
        """
        try:
            # Check if running on OpenShift (has 'oc' command or route.openshift.io API)
            check_cmd = ["kubectl", "api-resources", "--api-group=route.openshift.io"]
            result = subprocess.run(check_cmd, capture_output=True, text=True, timeout=10, check=False)

            if result.returncode != 0 or not result.stdout.strip():
                logger.info("Not running on OpenShift - skipping namespace monitoring label")
                return True

            # Running on OpenShift - label the namespace
            logger.info(f"Enabling user workload monitoring on namespace: {namespace}")
            label_cmd = ["kubectl", "label", "namespace", namespace, "openshift.io/user-monitoring=true", "--overwrite"]
            result = subprocess.run(label_cmd, capture_output=True, text=True, timeout=10, check=False)

            if result.returncode == 0:
                logger.info(f"Successfully enabled monitoring on namespace: {namespace}")
                return True
            else:
                logger.warning(f"Failed to label namespace: {result.stderr}")
                return False
        except Exception as e:
            logger.warning(f"Failed to enable namespace monitoring: {e}")
            return False

    def _profile_vllm_memory(
        self,
        test_id: str,
        gpu_memory_utilization: float,
        gpu_vram_gb: float,
        result: TestResult,
        log_callback: Optional[Callable[[str], None]] = None,
    ) -> None:
        """Read vLLM pod logs after startup to measure actual GPU memory overhead.

        Parses 'Available KV cache memory' and '# GPU blocks' from vLLM logs.
        Computes fixed_overhead = (gpu_vram × U) - available_kv_memory, which
        is the total non-KV memory (model weights + CUDA graphs + workspace).
        """
        try:
            kubectl = self.deployment_manager.kubectl
            pods_result = kubectl.run(
                [
                    "get",
                    "pods",
                    "-n",
                    self.namespace,
                    "-l",
                    f"llm-d.ai/test-id={test_id}",
                    "-o",
                    'jsonpath={range .items[*]}{.metadata.name}{"\\n"}{end}',
                ],
                check=False,
            )
            if pods_result.returncode != 0 or not pods_result.stdout.strip():
                return

            pod_name = pods_result.stdout.strip().splitlines()[0]
            logs_result = kubectl.run(["logs", "-n", self.namespace, pod_name, "-c", "vllm", "--tail=200"], check=False)
            if logs_result.returncode != 0:
                return

            import re

            for line in logs_result.stdout.splitlines():
                # Parse "Available KV cache memory: X.XX GiB"
                m = re.search(r"Available KV cache memory:\s*([-\d.]+)\s*GiB", line)
                if m:
                    result.vllm_available_kv_gb = float(m.group(1))

                # Parse "# GPU blocks: XXXX"
                m = re.search(r"#\s*GPU blocks:\s*(\d+)", line)
                if m:
                    result.vllm_gpu_blocks = int(m.group(1))

            if result.vllm_available_kv_gb is not None:
                budget_gb = gpu_vram_gb * gpu_memory_utilization
                result.vllm_fixed_overhead_gb = budget_gb - result.vllm_available_kv_gb
                if log_callback:
                    log_callback(
                        f"   📐 vLLM memory profile: overhead={result.vllm_fixed_overhead_gb:.1f}GB "
                        f"(budget={budget_gb:.1f}GB, KV available={result.vllm_available_kv_gb:.1f}GB)"
                    )

        except Exception as e:
            logger.debug(f"Failed to profile vLLM memory: {e}")
