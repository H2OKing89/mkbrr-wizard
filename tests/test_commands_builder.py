"""Tests for command builder functions and runtime selection."""

import subprocess
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest  # type: ignore[import-untyped]


def sample_cfg(mkbrr_wizard: ModuleType) -> Any:
    return mkbrr_wizard.AppCfg(
        runtime="auto",
        docker_support=True,
        chown=False,
        docker_user=None,
        mkbrr=mkbrr_wizard.MkbrrCfg(binary="mkbrr", image="ghcr.io/autobrr/mkbrr"),
        paths=mkbrr_wizard.PathsCfg(
            host_data_root="/mnt/user/data",
            container_data_root="/data",
            host_output_dir="/mnt/user/data/downloads/torrents/torrentfiles",
            container_output_dir="/torrentfiles",
            host_config_dir="/mnt/cache/appdata/mkbrr",
            container_config_dir="/root/.config/mkbrr",
        ),
        ownership=mkbrr_wizard.OwnershipCfg(uid=99, gid=100),
        batch=mkbrr_wizard.BatchCfg(mode="simple"),
        presets_yaml_host="/mnt/cache/appdata/mkbrr/presets.yaml",
        presets_yaml_container="/root/.config/mkbrr/presets.yaml",
    )


def test_build_create_command_docker(mkbrr_wizard: ModuleType) -> None:
    cfg = sample_cfg(mkbrr_wizard)
    spec = mkbrr_wizard.build_create_command(cfg, "docker", "/data/file.mkv", "btn")

    assert isinstance(spec, mkbrr_wizard.CommandSpec)
    assert spec.argv[0] == "docker"
    assert "create" in spec.argv
    assert "-P" in spec.argv
    assert cfg.presets_yaml_container in spec.argv
    assert spec.cwd is None


def test_build_create_command_native(mkbrr_wizard: ModuleType) -> None:
    cfg = sample_cfg(mkbrr_wizard)
    spec = mkbrr_wizard.build_create_command(cfg, "native", "/mnt/user/data/file.mkv", "btn")

    assert spec.argv[0] == cfg.mkbrr.binary
    assert "create" in spec.argv
    assert cfg.presets_yaml_host in spec.argv
    assert spec.cwd == cfg.paths.host_output_dir


@pytest.mark.parametrize(
    ("runtime", "expected_output_dir"),
    [
        pytest.param("native", "/mnt/user/data/downloads/torrents/torrentfiles", id="native"),
        pytest.param("docker", "/torrentfiles", id="docker"),
    ],
)
def test_build_create_command_overrides_preset_output_dir(
    mkbrr_wizard: ModuleType, runtime: str, expected_output_dir: str
) -> None:
    cfg = sample_cfg(mkbrr_wizard)
    content_path = "/data/file.mkv" if runtime == "docker" else "/mnt/user/data/file.mkv"

    spec = mkbrr_wizard.build_create_command(cfg, runtime, content_path, "btn")

    output_dir_index = spec.argv.index("--output-dir")
    assert spec.argv[output_dir_index + 1] == expected_output_dir


def test_command_spec_with_args_returns_new_value(mkbrr_wizard: ModuleType) -> None:
    spec = mkbrr_wizard.CommandSpec(argv=("mkbrr", "create"), cwd="working-directory")

    extended = spec.with_args("--workers", "4")

    assert spec.argv == ("mkbrr", "create")
    assert extended.argv == ("mkbrr", "create", "--workers", "4")
    assert extended.cwd == spec.cwd


@pytest.mark.parametrize(
    ("runtime", "backend_type"),
    [
        pytest.param("native", "NativeBackend", id="native"),
        pytest.param("docker", "DockerBackend", id="docker"),
    ],
)
def test_backend_for_runtime_selects_expected_backend(
    mkbrr_wizard: ModuleType, runtime: str, backend_type: str
) -> None:
    backend = mkbrr_wizard.backend_for_runtime(runtime)

    assert type(backend).__name__ == backend_type
    assert backend.runtime == runtime


def test_command_executor_runs_command_spec(mkbrr_wizard: ModuleType, monkeypatch: Any) -> None:
    calls: list[tuple[tuple[str, ...], str | None, bool, int | None]] = []

    def fake_run(command, *, cwd, check, timeout):
        calls.append((command, cwd, check, timeout))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(mkbrr_wizard.subprocess, "run", fake_run)
    executor = mkbrr_wizard.CommandExecutor(mkbrr_wizard.NativeBackend())

    result = executor.run(
        mkbrr_wizard.CommandSpec(argv=("mkbrr", "create"), cwd="working-directory"),
        timeout=45,
    )

    assert calls == [(("mkbrr", "create"), "working-directory", False, 45)]
    assert result.returncode == 0
    assert result.timed_out is False
    assert result.elapsed >= 0


def test_command_executor_maps_timeout_to_exit_code(
    mkbrr_wizard: ModuleType, monkeypatch: Any
) -> None:
    def raise_timeout(*args: Any, **kwargs: Any) -> None:
        raise subprocess.TimeoutExpired(cmd=["mkbrr"], timeout=10)

    monkeypatch.setattr(
        mkbrr_wizard.subprocess,
        "run",
        raise_timeout,
    )
    executor = mkbrr_wizard.CommandExecutor(mkbrr_wizard.DockerBackend())

    result = executor.run(mkbrr_wizard.CommandSpec(argv=("docker", "run")), timeout=10)

    assert result.returncode == 124
    assert result.timed_out is True
    assert result.elapsed >= 0


def test_command_executor_maps_launch_error_to_exit_code(
    mkbrr_wizard: ModuleType, monkeypatch: Any
) -> None:
    def raise_not_found(*args: Any, **kwargs: Any) -> None:
        raise FileNotFoundError("mkbrr not found")

    monkeypatch.setattr(mkbrr_wizard.subprocess, "run", raise_not_found)
    executor = mkbrr_wizard.CommandExecutor(mkbrr_wizard.NativeBackend())

    result = executor.run(mkbrr_wizard.CommandSpec(argv=("mkbrr", "create")))

    assert result.returncode == 127
    assert result.timed_out is False
    assert result.elapsed >= 0


def test_docker_backend_kills_named_container_on_timeout(
    mkbrr_wizard: ModuleType, monkeypatch: Any
) -> None:
    calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []

    def run(command: tuple[str, ...], **kwargs: Any) -> Any:
        calls.append((command, kwargs))
        if command[:2] == ("docker", "run"):
            raise subprocess.TimeoutExpired(cmd=command, timeout=10)
        if command[:2] == ("docker", "kill"):
            raise subprocess.TimeoutExpired(cmd=command, timeout=5)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(mkbrr_wizard.subprocess, "run", run)
    executor = mkbrr_wizard.CommandExecutor(mkbrr_wizard.DockerBackend())
    command = mkbrr_wizard.CommandSpec(
        argv=("docker", "run", "--name", "mkbrr-wizard-test", "image")
    )

    result = executor.run(command, timeout=10)

    assert result.returncode == 124
    assert result.timed_out is True
    assert calls == [
        (command.argv, {"cwd": None, "check": False, "timeout": 10}),
        (("docker", "kill", "mkbrr-wizard-test"), {"check": False, "timeout": 5}),
    ]


def test_build_inspect_command_verbose(mkbrr_wizard: ModuleType) -> None:
    cfg = sample_cfg(mkbrr_wizard)
    spec = mkbrr_wizard.build_inspect_command(
        cfg, "native", "/torrentfiles/test.torrent", verbose=True
    )
    assert "inspect" in spec.argv
    assert "-v" in spec.argv


def test_build_check_command_flags(mkbrr_wizard: ModuleType) -> None:
    cfg = sample_cfg(mkbrr_wizard)
    spec = mkbrr_wizard.build_check_command(
        cfg,
        "native",
        "/torrentfiles/t.torrent",
        "/mnt/user/data",
        verbose=True,
        quiet=True,
        workers=4,
    )
    assert "check" in spec.argv
    assert "-v" in spec.argv
    assert "--quiet" in spec.argv
    assert "--workers" in spec.argv
    assert "4" in spec.argv


def test_pick_runtime_forced_overrides(mkbrr_wizard: ModuleType, monkeypatch: Any) -> None:
    cfg = sample_cfg(mkbrr_wizard)
    # forced should win regardless of cfg.runtime
    assert mkbrr_wizard.pick_runtime(cfg, "native") == "native"
    assert mkbrr_wizard.pick_runtime(cfg, "docker") == "docker"


def test_pick_runtime_auto_favors_docker_then_native(
    mkbrr_wizard: ModuleType, monkeypatch: Any
) -> None:
    cfg = sample_cfg(mkbrr_wizard)

    monkeypatch.setattr(mkbrr_wizard, "docker_available", lambda: True)
    monkeypatch.setattr(mkbrr_wizard, "native_available", lambda binary: False)
    assert mkbrr_wizard.pick_runtime(cfg, None) == "docker"

    monkeypatch.setattr(mkbrr_wizard, "docker_available", lambda: False)
    monkeypatch.setattr(mkbrr_wizard, "native_available", lambda binary: True)
    assert mkbrr_wizard.pick_runtime(cfg, None) == "native"

    # none available should raise
    monkeypatch.setattr(mkbrr_wizard, "docker_available", lambda: False)
    monkeypatch.setattr(mkbrr_wizard, "native_available", lambda binary: False)
    with pytest.raises(RuntimeError):
        mkbrr_wizard.pick_runtime(cfg, None)
