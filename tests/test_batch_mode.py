"""Tests for batch mode helpers and main-flow integration."""

from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest  # type: ignore[import-untyped]


def _mk_args(config_path: str) -> SimpleNamespace:
    return SimpleNamespace(config=config_path, docker=False, native=False)


class _Seq:
    def __init__(self, items):
        self._it = iter(items)

    def __call__(self, *args, **kwargs):
        return next(self._it)


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


def test_validate_batch_payload_success(mkbrr_wizard: ModuleType) -> None:
    schema = mkbrr_wizard.load_batch_schema()
    payload = {
        "version": 1,
        "jobs": [{"output": "/torrentfiles/movie1.torrent", "path": "/data/movie1.mkv"}],
    }

    errors = mkbrr_wizard.validate_batch_payload(payload, schema)
    assert errors == []


def test_validate_batch_payload_failures(mkbrr_wizard: ModuleType) -> None:
    schema = mkbrr_wizard.load_batch_schema()

    invalid_payloads = [
        {"jobs": [{"output": "/torrentfiles/x.torrent", "path": "/data/x.mkv"}]},  # missing version
        {"version": 1},  # missing jobs
        {"version": 1, "jobs": [{"path": "/data/x.mkv"}]},  # missing output
        {
            "version": 1,
            "jobs": [
                {"output": "/torrentfiles/x.torrent", "path": "/data/x.mkv", "piece_length": 30}
            ],
        },  # out of range
    ]

    for payload in invalid_payloads:
        errors = mkbrr_wizard.validate_batch_payload(payload, schema)
        assert errors


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


def test_write_temp_batch_file_maps_to_container(mkbrr_wizard: ModuleType, tmp_path: Path) -> None:
    cfg = _sample_cfg(mkbrr_wizard, tmp_path)
    payload = {"version": 1, "jobs": [{"output": "/torrentfiles/a.torrent", "path": "/data/a.mkv"}]}

    host_file, container_file = mkbrr_wizard.write_temp_batch_file(cfg, payload)

    assert host_file.startswith(cfg.paths.host_config_dir)
    assert Path(host_file).exists()
    assert container_file is not None
    assert container_file.startswith(cfg.paths.container_config_dir)

    Path(host_file).unlink(missing_ok=True)


def test_build_batch_create_command_native_and_docker(
    mkbrr_wizard: ModuleType, tmp_path: Path
) -> None:
    cfg = _sample_cfg(mkbrr_wizard, tmp_path)

    native_cmd, native_cwd = mkbrr_wizard.build_batch_create_command(
        cfg,
        "native",
        "/tmp/batch.yaml",
        "btn",
    )
    assert native_cmd[:3] == ["mkbrr", "create", "-b"]
    assert "-P" in native_cmd
    assert "--preset-file" in native_cmd
    assert native_cwd == cfg.paths.host_output_dir

    docker_cmd, docker_cwd = mkbrr_wizard.build_batch_create_command(
        cfg,
        "docker",
        "/root/.config/mkbrr/batch.yaml",
        "btn",
    )
    assert docker_cmd[0] == "docker"
    assert "-b" in docker_cmd
    assert "-P" in docker_cmd
    assert docker_cwd is None


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
        "no_date": False,
        "webseeds": ["https://seed.example/file"],
        "exclude_patterns": ["*.nfo"],
        "include_patterns": ["*.mkv"],
    }


def test_main_batch_success_native(tmp_path, mkbrr_wizard: ModuleType, monkeypatch: Any) -> None:
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    presets_yaml = cfg_dir / "presets.yaml"
    presets_yaml.write_text("presets:\n  btn:\n    announce: https://example.com/announce\n")

    host_data = tmp_path / "data"
    host_data.mkdir()
    content = host_data / "movie.mkv"
    content.write_text("x")

    output_dir = tmp_path / "torrents"
    output_dir.mkdir()
    output = output_dir / "movie.torrent"

    config_yaml = tmp_path / "config.yaml"
    config_yaml.write_text(
        f"""
runtime: native
docker_support: false
chown: true
mkbrr:
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
    chown_called = {"count": 0}
    monkeypatch.setattr(
        mkbrr_wizard,
        "maybe_fix_torrent_permissions",
        lambda cfg: chown_called.__setitem__("count", chown_called["count"] + 1),
    )

    mkbrr_wizard.main()

    assert len(calls) == 1
    cmd, _ = calls[0]
    assert cmd[0] == "mkbrr"
    assert cmd[1] == "create"
    assert "-b" in cmd
    batch_file = cmd[cmd.index("-b") + 1]
    assert not Path(batch_file).exists()
    assert chown_called["count"] == 1


def test_main_batch_success_docker(tmp_path, mkbrr_wizard: ModuleType, monkeypatch: Any) -> None:
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    presets_yaml = cfg_dir / "presets.yaml"
    presets_yaml.write_text("presets:\n  btn:\n    announce: https://example.com/announce\n")

    host_data = tmp_path / "data"
    host_data.mkdir()
    content = host_data / "movie.mkv"
    content.write_text("x")

    output_dir = tmp_path / "torrents"
    output_dir.mkdir()
    output = output_dir / "movie.torrent"

    config_yaml = tmp_path / "config.yaml"
    config_yaml.write_text(
        f"""
runtime: auto
docker_support: true
chown: true
mkbrr:
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
    chown_called = {"count": 0}
    monkeypatch.setattr(
        mkbrr_wizard,
        "maybe_fix_torrent_permissions",
        lambda cfg: chown_called.__setitem__("count", chown_called["count"] + 1),
    )

    mkbrr_wizard.main()

    assert len(calls) == 1
    cmd = calls[0]
    assert cmd[0] == "docker"
    assert "-b" in cmd
    assert cmd[cmd.index("-b") + 1].startswith("/root/.config/mkbrr/mkbrr-batch-")
    assert not list(cfg_dir.glob("mkbrr-batch-*.yaml"))
    assert chown_called["count"] == 1


def test_main_batch_success_advanced_mode_prompts_optional(
    tmp_path, mkbrr_wizard: ModuleType, monkeypatch: Any
) -> None:
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    presets_yaml = cfg_dir / "presets.yaml"
    presets_yaml.write_text("presets:\n  btn:\n    announce: https://example.com/announce\n")

    host_data = tmp_path / "data"
    host_data.mkdir()
    content = host_data / "movie.mkv"
    content.write_text("x")

    output_dir = tmp_path / "torrents"
    output_dir.mkdir()
    output = output_dir / "movie.torrent"

    config_yaml = tmp_path / "config.yaml"
    config_yaml.write_text(
        f"""
runtime: native
docker_support: false
chown: true
batch:
  mode: advanced
mkbrr:
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
    monkeypatch.setattr(mkbrr_wizard, "maybe_fix_torrent_permissions", lambda cfg: None)

    mkbrr_wizard.main()
    assert len(calls) == 1


def test_main_batch_validation_failure_skips_execution(
    tmp_path, mkbrr_wizard: ModuleType, monkeypatch: Any
) -> None:
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    presets_yaml = cfg_dir / "presets.yaml"
    presets_yaml.write_text("presets:\n  btn:\n    announce: https://example.com/announce\n")

    config_yaml = tmp_path / "config.yaml"
    config_yaml.write_text(
        f"""
runtime: native
docker_support: false
chown: true
mkbrr:
  binary: mkbrr
paths:
  host_data_root: {tmp_path}/data
  container_data_root: /data
  host_output_dir: {tmp_path}/torrents
  container_output_dir: /torrentfiles
  host_config_dir: {cfg_dir}
  container_config_dir: /root/.config/mkbrr
presets_yaml: {presets_yaml}
"""
    )

    monkeypatch.setattr(mkbrr_wizard, "parse_args", lambda: _mk_args(str(config_yaml)))
    monkeypatch.setattr(mkbrr_wizard, "pick_runtime", lambda cfg, forced: "native")
    monkeypatch.setattr(mkbrr_wizard, "_has_prompt_toolkit", False)
    monkeypatch.setattr(mkbrr_wizard.Prompt, "ask", _Seq(["4", "1", "q"]))
    monkeypatch.setattr(
        mkbrr_wizard,
        "collect_batch_jobs_interactive",
        lambda cfg: {
            "version": 1,
            "jobs": [{"output": "/tmp/a.torrent", "path": "/tmp/a.mkv", "piece_length": 30}],
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


def test_main_batch_nonzero_exit_still_chowns(
    tmp_path, mkbrr_wizard: ModuleType, monkeypatch: Any
) -> None:
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    presets_yaml = cfg_dir / "presets.yaml"
    presets_yaml.write_text("presets:\n  btn:\n    announce: https://example.com/announce\n")

    config_yaml = tmp_path / "config.yaml"
    config_yaml.write_text(
        f"""
runtime: native
docker_support: false
chown: true
mkbrr:
  binary: mkbrr
paths:
  host_data_root: {tmp_path}/data
  container_data_root: /data
  host_output_dir: {tmp_path}/torrents
  container_output_dir: /torrentfiles
  host_config_dir: {cfg_dir}
  container_config_dir: /root/.config/mkbrr
presets_yaml: {presets_yaml}
"""
    )

    monkeypatch.setattr(mkbrr_wizard, "parse_args", lambda: _mk_args(str(config_yaml)))
    monkeypatch.setattr(mkbrr_wizard, "pick_runtime", lambda cfg, forced: "native")
    monkeypatch.setattr(mkbrr_wizard, "_has_prompt_toolkit", False)
    monkeypatch.setattr(mkbrr_wizard.Prompt, "ask", _Seq(["4", "1"]))
    monkeypatch.setattr(
        mkbrr_wizard,
        "collect_batch_jobs_interactive",
        lambda cfg: {"version": 1, "jobs": [{"output": "/tmp/a.torrent", "path": "/tmp/a.mkv"}]},
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
        lambda cfg: chown_called.__setitem__("count", chown_called["count"] + 1),
    )

    mkbrr_wizard.main()
    assert chown_called["count"] == 1


def test_main_batch_cancel_removes_temp_file(
    tmp_path, mkbrr_wizard: ModuleType, monkeypatch: Any
) -> None:
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    presets_yaml = cfg_dir / "presets.yaml"
    presets_yaml.write_text("presets:\n  btn:\n    announce: https://example.com/announce\n")

    config_yaml = tmp_path / "config.yaml"
    config_yaml.write_text(
        f"""
runtime: native
docker_support: false
chown: true
mkbrr:
  binary: mkbrr
paths:
  host_data_root: {tmp_path}/data
  container_data_root: /data
  host_output_dir: {tmp_path}/torrents
  container_output_dir: /torrentfiles
  host_config_dir: {cfg_dir}
  container_config_dir: /root/.config/mkbrr
presets_yaml: {presets_yaml}
"""
    )

    monkeypatch.setattr(mkbrr_wizard, "parse_args", lambda: _mk_args(str(config_yaml)))
    monkeypatch.setattr(mkbrr_wizard, "pick_runtime", lambda cfg, forced: "native")
    monkeypatch.setattr(mkbrr_wizard, "_has_prompt_toolkit", False)
    monkeypatch.setattr(mkbrr_wizard.Prompt, "ask", _Seq(["4", "1"]))
    monkeypatch.setattr(
        mkbrr_wizard,
        "collect_batch_jobs_interactive",
        lambda cfg: {"version": 1, "jobs": [{"output": "/tmp/a.torrent", "path": "/tmp/a.mkv"}]},
    )
    monkeypatch.setattr(mkbrr_wizard.Confirm, "ask", _Seq([False, False]))  # cancel run, do another
    monkeypatch.setattr(
        mkbrr_wizard.subprocess,
        "run",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("subprocess.run should not be called")
        ),
    )

    mkbrr_wizard.main()
    assert not list(cfg_dir.glob("mkbrr-batch-*.yaml"))
