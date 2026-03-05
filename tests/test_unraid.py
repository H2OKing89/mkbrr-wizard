"""Tests for Unraid disk resolution helpers."""

from __future__ import annotations

from types import ModuleType
from typing import Any

import pytest  # type: ignore[import-untyped]


@pytest.fixture
def unraid_cfg(mkbrr_wizard: ModuleType) -> Any:
    return mkbrr_wizard.AppCfg(
        runtime="auto",
        docker_support=True,
        chown=False,
        docker_user=None,
        mkbrr=mkbrr_wizard.MkbrrCfg(binary="mkbrr", image="ghcr.io/autobrr/mkbrr"),
        paths=mkbrr_wizard.PathsCfg(
            host_data_root="/mnt/user/data",
            container_data_root="/data",
            host_output_dir="/mnt/cache-temp/data/downloads/torrents/torrentfiles",
            container_output_dir="/torrentfiles",
            host_config_dir="/mnt/cache/appdata/mkbrr",
            container_config_dir="/root/.config/mkbrr",
        ),
        ownership=mkbrr_wizard.OwnershipCfg(uid=99, gid=100),
        batch=mkbrr_wizard.BatchCfg(mode="simple"),
        presets_yaml_host="/mnt/cache/appdata/mkbrr/presets.yaml",
        presets_yaml_container="/root/.config/mkbrr/presets.yaml",
        unraid=mkbrr_wizard.UnraidCfg(enabled=True, fuse_root="/mnt/user"),
    )


def test_resolve_unraid_disk_path_finds_disk(
    mkbrr_wizard: ModuleType, unraid_cfg: Any, monkeypatch: Any
) -> None:
    monkeypatch.setattr(
        mkbrr_wizard, "_unraid_candidate_roots", lambda: ["/mnt/disk5", "/mnt/disk7"]
    )
    monkeypatch.setattr(
        mkbrr_wizard.os.path,
        "exists",
        lambda p: p == "/mnt/disk5/data/downloads/test.mkv",
    )

    resolved = mkbrr_wizard.resolve_unraid_disk_path(
        unraid_cfg, "/mnt/user/data/downloads/test.mkv"
    )

    assert resolved == "/mnt/disk5/data/downloads/test.mkv"


def test_resolve_unraid_disk_path_finds_cache(
    mkbrr_wizard: ModuleType, unraid_cfg: Any, monkeypatch: Any
) -> None:
    monkeypatch.setattr(
        mkbrr_wizard, "_unraid_candidate_roots", lambda: ["/mnt/disk5", "/mnt/cache"]
    )
    monkeypatch.setattr(
        mkbrr_wizard.os.path,
        "exists",
        lambda p: p == "/mnt/cache/data/downloads/test.mkv",
    )

    resolved = mkbrr_wizard.resolve_unraid_disk_path(
        unraid_cfg, "/mnt/user/data/downloads/test.mkv"
    )

    assert resolved == "/mnt/cache/data/downloads/test.mkv"


def test_resolve_unraid_disk_path_cache_first_prefers_cache(
    mkbrr_wizard: ModuleType, unraid_cfg: Any, monkeypatch: Any
) -> None:
    cache_first_cfg = mkbrr_wizard.AppCfg(
        runtime=unraid_cfg.runtime,
        docker_support=unraid_cfg.docker_support,
        chown=unraid_cfg.chown,
        docker_user=unraid_cfg.docker_user,
        mkbrr=unraid_cfg.mkbrr,
        paths=unraid_cfg.paths,
        ownership=unraid_cfg.ownership,
        batch=unraid_cfg.batch,
        presets_yaml_host=unraid_cfg.presets_yaml_host,
        presets_yaml_container=unraid_cfg.presets_yaml_container,
        unraid=mkbrr_wizard.UnraidCfg(
            enabled=True,
            fuse_root="/mnt/user",
            mount_priority="cache_first",
        ),
    )

    monkeypatch.setattr(
        mkbrr_wizard,
        "_unraid_candidate_roots",
        lambda cache_first=False: (
            ["/mnt/cache", "/mnt/disk5"] if cache_first else ["/mnt/disk5", "/mnt/cache"]
        ),
    )
    monkeypatch.setattr(
        mkbrr_wizard.os.path,
        "exists",
        lambda p: p in {"/mnt/disk5/data/downloads/test.mkv", "/mnt/cache/data/downloads/test.mkv"},
    )

    resolved = mkbrr_wizard.resolve_unraid_disk_path(
        cache_first_cfg, "/mnt/user/data/downloads/test.mkv"
    )

    assert resolved == "/mnt/cache/data/downloads/test.mkv"


def test_resolve_unraid_disk_path_non_unraid_path_unchanged(
    mkbrr_wizard: ModuleType, unraid_cfg: Any
) -> None:
    raw = "/tmp/random/path"
    assert mkbrr_wizard.resolve_unraid_disk_path(unraid_cfg, raw) == raw


def test_resolve_unraid_content_path_native(
    mkbrr_wizard: ModuleType, unraid_cfg: Any, monkeypatch: Any
) -> None:
    monkeypatch.setattr(
        mkbrr_wizard,
        "resolve_unraid_disk_path",
        lambda cfg, raw: raw.replace("/mnt/user", "/mnt/disk5"),
    )

    mapped, override = mkbrr_wizard.resolve_unraid_content_path(
        unraid_cfg,
        "native",
        "/mnt/user/data/downloads/test.mkv",
    )

    assert mapped == "/mnt/disk5/data/downloads/test.mkv"
    assert override is None


def test_resolve_unraid_content_path_docker_from_container_path(
    mkbrr_wizard: ModuleType, unraid_cfg: Any, monkeypatch: Any
) -> None:
    monkeypatch.setattr(
        mkbrr_wizard,
        "resolve_unraid_disk_path",
        lambda cfg, raw: raw.replace("/mnt/user", "/mnt/disk5"),
    )

    mapped, override = mkbrr_wizard.resolve_unraid_content_path(
        unraid_cfg,
        "docker",
        "/data/downloads/test.mkv",
    )

    assert mapped == "/data/downloads/test.mkv"
    assert override == "/mnt/disk5/data"


def test_unraid_disabled_returns_normal_mapping(
    mkbrr_wizard: ModuleType,
) -> None:
    cfg = mkbrr_wizard.AppCfg(
        runtime="auto",
        docker_support=True,
        chown=False,
        docker_user=None,
        mkbrr=mkbrr_wizard.MkbrrCfg(binary="mkbrr", image="ghcr.io/autobrr/mkbrr"),
        paths=mkbrr_wizard.PathsCfg(
            host_data_root="/mnt/user/data",
            container_data_root="/data",
            host_output_dir="/mnt/cache-temp/data/downloads/torrents/torrentfiles",
            container_output_dir="/torrentfiles",
            host_config_dir="/mnt/cache/appdata/mkbrr",
            container_config_dir="/root/.config/mkbrr",
        ),
        ownership=mkbrr_wizard.OwnershipCfg(uid=99, gid=100),
        batch=mkbrr_wizard.BatchCfg(mode="simple"),
        presets_yaml_host="/mnt/cache/appdata/mkbrr/presets.yaml",
        presets_yaml_container="/root/.config/mkbrr/presets.yaml",
        unraid=mkbrr_wizard.UnraidCfg(enabled=False, fuse_root="/mnt/user"),
    )

    mapped, override = mkbrr_wizard.resolve_unraid_content_path(
        cfg,
        "docker",
        "/mnt/user/data/downloads/test.mkv",
    )

    assert mapped == "/data/downloads/test.mkv"
    assert override is None


def test_docker_run_base_uses_override(mkbrr_wizard: ModuleType, unraid_cfg: Any) -> None:
    cmd = mkbrr_wizard.docker_run_base(
        unraid_cfg,
        "/data",
        host_data_root_override="/mnt/disk5/data",
    )

    assert "/mnt/disk5/data:/data" in cmd
    assert "/mnt/user/data:/data" not in cmd


def test_candidate_roots_natural_sort(mkbrr_wizard: ModuleType, monkeypatch: Any) -> None:
    class _Entry:
        def __init__(self, path: str):
            self.path = path

        def is_dir(self) -> bool:
            return True

    monkeypatch.setattr(
        mkbrr_wizard.os,
        "scandir",
        lambda _: iter(
            [
                _Entry("/mnt/disk10"),
                _Entry("/mnt/cache-temp"),
                _Entry("/mnt/disk2"),
                _Entry("/mnt/cache"),
                _Entry("/mnt/disk1"),
            ]
        ),
    )

    roots = mkbrr_wizard._unraid_candidate_roots()
    assert roots == ["/mnt/disk1", "/mnt/disk2", "/mnt/disk10", "/mnt/cache", "/mnt/cache-temp"]


def test_candidate_roots_cache_first_order(mkbrr_wizard: ModuleType, monkeypatch: Any) -> None:
    class _Entry:
        def __init__(self, path: str):
            self.path = path

        def is_dir(self) -> bool:
            return True

    monkeypatch.setattr(
        mkbrr_wizard.os,
        "scandir",
        lambda _: iter(
            [
                _Entry("/mnt/disk2"),
                _Entry("/mnt/cache-temp"),
                _Entry("/mnt/disk1"),
                _Entry("/mnt/cache"),
            ]
        ),
    )

    roots = mkbrr_wizard._unraid_candidate_roots(cache_first=True)
    assert roots == ["/mnt/cache", "/mnt/cache-temp", "/mnt/disk1", "/mnt/disk2"]


def test_detect_split_share_mismatch_file_missing(
    mkbrr_wizard: ModuleType, monkeypatch: Any
) -> None:
    monkeypatch.setattr(mkbrr_wizard.os.path, "isfile", lambda p: p == "/mnt/user/data/test.mkv")
    monkeypatch.setattr(mkbrr_wizard.os.path, "isdir", lambda p: False)
    monkeypatch.setattr(
        mkbrr_wizard.os.path,
        "exists",
        lambda p: p == "/mnt/user/data/test.mkv",
    )

    missing_count, examples, permission_errors, capped = mkbrr_wizard._detect_split_share_mismatch(
        "/mnt/user/data/test.mkv",
        "/mnt/disk13/data/test.mkv",
        max_entries=100,
        follow_symlinks=False,
    )

    assert missing_count == 1
    assert examples == ["test.mkv"]
    assert permission_errors == 0
    assert capped is False


def test_preflight_split_share_fail_docker_raises(
    mkbrr_wizard: ModuleType, unraid_cfg: Any, monkeypatch: Any
) -> None:
    monkeypatch.setattr(mkbrr_wizard.os.path, "exists", lambda p: True)
    monkeypatch.setattr(
        mkbrr_wizard,
        "_detect_split_share_mismatch",
        lambda *a, **k: (3, ["ep01.mkv", "ep02.mkv"], 0, False),
    )

    with pytest.raises(ValueError, match="split-share"):
        mkbrr_wizard.preflight_unraid_split_share(
            unraid_cfg,
            runtime="docker",
            content_path="/data/downloads/pack",
            host_data_root_override="/mnt/disk13/data",
            context="batch job 1",
        )


def test_preflight_split_share_warn_docker_no_raise(
    mkbrr_wizard: ModuleType, unraid_cfg: Any, monkeypatch: Any
) -> None:
    warn_cfg = mkbrr_wizard.AppCfg(
        runtime=unraid_cfg.runtime,
        docker_support=unraid_cfg.docker_support,
        chown=unraid_cfg.chown,
        docker_user=unraid_cfg.docker_user,
        mkbrr=unraid_cfg.mkbrr,
        paths=unraid_cfg.paths,
        ownership=unraid_cfg.ownership,
        batch=unraid_cfg.batch,
        presets_yaml_host=unraid_cfg.presets_yaml_host,
        presets_yaml_container=unraid_cfg.presets_yaml_container,
        unraid=mkbrr_wizard.UnraidCfg(
            enabled=True,
            fuse_root="/mnt/user",
            split_share_preflight="warn",
            split_share_max_entries=20000,
            split_share_follow_symlinks=False,
        ),
    )

    monkeypatch.setattr(mkbrr_wizard.os.path, "exists", lambda p: True)
    monkeypatch.setattr(
        mkbrr_wizard,
        "_detect_split_share_mismatch",
        lambda *a, **k: (2, ["a.mkv"], 0, False),
    )

    mkbrr_wizard.preflight_unraid_split_share(
        warn_cfg,
        runtime="docker",
        content_path="/data/downloads/pack",
        host_data_root_override="/mnt/disk13/data",
        context="create",
    )


def test_preflight_split_share_native_derives_original(
    mkbrr_wizard: ModuleType, unraid_cfg: Any, monkeypatch: Any
) -> None:
    monkeypatch.setattr(mkbrr_wizard.os.path, "exists", lambda p: True)
    captured: dict[str, str] = {}

    def _fake_detect(original: str, resolved: str, **_: Any) -> tuple[int, list[str], int, bool]:
        captured["original"] = original
        captured["resolved"] = resolved
        return (0, [], 0, False)

    monkeypatch.setattr(mkbrr_wizard, "_detect_split_share_mismatch", _fake_detect)

    mkbrr_wizard.preflight_unraid_split_share(
        unraid_cfg,
        runtime="native",
        content_path="/mnt/disk14/data/downloads/pack",
        host_data_root_override=None,
        context="create",
    )

    assert captured["original"] == "/mnt/user/data/downloads/pack"
    assert captured["resolved"] == "/mnt/disk14/data/downloads/pack"


def test_preflight_split_share_unmapped_docker_path_fail_raises(
    mkbrr_wizard: ModuleType, unraid_cfg: Any
) -> None:
    fail_cfg = mkbrr_wizard.AppCfg(
        runtime=unraid_cfg.runtime,
        docker_support=unraid_cfg.docker_support,
        chown=unraid_cfg.chown,
        docker_user=unraid_cfg.docker_user,
        mkbrr=unraid_cfg.mkbrr,
        paths=unraid_cfg.paths,
        ownership=unraid_cfg.ownership,
        batch=unraid_cfg.batch,
        presets_yaml_host=unraid_cfg.presets_yaml_host,
        presets_yaml_container=unraid_cfg.presets_yaml_container,
        unraid=mkbrr_wizard.UnraidCfg(
            enabled=True,
            fuse_root="/mnt/user",
            split_share_unmapped_docker_path="fail",
        ),
    )

    with pytest.raises(ValueError, match="outside /data"):
        mkbrr_wizard.preflight_unraid_split_share(
            fail_cfg,
            runtime="docker",
            content_path="/mnt/user/data/downloads/pack",
            host_data_root_override=None,
            context="batch job 1",
        )


def test_preflight_split_share_unmapped_docker_path_warn_no_raise(
    mkbrr_wizard: ModuleType, unraid_cfg: Any
) -> None:
    warn_cfg = mkbrr_wizard.AppCfg(
        runtime=unraid_cfg.runtime,
        docker_support=unraid_cfg.docker_support,
        chown=unraid_cfg.chown,
        docker_user=unraid_cfg.docker_user,
        mkbrr=unraid_cfg.mkbrr,
        paths=unraid_cfg.paths,
        ownership=unraid_cfg.ownership,
        batch=unraid_cfg.batch,
        presets_yaml_host=unraid_cfg.presets_yaml_host,
        presets_yaml_container=unraid_cfg.presets_yaml_container,
        unraid=mkbrr_wizard.UnraidCfg(
            enabled=True,
            fuse_root="/mnt/user",
            split_share_unmapped_docker_path="warn",
        ),
    )

    mkbrr_wizard.preflight_unraid_split_share(
        warn_cfg,
        runtime="docker",
        content_path="/mnt/user/data/downloads/pack",
        host_data_root_override=None,
        context="create",
    )
