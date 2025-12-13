"""Docker runtime flow main() simulation tests."""

from types import ModuleType, SimpleNamespace
from typing import Any


def _mk_args(config_path: str) -> SimpleNamespace:
    return SimpleNamespace(config=config_path, docker=False, native=False)


class _Seq:
    def __init__(self, items):
        self._it = iter(items)

    def __call__(self, *args, **kwargs):
        return next(self._it)


def test_main_docker_full_flow(tmp_path, mkbrr_wizard: ModuleType, monkeypatch: Any) -> None:
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    # Docker paths are like /data (container) and host path tmp_path
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

    # Create content file under host_data_root
    host_content = tmp_path / "data" / "video.mkv"
    host_content.parent.mkdir()
    host_content.write_text("x")

    # Create torrent file in host_output
    torrent_file = tmp_path / "torrents" / "test.torrent"
    torrent_file.parent.mkdir(parents=True, exist_ok=True)
    torrent_file.write_text("tor")

    # Sequence: create -> pick_preset -> content path -> inspect -> torrent path -> check -> torrent path -> content path
    seq = _Seq(
        [
            "1",
            "1",
            str(host_content),
            "2",
            str(torrent_file),
            "3",
            str(torrent_file),
            str(host_content),
        ]
    )
    monkeypatch.setattr(mkbrr_wizard, "_has_prompt_toolkit", False)
    monkeypatch.setattr(mkbrr_wizard.Prompt, "ask", seq)

    # Confirm.ask sequence: create confirm True, inspect verbose False, inspect confirm True,
    # check verbose False, check quiet False, check confirm True, do another False
    cseq = _Seq([True, False, True, False, False, True, False])
    monkeypatch.setattr(mkbrr_wizard.Confirm, "ask", cseq)

    class Dummy:
        def __init__(self, returncode=0):
            self.returncode = returncode

    monkeypatch.setattr(mkbrr_wizard.subprocess, "run", lambda *a, **k: Dummy(0))

    mkbrr_wizard.main()
