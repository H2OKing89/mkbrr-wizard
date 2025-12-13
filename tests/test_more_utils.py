"""Additional tests to exercise more branches and improve coverage."""

import os
import sys
from types import ModuleType
from typing import Any


def test_prompt_toolkit_eof(monkeypatch: Any, mkbrr_wizard: ModuleType) -> None:
    # enable prompt_toolkit branch
    monkeypatch.setattr(mkbrr_wizard, "_has_prompt_toolkit", True)

    class FakeSession:
        def __init__(self, *a, **k):
            pass

        def prompt(self, *a, **k):
            raise EOFError()

    # Patch PromptSession at the prompt_toolkit top-level
    import prompt_toolkit

    monkeypatch.setattr(prompt_toolkit, "PromptSession", FakeSession, raising=False)

    # Ensure raise SystemExit when EOF encountered; pass non-None history to trigger PromptSession branch
    import pytest

    with pytest.raises(SystemExit):
        mkbrr_wizard.ask_path("Prompt", history=mkbrr_wizard._content_history)


def test_parse_args(monkeypatch: Any, mkbrr_wizard: ModuleType) -> None:
    monkeypatch.setattr(sys, "argv", ["mkbrr-wizard.py", "--config", "abc.yaml", "--docker"])
    args = mkbrr_wizard.parse_args()
    assert args.config == "abc.yaml"
    assert args.docker is True


def test_render_header_and_docker_run_base(monkeypatch: Any, mkbrr_wizard: ModuleType) -> None:
    cfg = mkbrr_wizard.AppCfg(
        runtime="docker",
        docker_support=True,
        chown=False,
        docker_user="1000:1000",
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

    mkbrr_wizard.render_header(cfg, "docker")

    # simulate TTY
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    cmd = mkbrr_wizard.docker_run_base(cfg, "/data")
    assert "-it" in cmd
    assert "--user" in cmd


def test_maybe_fix_torrent_permissions_permission_error(
    tmp_path, monkeypatch: Any, mkbrr_wizard: ModuleType
) -> None:
    cfg = mkbrr_wizard.AppCfg(
        runtime="native",
        docker_support=False,
        chown=True,
        docker_user=None,
        mkbrr=mkbrr_wizard.MkbrrCfg(binary="mkbrr", image="ghcr.io/autobrr/mkbrr"),
        paths=mkbrr_wizard.PathsCfg(
            host_data_root=str(tmp_path),
            container_data_root="/data",
            host_output_dir=str(tmp_path / "torrents"),
            container_output_dir="/torrentfiles",
            host_config_dir=str(tmp_path / "cfg"),
            container_config_dir="/root/.config/mkbrr",
        ),
        ownership=mkbrr_wizard.OwnershipCfg(uid=1000, gid=1000),
        presets_yaml_host="/tmp/presets.yaml",
        presets_yaml_container="/root/.config/mkbrr/presets.yaml",
    )

    os.makedirs(cfg.paths.host_output_dir, exist_ok=True)
    p = os.path.join(cfg.paths.host_output_dir, "a.torrent")
    with open(p, "w") as fh:
        fh.write("x")

    monkeypatch.setattr(os, "geteuid", lambda: 0)

    class Stat:
        st_uid = 0
        st_gid = 0
        st_mode = 0o100644

    orig_stat = os.stat

    def fake_stat(path):
        if str(path).endswith(".torrent"):
            return Stat()
        return orig_stat(path)

    monkeypatch.setattr(os, "stat", fake_stat)

    def fake_chown(path, uid, gid):
        raise PermissionError("nope")

    monkeypatch.setattr(os, "chown", fake_chown)

    # Should catch PermissionError and not raise
    mkbrr_wizard.maybe_fix_torrent_permissions(cfg)


def test_ask_verbose_and_quiet(monkeypatch: Any, mkbrr_wizard: ModuleType) -> None:
    monkeypatch.setattr(mkbrr_wizard.Confirm, "ask", lambda *a, **k: True)
    assert mkbrr_wizard.ask_verbose("inspect") is True
    assert mkbrr_wizard.ask_quiet() is True

    monkeypatch.setattr(mkbrr_wizard.Confirm, "ask", lambda *a, **k: False)
    assert mkbrr_wizard.ask_verbose("inspect") is False
    assert mkbrr_wizard.ask_quiet() is False
