"""Tests to exercise failure branches that return non-zero mkbrr exit codes."""

from types import ModuleType, SimpleNamespace
from typing import Any


def _mk_args(config_path: str) -> SimpleNamespace:
    return SimpleNamespace(config=config_path, docker=False, native=False)


class _Seq:
    def __init__(self, items):
        self._it = iter(items)

    def __call__(self, *args, **kwargs):
        return next(self._it)


def test_create_failure_native(tmp_path, mkbrr_wizard: ModuleType, monkeypatch: Any) -> None:
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    content_file = tmp_path / "data" / "movie.mkv"
    content_file.parent.mkdir()
    content_file.write_text("x")

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

    seq = _Seq(["1", "1", str(content_file), "q"])  # create, pick preset, content path, quit
    monkeypatch.setattr(mkbrr_wizard, "_has_prompt_toolkit", False)
    monkeypatch.setattr(mkbrr_wizard.Prompt, "ask", seq)

    # Confirm.ask: create confirm True, final do another -> False
    cseq = _Seq([True, False])
    monkeypatch.setattr(mkbrr_wizard.Confirm, "ask", cseq)

    class Dummy:
        def __init__(self, returncode=1):
            self.returncode = returncode

    monkeypatch.setattr(mkbrr_wizard.subprocess, "run", lambda *a, **k: Dummy(1))

    mkbrr_wizard.main()


def test_inspect_failure_docker(tmp_path, mkbrr_wizard: ModuleType, monkeypatch: Any) -> None:
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    torrent_file = tmp_path / "torrents" / "test.torrent"
    torrent_file.parent.mkdir(parents=True, exist_ok=True)
    torrent_file.write_text("tor")

    config_yaml = tmp_path / "config.yaml"
    config_yaml.write_text(
        f"""
runtime: auto
docker_support: true
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
    monkeypatch.setattr(mkbrr_wizard, "pick_runtime", lambda cfg, forced: "docker")

    # Sequence: choose '2' inspect, provide torrent, quit
    seq = _Seq(["2", str(torrent_file), "q"])
    monkeypatch.setattr(mkbrr_wizard, "_has_prompt_toolkit", False)
    monkeypatch.setattr(mkbrr_wizard.Prompt, "ask", seq)

    # Confirm.ask: verbose True, confirm True, final exit False
    cseq = _Seq([True, True, False])
    monkeypatch.setattr(mkbrr_wizard.Confirm, "ask", cseq)

    class Dummy:
        def __init__(self, returncode=2):
            self.returncode = returncode

    monkeypatch.setattr(mkbrr_wizard.subprocess, "run", lambda *a, **k: Dummy(2))

    mkbrr_wizard.main()


def test_check_failure_docker(tmp_path, mkbrr_wizard: ModuleType, monkeypatch: Any) -> None:
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    torrent_file = tmp_path / "torrents" / "test.torrent"
    torrent_file.parent.mkdir(parents=True, exist_ok=True)
    torrent_file.write_text("tor")
    content_file = tmp_path / "data" / "file.mkv"
    content_file.parent.mkdir(parents=True, exist_ok=True)
    content_file.write_text("x")

    config_yaml = tmp_path / "config.yaml"
    config_yaml.write_text(
        f"""
runtime: auto
docker_support: true
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
    monkeypatch.setattr(mkbrr_wizard, "pick_runtime", lambda cfg, forced: "docker")

    seq = _Seq(["3", str(torrent_file), str(content_file), "auto", "q"])
    monkeypatch.setattr(mkbrr_wizard, "_has_prompt_toolkit", False)
    monkeypatch.setattr(mkbrr_wizard.Prompt, "ask", seq)

    # Confirm.ask: verbose True, quiet False, confirm True, final False
    cseq = _Seq([True, False, True, False])
    monkeypatch.setattr(mkbrr_wizard.Confirm, "ask", cseq)

    class Dummy:
        def __init__(self, returncode=1):
            self.returncode = returncode

    monkeypatch.setattr(mkbrr_wizard.subprocess, "run", lambda *a, **k: Dummy(1))

    mkbrr_wizard.main()
