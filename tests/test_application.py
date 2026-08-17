from __future__ import annotations

import subprocess
import sys
import threading
import time
from unittest.mock import patch

import pytest

from mkbrr_wizard import application as application_module
from mkbrr_wizard.application import WizardApplication
from mkbrr_wizard.execution.docker_cleanup import cleanup_named_docker_container
from mkbrr_wizard.models import (
    ExecutionPlan,
    OperationKind,
    OperationStatus,
    PlannedOperation,
    ResolvedPath,
    RuntimeKind,
)


def test_native_timeout_cleanup_never_targets_a_docker_container() -> None:
    operation = PlannedOperation(
        operation_id="native-create",
        position=1,
        kind=OperationKind.CREATE,
        source_path=ResolvedPath(
            entered="/data/release",
            host="/data/release",
            runtime="/data/release",
        ),
        command=("mkbrr", "create", "/data/release", "--name", "release"),
    )

    with patch("mkbrr_wizard.execution.docker_cleanup.subprocess.run") as run:
        cleanup_named_docker_container(operation.command, capture_diagnostics=True)

    run.assert_not_called()


def test_create_rechecks_output_immediately_before_execution(base_app_cfg, tmp_path) -> None:
    output = tmp_path / "release.torrent"
    output.write_bytes(b"existing")
    operation = PlannedOperation(
        operation_id="create-race",
        position=1,
        kind=OperationKind.CREATE,
        source_path=ResolvedPath(
            entered="/data/release",
            host="/data/release",
            runtime="/data/release",
        ),
        output_path=ResolvedPath(
            entered=str(output),
            host=str(output),
            runtime=str(output),
        ),
        command=("/bin/true",),
        metadata={"output_exists": False},
    )
    application = WizardApplication(base_app_cfg(), "native")

    with patch("mkbrr_wizard.application.subprocess.run") as run:
        result = application.execute_operation("plan", operation)

    run.assert_not_called()
    assert result.status is OperationStatus.BLOCKED
    assert result.exit_code == 2


def test_docker_cleanup_failure_is_returned_to_the_result() -> None:
    operation = PlannedOperation(
        operation_id="docker-create",
        position=1,
        kind=OperationKind.CREATE,
        source_path=ResolvedPath(
            entered="/data/release",
            host="/data/release",
            runtime="/data/release",
        ),
        command=("docker", "run", "--name", "mkbrr-job", "image", "create", "/data/release"),
    )

    with patch(
        "mkbrr_wizard.execution.docker_cleanup.subprocess.run",
        return_value=subprocess.CompletedProcess(
            args=("docker", "kill", "mkbrr-job"),
            returncode=1,
            stderr="container still running",
        ),
    ) as run:
        error = cleanup_named_docker_container(
            operation.command,
            capture_diagnostics=True,
        )

    assert error == "Docker container cleanup failed: container still running"
    run.assert_called_once_with(
        ("docker", "kill", "mkbrr-job"),
        check=False,
        timeout=5,
        capture_output=True,
        text=True,
    )


def test_docker_cleanup_reports_a_missing_container_name() -> None:
    error = cleanup_named_docker_container(
        ("docker", "run", "image", "create", "/data/release"),
        capture_diagnostics=True,
    )

    assert error == "Could not identify the Docker container"


def test_cancel_operation_terminates_active_native_process(base_app_cfg) -> None:
    operation = PlannedOperation(
        operation_id="cancel-native",
        position=1,
        kind=OperationKind.CHECK,
        source_path=ResolvedPath(entered="/tmp", host="/tmp", runtime="/tmp"),
        command=(sys.executable, "-c", "import time; time.sleep(30)"),
    )
    application = WizardApplication(base_app_cfg(), "native")
    result_holder = []
    worker = threading.Thread(
        target=lambda: result_holder.append(application.execute_operation("plan", operation))
    )
    worker.start()

    deadline = time.monotonic() + 2
    registered = False
    while time.monotonic() < deadline:
        with application._process_lock:
            if operation.operation_id in application._active_processes:
                registered = True
                break
        time.sleep(0.01)

    assert registered, "operation process was not registered before the timeout"
    application.cancel_operation(operation)
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert result_holder
    assert result_holder[0].status is OperationStatus.CANCELLED
    assert result_holder[0].exit_code == 130


def test_cancellation_wins_a_timeout_race(base_app_cfg, monkeypatch) -> None:
    operation = PlannedOperation(
        operation_id="timeout-cancel-race",
        position=1,
        kind=OperationKind.CHECK,
        source_path=ResolvedPath(entered="/tmp", host="/tmp", runtime="/tmp"),
        command=("mkbrr", "check", "/tmp/release.torrent"),
        metadata={"timeout_seconds": 1},
    )
    application = WizardApplication(base_app_cfg(), "native")

    class TimeoutProcess:
        returncode = -15

        @staticmethod
        def wait(timeout=None):
            if timeout is not None:
                raise subprocess.TimeoutExpired(operation.command, timeout)
            return -15

        @staticmethod
        def poll():
            return None

    process = TimeoutProcess()
    monkeypatch.setattr(application_module.subprocess, "Popen", lambda *args, **kwargs: process)

    def cancel_during_timeout(_process) -> None:
        with application._process_lock:
            application._cancel_requested.add(operation.operation_id)

    monkeypatch.setattr(application, "_terminate_process", cancel_during_timeout)

    result = application.execute_operation("plan", operation)

    assert result.status is OperationStatus.CANCELLED
    assert result.exit_code == 130


def test_operation_output_is_spooled_and_bounded(base_app_cfg, monkeypatch) -> None:
    monkeypatch.setattr(application_module, "_CAPTURE_LIMIT_BYTES", 32)
    operation = PlannedOperation(
        operation_id="bounded-output",
        position=1,
        kind=OperationKind.INSPECT,
        source_path=ResolvedPath(entered="/tmp", host="/tmp", runtime="/tmp"),
        command=(sys.executable, "-c", "print('x' * 128)"),
    )

    result = WizardApplication(base_app_cfg(), "native").execute_operation("plan", operation)

    assert result.status is OperationStatus.SUCCEEDED
    assert result.stdout is not None
    assert result.stdout.startswith("[output truncated to last 32 bytes]")
    assert result.stdout.endswith("x" * 31 + "\n")
    assert result.details["stdout_truncated"] is True


def test_timeout_metadata_preserves_fractional_seconds(base_app_cfg, monkeypatch) -> None:
    operation = PlannedOperation(
        operation_id="fractional-timeout",
        position=1,
        kind=OperationKind.CHECK,
        source_path=ResolvedPath(entered="/tmp", host="/tmp", runtime="/tmp"),
        command=("mkbrr", "check", "/tmp/release.torrent"),
        metadata={"timeout_seconds": 1.5},
    )
    application = WizardApplication(base_app_cfg(), "native")
    captured_timeouts: list[float | None] = []

    class ImmediateProcess:
        returncode = 0

        @staticmethod
        def wait(timeout: float | None = None) -> int:
            captured_timeouts.append(timeout)
            return 0

        @staticmethod
        def poll() -> int:
            return 0

    monkeypatch.setattr(application_module.subprocess, "Popen", lambda *a, **kw: ImmediateProcess())

    result = application.execute_operation("plan", operation)

    assert captured_timeouts == [1.5]
    assert result.status is OperationStatus.SUCCEEDED


@pytest.mark.parametrize("bad_value", [True, False, -5, 0])
def test_timeout_metadata_ignores_bool_and_non_positive_values(
    base_app_cfg, monkeypatch, bad_value
) -> None:
    operation = PlannedOperation(
        operation_id="bad-timeout",
        position=1,
        kind=OperationKind.CHECK,
        source_path=ResolvedPath(entered="/tmp", host="/tmp", runtime="/tmp"),
        command=("mkbrr", "check", "/tmp/release.torrent"),
        metadata={"timeout_seconds": bad_value},
    )
    application = WizardApplication(base_app_cfg(), "native")
    captured_timeouts: list[float | None] = []

    class ImmediateProcess:
        returncode = 0

        @staticmethod
        def wait(timeout: float | None = None) -> int:
            captured_timeouts.append(timeout)
            return 0

        @staticmethod
        def poll() -> int:
            return 0

    monkeypatch.setattr(application_module.subprocess, "Popen", lambda *a, **kw: ImmediateProcess())

    application.execute_operation("plan", operation)

    assert captured_timeouts == [None]


def test_execute_plan_clears_stale_cancellation_flags(base_app_cfg, monkeypatch) -> None:
    """A cancellation left over from a prior run must not block a later, unrelated run."""
    operation = PlannedOperation(
        operation_id="reused-operation-id",
        position=1,
        kind=OperationKind.CHECK,
        source_path=ResolvedPath(entered="/tmp", host="/tmp", runtime="/tmp"),
        command=(sys.executable, "-c", "pass"),
    )
    plan = ExecutionPlan(runtime=RuntimeKind.NATIVE, operations=(operation,))
    application = WizardApplication(base_app_cfg(), "native")

    # Simulate a stale flag left behind by an operation that was cancelled
    # before it ever started (and so was never cleared by execute_operation).
    application._cancel_requested.add(operation.operation_id)

    run = application.execute_plan(plan)

    assert run.results[0].status is OperationStatus.SUCCEEDED
