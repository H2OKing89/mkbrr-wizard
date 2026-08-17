"""Load the subset of mkbrr preset values needed by effective plans.

mkbrr remains the authority for executing presets. This module provides a
strict, UI-neutral view of the values that the wizard displays and uses when it
fingerprints resumable operations.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from mkbrr_wizard.batch_models import PIECE_LENGTH_MAX, PIECE_LENGTH_MIN


class PresetValues(BaseModel):
    """Supported effective values from mkbrr's ``default`` and preset blocks."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, validate_default=True)

    trackers: tuple[str, ...] = ()
    webseeds: tuple[str, ...] = ()
    private: bool | None = None
    source: str | None = None
    comment: str | None = None
    output_dir: str | None = None
    piece_length: int | None = Field(default=None, ge=PIECE_LENGTH_MIN, le=PIECE_LENGTH_MAX)
    max_piece_length: int | None = Field(
        default=None,
        ge=PIECE_LENGTH_MIN,
        le=PIECE_LENGTH_MAX,
    )
    target_piece_count: int | None = Field(default=None, gt=0)
    workers: int | None = Field(default=None, ge=0)
    include_patterns: tuple[str, ...] = ()
    exclude_patterns: tuple[str, ...] = ()
    no_date: bool | None = None
    no_creator: bool | None = None
    entropy: bool | None = None
    skip_prefix: bool | None = None
    fail_on_season_warning: bool | None = None

    @field_validator(
        "trackers",
        "webseeds",
        "include_patterns",
        "exclude_patterns",
        mode="before",
    )
    @classmethod
    def _accept_yaml_arrays(cls, value: Any) -> Any:
        if isinstance(value, list):
            return tuple(value)
        return value

    @field_validator(
        "trackers",
        "webseeds",
        "include_patterns",
        "exclude_patterns",
    )
    @classmethod
    def _reject_blank_array_items(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() for item in value):
            raise ValueError("entries must not be blank")
        return value

    @field_validator("piece_length", "max_piece_length", "target_piece_count", mode="before")
    @classmethod
    def _normalize_unset_numeric_option(cls, value: Any) -> Any:
        # mkbrr decodes these YAML fields as unsigned integers. Zero is valid
        # and means that the option is unset rather than an out-of-range value.
        if type(value) is int and value == 0:
            return None
        return value


def _mapping(value: object, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a mapping")
    return value


def _validate_values(values: Mapping[str, Any], *, label: str) -> PresetValues:
    try:
        return PresetValues.model_validate(values)
    except ValidationError as error:
        first = error.errors(include_url=False, include_input=False)[0]
        location = ".".join(str(item) for item in first.get("loc", ())) or "root"
        raise ValueError(f"{label} has invalid {location}: {first['msg']}") from error


def _merge_preset_values(defaults: PresetValues, selected: PresetValues) -> PresetValues:
    """Mirror mkbrr v1.24.1's conditional ``GetPreset`` merge."""

    effective = PresetValues(
        private=True,
        no_date=False,
        no_creator=False,
        skip_prefix=False,
        workers=0,
    )

    default_updates: dict[str, Any] = {
        "trackers": defaults.trackers,
        "webseeds": defaults.webseeds,
    }
    for field_name in (
        "private",
        "source",
        "comment",
        "output_dir",
        "piece_length",
        "max_piece_length",
        "target_piece_count",
        "workers",
        "no_date",
        "no_creator",
        "entropy",
        "skip_prefix",
        "fail_on_season_warning",
    ):
        value = getattr(defaults, field_name)
        if value is not None:
            default_updates[field_name] = value
    if defaults.include_patterns:
        default_updates["include_patterns"] = defaults.include_patterns
    if defaults.exclude_patterns:
        default_updates["exclude_patterns"] = defaults.exclude_patterns
    effective = effective.model_copy(update=default_updates)

    selected_updates: dict[str, Any] = {}
    for field_name in ("trackers", "webseeds", "include_patterns", "exclude_patterns"):
        value = getattr(selected, field_name)
        if value:
            selected_updates[field_name] = value
    for field_name in ("source", "comment", "output_dir"):
        value = getattr(selected, field_name)
        if value:
            selected_updates[field_name] = value
    for field_name in (
        "private",
        "no_date",
        "no_creator",
        "entropy",
        "skip_prefix",
        "fail_on_season_warning",
    ):
        value = getattr(selected, field_name)
        if value is not None:
            selected_updates[field_name] = value
    # mkbrr v1.24.1 treats zero in a named preset as "not set" and keeps the
    # default block's value; only a nonzero selected value overrides it.
    if selected.workers:
        selected_updates["workers"] = selected.workers
    if selected.max_piece_length is not None:
        selected_updates["max_piece_length"] = selected.max_piece_length
    if selected.piece_length is not None:
        selected_updates["piece_length"] = selected.piece_length
        selected_updates["target_piece_count"] = None
    if selected.target_piece_count is not None:
        # A selected target_piece_count clears a default or selected piece_length.
        # Any remaining conflict comes from defaults and is rejected below.
        selected_updates["target_piece_count"] = selected.target_piece_count
        selected_updates["piece_length"] = None
    effective = effective.model_copy(update=selected_updates)
    if effective.piece_length is not None and effective.target_piece_count is not None:
        raise ValueError(
            "Effective preset has both piece_length and target_piece_count; choose one"
        )
    return effective


def load_preset_values(path: str | Path, name: str) -> PresetValues:
    """Load and merge mkbrr's global defaults with one named preset."""

    preset_path = Path(path).expanduser()
    try:
        raw = yaml.safe_load(preset_path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ValueError(f"Could not read presets file {preset_path}: {error}") from error
    except yaml.YAMLError as error:
        raise ValueError(f"Could not parse presets file {preset_path}: {error}") from error

    root = _mapping(raw, label=f"Presets file {preset_path}")
    version = root.get("version")
    if type(version) is not int or version != 1:
        raise ValueError(f"Presets file {preset_path} version must be the integer 1")
    defaults_value = root.get("default")
    defaults_raw = _mapping(
        {} if defaults_value is None else defaults_value,
        label="Preset default block",
    )
    presets = _mapping(root.get("presets"), label="Preset 'presets' block")
    if name not in presets:
        available = ", ".join(sorted(str(value) for value in presets)) or "none"
        raise ValueError(f"Preset {name!r} was not found in {preset_path} (available: {available})")
    selected_value = presets[name]
    selected_raw = _mapping(
        {} if selected_value is None else selected_value,
        label=f"Preset {name!r}",
    )

    defaults = _validate_values(defaults_raw, label="Preset default block")
    selected = _validate_values(selected_raw, label=f"Preset {name!r}")
    return _merge_preset_values(defaults, selected)


__all__ = ["PresetValues", "load_preset_values"]
