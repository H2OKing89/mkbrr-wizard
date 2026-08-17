"""UI-neutral orchestration for planning and executing mkbrr operations."""

from __future__ import annotations

import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import BinaryIO

from pydantic import JsonValue

from mkbrr_wizard import legacy_app as legacy
from mkbrr_wizard.execution.docker_cleanup import cleanup_named_docker_container
from mkbrr_wizard.execution.scheduler import (
    DiskAwareScheduler,
    ProgressCallback,
    RunJournal,
    SchedulerPolicy,
    SchedulerRun,
)
from mkbrr_wizard.models import (
    ExecutionPlan,
    OperationKind,
    OperationResult,
    OperationStatus,
    PlannedOperation,
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


_CAPTURE_LIMIT_BYTES = 1024 * 1024


class WizardApplication:
    """Execute serializable plans without knowing about prompts or Rich."""

    def __init__(self, cfg: legacy.AppCfg, runtime: str) -> None:
        self.cfg = cfg
        self.runtime = runtime
        self._process_lock = Lock()
        self._active_processes: dict[str, subprocess.Popen[str]] = {}
        self._cancel_requested: set[str] = set()

    @classmethod
    def from_config(
        cls,
        config_path: str | Path,
        *,
        forced_runtime: str | None = None,
    ) -> WizardApplication:
        path = Path(config_path).expanduser()
        legacy.load_config_environment(path)
        cfg = legacy.load_config(path)
        runtime = legacy.pick_runtime(cfg, forced_runtime)
        return cls(cfg, runtime)

    def execute_operation(self, plan_id: str, operation: PlannedOperation) -> OperationResult:
        output_path = operation.output_path.host if operation.output_path else None
        output_exists_now = (
            operation.kind is OperationKind.CREATE
            and output_path is not None
            and Path(output_path).exists()
        )
        if operation.metadata.get("output_exists") is True or output_exists_now:
            return OperationResult(
                plan_id=plan_id,
                operation_id=operation.operation_id,
                status=OperationStatus.BLOCKED,
                exit_code=2,
                output_path=output_path,
                error_message="Output already exists",
            )

        timeout_raw = operation.metadata.get("timeout_seconds")
        timeout: float | None = None
        if isinstance(timeout_raw, int | float) and not isinstance(timeout_raw, bool):
            if timeout_raw > 0:
                timeout = float(timeout_raw)
        started_at = _utc_now()
        started = time.monotonic()
        stdout: str | None = None
        stderr: str | None = None
        error_message: str | None
        details: dict[str, JsonValue] = {}
        process: subprocess.Popen[str] | None = None
        try:
            with self._process_lock:
                cancelled_before_start = operation.operation_id in self._cancel_requested
            if cancelled_before_start:
                return OperationResult(
                    plan_id=plan_id,
                    operation_id=operation.operation_id,
                    status=OperationStatus.CANCELLED,
                    exit_code=130,
                    output_path=output_path,
                    error_message="Cancelled by user",
                )

            with (
                tempfile.TemporaryFile(mode="w+b") as stdout_file,
                tempfile.TemporaryFile(mode="w+b") as stderr_file,
            ):
                process = subprocess.Popen(
                    operation.command,
                    cwd=operation.cwd,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    text=True,
                    start_new_session=True,
                )
                with self._process_lock:
                    self._active_processes[operation.operation_id] = process
                    cancellation_raced = operation.operation_id in self._cancel_requested
                if cancellation_raced:
                    self._terminate_process(process)

                timed_out = False
                try:
                    process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    self._terminate_process(process)
                    process.wait()

                stdout, stdout_truncated = self._read_captured_output(stdout_file)
                stderr, stderr_truncated = self._read_captured_output(stderr_file)
                if stdout_truncated:
                    details["stdout_truncated"] = True
                if stderr_truncated:
                    details["stderr_truncated"] = True

            # Cancellation wins a timeout race.  The scheduler may request
            # cancellation while ``wait`` is raising TimeoutExpired, so the
            # flag must be sampled after both paths have stopped the process.
            with self._process_lock:
                was_cancelled = operation.operation_id in self._cancel_requested

            cleanup_error = None
            if timed_out or was_cancelled:
                # Stopping the docker client does not necessarily stop its
                # container.  Cleanup is owned here (rather than by both this
                # path and cancel_operation) so every operation attempts it at
                # most once.
                cleanup_error = cleanup_named_docker_container(
                    operation.command,
                    capture_diagnostics=True,
                )

            if was_cancelled:
                status = OperationStatus.CANCELLED
                exit_code = 130
                error_message = "Cancelled by user"
            elif timed_out:
                exit_code = 124
                status = OperationStatus.TIMED_OUT
                error_message = f"Operation timed out after {timeout}s"
            else:
                exit_code = process.returncode
                status = OperationStatus.SUCCEEDED if exit_code == 0 else OperationStatus.FAILED
                error_message = None if exit_code == 0 else f"mkbrr exited with code {exit_code}"

            if cleanup_error is not None:
                details["container_cleanup_error"] = cleanup_error
                error_message = f"{error_message}; {cleanup_error}"
        except OSError as error:
            exit_code = 127
            status = OperationStatus.FAILED
            error_message = f"{type(error).__name__}: {error}"
        finally:
            with self._process_lock:
                self._active_processes.pop(operation.operation_id, None)
                self._cancel_requested.discard(operation.operation_id)

        result = OperationResult(
            plan_id=plan_id,
            operation_id=operation.operation_id,
            status=status,
            exit_code=exit_code,
            elapsed_seconds=time.monotonic() - started,
            started_at=started_at,
            finished_at=_utc_now(),
            output_path=output_path,
            stdout=stdout,
            stderr=stderr,
            error_message=error_message,
            details=details,
        )
        if result.succeeded and operation.kind is OperationKind.CREATE and output_path:
            legacy.maybe_fix_torrent_permissions(
                self.cfg,
                [output_path],
                emit_messages=False,
            )
        return result

    @staticmethod
    def _read_captured_output(stream: BinaryIO) -> tuple[str | None, bool]:
        """Return a UTF-8 tail while bounding each result's memory footprint."""

        stream.flush()
        stream.seek(0, 2)
        size = stream.tell()
        truncated = size > _CAPTURE_LIMIT_BYTES
        stream.seek(-_CAPTURE_LIMIT_BYTES if truncated else 0, 2 if truncated else 0)
        decoded = stream.read().decode("utf-8", errors="replace")
        if truncated:
            decoded = f"[output truncated to last {_CAPTURE_LIMIT_BYTES} bytes]\n{decoded}"
        return (decoded or None), truncated

    @staticmethod
    def _terminate_process(process: subprocess.Popen[str]) -> None:
        """Stop one isolated subprocess promptly, escalating when necessary."""

        if process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        except OSError:
            pass

    def cancel_operation(self, operation: PlannedOperation) -> None:
        """Request cancellation of an operation currently owned by this app."""

        with self._process_lock:
            self._cancel_requested.add(operation.operation_id)
            process = self._active_processes.get(operation.operation_id)
        if process is not None:
            self._terminate_process(process)

    def execute_plan(
        self,
        plan: ExecutionPlan,
        *,
        policy: SchedulerPolicy | None = None,
        on_event: ProgressCallback | None = None,
        journal: RunJournal | None = None,
        resume: bool = False,
    ) -> SchedulerRun:
        if plan.dry_run:
            raise ValueError("A dry-run plan cannot be executed")
        with self._process_lock:
            # Drop cancellation flags left by a prior run on this instance so
            # they cannot block an unrelated operation that reuses the same id.
            self._cancel_requested.clear()
        scheduler = DiskAwareScheduler(policy)
        return scheduler.run(
            plan,
            lambda operation: self.execute_operation(plan.plan_id, operation),
            on_event=on_event,
            journal=journal,
            resume=resume,
            cancel=self.cancel_operation,
        )


__all__ = ["WizardApplication"]
