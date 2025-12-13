"""Tests targeting edge cases in main loop for coverage gains."""

from types import ModuleType, SimpleNamespace
from typing import Any


def _mk_args(config_path: str) -> SimpleNamespace:
    return SimpleNamespace(config=config_path, docker=False, native=False)


class _Seq:
    def __init__(self, items):
        self._it = iter(items)

    def __call__(self, *args, **kwargs):
        return next(self._it)


def test_main_create_missing_content_native(
    tmp_path, mkbrr_wizard: ModuleType, monkeypatch: Any
) -> None:
    # prepare config
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    config_yaml = tmp_path / "config.yaml"
    config_yaml.write_text(
        f"""
runtime: native
docker_support: false
chown: false
mkbrr:
  binary: mkbrr
paths:
  host_data_root: {tmp_path}
  container_data_root: /data
  host_output_dir: {tmp_path}/torrents
  container_output_dir: /torrentfiles
  host_config_dir: {cfg_dir}
  container_config_dir: /root/.config/mkbrr
presets_yaml: {cfg_dir}/presets.yaml
"""
    )

    monkeypatch.setattr(mkbrr_wizard, "parse_args", lambda: _mk_args(str(config_yaml)))
    monkeypatch.setattr(mkbrr_wizard, "pick_runtime", lambda cfg, forced: "native")

    # sequence: create -> pick preset -> provide missing content -> then quit (q)
    seq = _Seq(["1", "1", "/nonexistent/path", "q"])  # choose create, pick preset, path, then quit
    monkeypatch.setattr(mkbrr_wizard, "_has_prompt_toolkit", False)
    monkeypatch.setattr(mkbrr_wizard.Prompt, "ask", seq)

    # Confirm.ask: should only be called for 'Do another operation?' -> False
    monkeypatch.setattr(mkbrr_wizard.Confirm, "ask", lambda *a, **k: False)

    import pytest

    with pytest.raises(SystemExit):
        mkbrr_wizard.main()


def test_main_check_invalid_paths_native(
    tmp_path, mkbrr_wizard: ModuleType, monkeypatch: Any
) -> None:
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    # create a fake torrent file path but ensure it's not present
    config_yaml = tmp_path / "config.yaml"
    config_yaml.write_text(
        f"""
runtime: native
docker_support: false
chown: false
mkbrr:
  binary: mkbrr
paths:
  host_data_root: {tmp_path}
  container_data_root: /data
  host_output_dir: {tmp_path}/torrents
  container_output_dir: /torrentfiles
  host_config_dir: {cfg_dir}
  container_config_dir: /root/.config/mkbrr
presets_yaml: {cfg_dir}/presets.yaml
"""
    )

    monkeypatch.setattr(mkbrr_wizard, "parse_args", lambda: _mk_args(str(config_yaml)))
    monkeypatch.setattr(mkbrr_wizard, "pick_runtime", lambda cfg, forced: "native")

    # Sequence: choose 'check' then provide missing torrent and content paths then quit
    seq = _Seq(["3", "/nonexistent/file.torrent", "/nonexistent/content", "q"])
    monkeypatch.setattr(mkbrr_wizard, "_has_prompt_toolkit", False)
    monkeypatch.setattr(mkbrr_wizard.Prompt, "ask", seq)

    # Confirm.ask: do another -> False
    monkeypatch.setattr(mkbrr_wizard.Confirm, "ask", lambda *a, **k: False)

    import pytest

    with pytest.raises(SystemExit):
        mkbrr_wizard.main()


def test_main_check_verbose_quiet_conflict(
    tmp_path, mkbrr_wizard: ModuleType, monkeypatch: Any
) -> None:
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    config_yaml = tmp_path / "config.yaml"
    config_yaml.write_text(
        f"""
runtime: native
docker_support: false
chown: false
mkbrr:
  binary: mkbrr
paths:
  host_data_root: {tmp_path}
  container_data_root: /data
  host_output_dir: {tmp_path}/torrents
  container_output_dir: /torrentfiles
  host_config_dir: {cfg_dir}
  container_config_dir: /root/.config/mkbrr
presets_yaml: {cfg_dir}/presets.yaml
"""
    )

    monkeypatch.setattr(mkbrr_wizard, "parse_args", lambda: _mk_args(str(config_yaml)))
    monkeypatch.setattr(mkbrr_wizard, "pick_runtime", lambda cfg, forced: "native")

    # create actual content and torrent
    content_file = tmp_path / "data" / "movie.mkv"
    content_file.parent.mkdir()
    content_file.write_text("x")
    torrent_file = tmp_path / "torrents" / "test.torrent"
    torrent_file.parent.mkdir()
    torrent_file.write_text("tor")

    # Sequence: choose check -> provide torrent path -> content path -> workers auto -> quit
    seq = _Seq(["3", str(torrent_file), str(content_file), "auto", "q"])
    monkeypatch.setattr(mkbrr_wizard, "_has_prompt_toolkit", False)
    monkeypatch.setattr(mkbrr_wizard.Prompt, "ask", seq)

    # Confirm.ask sequence: verbose True, quiet True, confirm True, do another False
    cseq = _Seq([True, True, True, False])
    monkeypatch.setattr(mkbrr_wizard.Confirm, "ask", cseq)

    class Dummy:
        def __init__(self, returncode=0):
            self.returncode = returncode

    monkeypatch.setattr(mkbrr_wizard.subprocess, "run", lambda *a, **k: Dummy(0))

    mkbrr_wizard.main()
