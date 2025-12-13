"""Tests for interactive prompts and confirm/choose.
"""

from types import ModuleType
from typing import Any

import pytest  # type: ignore[import-untyped]


def test_choose_action_prompts(mkbrr_wizard: ModuleType, monkeypatch: Any) -> None:
    # Choose '1' => create
    monkeypatch.setattr(mkbrr_wizard.Prompt, "ask", lambda prompt, **k: "1")
    assert mkbrr_wizard.choose_action() == "create"

    # Choose '2' => inspect
    monkeypatch.setattr(mkbrr_wizard.Prompt, "ask", lambda prompt, **k: "2")
    assert mkbrr_wizard.choose_action() == "inspect"

    # Choose '3' => check
    monkeypatch.setattr(mkbrr_wizard.Prompt, "ask", lambda prompt, **k: "3")
    assert mkbrr_wizard.choose_action() == "check"

    # Choose 'q' => SystemExit
    monkeypatch.setattr(mkbrr_wizard.Prompt, "ask", lambda prompt, **k: "q")
    with pytest.raises(SystemExit):
        mkbrr_wizard.choose_action()


def test_ask_path_strips_and_errors(mkbrr_wizard: ModuleType, monkeypatch: Any) -> None:
    # simulate Prompt.ask returning a quoted path
    monkeypatch.setattr(mkbrr_wizard.Prompt, "ask", lambda prompt, **k: "  '/tmp/file'  ")
    res = mkbrr_wizard.ask_path("Prompt")
    assert res == "/tmp/file"

    # simulate empty input -> SystemExit
    monkeypatch.setattr(mkbrr_wizard.Prompt, "ask", lambda prompt, **k: "  ")
    with pytest.raises(SystemExit):
        mkbrr_wizard.ask_path("Prompt")


def test_confirm_cmd(monkeypatch: Any, mkbrr_wizard: ModuleType) -> None:
    # simulate Confirm.ask returning False
    monkeypatch.setattr(mkbrr_wizard.Confirm, "ask", lambda *a, **k: False)
    assert mkbrr_wizard.confirm_cmd(["echo", "hi"], cwd=None) is False

    # simulate Confirm.ask returning True
    monkeypatch.setattr(mkbrr_wizard.Confirm, "ask", lambda *a, **k: True)
    assert mkbrr_wizard.confirm_cmd(["echo", "hi"], cwd=None) is True
