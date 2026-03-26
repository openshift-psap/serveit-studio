"""
Pod Error Scanner

Scans vLLM pod logs for critical errors after each test.
If errors are found, the optimization stops and leaves pods running
so the user can investigate.
"""

import re
import subprocess
import json
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import List, Optional


ERROR_PATTERNS = [
    ('CUDA_OOM', re.compile(r'CUDA out of memory|torch\.cuda\.OutOfMemoryError|cudaErrorMemoryAllocation')),
    ('CUDA_ERROR', re.compile(r'CUDA error|cudaError[A-Z]|CUDA_ERROR_')),
    ('NCCL_ERROR', re.compile(r'NCCL error|NCCL WARN.*(?:Timeout|error|fail)|ncclInternalError|ncclSystemError|ncclRemoteError')),
    ('NCCL_TIMEOUT', re.compile(r'Watchdog caught collective operation timeout|NCCL_ASYNC_ERROR')),
    ('OOM_KILLED', re.compile(r'Killed|oom-kill|Cannot allocate memory|OOMKilled')),
    ('NIXL_ERROR', re.compile(r'nixl.*(?:error|fail)|NIXL.*(?:error|fail)|xfer.*failed', re.IGNORECASE)),
    ('SEGFAULT', re.compile(r'Segmentation fault|SIGSEGV|signal 11|core dumped')),
    ('VLLM_FATAL', re.compile(r'FATAL|RuntimeError.*(?:CUDA|NCCL|out of memory)', re.IGNORECASE)),
    ('PYTHON_TRACEBACK', re.compile(r'^Traceback \(most recent call last\):')),
]

# Lines that match error patterns but are harmless
FALSE_POSITIVE_PATTERNS = [
    re.compile(r'error[_\s]*(rate|count|ratio)\s*[:=]\s*0', re.IGNORECASE),
    re.compile(r'no error', re.IGNORECASE),
    re.compile(r'error_code.*OK', re.IGNORECASE),
    re.compile(r'WARNING.*deprecated', re.IGNORECASE),
    re.compile(r'InsecureRequestWarning'),
]


@dataclass
class PodError:
    pattern_name: str
    line: str
    line_number: int


@dataclass
class PodErrorReport:
    pod_name: str
    errors: List[PodError] = field(default_factory=list)
    log_tail: str = ''


@dataclass
class ErrorScanResult:
    has_errors: bool = False
    pod_reports: List[PodErrorReport] = field(default_factory=list)
    scan_timestamp: str = ''
    test_id: str = ''
    summary: str = ''

    def to_json(self) -> str:
        return json.dumps(asdict(self), default=str)


class PodErrorsDetected(Exception):
    """Raised when critical errors are found in pod logs after a test."""
    def __init__(self, scan_result, test_id: str):
        self.scan_result = scan_result
        self.test_id = test_id
        summary = scan_result.summary if hasattr(scan_result, 'summary') else scan_result.get('summary', str(scan_result))
        super().__init__(f"Critical pod errors in {test_id}: {summary}")


def _is_false_positive(line: str) -> bool:
    return any(fp.search(line) for fp in FALSE_POSITIVE_PATTERNS)


def scan_pod_logs(namespace: str, test_id: str, tail_lines: int = 500) -> ErrorScanResult:
    """Scan all pod logs for a test_id and return any critical errors found."""
    result = ErrorScanResult(
        scan_timestamp=datetime.now().isoformat(),
        test_id=test_id,
    )

    # Get all pods for this test
    try:
        pods_proc = subprocess.run(
            ['kubectl', 'get', 'pods', '-n', namespace,
             '-l', f'llm-d.ai/test-id={test_id}',
             '-o', 'jsonpath={range .items[*]}{.metadata.name}{"\\n"}{end}'],
            capture_output=True, text=True, timeout=15, check=False
        )
    except Exception:
        try:
            pods_proc = subprocess.run(
                ['oc', 'get', 'pods', '-n', namespace,
                 '-l', f'llm-d.ai/test-id={test_id}',
                 '-o', 'jsonpath={range .items[*]}{.metadata.name}{"\\n"}{end}'],
                capture_output=True, text=True, timeout=15, check=False
            )
        except Exception:
            result.summary = "Could not list pods for error scanning"
            return result

    if pods_proc.returncode != 0 or not pods_proc.stdout.strip():
        result.summary = "No pods found for error scanning"
        return result

    pod_names = [p for p in pods_proc.stdout.strip().splitlines() if p]

    for pod_name in pod_names:
        report = _scan_single_pod(namespace, pod_name, tail_lines)
        if report.errors:
            result.pod_reports.append(report)

    if result.pod_reports:
        result.has_errors = True
        total_errors = sum(len(r.errors) for r in result.pod_reports)
        affected_pods = len(result.pod_reports)
        error_types = sorted(set(e.pattern_name for r in result.pod_reports for e in r.errors))
        result.summary = (
            f"{total_errors} error(s) in {affected_pods}/{len(pod_names)} pod(s): "
            f"{', '.join(error_types)}"
        )
    else:
        result.summary = f"No errors found in {len(pod_names)} pod(s)"

    return result


def _scan_single_pod(namespace: str, pod_name: str, tail_lines: int) -> PodErrorReport:
    """Scan a single pod's vllm container logs for errors."""
    report = PodErrorReport(pod_name=pod_name)

    try:
        logs_proc = subprocess.run(
            ['kubectl', 'logs', '-n', namespace, pod_name,
             '-c', 'vllm', f'--tail={tail_lines}'],
            capture_output=True, text=True, timeout=30, check=False
        )
    except Exception:
        return report

    if logs_proc.returncode != 0:
        # Try previous container logs (pod may have crashed)
        try:
            logs_proc = subprocess.run(
                ['kubectl', 'logs', '-n', namespace, pod_name,
                 '-c', 'vllm', '--previous', f'--tail={tail_lines}'],
                capture_output=True, text=True, timeout=30, check=False
            )
        except Exception:
            return report
        if logs_proc.returncode != 0:
            return report

    log_text = logs_proc.stdout
    lines = log_text.splitlines()

    for line_num, line in enumerate(lines, 1):
        if _is_false_positive(line):
            continue
        for pattern_name, pattern in ERROR_PATTERNS:
            if pattern.search(line):
                report.errors.append(PodError(
                    pattern_name=pattern_name,
                    line=line[:500],
                    line_number=line_num,
                ))
                break  # one match per line is enough

    # Keep last 50 lines as context
    report.log_tail = '\n'.join(lines[-50:]) if lines else ''

    return report
