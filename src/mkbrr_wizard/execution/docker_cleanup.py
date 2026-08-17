"""Shared best-effort cleanup for named ``docker run`` containers."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from typing import Any


def cleanup_named_docker_container(
    command: Sequence[str],
    *,
    capture_diagnostics: bool,
) -> str | None:
    """Kill the container named by a ``docker run`` command.

    Cleanup never raises for an invalid command, launch failure, timeout, or
    non-zero Docker result.  Interactive legacy callers can ignore the return
    value, while headless execution can attach it to a structured result.
    """

    if tuple(command[:2]) != ("docker", "run"):
        return None
    try:
        name_index = command.index("--name")
        container_name = command[name_index + 1]
    except (ValueError, IndexError):
        return "Could not identify the Docker container"

    completed: subprocess.CompletedProcess[Any]
    try:
        if capture_diagnostics:
            completed = subprocess.run(
                ("docker", "kill", container_name),
                check=False,
                timeout=5,
                capture_output=True,
                text=True,
            )
        else:
            completed = subprocess.run(
                ("docker", "kill", container_name),
                check=False,
                timeout=5,
            )
    except (OSError, subprocess.TimeoutExpired) as error:
        return f"Docker container cleanup failed: {type(error).__name__}: {error}"

    if completed.returncode != 0:
        stderr = getattr(completed, "stderr", None)
        stdout = getattr(completed, "stdout", None)
        detail = stderr or stdout or f"docker kill exited with code {completed.returncode}"
        return f"Docker container cleanup failed: {str(detail).strip()}"
    return None


__all__ = ["cleanup_named_docker_container"]
