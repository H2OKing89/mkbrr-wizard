"""Tests for maybe_fix_torrent_permissions."""

import os
import tempfile
from types import ModuleType
from typing import Any


def test_maybe_fix_torrent_permissions_skips_when_not_root(
    mkbrr_wizard: ModuleType, monkeypatch: Any
) -> None:
    cfg = mkbrr_wizard.AppCfg(
        runtime="native",
        docker_support=False,
        chown=True,
        docker_user=None,
        mkbrr=mkbrr_wizard.MkbrrCfg(binary="mkbrr", image="ghcr.io/autobrr/mkbrr"),
        paths=mkbrr_wizard.PathsCfg(
            host_data_root="/mnt/user/data",
            container_data_root="/data",
            host_output_dir="/tmp/mkbrr_test_out",
            container_output_dir="/torrentfiles",
            host_config_dir="/mnt/cache/appdata/mkbrr",
            container_config_dir="/root/.config/mkbrr",
        ),
        ownership=mkbrr_wizard.OwnershipCfg(uid=1000, gid=1000),
        batch=mkbrr_wizard.BatchCfg(mode="simple"),
        presets_yaml_host="/mnt/cache/appdata/mkbrr/presets.yaml",
        presets_yaml_container="/root/.config/mkbrr/presets.yaml",
    )

    # Ensure dir exists
    os.makedirs(cfg.paths.host_output_dir, exist_ok=True)

    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    # monkeypatch chown to capture calls
    called: dict[str, Any] = {"count": 0}

    def fake_chown(p, uid, gid):
        called["count"] += 1

    monkeypatch.setattr(os, "chown", fake_chown)

    mkbrr_wizard.maybe_fix_torrent_permissions(cfg, [])
    assert called["count"] == 0


def test_maybe_fix_torrent_permissions_executes_chown(
    mkbrr_wizard: ModuleType, monkeypatch: Any
) -> None:
    cfg = mkbrr_wizard.AppCfg(
        runtime="native",
        docker_support=False,
        chown=True,
        docker_user=None,
        mkbrr=mkbrr_wizard.MkbrrCfg(binary="mkbrr", image="ghcr.io/autobrr/mkbrr"),
        paths=mkbrr_wizard.PathsCfg(
            host_data_root="/mnt/user/data",
            container_data_root="/data",
            host_output_dir=tempfile.mkdtemp(prefix="mkbrr_test_out_"),
            container_output_dir="/torrentfiles",
            host_config_dir="/mnt/cache/appdata/mkbrr",
            container_config_dir="/root/.config/mkbrr",
        ),
        ownership=mkbrr_wizard.OwnershipCfg(uid=999999, gid=999999),
        batch=mkbrr_wizard.BatchCfg(mode="simple"),
        presets_yaml_host="/mnt/cache/appdata/mkbrr/presets.yaml",
        presets_yaml_container="/root/.config/mkbrr/presets.yaml",
    )

    # Create one operation output and one unrelated existing torrent.
    p = os.path.join(cfg.paths.host_output_dir, "test.torrent")
    with open(p, "w") as fh:
        fh.write("fake")
    unrelated = os.path.join(cfg.paths.host_output_dir, "unrelated.torrent")
    with open(unrelated, "w") as fh:
        fh.write("existing")

    # file has different uid/gid; ensure we run chown path
    monkeypatch.setattr(os, "geteuid", lambda: 0)

    class Stat:
        st_uid = 0
        st_gid = 0
        st_mode = 0

    orig_stat = os.stat

    def fake_stat(fd, *args, **kwargs):
        # Only return the fake Stat for our .torrent file
        if str(fd).endswith("test.torrent"):
            s = Stat()
            s.st_mode = 0o100644
            return s
        return orig_stat(fd)

    monkeypatch.setattr(os, "stat", fake_stat)
    called: dict[str, Any] = {"count": 0, "args": []}

    def fake_chown(path, uid, gid):
        called["count"] += 1
        called["args"].append((path, uid, gid))

    monkeypatch.setattr(os, "chown", fake_chown)

    mkbrr_wizard.maybe_fix_torrent_permissions(cfg, [p])
    assert called["args"] == [(p, cfg.ownership.uid, cfg.ownership.gid)]

    # cleanup
    try:
        os.unlink(p)
    except Exception:
        pass
    try:
        os.unlink(unrelated)
    except Exception:
        pass
    try:
        os.rmdir(cfg.paths.host_output_dir)
    except Exception:
        pass


def test_changed_torrent_outputs_returns_only_new_or_modified(
    mkbrr_wizard: ModuleType,
) -> None:
    before = {
        "/out/unchanged.torrent": (1, 100, 10),
        "/out/modified.torrent": (2, 100, 10),
    }
    after = {
        "/out/unchanged.torrent": (1, 100, 10),
        "/out/modified.torrent": (2, 200, 20),
        "/out/new.torrent": (3, 50, 30),
    }

    assert mkbrr_wizard._changed_torrent_outputs(before, after) == [
        "/out/modified.torrent",
        "/out/new.torrent",
    ]
