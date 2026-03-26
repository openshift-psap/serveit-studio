"""
Backward-compatible re-export — all code moved to core/orchestrator/ package.

Import from here continues to work:
    from core.test_orchestrator import TestOrchestrator, TestResult
"""

from core.orchestrator.result import TestResult
from core.orchestrator.runner import TestOrchestrator
