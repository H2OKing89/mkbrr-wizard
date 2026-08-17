from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from mkbrr_wizard.execution import scheduler as scheduler_module
from mkbrr_wizard.execution.scheduler import (
    DiskAwareScheduler,
    RunJournal,
    SchedulerPolicy,
    SchedulerRun,
)
from mkbrr_wizard.models import (
    EffectiveOptions,
    ExecutionPlan,
    OperationKind,
    OperationResult,
    OperationStatus,
    PlannedOperation,
    ProgressEventKind,
    ResolvedPath,
    RuntimeKind,
    StorageKind,
)


def _operation(
    position: int,
    device: str,
    *,
    workers: int = 1,
    storage: StorageKind = StorageKind.HDD,
) -> PlannedOperation:
    operation_id = f"job-{position}"
    return PlannedOperation(
        operation_id=operation_id,
        position=position,
        kind=OperationKind.CREATE,
        source_path=ResolvedPath(
            entered=f"/data/{operation_id}",
            host=f"/mnt/{device}/{operation_id}",
            runtime=f"/data/{operation_id}",
            physical=f"/mnt/{device}/{operation_id}",
        ),
        output_path=ResolvedPath(
            entered=f"/torrentfiles/{operation_id}.torrent",
            host=f"/output/{operation_id}.torrent",
            runtime=f"/torrentfiles/{operation_id}.torrent",
        ),
        command=("mkbrr", "create", f"/data/{operation_id}"),
        options=EffectiveOptions(workers=workers),
        storage=storage,
        storage_key=device,
    )


def _plan(*operations: PlannedOperation) -> ExecutionPlan:
    return ExecutionPlan(runtime=RuntimeKind.NATIVE, operations=operations)


def test_scheduler_parallelizes_devices_but_serializes_one_hdd() -> None:
    plan = _plan(_operation(1, "disk1"), _operation(2, "disk1"), _operation(3, "disk2"))
    lock = threading.Lock()
    active: dict[str, int] = {}
    max_active: dict[str, int] = {}
    global_active = 0
    max_global = 0

    def execute(operation: PlannedOperation) -> OperationResult:
        nonlocal global_active, max_global
        key = operation.storage_key or "unknown"
        with lock:
            active[key] = active.get(key, 0) + 1
            max_active[key] = max(max_active.get(key, 0), active[key])
            global_active += 1
            max_global = max(max_global, global_active)
        time.sleep(0.03)
        with lock:
            active[key] -= 1
            global_active -= 1
        return OperationResult(
            plan_id=plan.plan_id,
            operation_id=operation.operation_id,
            status=OperationStatus.SUCCEEDED,
            exit_code=0,
        )

    run = DiskAwareScheduler(SchedulerPolicy(max_parallel_jobs=3, hdd_parallel_per_device=1)).run(
        plan, execute
    )

    assert run.exit_code == 0
    assert run.succeeded == 3
    assert max_active == {"disk1": 1, "disk2": 1}
    assert max_global >= 2


def test_scheduler_resume_skips_previously_successful_jobs(tmp_path: Path) -> None:
    plan = _plan(_operation(1, "disk1"), _operation(2, "disk2"))
    journal = RunJournal(tmp_path / "run.json")

    def succeed(operation: PlannedOperation) -> OperationResult:
        return OperationResult(
            plan_id=plan.plan_id,
            operation_id=operation.operation_id,
            status=OperationStatus.SUCCEEDED,
            exit_code=0,
        )

    first = DiskAwareScheduler(SchedulerPolicy(max_parallel_jobs=2)).run(
        plan,
        succeed,
        journal=journal,
    )
    assert first.succeeded == 2

    def must_not_run(operation: PlannedOperation) -> OperationResult:
        raise AssertionError(f"unexpected resumed execution: {operation.operation_id}")

    resumed = DiskAwareScheduler(SchedulerPolicy(max_parallel_jobs=2)).run(
        plan,
        must_not_run,
        journal=journal,
        resume=True,
    )

    assert resumed.exit_code == 0
    assert resumed.skipped == 2
    assert all(result.status is OperationStatus.SKIPPED for result in resumed.results)


def test_scheduler_blocks_job_larger_than_worker_budget() -> None:
    plan = _plan(_operation(1, "disk1", workers=8))

    run = DiskAwareScheduler(SchedulerPolicy(max_parallel_jobs=2, max_total_workers=4)).run(
        plan,
        lambda operation: OperationResult(
            plan_id=plan.plan_id,
            operation_id=operation.operation_id,
            status=OperationStatus.SUCCEEDED,
        ),
    )

    assert run.exit_code == 2
    assert run.results[0].status is OperationStatus.BLOCKED


def test_automatic_worker_jobs_own_the_configured_budget() -> None:
    plan = _plan(
        _operation(1, "cache-a", workers=0, storage=StorageKind.SSD),
        _operation(2, "cache-b", workers=0, storage=StorageKind.SSD),
    )
    lock = threading.Lock()
    active = 0
    max_active = 0

    def execute(operation: PlannedOperation) -> OperationResult:
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.02)
        with lock:
            active -= 1
        return OperationResult(
            plan_id=plan.plan_id,
            operation_id=operation.operation_id,
            status=OperationStatus.SUCCEEDED,
        )

    run = DiskAwareScheduler(
        SchedulerPolicy(
            max_parallel_jobs=2,
            ssd_parallel_per_device=2,
            max_total_workers=4,
        )
    ).run(plan, execute)

    assert run.exit_code == 0
    assert max_active == 1


def test_keyboard_interrupt_returns_cancelled_run_and_partial_journal(tmp_path: Path) -> None:
    plan = _plan(_operation(1, "disk1"))
    released = threading.Event()
    cancelled: list[str] = []
    journal = RunJournal(tmp_path / "interrupted.json")

    def execute(operation: PlannedOperation) -> OperationResult:
        released.wait(timeout=5)
        return OperationResult(
            plan_id=plan.plan_id,
            operation_id=operation.operation_id,
            status=OperationStatus.CANCELLED,
            exit_code=130,
        )

    def on_event(event) -> None:
        if event.kind is ProgressEventKind.STARTED:
            raise KeyboardInterrupt

    def cancel(operation: PlannedOperation) -> None:
        cancelled.append(operation.operation_id)
        released.set()

    started = time.monotonic()
    run = DiskAwareScheduler().run(
        plan,
        execute,
        on_event=on_event,
        cancel=cancel,
        journal=journal,
    )

    assert time.monotonic() - started < 1
    assert cancelled == ["job-1"]
    assert run.exit_code == 130
    assert run.results[0].status is OperationStatus.CANCELLED
    payload = json.loads(journal.path.read_text(encoding="utf-8"))
    assert payload["complete"] is False
    assert payload["results"][0]["status"] == OperationStatus.CANCELLED.value


def test_keyboard_interrupt_returns_partial_results_when_journal_write_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    plan = _plan(_operation(1, "disk1"))
    released = threading.Event()
    journal = RunJournal(tmp_path / "unwritable.json")

    def execute(operation: PlannedOperation) -> OperationResult:
        released.wait(timeout=2)
        return OperationResult(
            plan_id=plan.plan_id,
            operation_id=operation.operation_id,
            status=OperationStatus.CANCELLED,
            exit_code=130,
        )

    def on_event(event) -> None:
        if event.kind is ProgressEventKind.STARTED:
            raise KeyboardInterrupt

    def cancel(_operation: PlannedOperation) -> None:
        released.set()

    def fail_write(*_args, **_kwargs) -> None:
        raise OSError("journal is unavailable")

    monkeypatch.setattr(journal, "write", fail_write)

    run = DiskAwareScheduler().run(
        plan,
        execute,
        on_event=on_event,
        cancel=cancel,
        journal=journal,
    )

    assert run.interrupted is True
    assert run.exit_code == 130
    assert run.results[0].status is OperationStatus.CANCELLED


def test_shutdown_does_not_retry_interrupt_indefinitely_without_cancel(monkeypatch) -> None:
    plan = _plan(_operation(1, "disk1"))
    original_shutdown = scheduler_module.ThreadPoolExecutor.shutdown
    shutdown_wait_values: list[bool] = []

    def interrupt_first_shutdown(executor, wait=True, *, cancel_futures=False):
        shutdown_wait_values.append(wait)
        if len(shutdown_wait_values) == 1:
            raise KeyboardInterrupt
        return original_shutdown(
            executor,
            wait=wait,
            cancel_futures=cancel_futures,
        )

    monkeypatch.setattr(
        scheduler_module.ThreadPoolExecutor,
        "shutdown",
        interrupt_first_shutdown,
    )

    def on_event(event) -> None:
        if event.kind is ProgressEventKind.STARTED:
            raise KeyboardInterrupt

    run = DiskAwareScheduler().run(
        plan,
        lambda operation: OperationResult(
            plan_id=plan.plan_id,
            operation_id=operation.operation_id,
            status=OperationStatus.SUCCEEDED,
            exit_code=0,
        ),
        on_event=on_event,
    )

    assert run.exit_code == 130
    assert shutdown_wait_values == [True, False]


def test_keyboard_interrupt_after_completion_still_returns_130() -> None:
    plan = _plan(_operation(1, "disk1"))

    def execute(operation: PlannedOperation) -> OperationResult:
        return OperationResult(
            plan_id=plan.plan_id,
            operation_id=operation.operation_id,
            status=OperationStatus.SUCCEEDED,
            exit_code=0,
        )

    def on_event(event) -> None:
        if event.kind is ProgressEventKind.COMPLETED:
            raise KeyboardInterrupt

    run = DiskAwareScheduler().run(plan, execute, on_event=on_event)

    assert run.interrupted is True
    assert run.results[0].status is OperationStatus.SUCCEEDED
    assert run.exit_code == 130


def test_exception_cleanup_cancels_every_active_job_when_one_cancel_raises(
    tmp_path: Path,
) -> None:
    plan = _plan(_operation(1, "disk1"), _operation(2, "disk2"))
    releases = {operation.operation_id: threading.Event() for operation in plan.operations}
    cancel_calls: list[str] = []
    started_count = 0
    journal = RunJournal(tmp_path / "failed-progress.json")

    def execute(operation: PlannedOperation) -> OperationResult:
        releases[operation.operation_id].wait(timeout=2)
        return OperationResult(
            plan_id=plan.plan_id,
            operation_id=operation.operation_id,
            status=OperationStatus.CANCELLED,
            exit_code=130,
        )

    def on_event(event) -> None:
        nonlocal started_count
        if event.kind is ProgressEventKind.STARTED:
            started_count += 1
            if started_count == 2:
                raise RuntimeError("progress callback failed")

    def cancel(operation: PlannedOperation) -> None:
        cancel_calls.append(operation.operation_id)
        releases[operation.operation_id].set()
        if operation.operation_id == "job-1":
            raise RuntimeError("first cancellation failed")

    with pytest.raises(RuntimeError, match="progress callback failed"):
        DiskAwareScheduler(SchedulerPolicy(max_parallel_jobs=2)).run(
            plan,
            execute,
            on_event=on_event,
            cancel=cancel,
            journal=journal,
        )

    assert cancel_calls == ["job-1", "job-2"]
    assert all(release.is_set() for release in releases.values())
    payload = json.loads(journal.path.read_text(encoding="utf-8"))
    assert payload["complete"] is False
    assert [item["status"] for item in payload["results"]] == [
        OperationStatus.CANCELLED.value,
        OperationStatus.CANCELLED.value,
    ]
    assert payload["results"][0]["details"]["cancellation_errors"] == [
        "RuntimeError: first cancellation failed"
    ]


def test_interrupt_cleanup_continues_when_one_cancel_callback_raises(
    tmp_path: Path,
) -> None:
    plan = _plan(_operation(1, "disk1"), _operation(2, "disk2"))
    releases = {operation.operation_id: threading.Event() for operation in plan.operations}
    cancel_calls: list[str] = []
    started_count = 0
    journal = RunJournal(tmp_path / "interrupted-cancel-error.json")

    def execute(operation: PlannedOperation) -> OperationResult:
        releases[operation.operation_id].wait(timeout=2)
        return OperationResult(
            plan_id=plan.plan_id,
            operation_id=operation.operation_id,
            status=OperationStatus.CANCELLED,
            exit_code=130,
        )

    def on_event(event) -> None:
        nonlocal started_count
        if event.kind is ProgressEventKind.STARTED:
            started_count += 1
            if started_count == 2:
                raise KeyboardInterrupt

    def cancel(operation: PlannedOperation) -> None:
        cancel_calls.append(operation.operation_id)
        releases[operation.operation_id].set()
        if operation.operation_id == "job-1":
            raise RuntimeError("first cancellation failed")

    run = DiskAwareScheduler(SchedulerPolicy(max_parallel_jobs=2)).run(
        plan,
        execute,
        on_event=on_event,
        cancel=cancel,
        journal=journal,
    )

    assert run.interrupted is True
    assert run.exit_code == 130
    assert cancel_calls == ["job-1", "job-2"]
    assert all(release.is_set() for release in releases.values())
    assert run.results[0].details["cancellation_errors"] == [
        "RuntimeError: first cancellation failed"
    ]
    payload = json.loads(journal.path.read_text(encoding="utf-8"))
    assert payload["complete"] is False
    assert len(payload["results"]) == 2


def test_submit_failure_records_terminal_result_and_continues(
    tmp_path: Path,
    monkeypatch,
) -> None:
    plan = _plan(_operation(1, "disk1"), _operation(2, "disk2"))
    journal = RunJournal(tmp_path / "submit-failure.json")
    original_submit = scheduler_module.ThreadPoolExecutor.submit

    def flaky_submit(executor, function, operation):
        if operation.operation_id == "job-1":
            raise RuntimeError("executor rejected job")
        return original_submit(executor, function, operation)

    monkeypatch.setattr(scheduler_module.ThreadPoolExecutor, "submit", flaky_submit)

    def execute(operation: PlannedOperation) -> OperationResult:
        return OperationResult(
            plan_id=plan.plan_id,
            operation_id=operation.operation_id,
            status=OperationStatus.SUCCEEDED,
            exit_code=0,
        )

    run = DiskAwareScheduler(SchedulerPolicy(max_parallel_jobs=2)).run(
        plan,
        execute,
        journal=journal,
    )

    assert [result.operation_id for result in run.results] == ["job-1", "job-2"]
    assert [result.status for result in run.results] == [
        OperationStatus.FAILED,
        OperationStatus.SUCCEEDED,
    ]
    assert "Failed to submit operation" in (run.results[0].error_message or "")
    payload = json.loads(journal.path.read_text(encoding="utf-8"))
    assert payload["complete"] is True
    assert len(payload["results"]) == 2


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ((OperationStatus.BLOCKED,), 2),
        ((OperationStatus.FAILED, OperationStatus.BLOCKED), 1),
        ((OperationStatus.FAILED, OperationStatus.TIMED_OUT), 1),
        ((OperationStatus.BLOCKED, OperationStatus.TIMED_OUT), 124),
        (
            (
                OperationStatus.SUCCEEDED,
                OperationStatus.BLOCKED,
                OperationStatus.TIMED_OUT,
            ),
            1,
        ),
        ((OperationStatus.BLOCKED, OperationStatus.CANCELLED), 130),
    ],
)
def test_scheduler_run_exit_code_precedence(
    statuses: tuple[OperationStatus, ...],
    expected: int,
) -> None:
    results = tuple(
        OperationResult(
            plan_id="plan",
            operation_id=f"result-{index}",
            status=status,
            exit_code={
                OperationStatus.SUCCEEDED: 0,
                OperationStatus.BLOCKED: 2,
                OperationStatus.TIMED_OUT: 124,
                OperationStatus.CANCELLED: 130,
            }.get(status, 1),
        )
        for index, status in enumerate(statuses, start=1)
    )

    run = SchedulerRun(plan_id="plan", results=results, events=(), elapsed_seconds=0)

    assert run.exit_code == expected
