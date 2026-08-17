"""Bounded, storage-aware execution with resumable JSON reports."""

from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from mkbrr_wizard.models import (
    ExecutionPlan,
    OperationResult,
    OperationStatus,
    PlannedOperation,
    ProgressEvent,
    ProgressEventKind,
    StorageKind,
)

ExecuteOperation = Callable[[PlannedOperation], OperationResult]
CancelOperation = Callable[[PlannedOperation], None]
ProgressCallback = Callable[[ProgressEvent], None]


class SchedulerPolicy(BaseModel):
    """Concurrency limits chosen to protect Unraid storage devices."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_parallel_jobs: int = Field(default=1, gt=0, le=64)
    hdd_parallel_per_device: int = Field(default=1, gt=0, le=8)
    ssd_parallel_per_device: int = Field(default=2, gt=0, le=32)
    unknown_parallel_per_device: int = Field(default=1, gt=0, le=8)
    max_total_workers: int | None = Field(default=None, gt=0, le=512)

    def device_limit(self, storage: StorageKind) -> int:
        if storage is StorageKind.HDD:
            return self.hdd_parallel_per_device
        if storage is StorageKind.SSD:
            return self.ssd_parallel_per_device
        return self.unknown_parallel_per_device


@dataclass(frozen=True)
class SchedulerRun:
    """Results and emitted events from one scheduler invocation."""

    plan_id: str
    results: tuple[OperationResult, ...]
    events: tuple[ProgressEvent, ...]
    elapsed_seconds: float
    interrupted: bool = False

    @property
    def succeeded(self) -> int:
        return sum(result.status is OperationStatus.SUCCEEDED for result in self.results)

    @property
    def failed(self) -> int:
        return sum(
            result.status
            in {OperationStatus.FAILED, OperationStatus.TIMED_OUT, OperationStatus.BLOCKED}
            for result in self.results
        )

    @property
    def skipped(self) -> int:
        return sum(result.status is OperationStatus.SKIPPED for result in self.results)

    @property
    def exit_code(self) -> int:
        if self.interrupted or any(
            result.status is OperationStatus.CANCELLED for result in self.results
        ):
            return 130
        if any(result.status is OperationStatus.FAILED for result in self.results):
            return 1
        if any(result.status is OperationStatus.TIMED_OUT for result in self.results):
            return 124 if self.succeeded == 0 else 1
        if any(result.status is OperationStatus.BLOCKED for result in self.results):
            return 2
        return 0

    def to_json(self, *, indent: int | None = 2) -> str:
        payload = {
            "schema_version": 1,
            "plan_id": self.plan_id,
            "elapsed_seconds": self.elapsed_seconds,
            "interrupted": self.interrupted,
            "exit_code": self.exit_code,
            "summary": {
                "succeeded": self.succeeded,
                "failed": self.failed,
                "skipped": self.skipped,
            },
            "results": [result.model_dump(mode="json") for result in self.results],
            "events": [event.model_dump(mode="json") for event in self.events],
        }
        return json.dumps(payload, indent=indent)


class RunJournal:
    """Atomic JSON persistence used to resume successful operations."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self._lock = Lock()

    def successful_operation_ids(self) -> set[str]:
        if not self.path.is_file():
            return set()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            return set()
        results = raw.get("results", []) if isinstance(raw, dict) else []
        if not isinstance(results, list):
            return set()
        return {
            str(item["operation_id"])
            for item in results
            if isinstance(item, dict)
            and (
                item.get("status") == OperationStatus.SUCCEEDED.value
                or (
                    item.get("status") == OperationStatus.SKIPPED.value
                    and isinstance(item.get("details"), dict)
                    and item["details"].get("reason") == "already_succeeded"
                )
            )
            and item.get("operation_id")
        }

    def write(
        self,
        plan: ExecutionPlan,
        results: Sequence[OperationResult],
        *,
        complete: bool,
    ) -> None:
        payload = {
            "schema_version": 1,
            "plan_id": plan.plan_id,
            "complete": complete,
            "plan": plan.model_dump(mode="json"),
            "results": [result.model_dump(mode="json") for result in results],
        }
        encoded = json.dumps(payload, indent=2) + "\n"
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=self.path.parent,
                    prefix=f".{self.path.name}.",
                    suffix=".tmp",
                    delete=False,
                ) as temporary:
                    temporary.write(encoded)
                    temporary.flush()
                    os.fsync(temporary.fileno())
                    temporary_path = Path(temporary.name)
                assert temporary_path is not None
                os.replace(temporary_path, self.path)
                temporary_path = None
                self._fsync_directory(self.path.parent)
            except BaseException:
                if temporary_path is not None:
                    temporary_path.unlink(missing_ok=True)
                raise

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        """Best-effort durability for the rename recorded in the directory entry."""
        try:
            dir_fd = os.open(directory, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(dir_fd)
        except OSError:
            pass
        finally:
            os.close(dir_fd)


class DiskAwareScheduler:
    """Schedule independent devices concurrently without overloading one disk."""

    def __init__(self, policy: SchedulerPolicy | None = None) -> None:
        self.policy = policy or SchedulerPolicy()

    @staticmethod
    def _device_key(operation: PlannedOperation) -> str:
        return operation.storage_key or f"{operation.storage.value}:shared"

    def _worker_cost(self, operation: PlannedOperation) -> int:
        workers = operation.options.workers
        if workers:
            return workers
        # A global budget makes an automatic mkbrr worker count own the full
        # budget, preventing multiple auto jobs from each scaling to all CPUs.
        return self.policy.max_total_workers or 1

    def run(
        self,
        plan: ExecutionPlan,
        execute: ExecuteOperation,
        *,
        on_event: ProgressCallback | None = None,
        journal: RunJournal | None = None,
        resume: bool = False,
        cancel: CancelOperation | None = None,
    ) -> SchedulerRun:
        started = time.monotonic()
        events: list[ProgressEvent] = []
        results_by_id: dict[str, OperationResult] = {}
        ordered_operations = sorted(plan.operations, key=lambda item: item.position)
        sequence = 0

        def emit(
            operation: PlannedOperation,
            kind: ProgressEventKind,
            status: OperationStatus,
            message: str,
            *,
            notify: bool = True,
        ) -> None:
            nonlocal sequence
            sequence += 1
            event = ProgressEvent(
                plan_id=plan.plan_id,
                operation_id=operation.operation_id,
                sequence=sequence,
                kind=kind,
                status=status,
                message=message,
            )
            events.append(event)
            if notify and on_event is not None:
                on_event(event)

        def current_results() -> list[OperationResult]:
            return [
                results_by_id[operation.operation_id]
                for operation in ordered_operations
                if operation.operation_id in results_by_id
            ]

        def persist(*, complete: bool) -> None:
            if journal is not None:
                journal.write(plan, current_results(), complete=complete)

        def result_event_kind(result: OperationResult) -> ProgressEventKind:
            return {
                OperationStatus.SUCCEEDED: ProgressEventKind.COMPLETED,
                OperationStatus.SKIPPED: ProgressEventKind.SKIPPED,
                OperationStatus.CANCELLED: ProgressEventKind.CANCELLED,
                OperationStatus.TIMED_OUT: ProgressEventKind.TIMED_OUT,
            }.get(result.status, ProgressEventKind.FAILED)

        def record_result(
            operation: PlannedOperation,
            result: OperationResult,
            *,
            notify: bool = True,
        ) -> None:
            if result.operation_id != operation.operation_id:
                raise ValueError("executor returned a result for another operation")
            if result.plan_id != plan.plan_id:
                result = result.model_copy(update={"plan_id": plan.plan_id})
            results_by_id[operation.operation_id] = result
            emit(
                operation,
                result_event_kind(result),
                result.status,
                result.error_message or result.status.value.replace("_", " ").title(),
                notify=notify,
            )

        resumed_ids = journal.successful_operation_ids() if resume and journal else set()
        pending: list[PlannedOperation] = []
        active: dict[Future[OperationResult], PlannedOperation] = {}
        device_usage: dict[str, int] = {}
        active_worker_cost = 0
        pool: ThreadPoolExecutor | None = None
        cancellation_requested = False

        def can_start(operation: PlannedOperation) -> bool:
            if len(active) >= self.policy.max_parallel_jobs:
                return False
            key = self._device_key(operation)
            if device_usage.get(key, 0) >= self.policy.device_limit(operation.storage):
                return False
            worker_budget = self.policy.max_total_workers
            return worker_budget is None or (
                active_worker_cost + self._worker_cost(operation) <= worker_budget
            )

        def cancellation_result(
            operation: PlannedOperation,
            message: str,
            errors: list[str] | None = None,
        ) -> OperationResult:
            error_values: list[JsonValue] = []
            error_values.extend(errors or ())
            details: dict[str, JsonValue] = (
                {"cancellation_errors": error_values} if error_values else {}
            )
            return OperationResult(
                plan_id=plan.plan_id,
                operation_id=operation.operation_id,
                status=OperationStatus.CANCELLED,
                exit_code=130,
                output_path=(operation.output_path.host if operation.output_path else None),
                error_message=message,
                details=details,
            )

        def cancel_outstanding(message: str) -> None:
            """Cancel every unfinished item without one callback aborting cleanup."""

            nonlocal cancellation_requested

            for future, operation in list(active.items()):
                if operation.operation_id in results_by_id:
                    continue

                # Preserve work that reached a terminal state before the
                # interrupt, even if the scheduler had not consumed it yet.
                if future.done() and not future.cancelled():
                    try:
                        completed_result = future.result()
                        record_result(operation, completed_result, notify=False)
                    except BaseException as error:
                        record_result(
                            operation,
                            OperationResult(
                                plan_id=plan.plan_id,
                                operation_id=operation.operation_id,
                                status=OperationStatus.FAILED,
                                exit_code=1,
                                error_message=f"{type(error).__name__}: {error}",
                            ),
                            notify=False,
                        )
                    continue

                cancellation_errors: list[str] = []
                if cancel is not None:
                    cancellation_requested = True
                    try:
                        cancel(operation)
                    except BaseException as error:
                        cancellation_errors.append(f"{type(error).__name__}: {error}")
                try:
                    future.cancel()
                except BaseException as error:
                    cancellation_errors.append(f"{type(error).__name__}: {error}")
                record_result(
                    operation,
                    cancellation_result(operation, message, cancellation_errors),
                    notify=False,
                )

            # This also covers operations not yet added to pending when a
            # callback interrupts queue initialization, and an item popped
            # immediately before pool.submit raises KeyboardInterrupt.
            for operation in ordered_operations:
                if operation.operation_id not in results_by_id:
                    record_result(
                        operation,
                        cancellation_result(operation, message),
                        notify=False,
                    )

        def shutdown_pool() -> None:
            if pool is None:
                return
            # A second Ctrl-C must not leave already-cancelled subprocess
            # workers behind.  Application cancellation makes this wait short.
            while True:
                try:
                    pool.shutdown(wait=True, cancel_futures=True)
                    return
                except KeyboardInterrupt:
                    if not cancellation_requested:
                        try:
                            pool.shutdown(wait=False, cancel_futures=True)
                        except KeyboardInterrupt:
                            pass
                        return
                    continue

        interrupted = False
        try:
            for operation in ordered_operations:
                if operation.operation_id in resumed_ids:
                    result = OperationResult(
                        plan_id=plan.plan_id,
                        operation_id=operation.operation_id,
                        status=OperationStatus.SKIPPED,
                        output_path=(operation.output_path.host if operation.output_path else None),
                        details={"reason": "already_succeeded", "resumed": True},
                    )
                    results_by_id[operation.operation_id] = result
                    emit(
                        operation,
                        ProgressEventKind.SKIPPED,
                        OperationStatus.SKIPPED,
                        "Already succeeded in the resume report",
                    )
                else:
                    pending.append(operation)
                    emit(
                        operation,
                        ProgressEventKind.QUEUED,
                        OperationStatus.PENDING,
                        "Queued",
                    )

            pool = ThreadPoolExecutor(
                max_workers=self.policy.max_parallel_jobs,
                thread_name_prefix="mkbrr-job",
            )
            while pending or active:
                launched = False
                pending_index = 0
                while pending_index < len(pending):
                    operation = pending[pending_index]
                    if not can_start(operation):
                        pending_index += 1
                        continue
                    pending.pop(pending_index)
                    key = self._device_key(operation)
                    device_usage[key] = device_usage.get(key, 0) + 1
                    active_worker_cost += self._worker_cost(operation)
                    try:
                        future = pool.submit(execute, operation)
                    except Exception as error:
                        device_usage[key] -= 1
                        active_worker_cost -= self._worker_cost(operation)
                        record_result(
                            operation,
                            OperationResult(
                                plan_id=plan.plan_id,
                                operation_id=operation.operation_id,
                                status=OperationStatus.FAILED,
                                exit_code=1,
                                error_message=(
                                    "Failed to submit operation: "
                                    f"{type(error).__name__}: {error}"
                                ),
                            ),
                        )
                        persist(complete=False)
                        launched = True
                        continue
                    active[future] = operation
                    launched = True
                    emit(
                        operation,
                        ProgressEventKind.STARTED,
                        OperationStatus.RUNNING,
                        f"Started on {key}",
                    )

                if not active:
                    if pending and not launched:
                        operation = pending.pop(0)
                        result = OperationResult(
                            plan_id=plan.plan_id,
                            operation_id=operation.operation_id,
                            status=OperationStatus.BLOCKED,
                            exit_code=2,
                            error_message="Scheduler limits prevent this operation from starting",
                        )
                        record_result(operation, result)
                        persist(complete=False)
                    continue

                completed, _ = wait(tuple(active), return_when=FIRST_COMPLETED)
                for future in completed:
                    operation = active.pop(future)
                    key = self._device_key(operation)
                    device_usage[key] -= 1
                    active_worker_cost -= self._worker_cost(operation)
                    try:
                        result = future.result()
                    except BaseException as error:  # executor isolation is intentional
                        result = OperationResult(
                            plan_id=plan.plan_id,
                            operation_id=operation.operation_id,
                            status=OperationStatus.FAILED,
                            exit_code=1,
                            error_message=f"{type(error).__name__}: {error}",
                        )
                    try:
                        record_result(operation, result)
                    except ValueError as error:
                        record_result(
                            operation,
                            OperationResult(
                                plan_id=plan.plan_id,
                                operation_id=operation.operation_id,
                                status=OperationStatus.FAILED,
                                exit_code=1,
                                error_message=f"ValueError: {error}",
                            ),
                        )
                    persist(complete=False)
        except KeyboardInterrupt:
            interrupted = True
            cancel_outstanding("Cancelled by user")
            shutdown_pool()
            try:
                persist(complete=False)
            except BaseException:
                pass
        except BaseException:
            # Preserve the original exception, but make best-effort cleanup
            # independent: one broken cancel callback or journal must not stop
            # other active operations from being cancelled.
            try:
                cancel_outstanding("Cancelled after scheduler error")
            finally:
                try:
                    shutdown_pool()
                finally:
                    try:
                        persist(complete=False)
                    except BaseException:
                        pass
            raise
        else:
            shutdown_pool()

        final_results = tuple(
            results_by_id[operation.operation_id]
            for operation in ordered_operations
            if operation.operation_id in results_by_id
        )
        if not interrupted:
            persist(complete=True)
        return SchedulerRun(
            plan_id=plan.plan_id,
            results=final_results,
            events=tuple(events),
            elapsed_seconds=time.monotonic() - started,
            interrupted=interrupted,
        )


__all__ = ["DiskAwareScheduler", "RunJournal", "SchedulerPolicy", "SchedulerRun"]
