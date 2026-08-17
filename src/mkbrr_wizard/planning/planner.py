"""Translate validated requests into UI-neutral, serializable plans."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import TypeVar

from pydantic import JsonValue

from mkbrr_wizard import legacy_app as legacy
from mkbrr_wizard.batch_models import BatchJob, BatchManifest
from mkbrr_wizard.models import (
    EffectiveOptions,
    ExecutionPlan,
    OperationKind,
    PlannedOperation,
    ResolvedPath,
    RuntimeKind,
    StorageKind,
)
from mkbrr_wizard.planning.presets import PresetValues, load_preset_values

_UNRAID_DEVICE = re.compile(r"^/mnt/(disk\d+|cache(?:-[^/]+)?)(?:/|$)")
_TRACKER_DEFAULT_SOURCES = (
    ("anthelion.me", "ANT"),
    ("nebulance.io", "NBL"),
    ("beyond-hd.me", "BHD"),
    ("passthepopcorn.me", "PTP"),
    ("morethantv.me", "MTV"),
    ("empornium.sx", "Emp"),
    ("gazellegames.net", "GGn"),
    ("tracker.alpharatio.cc", "AlphaRatio"),
    ("seedpool.org", "seedpool.org"),
    ("lst.gg", "lst.gg"),
    ("aither.cc", "Aither"),
    ("upload.cx", "ULCX"),
    ("capybarabr.com", "CapybaraBR"),
    ("hawke.uno", "HUNO"),
    ("tracker.torrentleech.org", "TorrentLeech.org"),
    ("tracker.tleechreload.org", "TorrentLeech.org"),
)
_ValueT = TypeVar("_ValueT")


def _operation_id(kind: OperationKind, *values: str) -> str:
    digest = hashlib.sha256("\0".join((kind.value, *values)).encode()).hexdigest()[:16]
    return f"{kind.value}-{digest}"


def _stable_command(command: tuple[str, ...], runtime: str) -> tuple[str, ...]:
    """Remove execution-only randomness from a command used as a resume key."""

    normalized = list(command)
    if runtime == "docker":
        try:
            name_index = normalized.index("--name")
            normalized[name_index + 1] = "<generated-container-name>"
        except (ValueError, IndexError):
            pass
    return tuple(normalized)


def _override(value: _ValueT | None, inherited: _ValueT | None) -> _ValueT | None:
    return inherited if value is None else value


def _tracker_default_source(trackers: tuple[str, ...]) -> str | None:
    """Mirror the tested mkbrr release's first-tracker source inference."""

    if not trackers:
        return None
    first_tracker = trackers[0]
    return next(
        (source for domain, source in _TRACKER_DEFAULT_SOURCES if domain in first_tracker),
        None,
    )


def _effective_create_options(
    job: BatchJob,
    preset_name: str,
    preset: PresetValues,
    workers: int,
    *,
    preset_host_path: str,
    preset_runtime_path: str,
) -> EffectiveOptions:
    effective_trackers = job.trackers or preset.trackers
    if job.source is not None:
        effective_source = job.source or None
    else:
        effective_source = preset.source or _tracker_default_source(effective_trackers)
    effective_comment = job.comment if job.comment is not None else preset.comment

    if job.piece_length is not None:
        piece_length = job.piece_length
        target_piece_count = None
    elif job.target_piece_count is not None:
        piece_length = None
        target_piece_count = job.target_piece_count
    else:
        piece_length = preset.piece_length
        target_piece_count = preset.target_piece_count

    return EffectiveOptions(
        preset=preset_name,
        trackers=effective_trackers,
        webseeds=job.webseeds or preset.webseeds,
        private=_override(job.private, preset.private),
        source=effective_source,
        comment=effective_comment or None,
        piece_length=piece_length,
        max_piece_length=(
            job.max_piece_length if job.max_piece_length is not None else preset.max_piece_length
        ),
        target_piece_count=target_piece_count,
        workers=workers,
        include_patterns=(*preset.include_patterns, *job.include_patterns),
        exclude_patterns=(*preset.exclude_patterns, *job.exclude_patterns),
        extra={
            "name": job.name or None,
            "no_creator": _override(job.no_creator, preset.no_creator),
            "no_date": _override(job.no_date, preset.no_date),
            "entropy": _override(job.entropy, preset.entropy),
            "skip_prefix": _override(job.skip_prefix, preset.skip_prefix),
            "fail_on_season_warning": _override(
                job.fail_on_season_warning,
                preset.fail_on_season_warning,
            ),
            "preset_file_host": preset_host_path,
            "preset_file_runtime": preset_runtime_path,
        },
    )


def _operation_identity(
    kind: OperationKind,
    command: tuple[str, ...],
    runtime: str,
    cwd: str | None,
    options: EffectiveOptions,
    preset: PresetValues | None = None,
) -> str:
    payload = {
        "command": _stable_command(command, runtime),
        "cwd": cwd,
        "options": options.model_dump(mode="json"),
        "preset": preset.model_dump(mode="python") if preset is not None else None,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return _operation_id(kind, encoded)


def _storage_identity(path: str, storage: StorageKind) -> str:
    match = _UNRAID_DEVICE.match(os.path.abspath(path))
    if match:
        return match.group(1)
    try:
        return f"dev-{os.stat(path).st_dev}"
    except OSError:
        return f"{storage.value}-unknown"


def _estimate_content(path: str, *, max_entries: int = 100_000) -> tuple[int | None, int | None]:
    try:
        if os.path.isfile(path):
            return 1, os.path.getsize(path)
        if not os.path.isdir(path):
            return None, None
    except OSError:
        return None, None

    file_count = 0
    size_bytes = 0
    try:
        for root, _, files in os.walk(path):
            for filename in files:
                file_count += 1
                if file_count > max_entries:
                    return None, None
                try:
                    size_bytes += os.path.getsize(os.path.join(root, filename))
                except OSError:
                    continue
    except OSError:
        return None, None
    return file_count, size_bytes


def _resolve_content_storage(
    cfg: legacy.AppCfg,
    runtime: str,
    content_path: str,
    *,
    context: str,
) -> tuple[legacy.ResolvedContent, StorageKind, list[str]]:
    """Resolve, preflight, and classify one content path consistently."""
    warnings: list[str] = []
    resolved = legacy.resolve_unraid_content_path(
        cfg,
        runtime,
        content_path,
        emit_messages=False,
    )
    resolved = legacy.preflight_unraid_split_share(
        cfg,
        resolved,
        context=context,
        warning_sink=warnings.append,
    )
    if not os.path.exists(resolved.host_path):
        raise ValueError(f"Content path does not exist: {resolved.host_path}")
    storage = StorageKind(
        legacy.detect_storage_type(
            resolved.host_path,
            fuse_root=cfg.unraid.fuse_root,
            mount_priority=cfg.unraid.mount_priority,
        )
    )
    return resolved, storage, warnings


class PlanBuilder:
    """Build effective plans without prompting, rendering, or running commands."""

    def __init__(self, cfg: legacy.AppCfg, runtime: str) -> None:
        if runtime not in {"native", "docker"}:
            raise ValueError(f"Unsupported runtime: {runtime}")
        self.cfg = cfg
        self.runtime = runtime

    def plan_create(
        self,
        job: BatchJob,
        *,
        preset: str,
        dry_run: bool = False,
        estimate: bool = True,
    ) -> ExecutionPlan:
        manifest = BatchManifest(version=1, jobs=(job,))
        return self.plan_batch(
            manifest,
            preset=preset,
            dry_run=dry_run,
            estimate=estimate,
            workflow="create",
        )

    def plan_batch(
        self,
        manifest: BatchManifest,
        *,
        preset: str,
        dry_run: bool = False,
        estimate: bool = True,
        workflow: str = "batch",
    ) -> ExecutionPlan:
        operations: list[PlannedOperation] = []
        plan_warnings: list[str] = []
        preset_values = load_preset_values(self.cfg.presets_yaml_host, preset)
        resolved_outputs: dict[str, int] = {}

        for position, original_job in enumerate(manifest.jobs, 1):
            resolved, storage, warnings = _resolve_content_storage(
                self.cfg,
                self.runtime,
                original_job.path,
                context=f"{workflow} job {position}",
            )

            mapped_output = legacy.resolve_mounted_torrent_path(
                self.cfg,
                self.runtime,
                original_job.output,
                context=f"{workflow.title()} output path",
            )
            host_output = legacy.host_torrent_output_path(self.cfg, mapped_output)
            output_identity = os.path.normpath(os.path.realpath(host_output))
            previous_position = resolved_outputs.get(output_identity)
            if previous_position is not None:
                raise ValueError(
                    f"{workflow.title()} job {position} resolves to the same output as "
                    f"job {previous_position}: {host_output}"
                )
            resolved_outputs[output_identity] = position
            output_dir = Path(host_output).parent
            output_exists = os.path.exists(host_output)
            if output_exists:
                warnings.append(
                    f"Output already exists and execution will be blocked: {host_output}"
                )
                if not dry_run:
                    raise ValueError(f"Output already exists: {host_output}")
            if not output_dir.is_dir():
                raise ValueError(f"Output directory does not exist: {output_dir}")
            if not os.access(output_dir, os.W_OK):
                raise ValueError(f"Output directory is not writable: {output_dir}")

            job = original_job.model_copy(
                update={"path": resolved.runtime_path, "output": mapped_output}
            )
            command = legacy.build_batch_job_create_command(
                self.cfg,
                self.runtime,
                preset,
                job,
                host_data_root_override=resolved.host_mount_override,
                interactive=False,
            )
            workers = legacy.resolve_workers(storage.value, self.cfg.workers)
            command = command.with_args("--workers", str(workers or 0))
            effective_options = _effective_create_options(
                job,
                preset,
                preset_values,
                workers or 0,
                preset_host_path=self.cfg.presets_yaml_host,
                preset_runtime_path=(
                    self.cfg.presets_yaml_container
                    if self.runtime == "docker"
                    else self.cfg.presets_yaml_host
                ),
            )
            file_count, size_bytes = (
                _estimate_content(resolved.host_path) if estimate else (None, None)
            )
            operation_id = _operation_identity(
                OperationKind.CREATE,
                command.argv,
                self.runtime,
                command.cwd,
                effective_options,
                preset_values,
            )
            operation = PlannedOperation(
                operation_id=operation_id,
                position=position,
                kind=OperationKind.CREATE,
                source_path=ResolvedPath(
                    entered=original_job.path,
                    host=resolved.fuse_host_path,
                    runtime=resolved.runtime_path,
                    physical=resolved.host_path,
                ),
                output_path=ResolvedPath(
                    entered=original_job.output,
                    host=host_output,
                    runtime=mapped_output,
                    physical=host_output,
                ),
                command=command.argv,
                cwd=command.cwd,
                options=effective_options,
                storage=storage,
                storage_key=_storage_identity(resolved.host_path, storage),
                estimated_file_count=file_count,
                estimated_size_bytes=size_bytes,
                warnings=tuple(warnings),
                metadata={
                    "output_exists": output_exists,
                    "timeout_seconds": self.cfg.batch.job_timeout_seconds,
                },
            )
            operations.append(operation)
            plan_warnings.extend(warnings)

        metadata: dict[str, JsonValue] = {"workflow": workflow, "preset": preset}
        if workflow == "batch":
            metadata["scheduler"] = {
                "max_parallel_jobs": self.cfg.batch.max_parallel_jobs,
                "hdd_parallel_per_device": self.cfg.batch.hdd_parallel_per_device,
                "ssd_parallel_per_device": self.cfg.batch.ssd_parallel_per_device,
                "max_total_workers": self.cfg.batch.max_total_workers,
            }

        return ExecutionPlan(
            runtime=RuntimeKind(self.runtime),
            dry_run=dry_run,
            operations=tuple(operations),
            warnings=tuple(plan_warnings),
            metadata=metadata,
        )

    def plan_inspect(
        self,
        torrent_path: str,
        *,
        verbose: bool = False,
        dry_run: bool = False,
    ) -> ExecutionPlan:
        runtime_path = legacy.resolve_mounted_torrent_path(
            self.cfg,
            self.runtime,
            torrent_path,
            context="Inspect torrent path",
        )
        host_path = legacy.map_torrent_path(self.cfg, "native", runtime_path)
        if not os.path.isfile(host_path):
            raise ValueError(f"Torrent file not found: {host_path}")
        command = legacy.build_inspect_command(
            self.cfg,
            self.runtime,
            runtime_path,
            verbose=verbose,
            interactive=False,
        )
        operation = PlannedOperation(
            operation_id=_operation_identity(
                OperationKind.INSPECT,
                command.argv,
                self.runtime,
                command.cwd,
                EffectiveOptions(),
            ),
            position=1,
            kind=OperationKind.INSPECT,
            source_path=ResolvedPath(
                entered=torrent_path,
                host=host_path,
                runtime=runtime_path,
                physical=host_path,
            ),
            command=command.argv,
            cwd=command.cwd,
            metadata={
                "verbose": verbose,
                "timeout_seconds": self.cfg.batch.job_timeout_seconds,
            },
        )
        return ExecutionPlan(
            runtime=RuntimeKind(self.runtime),
            dry_run=dry_run,
            operations=(operation,),
            metadata={"workflow": "inspect"},
        )

    def plan_check(
        self,
        torrent_path: str,
        content_path: str,
        *,
        verbose: bool = False,
        quiet: bool = False,
        workers: int | None = None,
        dry_run: bool = False,
    ) -> ExecutionPlan:
        runtime_torrent = legacy.resolve_mounted_torrent_path(
            self.cfg,
            self.runtime,
            torrent_path,
            context="Check torrent path",
        )
        host_torrent = legacy.map_torrent_path(self.cfg, "native", runtime_torrent)
        if not os.path.isfile(host_torrent):
            raise ValueError(f"Torrent file not found: {host_torrent}")

        resolved, storage, warnings = _resolve_content_storage(
            self.cfg,
            self.runtime,
            content_path,
            context="check",
        )
        selected_workers = (
            workers
            if workers is not None
            else legacy.resolve_workers(storage.value, self.cfg.workers)
        )
        command = legacy.build_check_command(
            self.cfg,
            self.runtime,
            runtime_torrent,
            resolved.runtime_path,
            verbose=verbose,
            quiet=quiet,
            workers=selected_workers,
            host_data_root_override=resolved.host_mount_override,
            interactive=False,
        )
        operation = PlannedOperation(
            operation_id=_operation_identity(
                OperationKind.CHECK,
                command.argv,
                self.runtime,
                command.cwd,
                EffectiveOptions(workers=selected_workers or 0),
            ),
            position=1,
            kind=OperationKind.CHECK,
            source_path=ResolvedPath(
                entered=content_path,
                host=resolved.fuse_host_path,
                runtime=resolved.runtime_path,
                physical=resolved.host_path,
            ),
            command=command.argv,
            cwd=command.cwd,
            options=EffectiveOptions(workers=selected_workers or 0),
            storage=storage,
            storage_key=_storage_identity(resolved.host_path, storage),
            warnings=tuple(warnings),
            metadata={
                "torrent_entered": torrent_path,
                "torrent_host": host_torrent,
                "verbose": verbose,
                "quiet": quiet,
                "timeout_seconds": self.cfg.batch.job_timeout_seconds,
            },
        )
        return ExecutionPlan(
            runtime=RuntimeKind(self.runtime),
            dry_run=dry_run,
            operations=(operation,),
            warnings=tuple(warnings),
            metadata={"workflow": "check"},
        )


__all__ = ["PlanBuilder"]
