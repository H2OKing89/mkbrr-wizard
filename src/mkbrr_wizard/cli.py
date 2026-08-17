"""Console entry point for interactive and headless mkbrr workflows."""

from __future__ import annotations

import argparse
import json
import os
import sys
import sysconfig
from collections.abc import Sequence
from contextlib import contextmanager
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path

import yaml
from pydantic import ValidationError
from rich.console import Console
from rich.live import Live

from mkbrr_wizard import legacy_app as legacy
from mkbrr_wizard.application import WizardApplication
from mkbrr_wizard.batch_models import (
    BatchJob,
    BatchManifest,
    export_batch_json_schema,
    generate_batch_json_schema,
)
from mkbrr_wizard.execution.scheduler import RunJournal, SchedulerPolicy
from mkbrr_wizard.models import ExecutionPlan, ProgressEvent
from mkbrr_wizard.planning import PlanBuilder
from mkbrr_wizard.ui.rendering import (
    build_batch_progress,
    render_batch_results,
    render_effective_plan,
)

console = Console()
error_console = Console(stderr=True)


def _add_output_flags(parser: argparse.ArgumentParser, *, dry_run: bool = True) -> None:
    if dry_run:
        parser.add_argument("--dry-run", action="store_true", help="Plan without executing")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    parser.add_argument(
        "--show-command",
        action="store_true",
        help="Include raw command lines in the terminal preview",
    )
    parser.add_argument(
        "--no-estimate",
        action="store_true",
        help="Skip recursive file-count and size estimation",
    )


def _add_scheduler_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--max-parallel-jobs", type=int)
    parser.add_argument("--hdd-parallel-per-device", type=int)
    parser.add_argument("--ssd-parallel-per-device", type=int)
    parser.add_argument("--max-total-workers", type=int)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mkbrr-wizard",
        description="Unraid-aware planning and orchestration for mkbrr",
    )
    parser.add_argument(
        "--config",
        default=legacy._default_config_path(),
        help="Configuration path (default: source config or user config directory)",
    )
    runtime = parser.add_mutually_exclusive_group()
    runtime.add_argument("--docker", action="store_true", help="Force Docker runtime")
    runtime.add_argument("--native", action="store_true", help="Force native runtime")

    commands = parser.add_subparsers(dest="command")
    commands.add_parser("interactive", help="Open the interactive wizard")

    create = commands.add_parser("create", help="Create one torrent without prompts")
    create.add_argument("path", help="Absolute host or container content path")
    create.add_argument("--preset", "-P", required=True)
    create.add_argument("--output", "-o", help="Absolute .torrent output path")
    create.add_argument("--tracker", action="append", default=[])
    create.add_argument("--webseed", action="append", default=[])
    create.add_argument("--private", action=argparse.BooleanOptionalAction, default=None)
    create.add_argument("--name", default="")
    create.add_argument("--comment")
    create.add_argument("--source")
    piece = create.add_mutually_exclusive_group()
    piece.add_argument("--piece-length", type=int)
    piece.add_argument("--target-piece-count", type=int)
    create.add_argument("--max-piece-length", type=int)
    date = create.add_mutually_exclusive_group()
    date.add_argument("--no-date", dest="no_date", action="store_true", default=None)
    date.add_argument("--with-date", dest="no_date", action="store_false")
    creator = create.add_mutually_exclusive_group()
    creator.add_argument("--no-creator", dest="no_creator", action="store_true", default=None)
    creator.add_argument("--with-creator", dest="no_creator", action="store_false")
    create.add_argument("--entropy", action=argparse.BooleanOptionalAction, default=None)
    create.add_argument("--skip-prefix", action=argparse.BooleanOptionalAction, default=None)
    season_warning = create.add_mutually_exclusive_group()
    season_warning.add_argument(
        "--fail-on-season-warning",
        dest="fail_on_season_warning",
        action="store_true",
        default=None,
    )
    season_warning.add_argument(
        "--allow-season-warning",
        dest="fail_on_season_warning",
        action="store_false",
    )
    create.add_argument("--include", action="append", default=[])
    create.add_argument("--exclude", action="append", default=[])
    _add_output_flags(create)

    batch = commands.add_parser("batch", help="Execute a YAML or JSON batch manifest")
    batch.add_argument("manifest")
    batch.add_argument("--preset", "-P", required=True)
    _add_scheduler_flags(batch)
    batch.add_argument("--report", help="Write an atomic resumable JSON report")
    batch.add_argument("--resume", action="store_true", help="Skip prior successful jobs")
    _add_output_flags(batch)

    plan = commands.add_parser("plan", help="Preview a batch manifest without executing")
    plan.add_argument("manifest")
    plan.add_argument("--preset", "-P", required=True)
    _add_scheduler_flags(plan)
    _add_output_flags(plan, dry_run=False)

    inspect = commands.add_parser("inspect", help="Inspect a torrent without prompts")
    inspect.add_argument("torrent")
    inspect.add_argument("--verbose", "-v", action="store_true")
    _add_output_flags(inspect)

    check = commands.add_parser("check", help="Verify content without prompts")
    check.add_argument("torrent")
    check.add_argument("content")
    check.add_argument("--verbose", "-v", action="store_true")
    check.add_argument("--quiet", "-q", action="store_true")
    check.add_argument("--workers", type=int)
    _add_output_flags(check)

    doctor = commands.add_parser("doctor", help="Validate configuration and runtime readiness")
    doctor.add_argument("--json", action="store_true")

    schema = commands.add_parser("schema", help="Export the generated batch JSON Schema")
    schema.add_argument("destination", nargs="?", default="-")

    init_config = commands.add_parser(
        "init-config",
        help="Write the sample configuration to --config",
    )
    init_config.add_argument(
        "--force",
        action="store_true",
        help="Replace an existing configuration file",
    )
    init_config.add_argument("--json", action="store_true")
    return parser


def _forced_runtime(args: argparse.Namespace) -> str | None:
    if args.docker:
        return "docker"
    if args.native:
        return "native"
    return None


def _absolute_path(value: str) -> str:
    cleaned = legacy._clean_user_path(value)
    if not os.path.isabs(cleaned):
        return os.path.abspath(cleaned)
    return cleaned


def _default_output(cfg: legacy.AppCfg, content: str) -> str:
    return legacy._default_batch_output_path(cfg, content)


def _load_manifest(path: str) -> BatchManifest:
    manifest_path = Path(path).expanduser()
    try:
        raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ValueError(f"Could not read batch manifest {manifest_path}: {error}") from error
    if not isinstance(raw, dict):
        raise ValueError("Batch manifest root must be a mapping")
    return BatchManifest.model_validate(raw)


def _create_job(args: argparse.Namespace, cfg: legacy.AppCfg) -> BatchJob:
    content = _absolute_path(args.path)
    output = _absolute_path(args.output) if args.output else _default_output(cfg, content)
    return BatchJob(
        path=content,
        output=output,
        trackers=tuple(args.tracker),
        webseeds=tuple(args.webseed),
        private=args.private,
        name=args.name,
        comment=args.comment,
        source=args.source,
        piece_length=args.piece_length,
        max_piece_length=args.max_piece_length,
        target_piece_count=args.target_piece_count,
        no_date=args.no_date,
        no_creator=args.no_creator,
        entropy=args.entropy,
        skip_prefix=args.skip_prefix,
        fail_on_season_warning=args.fail_on_season_warning,
        include_patterns=tuple(args.include),
        exclude_patterns=tuple(args.exclude),
    )


def _build_plan(
    args: argparse.Namespace,
    application: WizardApplication,
) -> ExecutionPlan:
    planner = PlanBuilder(application.cfg, application.runtime)
    estimate = not getattr(args, "no_estimate", False)
    if args.command == "create":
        return planner.plan_create(
            _create_job(args, application.cfg),
            preset=args.preset,
            dry_run=args.dry_run,
            estimate=estimate,
        )
    if args.command in {"batch", "plan"}:
        plan = planner.plan_batch(
            _load_manifest(args.manifest),
            preset=args.preset,
            dry_run=args.command == "plan" or args.dry_run,
            estimate=estimate,
        )
        policy = _scheduler_policy(args, application.cfg)
        return plan.model_copy(
            update={
                "metadata": {
                    **plan.metadata,
                    "scheduler": policy.model_dump(mode="json"),
                }
            }
        )
    if args.command == "inspect":
        return planner.plan_inspect(
            _absolute_path(args.torrent),
            verbose=args.verbose,
            dry_run=args.dry_run,
        )
    if args.command == "check":
        if args.workers is not None and args.workers < 0:
            raise ValueError("--workers must be zero or a positive integer")
        return planner.plan_check(
            _absolute_path(args.torrent),
            _absolute_path(args.content),
            verbose=args.verbose,
            quiet=args.quiet,
            workers=args.workers,
            dry_run=args.dry_run,
        )
    raise ValueError(f"Unsupported command: {args.command}")


def _scheduler_policy(args: argparse.Namespace, cfg: legacy.AppCfg) -> SchedulerPolicy:
    def selected(value: int | None, configured: int) -> int:
        return configured if value is None else value

    return SchedulerPolicy(
        max_parallel_jobs=selected(args.max_parallel_jobs, cfg.batch.max_parallel_jobs),
        hdd_parallel_per_device=selected(
            args.hdd_parallel_per_device,
            cfg.batch.hdd_parallel_per_device,
        ),
        ssd_parallel_per_device=selected(
            args.ssd_parallel_per_device,
            cfg.batch.ssd_parallel_per_device,
        ),
        max_total_workers=(
            cfg.batch.max_total_workers
            if args.max_total_workers is None
            else args.max_total_workers
        ),
    )


def _json_error(message: str, *, code: int) -> str:
    return json.dumps({"ok": False, "exit_code": code, "error": message}, indent=2)


def _write_json(value: str) -> None:
    """Write JSON without Rich line wrapping or markup processing."""
    sys.stdout.write(value)
    if not value.endswith("\n"):
        sys.stdout.write("\n")


def _emit_plan(plan: ExecutionPlan, args: argparse.Namespace) -> None:
    if args.json:
        _write_json(plan.to_json())
    else:
        render_effective_plan(console, plan, show_command=args.show_command)


def _execute_and_render(
    args: argparse.Namespace,
    application: WizardApplication,
    plan: ExecutionPlan,
) -> int:
    policy = (
        _scheduler_policy(args, application.cfg)
        if args.command == "batch"
        else SchedulerPolicy(max_parallel_jobs=1)
    )
    if getattr(args, "resume", False) and not getattr(args, "report", None):
        raise ValueError("--resume requires --report")
    journal = RunJournal(args.report) if getattr(args, "report", None) else None

    events: list[ProgressEvent] = []
    if args.json:
        callback = events.append
        run = application.execute_plan(
            plan,
            policy=policy,
            on_event=callback,
            journal=journal,
            resume=getattr(args, "resume", False),
        )
    else:
        with Live(
            build_batch_progress(plan, events),
            console=console,
            refresh_per_second=8,
            transient=False,
        ) as live:

            def update(event: ProgressEvent) -> None:
                events.append(event)
                live.update(build_batch_progress(plan, events))

            run = application.execute_plan(
                plan,
                policy=policy,
                on_event=update,
                journal=journal,
                resume=getattr(args, "resume", False),
            )
            # Interrupt cleanup deliberately suppresses callbacks so a broken
            # renderer cannot stop cancellation. Refresh once from the
            # scheduler's authoritative event log before closing the live view.
            live.update(build_batch_progress(plan, run.events))

    if args.json:
        payload = {
            "ok": run.exit_code == 0,
            "exit_code": run.exit_code,
            "plan": plan.model_dump(mode="json"),
            "run": json.loads(run.to_json()),
        }
        _write_json(json.dumps(payload, indent=2))
    else:
        render_batch_results(console, plan, run.results)
        for result in run.results:
            if result.stdout:
                console.print(result.stdout.rstrip())
            if result.stderr:
                error_console.print(result.stderr.rstrip())
    return run.exit_code


def _run_headless(args: argparse.Namespace) -> int:
    if args.command == "batch" and args.resume and not args.report:
        raise ValueError("--resume requires --report")
    application = WizardApplication.from_config(
        args.config,
        forced_runtime=_forced_runtime(args),
    )
    plan = _build_plan(args, application)
    if plan.dry_run:
        _emit_plan(plan, args)
        known_blocked = any(
            operation.metadata.get("output_exists") is True for operation in plan.operations
        )
        return 2 if known_blocked else 0
    if not args.json:
        render_effective_plan(console, plan, show_command=args.show_command)
    return _execute_and_render(args, application, plan)


def _run_doctor(args: argparse.Namespace) -> int:
    application = WizardApplication.from_config(
        args.config,
        forced_runtime=_forced_runtime(args),
    )
    cfg = application.cfg
    runtime_ready = (
        legacy.docker_available()
        if application.runtime == "docker"
        else legacy.native_available(cfg.mkbrr.binary)
    )
    runtime_value = cfg.mkbrr.image if application.runtime == "docker" else cfg.mkbrr.binary
    checks = {
        "config": {"ok": Path(args.config).is_file(), "value": str(Path(args.config))},
        "runtime": {
            "ok": runtime_ready,
            "value": f"{application.runtime}: {runtime_value}",
        },
        "presets": {
            "ok": Path(cfg.presets_yaml_host).is_file(),
            "value": cfg.presets_yaml_host,
        },
        "content_root": {
            "ok": Path(cfg.paths.host_data_root).is_dir(),
            "value": cfg.paths.host_data_root,
        },
        "output_dir": {
            "ok": Path(cfg.paths.host_output_dir).is_dir()
            and os.access(cfg.paths.host_output_dir, os.W_OK),
            "value": cfg.paths.host_output_dir,
        },
    }
    ok = all(item["ok"] for item in checks.values())
    if args.json:
        _write_json(json.dumps({"ok": ok, "checks": checks}, indent=2))
    else:
        for name, item in checks.items():
            marker = "✅" if item["ok"] else "❌"
            console.print(f"{marker} {name}: {item['value']}")
    return 0 if ok else 2


def _sample_config_path() -> Path:
    """Locate the canonical sample in a checkout or installed distribution."""

    checkout_sample = Path(__file__).resolve().parents[2] / "config.yaml.sample"
    installed_sample = (
        Path(sysconfig.get_path("data")) / "share" / "mkbrr-wizard" / "config.yaml.sample"
    )
    for candidate in (checkout_sample, installed_sample):
        if candidate.is_file():
            return candidate
    try:
        installed_distribution = distribution("mkbrr-wizard")
    except PackageNotFoundError:
        installed_distribution = None
    if installed_distribution is not None:
        for packaged_file in installed_distribution.files or ():
            if (
                str(packaged_file)
                .replace("\\", "/")
                .endswith("share/mkbrr-wizard/config.yaml.sample")
            ):
                candidate = Path(str(installed_distribution.locate_file(packaged_file)))
                if candidate.is_file():
                    return candidate
    raise FileNotFoundError(
        "The packaged config.yaml.sample could not be found; reinstall mkbrr-wizard"
    )


def _run_init_config(args: argparse.Namespace) -> int:
    destination = Path(args.config).expanduser()
    if destination.exists() and not args.force:
        raise ValueError(f"Configuration already exists: {destination} (use --force to replace it)")
    sample = _sample_config_path()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(sample.read_bytes())
    resolved = destination.resolve(strict=False)
    if args.json:
        _write_json(
            json.dumps(
                {"ok": True, "exit_code": 0, "config": str(resolved)},
                indent=2,
            )
        )
    else:
        console.print(f"Created configuration: {resolved}")
    return 0


@contextmanager
def _legacy_arguments(args: argparse.Namespace):
    original = sys.argv
    forwarded = [original[0], "--config", args.config]
    if args.docker:
        forwarded.append("--docker")
    elif args.native:
        forwarded.append("--native")
    sys.argv = forwarded
    try:
        yield
    finally:
        sys.argv = original


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        if args.command in {None, "interactive"}:
            with _legacy_arguments(args):
                legacy.main()
            return 0
        if args.command == "schema":
            if args.destination == "-":
                _write_json(json.dumps(generate_batch_json_schema(), indent=2))
            else:
                destination = export_batch_json_schema(args.destination)
                console.print(destination)
            return 0
        if args.command == "init-config":
            return _run_init_config(args)
        if args.command == "doctor":
            return _run_doctor(args)
        return _run_headless(args)
    except KeyboardInterrupt:
        if getattr(args, "json", False):
            _write_json(_json_error("Cancelled by user", code=130))
        else:
            error_console.print("Cancelled by user")
        return 130
    except (OSError, RuntimeError, ValueError, ValidationError, yaml.YAMLError) as error:
        if getattr(args, "json", False):
            _write_json(_json_error(str(error), code=2))
        else:
            error_console.print(f"Error: {error}")
        return 2


__all__ = ["build_parser", "main"]
