"""Integration-style tests for main() control flow (simulate user interactions)."""

from types import ModuleType, SimpleNamespace
from typing import Any

from .conftest import _Seq


def _mk_args(config_path: str) -> SimpleNamespace:
    return SimpleNamespace(config=config_path, docker=False, native=False)


def test_handle_inspect_uses_executor_and_notifies(
    tmp_path, mkbrr_wizard: ModuleType, monkeypatch: Any
) -> None:
    config_yaml = tmp_path / "config.yaml"
    config_yaml.write_text(
        f"""
runtime: native
docker_support: false
chown: false
paths:
  host_data_root: {tmp_path}/data
  container_data_root: /data
  host_output_dir: {tmp_path}/torrents
  container_output_dir: /torrentfiles
  host_config_dir: {tmp_path}/cfg
  container_config_dir: /root/.config/mkbrr
"""
    )
    cfg = mkbrr_wizard.load_config(config_yaml)
    executed: list[Any] = []
    notifications: list[Any] = []
    monkeypatch.setattr(mkbrr_wizard, "ask_path", lambda *args, **kwargs: "/torrents/test.torrent")
    monkeypatch.setattr(mkbrr_wizard, "ask_verbose", lambda mode: True)
    monkeypatch.setattr(mkbrr_wizard, "confirm_cmd", lambda *args, **kwargs: True)

    def run(command: Any) -> Any:
        executed.append(command)
        return mkbrr_wizard.ExecutionResult(returncode=0, elapsed=1.5)

    executor = SimpleNamespace(run=run)
    notifier = SimpleNamespace(notify=notifications.append)

    mkbrr_wizard.handle_inspect(cfg, "native", executor, notifier)

    assert len(executed) == 1
    assert executed[0].argv == ("mkbrr", "inspect", "/torrents/test.torrent", "-v")
    assert len(notifications) == 1
    assert notifications[0].event_type == "inspect"
    assert notifications[0].details["elapsed"] == 1.5


def test_handle_check_uses_executor_and_notifies(
    tmp_path, mkbrr_wizard: ModuleType, monkeypatch: Any
) -> None:
    config_yaml = tmp_path / "config.yaml"
    config_yaml.write_text(
        f"""
runtime: native
docker_support: false
chown: false
paths:
  host_data_root: {tmp_path}/data
  container_data_root: /data
  host_output_dir: {tmp_path}/torrents
  container_output_dir: /torrentfiles
  host_config_dir: {tmp_path}/cfg
  container_config_dir: /root/.config/mkbrr
"""
    )
    cfg = mkbrr_wizard.load_config(config_yaml)
    content_path = tmp_path / "data" / "movie.mkv"
    content_path.parent.mkdir()
    content_path.write_text("x")
    torrent_path = tmp_path / "torrents" / "movie.torrent"
    torrent_path.parent.mkdir()
    torrent_path.write_text("torrent")
    executed: list[Any] = []
    notifications: list[Any] = []
    monkeypatch.setattr(
        mkbrr_wizard,
        "ask_path",
        _Seq([str(torrent_path), str(content_path)]),
    )
    monkeypatch.setattr(mkbrr_wizard, "ask_verbose", lambda mode: False)
    monkeypatch.setattr(mkbrr_wizard, "ask_quiet", lambda: False)
    monkeypatch.setattr(mkbrr_wizard, "ask_workers", lambda: 2)
    monkeypatch.setattr(mkbrr_wizard, "confirm_cmd", lambda *args, **kwargs: True)

    def run(command: Any) -> Any:
        executed.append(command)
        return mkbrr_wizard.ExecutionResult(returncode=0, elapsed=2.5)

    executor = SimpleNamespace(run=run)
    notifier = SimpleNamespace(notify=notifications.append)

    mkbrr_wizard.handle_check(cfg, "native", executor, notifier)

    assert len(executed) == 1
    assert executed[0].argv == (
        "mkbrr",
        "check",
        str(torrent_path),
        str(content_path),
        "--workers",
        "2",
    )
    assert len(notifications) == 1
    assert notifications[0].event_type == "check"
    assert notifications[0].details["elapsed"] == 2.5


def test_main_create_inspect_check_native(
    tmp_path, mkbrr_wizard: ModuleType, monkeypatch: Any
) -> None:
    # Prepare config directory and presets
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    presets_yaml = config_dir / "presets.yaml"
    presets_yaml.write_text("""presets:\n  btn:\n    announce: https://example.com/announce\n""")

    # Prepare content file and torrent file
    content_file = tmp_path / "data" / "movie.mkv"
    content_file.parent.mkdir()
    content_file.write_text("x")

    torrent_file = tmp_path / "torrents" / "test.torrent"
    torrent_file.parent.mkdir(parents=True, exist_ok=True)
    torrent_file.write_text("torrent")

    config_yaml = tmp_path / "config.yaml"
    config_yaml.write_text(
        f"""
runtime: native
docker_support: false
chown: false
mkbrr:
  binary: mkbrr
paths:
  host_data_root: {tmp_path}/data
  container_data_root: /data
  host_output_dir: {tmp_path}/torrents
  container_output_dir: /torrentfiles
  host_config_dir: {config_dir}
  container_config_dir: /root/.config/mkbrr
presets_yaml: {presets_yaml}
"""
    )

    # Monkeypatch parse_args to use our config
    monkeypatch.setattr(mkbrr_wizard, "parse_args", lambda: _mk_args(str(config_yaml)))

    # Force runtime to native
    monkeypatch.setattr(mkbrr_wizard, "pick_runtime", lambda cfg, forced: "native")

    # Sequence of Prompt.ask responses:
    # 1 -> choose_action create
    # 1 -> pick_preset default
    # content path -> content_file
    # 2 -> choose_action inspect
    # torrent path -> torrent_file
    # 3 -> choose_action check
    # torrent path -> torrent_file
    # content path -> content_file
    seq = _Seq(
        [
            "1",  # create
            "1",  # preset
            str(content_file),  # content path
            "2",  # inspect
            str(torrent_file),  # torrent path
            "3",  # check
            str(torrent_file),
            str(content_file),
            "auto",
        ]
    )
    # disable prompt_toolkit so Prompt.ask is used
    monkeypatch.setattr(mkbrr_wizard, "_has_prompt_toolkit", False)
    monkeypatch.setattr(mkbrr_wizard.Prompt, "ask", seq)

    # Confirm.ask sequence: yes to execute commands, then no to 'do another operation?'
    # Confirm.ask sequence: create confirm, inspect verbose, inspect confirm,
    # check verbose, check quiet, check confirm, final do-another -> exit
    cseq = _Seq([True, True, True, False, False, True, False])
    monkeypatch.setattr(mkbrr_wizard.Confirm, "ask", cseq)

    # Subprocess.run: simulate success returncodes
    class Dummy:
        def __init__(self, returncode=0):
            self.returncode = returncode

    monkeypatch.setattr(mkbrr_wizard.subprocess, "run", lambda *a, **k: Dummy(0))

    # Now run main() -- should finish without errors
    mkbrr_wizard.main()


def test_main_docker_mode_build_and_exit(
    tmp_path, mkbrr_wizard: ModuleType, monkeypatch: Any
) -> None:
    # simple docker-mode check: main should build docker commands and exit
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
  host_config_dir: {tmp_path}/cfg
  container_config_dir: /root/.config/mkbrr
presets_yaml: presets.yaml
"""
    )

    monkeypatch.setattr(mkbrr_wizard, "parse_args", lambda: _mk_args(str(config_yaml)))
    # Force docker runtime selection
    monkeypatch.setattr(mkbrr_wizard, "pick_runtime", lambda cfg, forced: "docker")

    # simulate minimal user flow: choose inspect then quit
    seq = _Seq(["2", "/torrentfiles/test.torrent"])
    monkeypatch.setattr(mkbrr_wizard, "_has_prompt_toolkit", False)
    monkeypatch.setattr(mkbrr_wizard.Prompt, "ask", seq)
    monkeypatch.setattr(mkbrr_wizard.Confirm, "ask", lambda *a, **k: False)

    # don't actually invoke docker; patch subprocess.run
    class Dummy:
        def __init__(self, returncode=0):
            self.returncode = returncode

    monkeypatch.setattr(mkbrr_wizard.subprocess, "run", lambda *a, **k: Dummy(0))

    mkbrr_wizard.main()
