from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from mkbrr_wizard.models import (
    EffectiveOptions,
    ExecutionPlan,
    OperationKind,
    OperationResult,
    OperationStatus,
    PlannedOperation,
    ProgressEvent,
    ProgressEventKind,
    ResolvedPath,
    RuntimeKind,
    StorageKind,
)

NOW = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)


def _operation(*, operation_id: str = "create-1", position: int = 1) -> PlannedOperation:
    return PlannedOperation(
        operation_id=operation_id,
        position=position,
        kind=OperationKind.CREATE,
        source_path=ResolvedPath(
            entered="/mnt/user/media/show",
            host="/mnt/user/media/show",
            runtime="/data/show",
            physical="/mnt/disk3/media/show",
        ),
        output_path=ResolvedPath(
            entered="show.torrent",
            host="/mnt/user/torrents/show.torrent",
            runtime="/torrentfiles/show.torrent",
            physical="/mnt/cache/torrents/show.torrent",
        ),
        command=("mkbrr", "create", "/data/show", "--preset", "btn"),
        options=EffectiveOptions(
            preset="btn",
            trackers=("https://tracker.example/announce",),
            private=True,
            piece_length=20,
            workers=2,
        ),
        storage=StorageKind.HDD,
        storage_key="disk3",
        estimated_file_count=12,
        estimated_size_bytes=2_147_483_648,
        metadata={"resumable": True},
    )


def test_plan_json_round_trip_preserves_typed_values() -> None:
    plan = ExecutionPlan(
        plan_id="plan-1",
        created_at=NOW,
        runtime=RuntimeKind.DOCKER,
        dry_run=True,
        operations=(_operation(),),
        warnings=("Output already exists and will be skipped",),
        metadata={"source": "manifest.json", "attempt": 2},
    )

    restored = ExecutionPlan.from_json(plan.to_json())

    assert restored == plan
    assert restored.runtime is RuntimeKind.DOCKER
    assert restored.operations[0].kind is OperationKind.CREATE
    assert restored.operations[0].options.piece_strategy == "fixed 2^20 bytes"


def test_command_allows_explicit_empty_flag_values_but_not_a_blank_executable() -> None:
    operation = _operation().model_copy(
        update={"command": ("mkbrr", "--source", "")},
    )

    validated = PlannedOperation.model_validate(operation.model_dump())
    assert validated.command[-1] == ""

    with pytest.raises(ValidationError, match="executable must not be blank"):
        PlannedOperation.model_validate(
            {**operation.model_dump(), "command": ("", "--source", "value")}
        )


def test_shared_non_empty_text_contract_does_not_coerce_bytes() -> None:
    with pytest.raises(ValidationError):
        ResolvedPath(
            entered=b"/mnt/user/media",  # type: ignore[arg-type]
            host="/mnt/user/media",
            runtime="/data/media",
        )


def test_result_and_progress_json_round_trip() -> None:
    result = OperationResult(
        plan_id="plan-1",
        operation_id="create-1",
        status=OperationStatus.SUCCEEDED,
        exit_code=0,
        elapsed_seconds=4.25,
        started_at=NOW,
        finished_at=NOW,
        output_path="/mnt/user/torrents/show.torrent",
        details={"files": 12},
    )
    event = ProgressEvent(
        event_id="event-1",
        plan_id="plan-1",
        operation_id="create-1",
        sequence=3,
        occurred_at=NOW,
        kind=ProgressEventKind.PROGRESS,
        status=OperationStatus.RUNNING,
        completed_units=4,
        total_units=10,
        message="Hashing",
        metrics={"bytes_per_second": 512.5},
    )

    assert OperationResult.from_json(result.to_json(indent=None)) == result
    assert ProgressEvent.from_json(event.to_json(indent=None)) == event
    assert event.fraction_complete == pytest.approx(0.4)


def test_plan_rejects_duplicate_operation_ids_and_positions() -> None:
    with pytest.raises(ValidationError, match="operation_id values must be unique"):
        ExecutionPlan(
            plan_id="plan-1",
            created_at=NOW,
            runtime=RuntimeKind.NATIVE,
            operations=(_operation(), _operation(position=2)),
        )

    with pytest.raises(ValidationError, match="operation positions must be unique"):
        ExecutionPlan(
            plan_id="plan-1",
            created_at=NOW,
            runtime=RuntimeKind.NATIVE,
            operations=(_operation(), _operation(operation_id="create-2")),
        )


def test_result_requires_a_terminal_status() -> None:
    with pytest.raises(ValidationError, match="status must be terminal"):
        OperationResult(
            plan_id="plan-1",
            operation_id="create-1",
            status=OperationStatus.RUNNING,
            finished_at=NOW,
        )


def test_progress_rejects_completed_units_above_total() -> None:
    with pytest.raises(ValidationError, match="cannot exceed"):
        ProgressEvent(
            plan_id="plan-1",
            operation_id="create-1",
            sequence=1,
            occurred_at=NOW,
            status=OperationStatus.RUNNING,
            completed_units=11,
            total_units=10,
        )


def test_models_reject_unknown_fields_and_naive_timestamps() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ResolvedPath(entered="a", host="a", runtime="a", typo="value")  # type: ignore[call-arg]

    with pytest.raises(ValidationError, match="must include a timezone"):
        ExecutionPlan(
            plan_id="plan-1",
            created_at=datetime(2026, 8, 16, 12, 0),
            runtime=RuntimeKind.NATIVE,
        )


def test_result_rejects_finished_at_before_started_at() -> None:
    with pytest.raises(ValidationError, match="finished_at cannot precede started_at"):
        OperationResult(
            plan_id="plan-1",
            operation_id="create-1",
            status=OperationStatus.SUCCEEDED,
            exit_code=0,
            started_at=NOW,
            finished_at=datetime(2026, 8, 16, 11, 0, tzinfo=timezone.utc),
        )


def test_result_rejects_succeeded_with_non_zero_exit_code() -> None:
    with pytest.raises(ValidationError, match="cannot have a non-zero exit_code"):
        OperationResult(
            plan_id="plan-1",
            operation_id="create-1",
            status=OperationStatus.SUCCEEDED,
            exit_code=1,
            finished_at=NOW,
        )


def test_command_rejects_empty_tuple() -> None:
    with pytest.raises(ValidationError, match="at least 1 item"):
        PlannedOperation.model_validate({**_operation().model_dump(), "command": ()})


def test_command_rejects_nul_byte_argument() -> None:
    with pytest.raises(ValidationError, match="NUL byte"):
        PlannedOperation.model_validate(
            {**_operation().model_dump(), "command": ("mkbrr", "create", "bad\x00arg")}
        )
