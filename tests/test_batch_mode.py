"""Tests for batch mode helpers and main-flow integration."""

from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest  # type: ignore[import-untyped]

from .conftest import _Seq


def _mk_args(config_path: str) -> SimpleNamespace:
    return SimpleNamespace(config=config_path, docker=False, native=False)


def _sample_cfg(mkbrr_wizard: ModuleType, tmp_path: Path) -> Any:
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    return mkbrr_wizard.AppCfg(
        runtime="auto",
        docker_support=True,
        chown=False,
        docker_user=None,
        mkbrr=mkbrr_wizard.MkbrrCfg(binary="mkbrr", image="ghcr.io/autobrr/mkbrr"),
        paths=mkbrr_wizard.PathsCfg(
            host_data_root=str(tmp_path / "data"),
            container_data_root="/data",
            host_output_dir=str(tmp_path / "torrents"),
            container_output_dir="/torrentfiles",
            host_config_dir=str(cfg_dir),
            container_config_dir="/root/.config/mkbrr",
        ),
        ownership=mkbrr_wizard.OwnershipCfg(uid=99, gid=100),
        batch=mkbrr_wizard.BatchCfg(mode="simple"),
        presets_yaml_host=str(cfg_dir / "presets.yaml"),
        presets_yaml_container="/root/.config/mkbrr/presets.yaml",
    )


def _build_main_batch_test_files(
    tmp_path: Path,
    *,
    runtime: str,
    docker_support: bool,
    batch_mode: str = "simple",
) -> tuple[Path, Path, Path, Path, Path, Path, Path]:
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    presets_yaml = cfg_dir / "presets.yaml"
    presets_yaml.write_text(
        "presets:\n"
        "  btn:\n"
        "    trackers:\n"
        "      - https://example.com/announce\n"
        "    source: BTN\n"
        "    private: true\n"
        "    entropy: true\n"
        "    comment: Made via mkbrr\n"
    )

    host_data = tmp_path / "data"
    host_data.mkdir()
    content = host_data / "movie.mkv"
    content.write_text("x")

    output_dir = tmp_path / "torrents"
    output_dir.mkdir()
    output = output_dir / "movie.torrent"

    batch_block = ""
    if batch_mode != "simple":
        batch_block = f"batch:\n  mode: {batch_mode}\n"

    config_yaml = tmp_path / "config.yaml"
    config_yaml.write_text(
        f"""
runtime: {runtime}
docker_support: {'true' if docker_support else 'false'}
chown: true
{batch_block}mkbrr:
    binary: mkbrr
paths:
    host_data_root: {host_data}
    container_data_root: /data
    host_output_dir: {output_dir}
    container_output_dir: /torrentfiles
    host_config_dir: {cfg_dir}
    container_config_dir: /root/.config/mkbrr
presets_yaml: {presets_yaml}
"""
    )

    return config_yaml, cfg_dir, host_data, output_dir, presets_yaml, content, output


def test_validate_batch_payload_success(mkbrr_wizard: ModuleType) -> None:
    schema = mkbrr_wizard.load_batch_schema()
    payload = {
        "version": 1,
        "jobs": [{"output": "/torrentfiles/movie1.torrent", "path": "/data/movie1.mkv"}],
    }

    errors = mkbrr_wizard.validate_batch_payload(payload, schema)
    assert errors == []


@pytest.mark.parametrize("piece_length", [16, 27])
def test_validate_batch_payload_accepts_piece_length_boundaries(
    mkbrr_wizard: ModuleType, piece_length: int
) -> None:
    schema = mkbrr_wizard.load_batch_schema()
    payload = {
        "version": 1,
        "jobs": [
            {
                "output": "/torrentfiles/movie1.torrent",
                "path": "/data/movie1.mkv",
                "piece_length": piece_length,
            }
        ],
    }

    assert mkbrr_wizard.validate_batch_payload(payload, schema) == []


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(
            {"jobs": [{"output": "/torrentfiles/x.torrent", "path": "/data/x.mkv"}]},
            id="missing_version",
        ),
        pytest.param({"version": 1}, id="missing_jobs"),
        pytest.param(
            {"version": 1, "jobs": [{"path": "/data/x.mkv"}]},
            id="missing_output",
        ),
        pytest.param(
            {
                "version": 1,
                "jobs": [
                    {
                        "output": "/torrentfiles/x.torrent",
                        "path": "/data/x.mkv",
                        "piece_length": 30,
                    }
                ],
            },
            id="piece_length_out_of_range",
        ),
    ],
)
def test_validate_batch_payload_failures(mkbrr_wizard: ModuleType, payload: dict[str, Any]) -> None:
    schema = mkbrr_wizard.load_batch_schema()
    errors = mkbrr_wizard.validate_batch_payload(payload, schema)
    assert errors


def test_validate_batch_payload_rejects_duplicate_normalized_outputs(
    mkbrr_wizard: ModuleType,
) -> None:
    schema = mkbrr_wizard.load_batch_schema()
    payload = {
        "version": 1,
        "jobs": [
            {"output": "/torrentfiles/movie.torrent", "path": "/data/movie-a.mkv"},
            {"output": "/torrentfiles/./movie.torrent", "path": "/data/movie-b.mkv"},
        ],
    }

    errors = mkbrr_wizard.validate_batch_payload(payload, schema)

    assert errors == [
        "jobs.1.output: duplicates jobs.0.output after path resolution: "
        "/torrentfiles/movie.torrent"
    ]


def test_map_batch_job_paths_docker(mkbrr_wizard: ModuleType, tmp_path: Path) -> None:
    cfg = _sample_cfg(mkbrr_wizard, tmp_path)
    payload = {
        "version": 1,
        "jobs": [
            {
                "path": f"{cfg.paths.host_data_root}/movies/movie1.mkv",
                "output": f"{cfg.paths.host_output_dir}/movie1.torrent",
            }
        ],
    }

    mapped = mkbrr_wizard.map_batch_job_paths(cfg, "docker", payload)
    job = mapped["jobs"][0]
    assert job["path"] == "/data/movies/movie1.mkv"
    assert job["output"] == "/torrentfiles/movie1.torrent"


def test_map_batch_job_output_uses_content_fallback(
    mkbrr_wizard: ModuleType, tmp_path: Path
) -> None:
    cfg = _sample_cfg(mkbrr_wizard, tmp_path)
    payload = {
        "version": 1,
        "jobs": [
            {
                "path": f"{cfg.paths.host_data_root}/movies/movie1.mkv",
                "output": f"{cfg.paths.host_data_root}/custom/movie1.torrent",
            }
        ],
    }

    mapped = mkbrr_wizard.map_batch_job_paths(cfg, "docker", payload)
    job = mapped["jobs"][0]
    assert job["output"] == "/data/custom/movie1.torrent"


@pytest.mark.parametrize(
    ("field", "value", "configured_root"),
    [
        pytest.param("path", "/srv/media/movie.mkv", "paths.host_data_root", id="content"),
        pytest.param(
            "output",
            "/srv/torrents/movie.torrent",
            "paths.host_output_dir",
            id="output",
        ),
    ],
)
def test_map_batch_job_paths_rejects_paths_outside_docker_mounts(
    mkbrr_wizard: ModuleType,
    tmp_path: Path,
    field: str,
    value: str,
    configured_root: str,
) -> None:
    cfg = _sample_cfg(mkbrr_wizard, tmp_path)
    job = {
        "path": f"{cfg.paths.host_data_root}/movies/movie.mkv",
        "output": f"{cfg.paths.host_output_dir}/movie.torrent",
    }
    job[field] = value

    with pytest.raises(ValueError, match=configured_root):
        mkbrr_wizard.map_batch_job_paths(cfg, "docker", {"version": 1, "jobs": [job]})


def test_build_batch_job_create_command_native_and_docker(
    mkbrr_wizard: ModuleType, tmp_path: Path
) -> None:
    cfg = _sample_cfg(mkbrr_wizard, tmp_path)
    native_content = tmp_path / "content.mkv"
    native_content.write_text("x")
    native_out = tmp_path / "out.torrent"
    native_job = {
        "path": str(native_content),
        "output": str(native_out),
        "trackers": ["https://tracker.example/announce"],
        "private": True,
        "comment": "note",
        "source": "BTN",
        "no_date": False,
        "entropy": True,
        "exclude_patterns": ["*.nfo"],
        "include_patterns": ["*.mkv"],
    }

    native_spec = mkbrr_wizard.build_batch_job_create_command(
        cfg,
        "native",
        "btn",
        native_job,
    )
    native_cmd = native_spec.argv
    assert native_cmd[:2] == ("mkbrr", "create")
    assert "-b" not in native_cmd
    assert "-P" in native_cmd
    assert "--output" in native_cmd
    assert "--tracker" in native_cmd
    assert "--private=true" in native_cmd
    assert "--no-date=false" in native_cmd
    assert "--entropy=true" in native_cmd
    assert native_spec.cwd == cfg.paths.host_output_dir

    docker_content = tmp_path / "docker-content.mkv"
    docker_content.write_text("x")
    docker_out = tmp_path / "docker-out.torrent"

    docker_spec = mkbrr_wizard.build_batch_job_create_command(
        cfg,
        "docker",
        "btn",
        {
            "path": str(docker_content),
            "output": str(docker_out),
        },
    )
    docker_cmd = docker_spec.argv
    assert docker_cmd[0] == "docker"
    assert "-b" not in docker_cmd
    assert "-P" in docker_cmd
    assert "--output" in docker_cmd
    assert docker_spec.cwd is None


def test_batch_job_from_mapping_normalizes_typed_fields(mkbrr_wizard: ModuleType) -> None:
    job = mkbrr_wizard.BatchJob.from_mapping(
        {
            "path": " /data/show ",
            "output": " /torrentfiles/show.torrent ",
            "trackers": [" https://tracker.example/announce ", ""],
            "webseeds": ["https://seed.example/file"],
            "private": False,
            "piece_length": 20,
            "include_patterns": [" *.mkv "],
        }
    )

    assert job.path == "/data/show"
    assert job.output == "/torrentfiles/show.torrent"
    assert job.trackers == ("https://tracker.example/announce",)
    assert job.webseeds == ("https://seed.example/file",)
    assert job.private is False
    assert job.piece_length == 20
    assert job.include_patterns == ("*.mkv",)


@pytest.mark.parametrize(
    ("field_name", "field_value", "match"),
    [
        pytest.param("piece_length", 15, "between 16 and 27", id="piece_length_below_range"),
        pytest.param("piece_length", 28, "between 16 and 27", id="piece_length_above_range"),
        pytest.param("piece_length", True, "must be an integer", id="piece_length_boolean"),
        pytest.param(
            "trackers", ["https://tracker.example", 1], "list of strings", id="tracker_item"
        ),
        pytest.param("comment", 1, "must be a string", id="comment_type"),
    ],
)
def test_batch_job_from_mapping_rejects_invalid_fields(
    mkbrr_wizard: ModuleType,
    field_name: str,
    field_value: object,
    match: str,
) -> None:
    raw_job: dict[str, object] = {
        "path": "/data/show",
        "output": "/torrentfiles/show.torrent",
        field_name: field_value,
    }

    with pytest.raises(ValueError, match=match):
        mkbrr_wizard.BatchJob.from_mapping(raw_job)


def test_job_result_exposes_status_and_transport_tuple(mkbrr_wizard: ModuleType) -> None:
    result = mkbrr_wizard.JobResult(
        index=2,
        content_path="/data/show",
        output_path="/torrentfiles/show.torrent",
        exit_code=0,
    )

    assert result.succeeded is True
    assert result.as_tuple() == (
        2,
        "/data/show",
        "/torrentfiles/show.torrent",
        0,
    )


def test_build_batch_job_create_command_accepts_batch_job(
    mkbrr_wizard: ModuleType, tmp_path: Path
) -> None:
    cfg = _sample_cfg(mkbrr_wizard, tmp_path)
    job = mkbrr_wizard.BatchJob.from_mapping(
        {
            "path": str(tmp_path / "content.mkv"),
            "output": str(tmp_path / "out.torrent"),
            "private": False,
        }
    )

    spec = mkbrr_wizard.build_batch_job_create_command(cfg, "native", "btn", job)

    assert "--private=false" in spec.argv


@pytest.mark.parametrize("private", [True, False])
def test_build_batch_job_create_command_emits_explicit_private_value(
    mkbrr_wizard: ModuleType, tmp_path: Path, private: bool
) -> None:
    cfg = _sample_cfg(mkbrr_wizard, tmp_path)
    spec = mkbrr_wizard.build_batch_job_create_command(
        cfg,
        "native",
        "btn",
        {
            "path": str(tmp_path / "content.mkv"),
            "output": str(tmp_path / "out.torrent"),
            "private": private,
        },
    )

    assert f"--private={str(private).lower()}" in spec.argv


@pytest.mark.parametrize("value", [None, True, False])
def test_build_batch_job_create_command_emits_optional_boolean_overrides(
    mkbrr_wizard: ModuleType, tmp_path: Path, value: bool | None
) -> None:
    cfg = _sample_cfg(mkbrr_wizard, tmp_path)
    fields = {
        "no_date": "--no-date",
        "entropy": "--entropy",
        "skip_prefix": "--skip-prefix",
        "fail_on_season_warning": "--fail-on-season-warning",
    }
    job: dict[str, Any] = {
        "path": str(tmp_path / "content.mkv"),
        "output": str(tmp_path / "out.torrent"),
        **dict.fromkeys(fields, value),
    }

    spec = mkbrr_wizard.build_batch_job_create_command(cfg, "native", "btn", job)

    for flag in fields.values():
        expected = None if value is None else f"{flag}={str(value).lower()}"
        assert expected in spec.argv if expected else flag not in spec.argv


@pytest.mark.parametrize(
    ("piece_length", "expected"),
    [
        pytest.param(15, None, id="below_minimum"),
        pytest.param(16, 16, id="minimum"),
        pytest.param(27, 27, id="maximum"),
        pytest.param(28, None, id="above_maximum"),
    ],
)
def test_collect_job_optional_settings_uses_mkbrr_piece_length_range(
    mkbrr_wizard: ModuleType,
    monkeypatch: Any,
    piece_length: int,
    expected: int | None,
) -> None:
    monkeypatch.setattr(
        mkbrr_wizard.Prompt,
        "ask",
        _Seq(["", "skip", str(piece_length), "", "", "skip", "skip", "", "", ""]),
    )

    result = mkbrr_wizard._collect_job_optional_settings(None, 1)

    assert result.get("piece_length") == expected


def test_build_batch_job_create_command_rejects_empty_content_path(
    mkbrr_wizard: ModuleType, tmp_path: Path
) -> None:
    cfg = _sample_cfg(mkbrr_wizard, tmp_path)

    with pytest.raises(ValueError, match="Batch job content path cannot be empty"):
        mkbrr_wizard.build_batch_job_create_command(
            cfg,
            "native",
            "btn",
            {
                "path": "   ",
                "output": str(tmp_path / "out.torrent"),
            },
        )


def test_default_batch_output_path_uses_host_output_dir(
    mkbrr_wizard: ModuleType, tmp_path: Path
) -> None:
    cfg = _sample_cfg(mkbrr_wizard, tmp_path)
    out_file = mkbrr_wizard._default_batch_output_path(cfg, "/data/movies/movie1.mkv")
    out_dir = mkbrr_wizard._default_batch_output_path(cfg, "/data/movies/Series.S01/")

    assert out_file == str(Path(cfg.paths.host_output_dir) / "movie1.torrent")
    assert out_dir == str(Path(cfg.paths.host_output_dir) / "Series.S01.torrent")


def test_collect_batch_jobs_interactive_simple_only_required(
    mkbrr_wizard: ModuleType, tmp_path: Path, monkeypatch: Any
) -> None:
    cfg = _sample_cfg(mkbrr_wizard, tmp_path)
    cfg = mkbrr_wizard.AppCfg(
        runtime=cfg.runtime,
        docker_support=cfg.docker_support,
        chown=cfg.chown,
        docker_user=cfg.docker_user,
        mkbrr=cfg.mkbrr,
        paths=cfg.paths,
        ownership=cfg.ownership,
        batch=mkbrr_wizard.BatchCfg(mode="simple"),
        presets_yaml_host=cfg.presets_yaml_host,
        presets_yaml_container=cfg.presets_yaml_container,
    )

    monkeypatch.setattr(mkbrr_wizard, "_has_prompt_toolkit", False)
    monkeypatch.setattr(
        mkbrr_wizard.Prompt,
        "ask",
        _Seq(
            [
                "2",
                "/data/movies/movie1.mkv",
                "/tmp/movie1.torrent",
                "/data/movies/movie2.mkv",
                "/tmp/movie2.torrent",
            ]
        ),
    )

    payload = mkbrr_wizard.collect_batch_jobs_interactive(cfg)
    assert payload["version"] == 1
    assert payload["jobs"] == [
        {"path": "/data/movies/movie1.mkv", "output": "/tmp/movie1.torrent"},
        {"path": "/data/movies/movie2.mkv", "output": "/tmp/movie2.torrent"},
    ]


def test_collect_batch_jobs_interactive_advanced_includes_optional(
    mkbrr_wizard: ModuleType, tmp_path: Path, monkeypatch: Any
) -> None:
    cfg = _sample_cfg(mkbrr_wizard, tmp_path)
    cfg = mkbrr_wizard.AppCfg(
        runtime=cfg.runtime,
        docker_support=cfg.docker_support,
        chown=cfg.chown,
        docker_user=cfg.docker_user,
        mkbrr=cfg.mkbrr,
        paths=cfg.paths,
        ownership=cfg.ownership,
        batch=mkbrr_wizard.BatchCfg(mode="advanced"),
        presets_yaml_host=cfg.presets_yaml_host,
        presets_yaml_container=cfg.presets_yaml_container,
    )

    monkeypatch.setattr(mkbrr_wizard, "_has_prompt_toolkit", False)
    monkeypatch.setattr(
        mkbrr_wizard.Prompt,
        "ask",
        _Seq(
            [
                "1",
                "/data/movies/movie1.mkv",
                "/tmp/movie1.torrent",
                "https://tracker.example/announce",
                "y",
                "18",
                "comment",
                "source",
                "y",
                "n",
                "https://seed.example/file",
                "*.nfo",
                "*.mkv",
            ]
        ),
    )

    payload = mkbrr_wizard.collect_batch_jobs_interactive(cfg)
    assert payload["version"] == 1
    assert payload["jobs"][0] == {
        "path": "/data/movies/movie1.mkv",
        "output": "/tmp/movie1.torrent",
        "trackers": ["https://tracker.example/announce"],
        "private": True,
        "piece_length": 18,
        "comment": "comment",
        "source": "source",
        "entropy": True,
        "no_date": False,
        "webseeds": ["https://seed.example/file"],
        "exclude_patterns": ["*.nfo"],
        "include_patterns": ["*.mkv"],
    }


def test_handle_batch_executes_job_notifies_and_fixes_ownership(
    tmp_path: Path, mkbrr_wizard: ModuleType, monkeypatch: Any
) -> None:
    config_yaml, _, _, _, _, content, output = _build_main_batch_test_files(
        tmp_path, runtime="native", docker_support=False
    )
    cfg = mkbrr_wizard.load_config(config_yaml)
    executed: list[Any] = []
    notifications: list[Any] = []
    owned_paths: list[str] = []
    monkeypatch.setattr(mkbrr_wizard, "pick_preset", lambda cfg: "btn")
    monkeypatch.setattr(
        mkbrr_wizard,
        "collect_batch_jobs_interactive",
        lambda cfg: {
            "version": 1,
            "jobs": [{"path": str(content), "output": str(output)}],
        },
    )
    monkeypatch.setattr(mkbrr_wizard, "confirm_cmd", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        mkbrr_wizard,
        "maybe_fix_torrent_permissions",
        lambda cfg, paths: owned_paths.extend(paths),
    )

    def run(command: Any, *, timeout: int | None = None) -> Any:
        executed.append((command, timeout))
        return mkbrr_wizard.ExecutionResult(returncode=0, elapsed=3.5)

    executor = SimpleNamespace(run=run)
    notifier = SimpleNamespace(notify=notifications.append)

    assert mkbrr_wizard.handle_batch(cfg, "native", executor, notifier) is True
    assert len(executed) == 1
    assert executed[0][0].argv[:2] == ("mkbrr", "create")
    assert executed[0][1] == cfg.batch.job_timeout_seconds
    assert owned_paths == [str(output)]
    assert notifications[0].event_type == "batch"
    assert notifications[0].details["elapsed"] >= 0


def test_main_batch_success_native(tmp_path, mkbrr_wizard: ModuleType, monkeypatch: Any) -> None:
    config_yaml, _, _, _, _, content, output = _build_main_batch_test_files(
        tmp_path, runtime="native", docker_support=False
    )

    monkeypatch.setattr(mkbrr_wizard, "parse_args", lambda: _mk_args(str(config_yaml)))
    monkeypatch.setattr(mkbrr_wizard, "pick_runtime", lambda cfg, forced: "native")
    monkeypatch.setattr(mkbrr_wizard, "_has_prompt_toolkit", False)

    seq = _Seq(
        [
            "4",  # choose batch
            "1",  # preset
            "1",  # num jobs
            str(content),  # job path
            str(output),  # job output
        ]
    )
    monkeypatch.setattr(mkbrr_wizard.Prompt, "ask", seq)
    monkeypatch.setattr(mkbrr_wizard.Confirm, "ask", _Seq([True, False]))  # proceed, do another

    calls: list[tuple[list[str], str | None]] = []

    class Dummy:
        def __init__(self, returncode=0):
            self.returncode = returncode

    def fake_run(cmd, *a, **k):
        calls.append((cmd, k.get("cwd")))
        return Dummy(0)

    monkeypatch.setattr(mkbrr_wizard.subprocess, "run", fake_run)
    chown_paths: list[str] = []
    monkeypatch.setattr(
        mkbrr_wizard,
        "maybe_fix_torrent_permissions",
        lambda cfg, paths: chown_paths.extend(paths),
    )

    mkbrr_wizard.main()

    assert len(calls) == 1
    cmd, _ = calls[0]
    assert cmd[0] == "mkbrr"
    assert cmd[1] == "create"
    assert "-b" not in cmd
    assert "-P" in cmd
    assert "--output" in cmd
    assert chown_paths == [str(output)]


def test_main_batch_success_docker(tmp_path, mkbrr_wizard: ModuleType, monkeypatch: Any) -> None:
    config_yaml, _, _, _, _, content, output = _build_main_batch_test_files(
        tmp_path, runtime="auto", docker_support=True
    )

    monkeypatch.setattr(mkbrr_wizard, "parse_args", lambda: _mk_args(str(config_yaml)))
    monkeypatch.setattr(mkbrr_wizard, "pick_runtime", lambda cfg, forced: "docker")
    monkeypatch.setattr(mkbrr_wizard, "_has_prompt_toolkit", False)

    seq = _Seq(
        [
            "4",  # choose batch
            "1",  # preset
            "1",  # num jobs
            str(content),  # job path
            str(output),  # job output
        ]
    )
    monkeypatch.setattr(mkbrr_wizard.Prompt, "ask", seq)
    monkeypatch.setattr(mkbrr_wizard.Confirm, "ask", _Seq([True, False]))  # proceed, do another

    calls: list[list[str]] = []

    class Dummy:
        def __init__(self, returncode=0):
            self.returncode = returncode

    def _run_and_record(
        calls_ref: list[list[str]], cmd: list[str], dummy_type: type[Dummy]
    ) -> Dummy:
        calls_ref.append(cmd)
        return dummy_type(0)

    monkeypatch.setattr(
        mkbrr_wizard.subprocess,
        "run",
        lambda cmd, *a, **k: _run_and_record(calls, cmd, Dummy),
    )
    chown_paths: list[str] = []
    monkeypatch.setattr(
        mkbrr_wizard,
        "maybe_fix_torrent_permissions",
        lambda cfg, paths: chown_paths.extend(paths),
    )

    mkbrr_wizard.main()

    assert len(calls) == 1
    cmd = calls[0]
    assert cmd[0] == "docker"
    assert "-b" not in cmd
    assert "-P" in cmd
    assert "--output" in cmd
    assert chown_paths == [str(output)]


def test_main_batch_success_advanced_mode_prompts_optional(
    tmp_path, mkbrr_wizard: ModuleType, monkeypatch: Any
) -> None:
    config_yaml, _, _, _, _, content, output = _build_main_batch_test_files(
        tmp_path, runtime="native", docker_support=False, batch_mode="advanced"
    )

    monkeypatch.setattr(mkbrr_wizard, "parse_args", lambda: _mk_args(str(config_yaml)))
    monkeypatch.setattr(mkbrr_wizard, "pick_runtime", lambda cfg, forced: "native")
    monkeypatch.setattr(mkbrr_wizard, "_has_prompt_toolkit", False)

    monkeypatch.setattr(
        mkbrr_wizard.Prompt,
        "ask",
        _Seq(
            [
                "4",  # choose batch
                "1",  # preset
                "1",  # num jobs
                str(content),  # job path
                str(output),  # job output
                "",  # trackers
                "skip",  # private
                "",  # piece_length
                "",  # comment
                "",  # source
                "skip",  # entropy
                "skip",  # no_date
                "",  # webseeds
                "",  # exclude_patterns
                "",  # include_patterns
            ]
        ),
    )
    monkeypatch.setattr(mkbrr_wizard.Confirm, "ask", _Seq([True, False]))  # proceed, do another

    class Dummy:
        def __init__(self, returncode=0):
            self.returncode = returncode

    calls: list[list[str]] = []

    def _run_and_record(
        calls_ref: list[list[str]], cmd: list[str], dummy_type: type[Dummy]
    ) -> Dummy:
        calls_ref.append(cmd)
        return dummy_type(0)

    monkeypatch.setattr(
        mkbrr_wizard.subprocess,
        "run",
        lambda cmd, *a, **k: _run_and_record(calls, cmd, Dummy),
    )
    monkeypatch.setattr(mkbrr_wizard, "maybe_fix_torrent_permissions", lambda cfg, paths: None)

    mkbrr_wizard.main()
    assert len(calls) == 1


def test_main_batch_validation_failure_skips_execution(
    tmp_path, mkbrr_wizard: ModuleType, monkeypatch: Any
) -> None:
    config_yaml, _, _, _, _, _, _ = _build_main_batch_test_files(
        tmp_path, runtime="native", docker_support=False
    )

    monkeypatch.setattr(mkbrr_wizard, "parse_args", lambda: _mk_args(str(config_yaml)))
    monkeypatch.setattr(mkbrr_wizard, "pick_runtime", lambda cfg, forced: "native")
    monkeypatch.setattr(mkbrr_wizard, "_has_prompt_toolkit", False)
    monkeypatch.setattr(mkbrr_wizard.Prompt, "ask", _Seq(["4", "1", "q"]))
    out_a = tmp_path / "a.torrent"
    in_a = tmp_path / "a.mkv"
    monkeypatch.setattr(
        mkbrr_wizard,
        "collect_batch_jobs_interactive",
        lambda cfg: {
            "version": 1,
            "jobs": [{"output": str(out_a), "path": str(in_a), "piece_length": 30}],
        },
    )

    called = {"count": 0}

    def fake_run(*a, **k):
        called["count"] += 1
        raise AssertionError("subprocess.run should not be called on validation failure")

    monkeypatch.setattr(mkbrr_wizard.subprocess, "run", fake_run)

    with pytest.raises(SystemExit):
        mkbrr_wizard.main()
    assert called["count"] == 0


def test_main_batch_rejects_non_mapping_job_after_schema_validation(
    tmp_path: Path, mkbrr_wizard: ModuleType, monkeypatch: Any
) -> None:
    config_yaml, _, _, _, _, content, output = _build_main_batch_test_files(
        tmp_path, runtime="native", docker_support=False
    )
    cfg = mkbrr_wizard.load_config(config_yaml)
    monkeypatch.setattr(mkbrr_wizard, "pick_preset", lambda cfg: "btn")
    monkeypatch.setattr(
        mkbrr_wizard,
        "collect_batch_jobs_interactive",
        lambda cfg: {
            "version": 1,
            "jobs": [
                {"output": str(output), "path": str(content)},
                "not-a-mapping",
            ],
        },
    )
    monkeypatch.setattr(mkbrr_wizard, "validate_batch_payload", lambda payload, schema: [])

    messages: list[str] = []
    monkeypatch.setattr(
        mkbrr_wizard.console,
        "print",
        lambda *args, **kwargs: messages.extend(str(arg) for arg in args),
    )

    def fail_run(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("subprocess.run should not be called for non-mapping jobs")

    executor = SimpleNamespace(run=fail_run)
    notifier = SimpleNamespace(notify=lambda event: None)

    assert mkbrr_wizard.handle_batch(cfg, "native", executor, notifier) is False
    assert any("Batch job 2 must be a mapping" in message for message in messages)


def test_main_batch_duplicate_outputs_skip_all_execution(
    tmp_path, mkbrr_wizard: ModuleType, monkeypatch: Any
) -> None:
    config_yaml, _, _, _, _, _, _ = _build_main_batch_test_files(
        tmp_path, runtime="native", docker_support=False
    )

    monkeypatch.setattr(mkbrr_wizard, "parse_args", lambda: _mk_args(str(config_yaml)))
    monkeypatch.setattr(mkbrr_wizard, "pick_runtime", lambda cfg, forced: "native")
    monkeypatch.setattr(mkbrr_wizard, "_has_prompt_toolkit", False)
    monkeypatch.setattr(mkbrr_wizard.Prompt, "ask", _Seq(["4", "1", "q"]))
    duplicate_output = tmp_path / "same.torrent"
    monkeypatch.setattr(
        mkbrr_wizard,
        "collect_batch_jobs_interactive",
        lambda cfg: {
            "version": 1,
            "jobs": [
                {"output": str(duplicate_output), "path": str(tmp_path / "a.mkv")},
                {"output": str(duplicate_output), "path": str(tmp_path / "b.mkv")},
            ],
        },
    )

    def fail_run(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("subprocess.run should not be called for duplicate outputs")

    monkeypatch.setattr(mkbrr_wizard.subprocess, "run", fail_run)

    with pytest.raises(SystemExit):
        mkbrr_wizard.main()


def test_main_batch_nonzero_exit_skips_chown(
    tmp_path, mkbrr_wizard: ModuleType, monkeypatch: Any
) -> None:
    config_yaml, _, _, _, _, _, _ = _build_main_batch_test_files(
        tmp_path, runtime="native", docker_support=False
    )

    monkeypatch.setattr(mkbrr_wizard, "parse_args", lambda: _mk_args(str(config_yaml)))
    monkeypatch.setattr(mkbrr_wizard, "pick_runtime", lambda cfg, forced: "native")
    monkeypatch.setattr(mkbrr_wizard, "_has_prompt_toolkit", False)
    monkeypatch.setattr(mkbrr_wizard.Prompt, "ask", _Seq(["4", "1", "q"]))
    out_a = tmp_path / "a.torrent"
    in_a = tmp_path / "a.mkv"
    monkeypatch.setattr(
        mkbrr_wizard,
        "collect_batch_jobs_interactive",
        lambda cfg: {
            "version": 1,
            "jobs": [{"output": str(out_a), "path": str(in_a)}],
        },
    )
    monkeypatch.setattr(mkbrr_wizard.Confirm, "ask", _Seq([True, False]))  # proceed, do another

    class Dummy:
        def __init__(self, returncode):
            self.returncode = returncode

    monkeypatch.setattr(mkbrr_wizard.subprocess, "run", lambda *a, **k: Dummy(2))

    chown_called = {"count": 0}
    monkeypatch.setattr(
        mkbrr_wizard,
        "maybe_fix_torrent_permissions",
        lambda cfg, paths: chown_called.__setitem__("count", chown_called["count"] + 1),
    )

    mkbrr_wizard.main()
    assert chown_called["count"] == 0


def test_main_batch_continue_on_error_and_chown_once(
    tmp_path, mkbrr_wizard: ModuleType, monkeypatch: Any
) -> None:
    config_yaml, _, _, _, _, _, _ = _build_main_batch_test_files(
        tmp_path, runtime="native", docker_support=False
    )

    monkeypatch.setattr(mkbrr_wizard, "parse_args", lambda: _mk_args(str(config_yaml)))
    monkeypatch.setattr(mkbrr_wizard, "pick_runtime", lambda cfg, forced: "native")
    monkeypatch.setattr(mkbrr_wizard, "_has_prompt_toolkit", False)
    monkeypatch.setattr(mkbrr_wizard.Prompt, "ask", _Seq(["4", "1", "q"]))
    out_a = tmp_path / "a.torrent"
    in_a = tmp_path / "a.mkv"
    out_b = tmp_path / "b.torrent"
    in_b = tmp_path / "b.mkv"
    monkeypatch.setattr(
        mkbrr_wizard,
        "collect_batch_jobs_interactive",
        lambda cfg: {
            "version": 1,
            "jobs": [
                {"output": str(out_a), "path": str(in_a)},
                {"output": str(out_b), "path": str(in_b)},
            ],
        },
    )
    monkeypatch.setattr(mkbrr_wizard.Confirm, "ask", _Seq([True, False]))  # proceed, do another

    class Dummy:
        def __init__(self, returncode):
            self.returncode = returncode

    run_codes = [2, 0]
    calls = {"count": 0}

    def fake_run(*args, **kwargs):
        if not run_codes:
            raise AssertionError("Unexpected subprocess call")
        calls["count"] += 1
        return Dummy(run_codes.pop(0))

    monkeypatch.setattr(mkbrr_wizard.subprocess, "run", fake_run)

    chown_paths: list[str] = []
    monkeypatch.setattr(
        mkbrr_wizard,
        "maybe_fix_torrent_permissions",
        lambda cfg, paths: chown_paths.extend(paths),
    )

    mkbrr_wizard.main()
    assert calls["count"] == 2
    assert not run_codes
    assert chown_paths == [str(out_b)]


def test_main_batch_timeout_marks_failed_and_continues(
    tmp_path, mkbrr_wizard: ModuleType, monkeypatch: Any
) -> None:
    config_yaml, _, _, _, _, _, _ = _build_main_batch_test_files(
        tmp_path, runtime="native", docker_support=False
    )

    monkeypatch.setattr(mkbrr_wizard, "parse_args", lambda: _mk_args(str(config_yaml)))
    monkeypatch.setattr(mkbrr_wizard, "pick_runtime", lambda cfg, forced: "native")
    monkeypatch.setattr(mkbrr_wizard, "_has_prompt_toolkit", False)
    monkeypatch.setattr(mkbrr_wizard.Prompt, "ask", _Seq(["4", "1", "q"]))

    out_a = tmp_path / "a.torrent"
    in_a = tmp_path / "a.mkv"
    out_b = tmp_path / "b.torrent"
    in_b = tmp_path / "b.mkv"
    monkeypatch.setattr(
        mkbrr_wizard,
        "collect_batch_jobs_interactive",
        lambda cfg: {
            "version": 1,
            "jobs": [
                {"output": str(out_a), "path": str(in_a)},
                {"output": str(out_b), "path": str(in_b)},
            ],
        },
    )
    monkeypatch.setattr(mkbrr_wizard.Confirm, "ask", _Seq([True, False]))

    timeout_exc = mkbrr_wizard.subprocess.TimeoutExpired(cmd=["mkbrr"], timeout=1)
    calls = {"count": 0}

    class Dummy:
        def __init__(self, returncode):
            self.returncode = returncode

    def fake_run(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise timeout_exc
        return Dummy(0)

    monkeypatch.setattr(mkbrr_wizard.subprocess, "run", fake_run)
    monkeypatch.setattr(
        mkbrr_wizard,
        "load_config",
        lambda path: mkbrr_wizard.AppCfg(
            runtime="native",
            docker_support=False,
            chown=True,
            docker_user=None,
            mkbrr=mkbrr_wizard.MkbrrCfg(binary="mkbrr", image="ghcr.io/autobrr/mkbrr"),
            paths=mkbrr_wizard.PathsCfg(
                host_data_root=str(tmp_path / "data"),
                container_data_root="/data",
                host_output_dir=str(tmp_path / "torrents"),
                container_output_dir="/torrentfiles",
                host_config_dir=str(tmp_path / "cfg"),
                container_config_dir="/root/.config/mkbrr",
            ),
            ownership=mkbrr_wizard.OwnershipCfg(uid=99, gid=100),
            batch=mkbrr_wizard.BatchCfg(mode="simple", job_timeout_seconds=1),
            presets_yaml_host=str(tmp_path / "cfg" / "presets.yaml"),
            presets_yaml_container="/root/.config/mkbrr/presets.yaml",
        ),
    )

    chown_called = {"count": 0}
    monkeypatch.setattr(
        mkbrr_wizard,
        "maybe_fix_torrent_permissions",
        lambda cfg, paths: chown_called.__setitem__("count", chown_called["count"] + 1),
    )

    mkbrr_wizard.main()

    assert calls["count"] == 2
    assert chown_called["count"] == 1


def test_main_batch_cancel_skips_execution(
    tmp_path, mkbrr_wizard: ModuleType, monkeypatch: Any
) -> None:
    config_yaml, _, _, _, _, _, _ = _build_main_batch_test_files(
        tmp_path, runtime="native", docker_support=False
    )

    monkeypatch.setattr(mkbrr_wizard, "parse_args", lambda: _mk_args(str(config_yaml)))
    monkeypatch.setattr(mkbrr_wizard, "pick_runtime", lambda cfg, forced: "native")
    monkeypatch.setattr(mkbrr_wizard, "_has_prompt_toolkit", False)
    monkeypatch.setattr(mkbrr_wizard.Prompt, "ask", _Seq(["4", "1", "q"]))
    out_a = tmp_path / "a.torrent"
    in_a = tmp_path / "a.mkv"
    monkeypatch.setattr(
        mkbrr_wizard,
        "collect_batch_jobs_interactive",
        lambda cfg: {
            "version": 1,
            "jobs": [{"output": str(out_a), "path": str(in_a)}],
        },
    )
    monkeypatch.setattr(mkbrr_wizard.Confirm, "ask", _Seq([False]))  # cancel run

    def raise_on_run(*args, **kwargs):
        raise AssertionError("subprocess.run should not be called")

    monkeypatch.setattr(mkbrr_wizard.subprocess, "run", raise_on_run)

    with pytest.raises(SystemExit):
        mkbrr_wizard.main()
