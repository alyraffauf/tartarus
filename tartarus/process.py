"""Checked subprocess helpers shared across the harness."""

import subprocess


class ProcessError(Exception):
    """Raised when a checked subprocess command cannot be run or exits non-zero."""


def run_checked(command: list[str]) -> str:
    """Run a command with captured text output, raising ProcessError on failure."""
    if not command:
        raise ProcessError("command is empty")

    try:
        completed = subprocess.run(command, capture_output=True, text=True, check=True)
    except FileNotFoundError as missing:
        raise ProcessError(f"`{command[0]}` not found") from missing
    except subprocess.CalledProcessError as failed:
        raise ProcessError(
            f"`{' '.join(command)}` failed: {failed.stderr.strip()}"
        ) from failed
    return completed.stdout
