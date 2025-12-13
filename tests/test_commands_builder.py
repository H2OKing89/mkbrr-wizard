"""Tests for command builder functions and runtime selection."""

from types import ModuleType
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
        presets_yaml_host="/mnt/cache/appdata/mkbrr/presets.yaml",
        presets_yaml_container="/root/.config/mkbrr/presets.yaml",
    )


def test_build_create_command_docker(mkbrr_wizard: ModuleType) -> None:
    cfg = sample_cfg(mkbrr_wizard)
    cmd, cwd = mkbrr_wizard.build_create_command(cfg, "docker", "/data/file.mkv", "btn")

    assert cmd[0] == "docker"
    assert "create" in cmd
    assert "-P" in cmd
    assert cfg.presets_yaml_container in cmd
    assert cwd is None


def test_build_create_command_native(mkbrr_wizard: ModuleType) -> None:
    cfg = sample_cfg(mkbrr_wizard)
    cmd, cwd = mkbrr_wizard.build_create_command(cfg, "native", "/mnt/user/data/file.mkv", "btn")

    assert cmd[0] == cfg.mkbrr.binary
    assert "create" in cmd
    assert cfg.presets_yaml_host in cmd
    assert cwd == cfg.paths.host_output_dir


def test_build_inspect_command_verbose(mkbrr_wizard: ModuleType) -> None:
    cfg = sample_cfg(mkbrr_wizard)
    cmd = mkbrr_wizard.build_inspect_command(
        cfg, "native", "/torrentfiles/test.torrent", verbose=True
    )
    assert "inspect" in cmd
    assert "-v" in cmd


def test_build_check_command_flags(mkbrr_wizard: ModuleType) -> None:
    cfg = sample_cfg(mkbrr_wizard)
    cmd = mkbrr_wizard.build_check_command(
        cfg,
        "native",
        "/torrentfiles/t.torrent",
        "/mnt/user/data",
        verbose=True,
        quiet=True,
        workers=4,
    )
    assert "check" in cmd
    assert "-v" in cmd
    assert "--quiet" in cmd
    assert "--workers" in cmd and "4" in cmd


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
