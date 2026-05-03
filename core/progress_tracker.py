"""
Progress Tracking Utilities for InfeRecipe

Provides reusable progress bar functionality for:
- Model downloads
- Guidellm test execution
- Any long-running operations
"""

import time
from typing import Callable, Optional


class ProgressBar:
    """
    ASCII progress bar for console output.

    Example:
        [████████████████████░░░░░░░░] 75% - Downloading model...
    """

    def __init__(
        self,
        total: int,
        width: int = 40,
        fill_char: str = '█',
        empty_char: str = '░',
        callback: Optional[Callable[[str], None]] = None
    ):
        """
        Initialize progress bar.

        Args:
            total: Total number of steps
            width: Width of the progress bar in characters
            fill_char: Character for filled portion
            empty_char: Character for empty portion
            callback: Optional callback function to send progress updates
        """
        self.total = total
        self.current = 0
        self.width = width
        self.fill_char = fill_char
        self.empty_char = empty_char
        self.callback = callback
        self.start_time = time.time()

    def update(self, current: int, message: str = ""):
        """
        Update progress bar.

        Args:
            current: Current progress value
            message: Optional message to display
        """
        self.current = current
        percentage = int((current / self.total) * 100) if self.total > 0 else 0
        filled = int((current / self.total) * self.width) if self.total > 0 else 0
        bar = self.fill_char * filled + self.empty_char * (self.width - filled)

        # Calculate elapsed time and ETA
        elapsed = time.time() - self.start_time
        if current > 0:
            eta = (elapsed / current) * (self.total - current)
            eta_str = f"ETA: {int(eta)}s"
        else:
            eta_str = "ETA: --"

        # Format progress line
        progress_line = f"[{bar}] {percentage}% - {message} ({eta_str})"

        if self.callback:
            self.callback(progress_line)

        return progress_line

    def increment(self, step: int = 1, message: str = ""):
        """
        Increment progress by step amount.

        Args:
            step: Amount to increment
            message: Optional message to display
        """
        return self.update(self.current + step, message)

    def complete(self, message: str = "Complete"):
        """
        Mark progress as complete.

        Args:
            message: Completion message
        """
        elapsed = time.time() - self.start_time
        completion_line = f"[{self.fill_char * self.width}] 100% - {message} (took {int(elapsed)}s)"

        if self.callback:
            self.callback(completion_line)

        return completion_line


def format_bytes(bytes_value: int) -> str:
    """
    Format bytes into human-readable string.

    Args:
        bytes_value: Number of bytes

    Returns:
        Formatted string (e.g., "1.5 GB", "256 MB")
    """
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if bytes_value < 1024.0:
            return f"{bytes_value:.1f} {unit}"
        bytes_value /= 1024.0
    return f"{bytes_value:.1f} PB"


def format_time(seconds: float) -> str:
    """
    Format seconds into human-readable time string.

    Args:
        seconds: Number of seconds

    Returns:
        Formatted string (e.g., "2h 15m 30s", "45s")
    """
    if seconds < 60:
        return f"{int(seconds)}s"
    elif seconds < 3600:
        minutes = int(seconds / 60)
        secs = int(seconds % 60)
        return f"{minutes}m {secs}s"
    else:
        hours = int(seconds / 3600)
        minutes = int((seconds % 3600) / 60)
        secs = int(seconds % 60)
        return f"{hours}h {minutes}m {secs}s"
