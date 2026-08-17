"""Focused coverage for the richer interactive batch form."""

from __future__ import annotations

from types import ModuleType
from typing import Any


class RecordingAnswers:
    """Return scripted prompt answers while retaining the prompt text."""

    def __init__(self, answers: list[str]) -> None:
        self.remaining_answers = list(answers)
        self.prompts: list[str] = []

    def __call__(self, prompt: str, *args: Any, **kwargs: Any) -> str:
        del args, kwargs
        self.prompts.append(prompt)
        return self.remaining_answers.pop(0)


def test_advanced_optional_settings_collects_extended_batch_fields(
    mkbrr_wizard: ModuleType,
    monkeypatch: Any,
) -> None:
    answers = RecordingAnswers(
        [
            "",  # trackers
            "skip",  # private
            "",  # piece_length: leave unset so target_piece_count is available
            "",  # comment
            "",  # source
            "skip",  # entropy
            "skip",  # no_date
            "",  # webseeds
            "",  # exclude_patterns
            "",  # include_patterns
            "Season One",  # name
            "24",  # max_piece_length
            "900",  # target_piece_count
            "y",  # no_creator
            "n",  # skip_prefix
            "y",  # fail_on_season_warning
        ]
    )
    monkeypatch.setattr(mkbrr_wizard.Prompt, "ask", answers)

    result = mkbrr_wizard._collect_job_optional_settings(None, 1)

    assert answers.remaining_answers == []
    assert result == {
        "name": "Season One",
        "max_piece_length": 24,
        "target_piece_count": 900,
        "no_creator": True,
        "skip_prefix": False,
        "fail_on_season_warning": True,
    }


def test_explicit_piece_length_suppresses_target_piece_count_prompt(
    mkbrr_wizard: ModuleType,
    monkeypatch: Any,
) -> None:
    answers = RecordingAnswers(
        [
            "",  # trackers
            "skip",  # private
            "18",  # piece_length
            "",  # comment
            "",  # source
            "skip",  # entropy
            "skip",  # no_date
            "",  # webseeds
            "",  # exclude_patterns
            "",  # include_patterns
            "",  # name
            "",  # max_piece_length
            "skip",  # no_creator
            "skip",  # skip_prefix
            "skip",  # fail_on_season_warning
        ]
    )
    monkeypatch.setattr(mkbrr_wizard.Prompt, "ask", answers)

    result = mkbrr_wizard._collect_job_optional_settings(None, 1)

    assert result == {"piece_length": 18}
    assert not any("Target piece count" in prompt for prompt in answers.prompts)
