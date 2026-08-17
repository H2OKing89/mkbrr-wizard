from __future__ import annotations

import json
from pathlib import Path

import yaml

from mkbrr_wizard import cli
from mkbrr_wizard.execution.scheduler import SchedulerRun
from mkbrr_wizard.models import OperationResult, OperationStatus


def _environment(tmp_path: Path, *, binary: str = "/bin/true") -> tuple[Path, Path, Path]:
    content_root = tmp_path / "data"
    output_root = tmp_path / "torrents"
    config_root = tmp_path / "mkbrr"
    content_root.mkdir()
    output_root.mkdir()
    config_root.mkdir()
    content = content_root / "movie.mkv"
    content.write_bytes(b"content")
    presets = config_root / "presets.yaml"
    presets.write_text("version: 1\npresets:\n  test:\n    private: true\n", encoding="utf-8")
    config = tmp_path / "config.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "runtime": "native",
                "docker_support": False,
                "chown": False,
                "mkbrr": {"binary": binary},
                "paths": {
                    "host_data_root": str(content_root),
                    "container_data_root": "/data",
                    "host_output_dir": str(output_root),
                    "container_output_dir": "/torrentfiles",
                    "host_config_dir": str(config_root),
                    "container_config_dir": "/root/.config/mkbrr",
                },
                "presets_yaml": "presets.yaml",
            }
        ),
        encoding="utf-8",
    )
    return config, content, output_root


def test_init_config_writes_packaged_sample_and_refuses_overwrite(
    tmp_path: Path,
    capsys,
) -> None:
    destination = tmp_path / "nested" / "config.yaml"
    arguments = ["--config", str(destination), "init-config", "--json"]

    assert cli.main(arguments) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"ok": True, "exit_code": 0, "config": str(destination.resolve())}
    assert yaml.safe_load(destination.read_text(encoding="utf-8"))["runtime"] == "auto"

    assert cli.main(arguments) == 2
    error = json.loads(capsys.readouterr().out)
    assert "already exists" in error["error"]


def test_init_config_force_replaces_existing_file(tmp_path: Path) -> None:
    destination = tmp_path / "config.yaml"
    destination.write_text("old: true\n", encoding="utf-8")

    assert cli.main(["--config", str(destination), "init-config", "--force", "--json"]) == 0

    assert "old" not in yaml.safe_load(destination.read_text(encoding="utf-8"))


def test_create_dry_run_emits_json_plan(tmp_path: Path, capsys) -> None:
    config, content, output_root = _environment(tmp_path)
    output = output_root / "movie.torrent"

    exit_code = cli.main(
        [
            "--config",
            str(config),
            "--native",
            "create",
            str(content),
            "--preset",
            "test",
            "--output",
            str(output),
            "--dry-run",
            "--json",
            "--no-estimate",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert payload["operations"][0]["output_path"]["host"] == str(output)
    assert payload["operations"][0]["options"]["private"] is True


def test_docker_plan_json_labels_host_and_runtime_preset_paths(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    config, content, output_root = _environment(tmp_path)
    monkeypatch.setattr(cli.legacy.sys.stdin, "isatty", lambda: True)

    exit_code = cli.main(
        [
            "--config",
            str(config),
            "--docker",
            "create",
            str(content),
            "--preset",
            "test",
            "--output",
            str(output_root / "movie.torrent"),
            "--dry-run",
            "--json",
            "--no-estimate",
        ]
    )

    assert exit_code == 0
    operation = json.loads(capsys.readouterr().out)["operations"][0]
    assert "-it" not in operation["command"]
    extra = operation["options"]["extra"]
    assert extra == {
        "name": None,
        "no_creator": False,
        "no_date": False,
        "entropy": None,
        "skip_prefix": False,
        "fail_on_season_warning": None,
        "preset_file_host": str(tmp_path / "mkbrr" / "presets.yaml"),
        "preset_file_runtime": "/root/.config/mkbrr/presets.yaml",
    }


def test_docker_inspect_and_check_plans_never_allocate_a_tty(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    config, content, output_root = _environment(tmp_path)
    torrent = output_root / "movie.torrent"
    torrent.write_bytes(b"torrent")
    monkeypatch.setattr(cli.legacy.sys.stdin, "isatty", lambda: True)

    commands: list[list[str]] = []
    for arguments in (
        ["inspect", str(torrent)],
        ["check", str(torrent), str(content)],
    ):
        exit_code = cli.main(
            [
                "--config",
                str(config),
                "--docker",
                *arguments,
                "--dry-run",
                "--json",
                "--no-estimate",
            ]
        )
        assert exit_code == 0
        commands.append(json.loads(capsys.readouterr().out)["operations"][0]["command"])

    assert all("-it" not in command for command in commands)


def test_effective_plan_merges_defaults_preset_and_cli_options(tmp_path: Path, capsys) -> None:
    config, content, output_root = _environment(tmp_path)
    presets = tmp_path / "mkbrr" / "presets.yaml"
    presets.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "default": {
                    "private": True,
                    "source": "DEFAULT",
                    "exclude_patterns": ["*.nfo"],
                },
                "presets": {
                    "test": {
                        "source": "PRESET",
                        "trackers": ["https://tracker.example/announce"],
                        "include_patterns": ["*.mkv"],
                        "no_creator": True,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    exit_code = cli.main(
        [
            "--config",
            str(config),
            "--native",
            "create",
            str(content),
            "--preset",
            "test",
            "--output",
            str(output_root / "movie.torrent"),
            "--source",
            "CLI",
            "--include",
            "*.srt",
            "--dry-run",
            "--json",
            "--no-estimate",
        ]
    )

    assert exit_code == 0
    options = json.loads(capsys.readouterr().out)["operations"][0]["options"]
    assert options["private"] is True
    assert options["source"] == "CLI"
    assert options["trackers"] == ["https://tracker.example/announce"]
    assert options["include_patterns"] == ["*.mkv", "*.srt"]
    assert options["exclude_patterns"] == ["*.nfo"]
    assert options["extra"]["no_creator"] is True


def test_explicit_empty_source_and_comment_clear_preset_values(
    tmp_path: Path,
    capsys,
) -> None:
    config, content, output_root = _environment(tmp_path)
    presets = tmp_path / "mkbrr" / "presets.yaml"
    presets.write_text(
        "version: 1\npresets:\n  test:\n    source: PRESET\n    comment: preset comment\n",
        encoding="utf-8",
    )

    exit_code = cli.main(
        [
            "--config",
            str(config),
            "--native",
            "create",
            str(content),
            "--preset",
            "test",
            "--output",
            str(output_root / "movie.torrent"),
            "--source",
            "",
            "--comment",
            "",
            "--dry-run",
            "--json",
            "--no-estimate",
        ]
    )

    assert exit_code == 0
    operation = json.loads(capsys.readouterr().out)["operations"][0]
    assert operation["options"]["source"] is None
    assert operation["options"]["comment"] is None
    assert operation["command"][operation["command"].index("--source") + 1] == ""
    assert operation["command"][operation["command"].index("--comment") + 1] == ""


def test_effective_plan_infers_source_from_first_known_tracker(tmp_path: Path, capsys) -> None:
    config, content, output_root = _environment(tmp_path)
    presets = tmp_path / "mkbrr" / "presets.yaml"
    presets.write_text(
        """\
version: 1
presets:
  test:
    trackers:
      - https://passthepopcorn.me/announce/example
""",
        encoding="utf-8",
    )

    exit_code = cli.main(
        [
            "--config",
            str(config),
            "--native",
            "create",
            str(content),
            "--preset",
            "test",
            "--output",
            str(output_root / "movie.torrent"),
            "--dry-run",
            "--json",
            "--no-estimate",
        ]
    )

    assert exit_code == 0
    operation = json.loads(capsys.readouterr().out)["operations"][0]
    assert operation["options"]["source"] == "PTP"


def test_create_returns_underlying_failure_exit_code(tmp_path: Path, capsys) -> None:
    config, content, output_root = _environment(tmp_path, binary="/bin/false")

    exit_code = cli.main(
        [
            "--config",
            str(config),
            "--native",
            "create",
            str(content),
            "--preset",
            "test",
            "--output",
            str(output_root / "movie.torrent"),
            "--json",
            "--no-estimate",
        ]
    )

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["exit_code"] == 1
    assert payload["run"]["results"][0]["status"] == "failed"


def test_batch_report_can_be_resumed(tmp_path: Path, capsys) -> None:
    config, content, output_root = _environment(tmp_path)
    manifest_path = tmp_path / "batch.yaml"
    manifest_path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "jobs": [
                    {
                        "path": str(content),
                        "output": str(output_root / "movie.torrent"),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    report = tmp_path / "report.json"
    arguments = [
        "--config",
        str(config),
        "--native",
        "batch",
        str(manifest_path),
        "--preset",
        "test",
        "--report",
        str(report),
        "--json",
        "--no-estimate",
    ]

    assert cli.main(arguments) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["run"]["summary"]["succeeded"] == 1
    assert report.is_file()

    assert cli.main([*arguments, "--resume"]) == 0
    resumed = json.loads(capsys.readouterr().out)
    assert resumed["run"]["summary"]["skipped"] == 1

    assert cli.main([*arguments, "--resume"]) == 0
    resumed_again = json.loads(capsys.readouterr().out)
    assert resumed_again["run"]["summary"]["skipped"] == 1


def test_resume_reexecutes_when_effective_job_options_change(tmp_path: Path, capsys) -> None:
    config, content, output_root = _environment(tmp_path)
    manifest_path = tmp_path / "batch.yaml"
    report = tmp_path / "report.json"

    def write_manifest(source: str) -> None:
        manifest_path.write_text(
            yaml.safe_dump(
                {
                    "version": 1,
                    "jobs": [
                        {
                            "path": str(content),
                            "output": str(output_root / "movie.torrent"),
                            "source": source,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    arguments = [
        "--config",
        str(config),
        "--native",
        "batch",
        str(manifest_path),
        "--preset",
        "test",
        "--report",
        str(report),
        "--json",
        "--no-estimate",
    ]
    write_manifest("FIRST")
    assert cli.main(arguments) == 0
    first = json.loads(capsys.readouterr().out)

    write_manifest("SECOND")
    assert cli.main([*arguments, "--resume"]) == 0
    second = json.loads(capsys.readouterr().out)

    assert (
        first["plan"]["operations"][0]["operation_id"]
        != second["plan"]["operations"][0]["operation_id"]
    )
    assert second["run"]["summary"] == {"succeeded": 1, "failed": 0, "skipped": 0}


def test_docker_operation_identity_ignores_generated_container_name(tmp_path: Path, capsys) -> None:
    config, content, output_root = _environment(tmp_path)
    arguments = [
        "--config",
        str(config),
        "--docker",
        "create",
        str(content),
        "--preset",
        "test",
        "--output",
        str(output_root / "movie.torrent"),
        "--dry-run",
        "--json",
        "--no-estimate",
    ]

    assert cli.main(arguments) == 0
    first = json.loads(capsys.readouterr().out)["operations"][0]
    assert cli.main(arguments) == 0
    second = json.loads(capsys.readouterr().out)["operations"][0]

    assert first["command"] != second["command"]
    assert first["operation_id"] == second["operation_id"]


def test_zero_scheduler_limit_is_validation_exit_two(tmp_path: Path, capsys) -> None:
    config, content, output_root = _environment(tmp_path)
    manifest = tmp_path / "batch.yaml"
    manifest.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "jobs": [
                    {
                        "path": str(content),
                        "output": str(output_root / "movie.torrent"),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    exit_code = cli.main(
        [
            "--config",
            str(config),
            "--native",
            "batch",
            str(manifest),
            "--preset",
            "test",
            "--max-parallel-jobs",
            "0",
            "--json",
            "--no-estimate",
        ]
    )

    assert exit_code == 2
    assert json.loads(capsys.readouterr().out)["exit_code"] == 2


def test_batch_rejects_outputs_that_collide_after_path_mapping(tmp_path: Path, capsys) -> None:
    config, first_content, output_root = _environment(tmp_path)
    second_content = first_content.with_name("second.mkv")
    second_content.write_bytes(b"content")
    manifest = tmp_path / "batch.yaml"
    manifest.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "jobs": [
                    {
                        "path": str(first_content),
                        "output": str(output_root / "movie.torrent"),
                    },
                    {
                        "path": str(second_content),
                        "output": "/torrentfiles/movie.torrent",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    exit_code = cli.main(
        [
            "--config",
            str(config),
            "--native",
            "batch",
            str(manifest),
            "--preset",
            "test",
            "--dry-run",
            "--json",
            "--no-estimate",
        ]
    )

    assert exit_code == 2
    error = json.loads(capsys.readouterr().out)["error"]
    assert "resolves to the same output as job 1" in error


def test_create_can_explicitly_disable_boolean_preset_values(tmp_path: Path, capsys) -> None:
    config, content, output_root = _environment(tmp_path)
    presets = tmp_path / "mkbrr" / "presets.yaml"
    presets.write_text(
        "version: 1\npresets:\n  test:\n    no_date: true\n    entropy: true\n",
        encoding="utf-8",
    )

    exit_code = cli.main(
        [
            "--config",
            str(config),
            "--native",
            "create",
            str(content),
            "--preset",
            "test",
            "--output",
            str(output_root / "movie.torrent"),
            "--with-date",
            "--no-entropy",
            "--dry-run",
            "--json",
            "--no-estimate",
        ]
    )

    assert exit_code == 0
    operation = json.loads(capsys.readouterr().out)["operations"][0]
    assert "--no-date=false" in operation["command"]
    assert "--entropy=false" in operation["command"]
    assert operation["options"]["extra"]["no_date"] is False
    assert operation["options"]["extra"]["entropy"] is False


def test_doctor_reports_an_unavailable_forced_runtime(tmp_path: Path, capsys) -> None:
    config, _, _ = _environment(tmp_path, binary=str(tmp_path / "missing-mkbrr"))

    exit_code = cli.main(
        [
            "--config",
            str(config),
            "--native",
            "doctor",
            "--json",
        ]
    )

    assert exit_code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["checks"]["runtime"]["ok"] is False


def test_interactive_entry_reports_missing_config_without_traceback(tmp_path: Path, capsys) -> None:
    exit_code = cli.main(
        ["--config", str(tmp_path / "missing-config.yaml"), "--native", "interactive"]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "Config not found" in captured.err
    assert "Traceback" not in captured.err


def test_missing_content_is_validation_exit_two(tmp_path: Path, capsys) -> None:
    config, _, output_root = _environment(tmp_path)

    exit_code = cli.main(
        [
            "--config",
            str(config),
            "--native",
            "create",
            str(tmp_path / "missing"),
            "--preset",
            "test",
            "--output",
            str(output_root / "missing.torrent"),
            "--dry-run",
            "--json",
        ]
    )

    assert exit_code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["exit_code"] == 2


def test_dry_run_returns_two_when_output_is_known_to_exist(tmp_path: Path, capsys) -> None:
    config, content, output_root = _environment(tmp_path)
    output = output_root / "movie.torrent"
    output.write_bytes(b"existing")

    exit_code = cli.main(
        [
            "--config",
            str(config),
            "--native",
            "create",
            str(content),
            "--preset",
            "test",
            "--output",
            str(output),
            "--dry-run",
            "--json",
            "--no-estimate",
        ]
    )

    assert exit_code == 2
    operation = json.loads(capsys.readouterr().out)["operations"][0]
    assert operation["metadata"]["output_exists"] is True


def test_real_run_fails_before_execution_when_output_exists(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    config, content, output_root = _environment(tmp_path)
    output = output_root / "movie.torrent"
    output.write_bytes(b"existing")

    def unexpected_execution(*args, **kwargs):
        raise AssertionError("execution must not start when a planned output exists")

    monkeypatch.setattr(cli.WizardApplication, "execute_plan", unexpected_execution)
    exit_code = cli.main(
        [
            "--config",
            str(config),
            "--native",
            "create",
            str(content),
            "--preset",
            "test",
            "--output",
            str(output),
            "--json",
            "--no-estimate",
        ]
    )

    assert exit_code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["exit_code"] == 2
    assert payload["error"] == f"Output already exists: {output}"


def test_batch_with_existing_output_never_starts_other_jobs(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    config, first_content, output_root = _environment(tmp_path)
    second_content = first_content.with_name("second.mkv")
    second_content.write_bytes(b"second")
    existing_output = output_root / "movie.torrent"
    existing_output.write_bytes(b"existing")
    manifest = tmp_path / "batch.yaml"
    manifest.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "jobs": [
                    {"path": str(first_content), "output": str(existing_output)},
                    {
                        "path": str(second_content),
                        "output": str(output_root / "second.torrent"),
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    def unexpected_execution(*args, **kwargs):
        raise AssertionError("a batch with an existing output must fail before execution")

    monkeypatch.setattr(cli.WizardApplication, "execute_plan", unexpected_execution)
    exit_code = cli.main(
        [
            "--config",
            str(config),
            "--native",
            "batch",
            str(manifest),
            "--preset",
            "test",
            "--json",
            "--no-estimate",
        ]
    )

    assert exit_code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == f"Output already exists: {existing_output}"


def test_interrupted_batch_json_keeps_partial_results(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    config, first_content, output_root = _environment(tmp_path)
    second_content = first_content.with_name("second.mkv")
    second_content.write_bytes(b"second")
    manifest = tmp_path / "batch.yaml"
    manifest.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "jobs": [
                    {
                        "path": str(first_content),
                        "output": str(output_root / "first.torrent"),
                    },
                    {
                        "path": str(second_content),
                        "output": str(output_root / "second.torrent"),
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    def interrupted_run(_application, plan, **_kwargs):
        first, second = plan.operations
        return SchedulerRun(
            plan_id=plan.plan_id,
            results=(
                OperationResult(
                    plan_id=plan.plan_id,
                    operation_id=first.operation_id,
                    status=OperationStatus.SUCCEEDED,
                    exit_code=0,
                ),
                OperationResult(
                    plan_id=plan.plan_id,
                    operation_id=second.operation_id,
                    status=OperationStatus.CANCELLED,
                    exit_code=130,
                    error_message="Cancelled by user",
                ),
            ),
            events=(),
            elapsed_seconds=0.1,
            interrupted=True,
        )

    monkeypatch.setattr(cli.WizardApplication, "execute_plan", interrupted_run)
    exit_code = cli.main(
        [
            "--config",
            str(config),
            "--native",
            "batch",
            str(manifest),
            "--preset",
            "test",
            "--json",
            "--no-estimate",
        ]
    )

    assert exit_code == 130
    payload = json.loads(capsys.readouterr().out)
    assert payload["exit_code"] == 130
    assert payload["run"]["interrupted"] is True
    assert [result["status"] for result in payload["run"]["results"]] == [
        OperationStatus.SUCCEEDED.value,
        OperationStatus.CANCELLED.value,
    ]
