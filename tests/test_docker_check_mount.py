"""Docker `check` planning must keep the torrent reachable when Unraid content
resolution redirects the data mount to a specific physical disk.

Unraid disk/cache detection is hardcoded to the real "/mnt" layout, so these
tests use literal "/mnt/..." paths and monkeypatch filesystem lookups instead
of writing real files under "/mnt".
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from mkbrr_wizard import legacy_app as legacy
from mkbrr_wizard.planning.planner import PlanBuilder


def _cfg(tmp_path: Path) -> legacy.AppCfg:
    presets = tmp_path / "presets.yaml"
    presets.write_text("version: 1\npresets:\n  test:\n    private: true\n", encoding="utf-8")
    return legacy.AppCfg(
        runtime="auto",
        docker_support=True,
        chown=False,
        docker_user=None,
        mkbrr=legacy.MkbrrCfg(binary="mkbrr", image="ghcr.io/autobrr/mkbrr"),
        paths=legacy.PathsCfg(
            host_data_root="/mnt/user/data",
            container_data_root="/data",
            host_output_dir="/mnt/cache/torrentfiles",
            container_output_dir="/torrentfiles",
            host_config_dir=str(tmp_path),
            container_config_dir="/root/.config/mkbrr",
        ),
        ownership=legacy.OwnershipCfg(uid=99, gid=100),
        batch=legacy.BatchCfg(mode="simple"),
        unraid=legacy.UnraidCfg(
            enabled=True,
            fuse_root="/mnt/user",
            split_share_preflight="off",
        ),
    )


def _allow_paths(monkeypatch: pytest.MonkeyPatch, *existing: str) -> None:
    allowed = set(existing)
    monkeypatch.setattr(os.path, "exists", lambda p: p in allowed)
    monkeypatch.setattr(os.path, "isfile", lambda p: p in allowed)


def _resolve_to_disk5(cfg: Any, raw: str, **_: Any) -> str:
    return raw.replace("/mnt/user", "/mnt/disk5")


def test_plan_check_reverses_data_root_torrent_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """host_torrent must reverse container_data_root, not just container_output_dir."""
    cfg = _cfg(tmp_path)
    torrent = "/mnt/user/data/release.torrent"
    content = "/mnt/disk5/data/movie"
    monkeypatch.setattr(legacy, "resolve_unraid_disk_path", _resolve_to_disk5)
    _allow_paths(monkeypatch, torrent, content)

    planner = PlanBuilder(cfg, "docker")
    plan = planner.plan_check(torrent, "/mnt/user/data/movie", dry_run=True)

    command = plan.operations[0].command
    assert command[command.index("check") + 1] == "/data/release.torrent"


def test_plan_check_binds_torrent_hidden_by_data_root_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A physical-disk override for content must not hide a torrent on another disk."""
    cfg = _cfg(tmp_path)
    torrent = "/mnt/user/data/other-share/release.torrent"
    content = "/mnt/disk5/data/movie"
    monkeypatch.setattr(legacy, "resolve_unraid_disk_path", _resolve_to_disk5)
    _allow_paths(monkeypatch, torrent, content)

    planner = PlanBuilder(cfg, "docker")
    plan = planner.plan_check(torrent, "/mnt/user/data/movie", dry_run=True)

    operation = plan.operations[0]
    assert f"{torrent}:/data/other-share/release.torrent:ro" in operation.command


def test_plan_check_skips_extra_bind_when_torrent_is_in_output_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No redundant bind is added for the common case: a torrent under host_output_dir."""
    cfg = _cfg(tmp_path)
    # host_output_dir is always mounted, regardless of any content override.
    torrent = "/mnt/cache/torrentfiles/release.torrent"
    content = "/mnt/disk5/data/movie"
    monkeypatch.setattr(legacy, "resolve_unraid_disk_path", _resolve_to_disk5)
    _allow_paths(monkeypatch, torrent, content)

    planner = PlanBuilder(cfg, "docker")
    plan = planner.plan_check(torrent, "/mnt/user/data/movie", dry_run=True)

    operation = plan.operations[0]
    assert ":ro" not in " ".join(operation.command)
