"""Misc utility tests to increase coverage for various helper functions."""

from types import ModuleType
from typing import Any


def test_docker_available_true(monkeypatch: Any, mkbrr_wizard: ModuleType) -> None:
    class Dummy:
        returncode = 0

    monkeypatch.setattr(mkbrr_wizard.subprocess, "run", lambda *a, **k: Dummy())
    assert mkbrr_wizard.docker_available() is True


def test_docker_available_false(monkeypatch: Any, mkbrr_wizard: ModuleType) -> None:
    class Dummy:
        returncode = 1

    monkeypatch.setattr(mkbrr_wizard.subprocess, "run", lambda *a, **k: Dummy())
    assert mkbrr_wizard.docker_available() is False


def test_native_available_true(monkeypatch: Any, mkbrr_wizard: ModuleType) -> None:
    monkeypatch.setattr(mkbrr_wizard.shutil, "which", lambda bin: "/usr/local/bin/mkbrr")
    assert mkbrr_wizard.native_available("mkbrr") is True


def test_native_available_false(monkeypatch: Any, mkbrr_wizard: ModuleType) -> None:
    monkeypatch.setattr(mkbrr_wizard.shutil, "which", lambda bin: None)
    assert mkbrr_wizard.native_available("mkbrr") is False


def test_confirm_cmd_with_cwd(monkeypatch: Any, mkbrr_wizard: ModuleType) -> None:
    # Simulate Confirm.ask returning False
    monkeypatch.setattr(mkbrr_wizard.Confirm, "ask", lambda *a, **k: False)
    res = mkbrr_wizard.confirm_cmd(["echo", "hi"], cwd="/tmp")
    assert res is False


def test_ask_workers_inputs(monkeypatch: Any, mkbrr_wizard: ModuleType) -> None:
    # default auto returns None
    monkeypatch.setattr(mkbrr_wizard.Prompt, "ask", lambda *a, **k: "auto")
    assert mkbrr_wizard.ask_workers() is None

    # valid integer
    monkeypatch.setattr(mkbrr_wizard.Prompt, "ask", lambda *a, **k: "3")
    assert mkbrr_wizard.ask_workers() == 3

    # invalid integer should print warning and return None
    monkeypatch.setattr(mkbrr_wizard.Prompt, "ask", lambda *a, **k: "bad")
    assert mkbrr_wizard.ask_workers() is None
