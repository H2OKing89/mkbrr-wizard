from datetime import datetime, timezone
from io import StringIO

from rich.console import Console

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
from mkbrr_wizard.ui.rendering import (
    render_batch_progress,
    render_batch_results,
    render_effective_plan,
)

NOW = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)


def _console() -> Console:
    return Console(
        file=StringIO(),
        record=True,
        width=220,
        color_system=None,
        force_terminal=False,
    )


def _plan() -> ExecutionPlan:
    create = PlannedOperation(
        operation_id="create-show",
        position=1,
        kind=OperationKind.CREATE,
        source_path=ResolvedPath(
            entered="/mnt/user/media/Show [2026]",
            host="/mnt/user/media/Show [2026]",
            runtime="/data/Show [2026]",
            physical="/mnt/disk2/media/Show [2026]",
        ),
        output_path=ResolvedPath(
            entered="show.torrent",
            host="/mnt/user/torrents/show.torrent",
            runtime="/torrentfiles/show.torrent",
            physical="/mnt/cache/torrents/show.torrent",
        ),
        command=(
            "mkbrr",
            "create",
            "/data/Show [2026]",
            "--preset",
            "btn",
            "--preset-file",
            "/root/.config/mkbrr/presets.yaml",
        ),
        options=EffectiveOptions(
            preset="btn",
            trackers=("https://tracker.example/announce",),
            private=True,
            piece_length=20,
            workers=2,
            include_patterns=("*.mkv",),
            exclude_patterns=("*.nfo",),
            extra={
                "preset_file_host": "/mnt/cache/appdata/mkbrr/presets.yaml",
                "preset_file_runtime": "/root/.config/mkbrr/presets.yaml",
            },
        ),
        storage=StorageKind.HDD,
        storage_key="disk2",
        estimated_file_count=8,
        estimated_size_bytes=1_073_741_824,
        warnings=("Output collision [review required]",),
    )
    check = PlannedOperation(
        operation_id="check-show",
        position=2,
        kind=OperationKind.CHECK,
        source_path=ResolvedPath(
            entered="show.torrent",
            host="/mnt/user/torrents/show.torrent",
            runtime="/torrentfiles/show.torrent",
        ),
        command=("mkbrr", "check", "show.torrent"),
        storage=StorageKind.SSD,
    )
    return ExecutionPlan(
        plan_id="plan-rich",
        created_at=NOW,
        runtime=RuntimeKind.DOCKER,
        dry_run=True,
        operations=(create, check),
        warnings=("Review all paths before execution",),
    )


def test_effective_plan_renders_resolved_values_without_global_console() -> None:
    console = _console()

    render_effective_plan(console, _plan(), show_command=True)
    rendered = console.export_text()

    assert "Effective Plan" in rendered
    assert "dry run" in rendered
    assert "/mnt/disk2/media/Show [2026]" in rendered
    assert "https://tracker.example/announce" in rendered
    assert "Preset file (host)" in rendered
    assert "/mnt/cache/appdata/mkbrr/presets.yaml" in rendered
    assert "Preset file (runtime)" in rendered
    assert "/root/.config/mkbrr/presets.yaml" in rendered
    assert "fixed 2^20 bytes" in rendered
    assert "1.0 GiB" in rendered
    assert "Output collision [review required]" in rendered
    assert "mkbrr create '/data/Show [2026]' --preset btn" in rendered


def test_effective_plan_uses_scheduler_policy_ssd_default() -> None:
    plan = _plan().model_copy(update={"metadata": {"scheduler": {}}})
    console = _console()

    render_effective_plan(console, plan)

    assert "SSD 2/device" in console.export_text()


def test_batch_progress_uses_latest_event_and_shows_pending_jobs() -> None:
    plan = _plan()
    events = [
        ProgressEvent(
            event_id="event-1",
            plan_id=plan.plan_id,
            operation_id="create-show",
            sequence=1,
            occurred_at=NOW,
            kind=ProgressEventKind.STARTED,
            status=OperationStatus.RUNNING,
            completed_units=1,
            total_units=8,
            message="Starting",
        ),
        ProgressEvent(
            event_id="event-2",
            plan_id=plan.plan_id,
            operation_id="create-show",
            sequence=2,
            occurred_at=NOW,
            kind=ProgressEventKind.PROGRESS,
            status=OperationStatus.RUNNING,
            completed_units=6,
            total_units=8,
            message="Hashing [safe text]",
        ),
        ProgressEvent(
            event_id="ignored",
            plan_id="another-plan",
            operation_id="create-show",
            sequence=99,
            occurred_at=NOW,
            kind=ProgressEventKind.FAILED,
            status=OperationStatus.FAILED,
            message="Must not render",
        ),
    ]
    console = _console()

    render_batch_progress(console, plan, events)
    rendered = console.export_text()

    assert "Batch Progress" in rendered
    assert "6 / 8 (75%)" in rendered
    assert "Hashing [safe text]" in rendered
    assert "Starting" not in rendered
    assert "Must not render" not in rendered
    assert "Pending" in rendered


def test_batch_progress_derives_job_lifecycle_without_unit_events() -> None:
    plan = _plan()
    events = [
        ProgressEvent(
            event_id="event-started",
            plan_id=plan.plan_id,
            operation_id="create-show",
            sequence=1,
            occurred_at=NOW,
            kind=ProgressEventKind.STARTED,
            status=OperationStatus.RUNNING,
            message="Started on disk2",
        ),
        ProgressEvent(
            event_id="event-completed",
            plan_id=plan.plan_id,
            operation_id="check-show",
            sequence=2,
            occurred_at=NOW,
            kind=ProgressEventKind.COMPLETED,
            status=OperationStatus.SUCCEEDED,
            message="Succeeded",
        ),
    ]
    console = _console()

    render_batch_progress(console, plan, events)
    rendered = console.export_text()

    assert "1 / 2 jobs finished (50%)" in rendered
    assert "Job progress" in rendered
    assert "active" in rendered
    assert "finished" in rendered


def test_batch_results_renders_summary_and_failure_details() -> None:
    plan = _plan()
    results = [
        OperationResult(
            plan_id=plan.plan_id,
            operation_id="create-show",
            status=OperationStatus.SUCCEEDED,
            exit_code=0,
            elapsed_seconds=1.25,
            finished_at=NOW,
            output_path="/mnt/user/torrents/show.torrent",
        ),
        OperationResult(
            plan_id=plan.plan_id,
            operation_id="check-show",
            status=OperationStatus.FAILED,
            exit_code=2,
            elapsed_seconds=0.5,
            finished_at=NOW,
            error_message="Hash mismatch [piece 4]",
        ),
    ]
    console = _console()

    render_batch_results(console, plan, results)
    rendered = console.export_text()

    assert "Batch Results · 1 succeeded, 1 failed" in rendered
    assert "Succeeded" in rendered
    assert "Failed" in rendered
    assert "1.2 s" in rendered
    assert "500 ms" in rendered
    assert "Hash mismatch [piece 4]" in rendered


def test_batch_results_duration_keeps_seconds_above_one_hour() -> None:
    plan = _plan()
    result = OperationResult(
        plan_id=plan.plan_id,
        operation_id="create-show",
        status=OperationStatus.SUCCEEDED,
        exit_code=0,
        elapsed_seconds=3661,
        finished_at=NOW,
    )
    console = _console()

    render_batch_results(console, plan, [result])

    assert "1h 1m 1s" in console.export_text()
