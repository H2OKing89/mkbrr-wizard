"""Rich renderers for execution plans, progress snapshots, and results.

This module never creates a global console.  Callers own the :class:`~rich.console.Console`
and can therefore choose a theme, redirect output, or use ``record=True`` in tests.
"""

from __future__ import annotations

import shlex
from collections import Counter
from collections.abc import Iterable, Sequence

from rich import box
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from mkbrr_wizard.execution.scheduler import SchedulerPolicy
from mkbrr_wizard.models import (
    ExecutionPlan,
    OperationKind,
    OperationResult,
    OperationStatus,
    PlannedOperation,
    ProgressEvent,
    ResolvedPath,
)

_DEFAULT_SCHEDULER_POLICY = SchedulerPolicy()

_STATUS_STYLES: dict[OperationStatus, str] = {
    OperationStatus.PENDING: "dim",
    OperationStatus.READY: "cyan",
    OperationStatus.RUNNING: "bold blue",
    OperationStatus.SUCCEEDED: "bold green",
    OperationStatus.FAILED: "bold red",
    OperationStatus.SKIPPED: "yellow",
    OperationStatus.CANCELLED: "yellow",
    OperationStatus.TIMED_OUT: "bold red",
    OperationStatus.BLOCKED: "bold magenta",
}


def _plain(value: object, *, style: str | None = None) -> Text:
    if style is None:
        return Text(str(value))
    return Text(str(value), style=style)


def _status_text(status: OperationStatus) -> Text:
    label = status.value.replace("_", " ").title()
    return Text(label, style=_STATUS_STYLES[status])


def _format_bytes(size: int) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
    value = float(size)
    for unit in units:
        if abs(value) < 1024 or unit == units[-1]:
            precision = 0 if unit == "B" else 1
            return f"{value:.{precision}f} {unit}"
        value /= 1024
    return f"{size} B"


def _format_duration(seconds: float) -> str:
    if seconds < 1:
        return f"{seconds * 1000:.0f} ms"
    if seconds < 60:
        return f"{seconds:.1f} s"
    minutes, remainder = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{int(minutes)}m {remainder:.0f}s"
    hours, minutes = divmod(minutes, 60)
    return f"{int(hours)}h {int(minutes)}m {remainder:.0f}s"


def _command_option(command: Sequence[str], option: str) -> str | None:
    """Return an option value exactly as the runtime command will receive it."""

    prefix = f"{option}="
    for index, argument in enumerate(command):
        if argument.startswith(prefix):
            return argument[len(prefix) :]
        if argument == option and index + 1 < len(command):
            return command[index + 1]
    return None


def _add_path_rows(table: Table, label: str, path: ResolvedPath) -> None:
    table.add_row(f"{label} (entered)", _plain(path.entered, style="bright_white"))
    table.add_row(f"{label} (host)", _plain(path.host, style="bright_white"))
    table.add_row(f"{label} (runtime)", _plain(path.runtime, style="bright_white"))
    table.add_row(
        f"{label} (physical)",
        _plain(path.physical or "—", style="bright_white" if path.physical else "dim"),
    )


def _operation_panel(operation: PlannedOperation, *, show_command: bool) -> Panel:
    table = Table.grid(padding=(0, 1))
    table.add_column("Setting", style="cyan", no_wrap=True)
    table.add_column("Effective value", overflow="fold")

    _add_path_rows(table, "Source", operation.source_path)
    if operation.output_path is not None:
        _add_path_rows(table, "Output", operation.output_path)
    else:
        table.add_row("Output", _plain("—", style="dim"))

    options = operation.options
    if operation.kind is OperationKind.CREATE:
        table.add_row("Preset", _plain(options.preset or "—", style="bright_white"))
        host_preset_file = options.extra.get("preset_file_host")
        if isinstance(host_preset_file, str) and host_preset_file:
            table.add_row(
                "Preset file (host)",
                _plain(host_preset_file, style="bright_white"),
            )
        runtime_preset_file = options.extra.get("preset_file_runtime")
        if not isinstance(runtime_preset_file, str) or not runtime_preset_file:
            runtime_preset_file = _command_option(operation.command, "--preset-file")
        if runtime_preset_file:
            table.add_row(
                "Preset file (runtime)",
                _plain(runtime_preset_file, style="bright_white"),
            )
        privacy = "private" if options.private else "public"
        table.add_row("Privacy", _plain(privacy))
        table.add_row("Trackers", _plain(", ".join(options.trackers) or "—"))
        table.add_row("Webseeds", _plain(", ".join(options.webseeds) or "—"))
        table.add_row("Source", _plain(options.source or "—"))
        table.add_row("Comment", _plain(options.comment or "—"))
        table.add_row("Name override", _plain(options.extra.get("name") or "—"))

        include_filters = ", ".join(options.include_patterns) or "—"
        exclude_filters = ", ".join(options.exclude_patterns) or "—"
        table.add_row("Include filters", _plain(include_filters))
        table.add_row("Exclude filters", _plain(exclude_filters))
        table.add_row("Piece strategy", _plain(options.piece_strategy))

        boolean_labels = {
            "no_date": "no date",
            "no_creator": "no creator",
            "entropy": "entropy",
            "skip_prefix": "skip prefix",
            "fail_on_season_warning": "fail on season warning",
        }
        enabled = [label for key, label in boolean_labels.items() if options.extra.get(key) is True]
        disabled = [
            label for key, label in boolean_labels.items() if options.extra.get(key) is False
        ]
        flags = [
            *(f"{label}=on" for label in enabled),
            *(f"{label}=off" for label in disabled),
        ]
        table.add_row("Create flags", _plain(", ".join(flags) or "defaults"))
    elif operation.kind is OperationKind.CHECK:
        table.add_row("Torrent (entered)", _plain(operation.metadata.get("torrent_entered", "—")))
        table.add_row("Torrent (host)", _plain(operation.metadata.get("torrent_host", "—")))
        table.add_row("Verbose", _plain("yes" if operation.metadata.get("verbose") else "no"))
        table.add_row("Quiet", _plain("yes" if operation.metadata.get("quiet") else "no"))
    elif operation.kind is OperationKind.INSPECT:
        table.add_row("Verbose", _plain("yes" if operation.metadata.get("verbose") else "no"))

    if operation.kind in {OperationKind.CREATE, OperationKind.CHECK}:
        table.add_row("Workers", _plain(str(options.workers) if options.workers else "auto"))

    storage = operation.storage.value.upper()
    if operation.storage_key:
        storage = f"{storage} ({operation.storage_key})"
    table.add_row("Storage", _plain(storage))

    if operation.kind is OperationKind.CREATE:
        estimates: list[str] = []
        if operation.estimated_file_count is not None:
            suffix = "file" if operation.estimated_file_count == 1 else "files"
            estimates.append(f"{operation.estimated_file_count:,} {suffix}")
        if operation.estimated_size_bytes is not None:
            estimates.append(_format_bytes(operation.estimated_size_bytes))
        table.add_row("Estimate", _plain(" / ".join(estimates) or "—"))

    if operation.warnings:
        warnings = Text("\n").join(
            Text(warning, style="bold yellow") for warning in operation.warnings
        )
        table.add_row("Warnings", warnings)
    if show_command:
        table.add_row("Command", _plain(shlex.join(operation.command), style="dim"))

    title = Text(
        f"{operation.position}. {operation.kind.value.title()} · {operation.operation_id}",
        style="bold cyan",
    )
    return Panel(table, title=title, border_style="cyan", box=box.ROUNDED)


def build_effective_plan(plan: ExecutionPlan, *, show_command: bool = False) -> Group:
    """Build a renderable preview of all resolved inputs and effective settings."""

    summary = Table.grid(padding=(0, 1))
    summary.add_column("Key", style="cyan", no_wrap=True)
    summary.add_column("Value")
    summary.add_row("Plan", _plain(plan.plan_id))
    summary.add_row("Runtime", _plain(plan.runtime.value))
    summary.add_row("Mode", _plain("dry run" if plan.dry_run else "execute"))
    summary.add_row("Operations", _plain(len(plan.operations)))
    summary.add_row("Created", _plain(plan.created_at.isoformat()))
    scheduler = plan.metadata.get("scheduler")
    if isinstance(scheduler, dict):
        max_parallel_jobs = scheduler.get(
            "max_parallel_jobs", _DEFAULT_SCHEDULER_POLICY.max_parallel_jobs
        )
        hdd_parallel = scheduler.get(
            "hdd_parallel_per_device",
            _DEFAULT_SCHEDULER_POLICY.hdd_parallel_per_device,
        )
        ssd_parallel = scheduler.get(
            "ssd_parallel_per_device",
            _DEFAULT_SCHEDULER_POLICY.ssd_parallel_per_device,
        )
        worker_budget = scheduler.get("max_total_workers")
        budget_text = str(worker_budget) if worker_budget is not None else "unlimited"
        summary.add_row(
            "Concurrency",
            _plain(
                f"{max_parallel_jobs} global · "
                f"HDD {hdd_parallel}/device · "
                f"SSD {ssd_parallel}/device · "
                f"worker budget {budget_text}"
            ),
        )

    renderables: list[Panel] = [
        Panel(summary, title="Effective Plan", border_style="magenta", box=box.ROUNDED)
    ]
    if plan.warnings:
        warning_text = Text("\n").join(
            Text(f"• {warning}", style="bold yellow") for warning in plan.warnings
        )
        renderables.append(Panel(warning_text, title="Plan warnings", border_style="yellow"))
    renderables.extend(
        _operation_panel(operation, show_command=show_command)
        for operation in sorted(plan.operations, key=lambda item: item.position)
    )
    return Group(*renderables)


def render_effective_plan(
    console: Console, plan: ExecutionPlan, *, show_command: bool = False
) -> None:
    """Print an effective-plan preview to a caller-provided console."""

    console.print(build_effective_plan(plan, show_command=show_command))


def _latest_operation_events(
    plan: ExecutionPlan, events: Iterable[ProgressEvent]
) -> dict[str, ProgressEvent]:
    operation_ids = {operation.operation_id for operation in plan.operations}
    latest: dict[str, ProgressEvent] = {}
    for event in events:
        if event.plan_id != plan.plan_id or event.operation_id not in operation_ids:
            continue
        previous = latest.get(event.operation_id)
        if previous is None or (event.sequence, event.occurred_at) > (
            previous.sequence,
            previous.occurred_at,
        ):
            latest[event.operation_id] = event
    return latest


def _progress_text(event: ProgressEvent | None) -> Text:
    if event is None:
        return Text("waiting", style="dim")
    if event.completed_units is None and event.total_units is None:
        if event.status is OperationStatus.RUNNING:
            return Text("active", style=_STATUS_STYLES[event.status])
        if event.status.is_terminal:
            return Text("finished", style=_STATUS_STYLES[event.status])
        return Text("waiting", style=_STATUS_STYLES[event.status])
    if event.total_units is None:
        return Text(f"{event.completed_units:,} units")
    completed = event.completed_units or 0
    if event.total_units == 0:
        return Text(f"{completed:,} / 0")
    percentage = completed / event.total_units * 100
    return Text(f"{completed:,} / {event.total_units:,} ({percentage:.0f}%)")


def build_batch_progress(
    plan: ExecutionPlan,
    events: Sequence[ProgressEvent],
    *,
    title: str = "Batch Progress",
) -> Table:
    """Build a stable snapshot using the latest event for each operation."""

    latest = _latest_operation_events(plan, events)
    terminal_count = sum(event.status.is_terminal for event in latest.values())
    operation_count = len(plan.operations)
    percentage = terminal_count / operation_count * 100 if operation_count else 100.0
    progress_title = (
        f"{title} · {terminal_count} / {operation_count} jobs finished ({percentage:.0f}%)"
    )
    table = Table(title=progress_title, box=box.SIMPLE_HEAVY, show_lines=False, expand=True)
    table.add_column("#", justify="right", style="cyan", no_wrap=True)
    table.add_column("Operation", no_wrap=True)
    table.add_column("Source", overflow="fold")
    table.add_column("Status", no_wrap=True)
    table.add_column("Job progress", justify="right", no_wrap=True)
    table.add_column("Message", overflow="fold")

    for operation in sorted(plan.operations, key=lambda item: item.position):
        event = latest.get(operation.operation_id)
        status = event.status if event is not None else OperationStatus.PENDING
        message = event.message if event is not None and event.message else "—"
        table.add_row(
            str(operation.position),
            _plain(operation.kind.value.title()),
            _plain(operation.source_path.entered),
            _status_text(status),
            _progress_text(event),
            _plain(message, style="dim" if message == "—" else None),
        )
    return table


def render_batch_progress(
    console: Console,
    plan: ExecutionPlan,
    events: Sequence[ProgressEvent],
    *,
    title: str = "Batch Progress",
) -> None:
    """Print a batch-progress snapshot to a caller-provided console."""

    console.print(build_batch_progress(plan, events, title=title))


def _result_detail(result: OperationResult) -> str:
    if result.error_message:
        return result.error_message
    if result.status is OperationStatus.TIMED_OUT:
        return "Timed out"
    return "—"


def build_batch_results(
    plan: ExecutionPlan,
    results: Sequence[OperationResult],
    *,
    title: str = "Batch Results",
) -> Table:
    """Build a terminal-result table with a compact status summary."""

    operations = {operation.operation_id: operation for operation in plan.operations}
    positions = {operation.operation_id: operation.position for operation in plan.operations}
    ordered_results = sorted(
        results,
        key=lambda result: (
            positions.get(result.operation_id, len(positions) + 1),
            result.operation_id,
        ),
    )
    status_counts = Counter(result.status for result in results)
    summary_parts = [
        f"{status_counts[status]} {status.value.replace('_', ' ')}"
        for status in OperationStatus
        if status_counts[status]
    ]
    summary = ", ".join(summary_parts) if summary_parts else "no results"

    table = Table(title=f"{title} · {summary}", box=box.SIMPLE_HEAVY, show_lines=False, expand=True)
    table.add_column("#", justify="right", style="cyan", no_wrap=True)
    table.add_column("Operation", no_wrap=True)
    table.add_column("Source", overflow="fold")
    table.add_column("Output", overflow="fold")
    table.add_column("Status", no_wrap=True)
    table.add_column("Code", justify="right", no_wrap=True)
    table.add_column("Duration", justify="right", no_wrap=True)
    table.add_column("Details", overflow="fold")

    for result in ordered_results:
        operation = operations.get(result.operation_id)
        position = str(operation.position) if operation is not None else "—"
        operation_name = (
            operation.kind.value.title() if operation is not None else result.operation_id
        )
        source = operation.source_path.entered if operation is not None else "—"
        planned_output = operation.output_path.host if operation and operation.output_path else None
        output = result.output_path or planned_output or "—"
        table.add_row(
            position,
            _plain(operation_name),
            _plain(source),
            _plain(output),
            _status_text(result.status),
            _plain(result.exit_code if result.exit_code is not None else "—"),
            _plain(_format_duration(result.elapsed_seconds)),
            _plain(_result_detail(result)),
        )
    return table


def render_batch_results(
    console: Console,
    plan: ExecutionPlan,
    results: Sequence[OperationResult],
    *,
    title: str = "Batch Results",
) -> None:
    """Print terminal batch results to a caller-provided console."""

    console.print(build_batch_results(plan, results, title=title))
