"""Integration-style tests for main() control flow (simulate user interactions)."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Protocol, cast

import pytest  # type: ignore[import-untyped]

from .conftest import _Seq


class _Command(Protocol):
    argv: tuple[str, ...]


class _Notification(Protocol):
    event_type: str
    details: Mapping[str, object]


class _HandlerConfig(Protocol):
    chown: bool


@dataclass(frozen=True)
class _Execution:
    returncode: int
    elapsed: float


@dataclass
class _RecordingExecutor:
    elapsed: float
    commands: list[_Command] = field(default_factory=list)

    def run(self, command: _Command, *, timeout: int | None = None) -> _Execution:
        del timeout
        self.commands.append(command)
        return _Execution(returncode=0, elapsed=self.elapsed)


@dataclass
class _RecordingNotifier:
    events: list[_Notification] = field(default_factory=list)

    def notify(self, event: _Notification) -> None:
        self.events.append(event)


def _mk_args(config_path: str) -> SimpleNamespace:
    return SimpleNamespace(config=config_path, docker=False, native=False)


@pytest.fixture
def native_handler_cfg(tmp_path: Path, mkbrr_wizard: ModuleType) -> _HandlerConfig:
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
    return cast(_HandlerConfig, mkbrr_wizard.load_config(config_yaml))


def test_handle_inspect_uses_executor_and_notifies(
    tmp_path: Path,
    mkbrr_wizard: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    native_handler_cfg: _HandlerConfig,
) -> None:
    torrent_path = tmp_path / "test.torrent"
    torrent_path.write_text("torrent")
    monkeypatch.setattr(mkbrr_wizard, "ask_path", lambda *_args, **_kwargs: str(torrent_path))
    monkeypatch.setattr(mkbrr_wizard, "ask_verbose", lambda _mode: True)
    monkeypatch.setattr(mkbrr_wizard, "confirm_cmd", lambda *_args, **_kwargs: True)

    executor = _RecordingExecutor(elapsed=1.5)
    notifier = _RecordingNotifier()

    assert mkbrr_wizard.handle_inspect(native_handler_cfg, "native", executor, notifier) is True

    assert len(executor.commands) == 1
    assert executor.commands[0].argv == ("mkbrr", "inspect", str(torrent_path), "-v")
    assert len(notifier.events) == 1
    assert notifier.events[0].event_type == "inspect"
    assert notifier.events[0].details["elapsed"] == 1.5


def test_handle_inspect_rejects_missing_native_torrent(
    tmp_path: Path,
    mkbrr_wizard: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    native_handler_cfg: _HandlerConfig,
) -> None:
    missing_torrent = tmp_path / "missing.torrent"
    monkeypatch.setattr(
        mkbrr_wizard,
        "ask_path",
        lambda *_args, **_kwargs: str(missing_torrent),
    )
    executor = _RecordingExecutor(elapsed=1.5)
    notifier = _RecordingNotifier()

    assert mkbrr_wizard.handle_inspect(native_handler_cfg, "native", executor, notifier) is False

    assert executor.commands == []
    assert notifier.events == []


def test_handle_check_uses_executor_and_notifies(
    tmp_path: Path,
    mkbrr_wizard: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    native_handler_cfg: _HandlerConfig,
) -> None:
    content_path = tmp_path / "data" / "movie.mkv"
    content_path.parent.mkdir()
    content_path.write_text("x")
    torrent_path = tmp_path / "torrents" / "movie.torrent"
    torrent_path.parent.mkdir()
    torrent_path.write_text("torrent")
    monkeypatch.setattr(
        mkbrr_wizard,
        "ask_path",
        _Seq([str(torrent_path), str(content_path)]),
    )
    monkeypatch.setattr(mkbrr_wizard, "ask_verbose", lambda _mode: False)
    monkeypatch.setattr(mkbrr_wizard, "ask_quiet", lambda: False)
    monkeypatch.setattr(mkbrr_wizard, "ask_workers", lambda: 2)
    monkeypatch.setattr(mkbrr_wizard, "confirm_cmd", lambda *_args, **_kwargs: True)

    executor = _RecordingExecutor(elapsed=2.5)
    notifier = _RecordingNotifier()

    assert mkbrr_wizard.handle_check(native_handler_cfg, "native", executor, notifier) is True

    assert len(executor.commands) == 1
    assert executor.commands[0].argv == (
        "mkbrr",
        "check",
        str(torrent_path),
        str(content_path),
        "--workers",
        "2",
    )
    assert len(notifier.events) == 1
    assert notifier.events[0].event_type == "check"
    assert notifier.events[0].details["elapsed"] == 2.5


def test_handle_create_uses_executor_and_notifies(
    tmp_path: Path,
    mkbrr_wizard: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    native_handler_cfg: _HandlerConfig,
) -> None:
    content_path = tmp_path / "data" / "movie.mkv"
    content_path.parent.mkdir()
    content_path.write_text("x")
    monkeypatch.setattr(mkbrr_wizard, "pick_preset", lambda _cfg: "scene")
    monkeypatch.setattr(mkbrr_wizard, "ask_path", lambda *_args, **_kwargs: str(content_path))
    monkeypatch.setattr(mkbrr_wizard, "scan_episodes", lambda _path: [])
    monkeypatch.setattr(mkbrr_wizard, "detect_storage_type", lambda *_args, **_kwargs: "ssd")
    monkeypatch.setattr(mkbrr_wizard, "resolve_workers", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(mkbrr_wizard, "confirm_cmd", lambda *_args, **_kwargs: True)
    executor = _RecordingExecutor(elapsed=3.5)
    notifier = _RecordingNotifier()

    assert mkbrr_wizard.handle_create(native_handler_cfg, "native", executor, notifier) is True

    assert len(executor.commands) == 1
    assert executor.commands[0].argv[:5] == ("mkbrr", "create", str(content_path), "-P", "scene")
    assert "--output-dir" in executor.commands[0].argv
    assert "--workers" in executor.commands[0].argv
    assert executor.commands[0].argv[-2:] == ("--workers", "0")
    assert len(notifier.events) == 1
    assert notifier.events[0].event_type == "create"
    assert notifier.events[0].details["elapsed"] == 3.5


def test_handle_split_series_rejects_preset_include_patterns(
    tmp_path: Path,
    mkbrr_wizard: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    native_handler_cfg: _HandlerConfig,
) -> None:
    presets_dir = tmp_path / "cfg"
    presets_dir.mkdir()
    (presets_dir / "presets.yaml").write_text(
        "default:\n  include_patterns:\n    - '*.mkv'\npresets:\n  scene: {}\n"
    )
    monkeypatch.setattr(
        mkbrr_wizard.Prompt,
        "ask",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("range prompt should not run")
        ),
    )
    executor = _RecordingExecutor(elapsed=1.0)
    notifier = _RecordingNotifier()

    assert (
        mkbrr_wizard.handle_split_series(
            native_handler_cfg,
            "native",
            executor,
            notifier,
            preset="scene",
            raw=str(tmp_path / "data" / "Show.S01"),
            content_path=str(tmp_path / "data" / "Show.S01"),
            host_data_root_override=None,
            episodes=[((1, 1), "Show.S01E01.mkv"), ((1, 2), "Show.S01E02.mkv")],
            episode_keys=[(1, 1), (1, 2)],
        )
        is False
    )

    assert executor.commands == []
    assert notifier.events == []


def test_main_create_inspect_check_native(
    tmp_path: Path, mkbrr_wizard: ModuleType, monkeypatch: pytest.MonkeyPatch
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
    monkeypatch.setattr(mkbrr_wizard, "pick_runtime", lambda _cfg, _forced: "native")

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

    monkeypatch.setattr(mkbrr_wizard.subprocess, "run", lambda *_args, **_kwargs: Dummy(0))

    # Now run main() -- should finish without errors
    mkbrr_wizard.main()


def test_main_docker_mode_build_and_exit(
    tmp_path: Path, mkbrr_wizard: ModuleType, monkeypatch: pytest.MonkeyPatch
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
    monkeypatch.setattr(mkbrr_wizard, "pick_runtime", lambda _cfg, _forced: "docker")

    # simulate minimal user flow: choose inspect then quit
    seq = _Seq(["2", "/torrentfiles/test.torrent"])
    monkeypatch.setattr(mkbrr_wizard, "_has_prompt_toolkit", False)
    monkeypatch.setattr(mkbrr_wizard.Prompt, "ask", seq)
    monkeypatch.setattr(mkbrr_wizard.Confirm, "ask", lambda *_args, **_kwargs: False)

    # don't actually invoke docker; patch subprocess.run
    class Dummy:
        def __init__(self, returncode=0):
            self.returncode = returncode

    monkeypatch.setattr(mkbrr_wizard.subprocess, "run", lambda *_args, **_kwargs: Dummy(0))

    mkbrr_wizard.main()
