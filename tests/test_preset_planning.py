from __future__ import annotations

from pathlib import Path

import pytest

from mkbrr_wizard.planning.presets import load_preset_values


def test_preset_merge_matches_mkbrr_conditional_overrides(tmp_path: Path) -> None:
    presets = tmp_path / "presets.yaml"
    presets.write_text(
        """\
version: 1
default:
  trackers:
    - https://default.example/announce
  webseeds:
    - https://seed.example/default
  private: false
  piece_length: 18
  include_patterns:
    - "*.mkv"
presets:
  release:
    trackers: []
    webseeds: []
    target_piece_count: 900
    include_patterns: []
""",
        encoding="utf-8",
    )

    values = load_preset_values(presets, "release")

    assert values.trackers == ("https://default.example/announce",)
    assert values.webseeds == ("https://seed.example/default",)
    assert values.private is False
    assert values.piece_length is None
    assert values.target_piece_count == 900
    assert values.include_patterns == ("*.mkv",)


def test_preset_hard_defaults_match_mkbrr(tmp_path: Path) -> None:
    presets = tmp_path / "presets.yaml"
    presets.write_text("version: 1\npresets:\n  release: {}\n", encoding="utf-8")

    values = load_preset_values(presets, "release")

    assert values.private is True
    assert values.no_date is False
    assert values.no_creator is False
    assert values.skip_prefix is False
    assert values.workers == 0


def test_selected_zero_workers_inherits_nonzero_default_like_mkbrr(tmp_path: Path) -> None:
    presets = tmp_path / "presets.yaml"
    presets.write_text(
        """\
version: 1
default:
  workers: 8
presets:
  release:
    workers: 0
""",
        encoding="utf-8",
    )

    values = load_preset_values(presets, "release")

    assert values.workers == 8


@pytest.mark.parametrize("version_line", ["", "version: 2\n", "version: '1'\n"])
def test_preset_file_requires_integer_version_one(tmp_path: Path, version_line: str) -> None:
    presets = tmp_path / "presets.yaml"
    presets.write_text(f"{version_line}presets:\n  release: {{}}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="version must be the integer 1"):
        load_preset_values(presets, "release")


def test_preset_rejects_fields_mkbrr_does_not_support(tmp_path: Path) -> None:
    presets = tmp_path / "presets.yaml"
    presets.write_text(
        "version: 1\npresets:\n  release:\n    announce: https://invalid.example/announce\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="announce"):
        load_preset_values(presets, "release")


def test_zero_piece_options_are_valid_unset_values(tmp_path: Path) -> None:
    presets = tmp_path / "presets.yaml"
    presets.write_text(
        """\
version: 1
default:
  piece_length: 0
  max_piece_length: 0
  target_piece_count: 0
presets:
  release: {}
""",
        encoding="utf-8",
    )

    values = load_preset_values(presets, "release")

    assert values.piece_length is None
    assert values.max_piece_length is None
    assert values.target_piece_count is None


@pytest.mark.parametrize("invalid_block", ["false", "[]", "workers: auto"])
def test_preset_rejects_values_mkbrr_yaml_cannot_decode(
    tmp_path: Path,
    invalid_block: str,
) -> None:
    presets = tmp_path / "presets.yaml"
    presets.write_text(
        f"version: 1\npresets:\n  release: {invalid_block}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError):
        load_preset_values(presets, "release")


def test_default_piece_strategies_must_not_conflict(tmp_path: Path) -> None:
    presets = tmp_path / "presets.yaml"
    presets.write_text(
        """\
version: 1
default:
  piece_length: 18
  target_piece_count: 900
presets:
  release: {}
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="both piece_length and target_piece_count"):
        load_preset_values(presets, "release")


def test_selected_target_strategy_overrides_selected_piece_strategy(tmp_path: Path) -> None:
    presets = tmp_path / "presets.yaml"
    presets.write_text(
        """\
version: 1
presets:
  release:
    piece_length: 18
    target_piece_count: 900
""",
        encoding="utf-8",
    )

    values = load_preset_values(presets, "release")

    assert values.piece_length is None
    assert values.target_piece_count == 900


def test_preset_rejects_blank_sequence_entries(tmp_path: Path) -> None:
    presets = tmp_path / "presets.yaml"
    presets.write_text(
        "version: 1\npresets:\n  release:\n    trackers: ['']\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="entries must not be blank"):
        load_preset_values(presets, "release")
