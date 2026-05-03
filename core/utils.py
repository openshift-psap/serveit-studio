"""
Shared utility functions for InferRecipe.
"""

from enum import Enum


class Architecture(str, Enum):
    """Deployment architecture types."""
    AGGREGATED = "aggregated"
    PD = "pd"  # Prefill/Decode disaggregation
    EP = "ep"  # Expert Parallelism


class LogLevel(str, Enum):
    """Log levels for console output."""
    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"
    DEBUG = "debug"


def next_power_of_2(n: int) -> int:
    """
    Round up to the next power of 2.

    Args:
        n: Input number

    Returns:
        Next power of 2 >= n

    Examples:
        >>> next_power_of_2(3)
        4
        >>> next_power_of_2(8)
        8
        >>> next_power_of_2(15)
        16
    """
    if n <= 1:
        return 1

    power = 1
    while power < n:
        power *= 2

    return power
