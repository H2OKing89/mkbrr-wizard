"""Typed batch-manifest models and their generated JSON Schema.

These models are deliberately independent of the interactive UI and execution
backend.  Loading a manifest through :class:`BatchManifest` is therefore the
single validation boundary for interactive, file-based, and future headless
batch workflows.
"""

from __future__ import annotations

import json
import posixpath
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    WithJsonSchema,
    field_validator,
    model_validator,
)

JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
PIECE_LENGTH_MIN = 16
PIECE_LENGTH_MAX = 27
TARGET_PIECE_COUNT_MAX = (1 << 64) - 1

_SEQUENCE_FIELDS = (
    "trackers",
    "webseeds",
    "exclude_patterns",
    "include_patterns",
)
_TRACKER_SCHEMES = frozenset({"http", "https", "udp", "ws", "wss"})
_WEBSEED_SCHEMES = frozenset({"http", "https"})


def _validate_absolute_path(value: str) -> str:
    """Validate a Linux host/container path without touching the filesystem."""
    if "\x00" in value:
        raise ValueError("path must not contain a NUL byte")
    if value != value.strip():
        raise ValueError("path must not have surrounding whitespace")
    if not value.startswith("/"):
        raise ValueError("path must be absolute")
    return value


def _validate_argv_text(value: str) -> str:
    """Reject text that cannot be represented safely as one subprocess argument."""
    if "\x00" in value:
        raise ValueError("text must not contain a NUL byte")
    if value != value.strip():
        raise ValueError("text must not have surrounding whitespace")
    return value


def _validate_uri(value: str, *, schemes: frozenset[str], kind: str) -> str:
    """Validate a network URI while preserving the exact user-provided value."""
    if any(character.isspace() or ord(character) < 0x20 for character in value):
        raise ValueError(f"{kind} URI must not contain whitespace or control characters")

    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        # Accessing ``port`` makes urllib validate malformed and out-of-range ports.
        _ = parsed.port
    except ValueError as error:
        raise ValueError(f"invalid {kind} URI: {error}") from error

    scheme = parsed.scheme.lower()
    if scheme not in schemes:
        allowed = ", ".join(sorted(schemes))
        raise ValueError(f"{kind} URI scheme must be one of: {allowed}")
    if not parsed.netloc or not hostname:
        raise ValueError(f"{kind} URI must include a host")
    return value


def _validate_tracker_uri(value: str) -> str:
    return _validate_uri(value, schemes=_TRACKER_SCHEMES, kind="tracker")


def _validate_webseed_uri(value: str) -> str:
    return _validate_uri(value, schemes=_WEBSEED_SCHEMES, kind="webseed")


AbsolutePath = Annotated[
    str,
    StringConstraints(strict=True, min_length=1),
    AfterValidator(_validate_absolute_path),
    WithJsonSchema(
        {
            "type": "string",
            "minLength": 1,
            "pattern": r"^/(?:[\s\S]*\S)?$",
            "not": {"pattern": r"(?:\u0000|\s$)"},
            "format": "absolute-path",
        }
    ),
]
TorrentOutputPath = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=1,
        pattern=r"^/[\s\S]*\.torrent$",
    ),
    AfterValidator(_validate_absolute_path),
    WithJsonSchema(
        {
            "type": "string",
            "minLength": 1,
            "pattern": r"^/[\s\S]*\.torrent$",
            "not": {"pattern": r"(?:\u0000|\s$)"},
            "format": "absolute-path",
        }
    ),
]
CleanText = Annotated[
    str,
    StringConstraints(strict=True),
    AfterValidator(_validate_argv_text),
    WithJsonSchema(
        {
            "type": "string",
            "pattern": r"^(?:$|\S(?:[\s\S]*\S)?)$",
            "not": {"pattern": r"(?:\u0000|\s$)"},
        }
    ),
]
_NonEmptyArgumentText = Annotated[
    str,
    StringConstraints(strict=True, min_length=1),
    AfterValidator(_validate_argv_text),
    WithJsonSchema(
        {
            "type": "string",
            "minLength": 1,
            "pattern": r"^\S(?:[\s\S]*\S)?$",
            "not": {"pattern": r"(?:\u0000|\s$)"},
        }
    ),
]
TrackerURI = Annotated[
    str,
    StringConstraints(strict=True, min_length=1),
    AfterValidator(_validate_tracker_uri),
    WithJsonSchema(
        {
            "type": "string",
            "format": "uri",
            "minLength": 1,
            "pattern": (
                r"^(?:[Hh][Tt][Tt][Pp][Ss]?|[Uu][Dd][Pp]|[Ww][Ss][Ss]?)://"
                r"[^\s\u0000-\u001F/?#]+[^\s\u0000-\u001F]*$"
            ),
            "not": {"pattern": r"[\s\u0000-\u001F]"},
        }
    ),
]
WebseedURI = Annotated[
    str,
    StringConstraints(strict=True, min_length=1),
    AfterValidator(_validate_webseed_uri),
    WithJsonSchema(
        {
            "type": "string",
            "format": "uri",
            "minLength": 1,
            "pattern": (r"^[Hh][Tt][Tt][Pp][Ss]?://" r"[^\s\u0000-\u001F/?#]+[^\s\u0000-\u001F]*$"),
            "not": {"pattern": r"[\s\u0000-\u001F]"},
        }
    ),
]


class BatchJob(BaseModel):
    """One independently validated ``mkbrr create`` operation."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
        json_schema_extra={
            "allOf": [
                {
                    "not": {
                        "properties": {
                            "piece_length": {"type": "integer"},
                            "target_piece_count": {"type": "integer"},
                        },
                        "required": ["piece_length", "target_piece_count"],
                    }
                }
            ]
        },
    )

    path: AbsolutePath = Field(description="Absolute host or container source path")
    output: TorrentOutputPath = Field(description="Absolute .torrent output path")
    trackers: tuple[TrackerURI, ...] = Field(
        default_factory=tuple,
        description="Tracker announce URIs",
    )
    webseeds: tuple[WebseedURI, ...] = Field(
        default_factory=tuple,
        description="HTTP(S) webseed URIs",
    )
    private: bool | None = Field(default=None, description="Override the preset privacy flag")
    no_date: bool | None = Field(default=None, description="Omit the creation date")
    entropy: bool | None = Field(default=None, description="Add entropy to the info dictionary")
    skip_prefix: bool | None = Field(default=None, description="Do not prefix output with tracker")
    fail_on_season_warning: bool | None = Field(
        default=None,
        description="Fail when a season pack appears incomplete",
    )
    no_creator: bool | None = Field(default=None, description="Omit the creator string")
    piece_length: int | None = Field(
        default=None,
        ge=PIECE_LENGTH_MIN,
        le=PIECE_LENGTH_MAX,
        description="Explicit piece-length exponent",
    )
    max_piece_length: int | None = Field(
        default=None,
        ge=PIECE_LENGTH_MIN,
        le=PIECE_LENGTH_MAX,
        description="Maximum automatically selected piece-length exponent",
    )
    target_piece_count: int | None = Field(
        default=None,
        gt=0,
        le=TARGET_PIECE_COUNT_MAX,
        description="Approximate target number of pieces",
    )
    name: CleanText = Field(default="", description="Override the torrent metadata name")
    comment: CleanText | None = Field(
        default=None,
        description="Torrent comment override; empty explicitly clears a preset value",
    )
    source: CleanText | None = Field(
        default=None,
        description="Torrent source override; empty also disables tracker inference",
    )
    exclude_patterns: tuple[_NonEmptyArgumentText, ...] = Field(
        default_factory=tuple,
        description="Glob patterns to exclude",
    )
    include_patterns: tuple[_NonEmptyArgumentText, ...] = Field(
        default_factory=tuple,
        description="Glob patterns to include",
    )

    @model_validator(mode="before")
    @classmethod
    def _normalize_supported_strings(cls, value: Any) -> Any:
        """Normalize manifest text consistently for every validation entry point."""
        if not isinstance(value, Mapping):
            return value

        normalized = dict(value)
        for field_name in ("path", "output", "name", "comment", "source"):
            field_value = normalized.get(field_name)
            if isinstance(field_value, str):
                normalized[field_name] = field_value.strip()
        for field_name in _SEQUENCE_FIELDS:
            field_value = normalized.get(field_name)
            if isinstance(field_value, list | tuple):
                normalized[field_name] = tuple(
                    item.strip() if isinstance(item, str) else item for item in field_value
                )
        return normalized

    @field_validator(*_SEQUENCE_FIELDS, mode="before")
    @classmethod
    def _accept_manifest_arrays(cls, value: Any) -> Any:
        """Convert JSON/YAML arrays while retaining strict element validation."""
        if isinstance(value, list | tuple):
            return tuple(value)
        return value

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> BatchJob:
        """Compatibility constructor with concise errors for CLI callers."""
        try:
            return cls.model_validate(raw)
        except ValidationError as error:
            first = error.errors(include_url=False, include_input=False)[0]
            location = first.get("loc") or ()
            if not location:
                raise ValueError(f"Invalid batch job: {first['msg']}") from error
            field_name = str(location[0])
            raw_value = raw.get(field_name)
            if field_name == "path" and (not isinstance(raw_value, str) or not raw_value.strip()):
                raise ValueError("Batch job content path cannot be empty") from error
            if field_name == "output" and (not isinstance(raw_value, str) or not raw_value.strip()):
                raise ValueError("Batch job output cannot be empty") from error
            if field_name in {"piece_length", "max_piece_length"}:
                if isinstance(raw_value, bool) or not isinstance(raw_value, int):
                    raise ValueError(f"Batch job {field_name} must be an integer") from error
                raise ValueError(
                    f"Batch job {field_name} must be between "
                    f"{PIECE_LENGTH_MIN} and {PIECE_LENGTH_MAX}"
                ) from error
            if field_name in _SEQUENCE_FIELDS and (
                not isinstance(raw_value, list | tuple)
                or not all(isinstance(item, str) for item in raw_value)
            ):
                raise ValueError(f"Batch job {field_name} must be a list of strings") from error
            if field_name in {"name", "comment", "source"} and not isinstance(raw_value, str):
                raise ValueError(f"Batch job {field_name} must be a string") from error
            raise ValueError(f"Invalid batch job {field_name}: {first['msg']}") from error

    @model_validator(mode="after")
    def _validate_piece_strategy(self) -> BatchJob:
        if self.piece_length is not None and self.target_piece_count is not None:
            raise ValueError(
                "piece_length and target_piece_count are mutually exclusive; choose one"
            )
        return self


class BatchManifest(BaseModel):
    """Versioned collection of batch jobs with manifest-wide invariants."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
        title="mkbrr Batch Manifest",
        json_schema_extra={"$schema": JSON_SCHEMA_DIALECT},
    )

    version: Literal[1] = Field(description="Batch manifest format version")
    jobs: Annotated[tuple[BatchJob, ...], Field(min_length=1)] = Field(
        description="Torrent creation jobs"
    )

    @field_validator("jobs", mode="before")
    @classmethod
    def _accept_jobs_array(cls, value: Any) -> Any:
        if isinstance(value, list):
            return tuple(value)
        return value

    @field_validator("version", mode="before")
    @classmethod
    def _require_strict_version_one(cls, value: Any) -> Any:
        # Pydantic's Literal[1] intentionally follows Python equality, where
        # True == 1 and 1.0 == 1. Manifest versions must be actual integers.
        if type(value) is not int or value != 1:
            raise ValueError("version must be the integer 1")
        return value

    @model_validator(mode="after")
    def _reject_duplicate_outputs(self) -> BatchManifest:
        seen: dict[str, int] = {}
        for index, job in enumerate(self.jobs):
            normalized = _normalize_output_path(job.output)
            previous = seen.get(normalized)
            if previous is not None:
                raise ValueError(
                    f"jobs.{index}.output duplicates jobs.{previous}.output "
                    f"after path normalization: {normalized}"
                )
            seen[normalized] = index
        return self


def _normalize_output_path(value: str) -> str:
    """Return a stable lexical Linux path for collision checks."""
    normalized = posixpath.normpath(value)
    # POSIX permits an implementation-defined meaning for exactly two leading
    # slashes. Host and container paths in this application treat them as root.
    return f"/{normalized.lstrip('/')}" if normalized != "/" else "/"


def generate_batch_json_schema() -> dict[str, Any]:
    """Generate the batch JSON Schema directly from the validation models."""
    return BatchManifest.model_json_schema(mode="validation")


def export_batch_json_schema(destination: str | Path, *, indent: int = 2) -> Path:
    """Write the generated batch schema and return its resolved destination."""
    if indent < 0:
        raise ValueError("indent must be non-negative")

    destination_path = Path(destination).expanduser()
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    destination_path.write_text(
        f"{json.dumps(generate_batch_json_schema(), indent=indent)}\n",
        encoding="utf-8",
    )
    return destination_path.resolve(strict=False)


__all__ = [
    "BatchJob",
    "BatchManifest",
    "JSON_SCHEMA_DIALECT",
    "PIECE_LENGTH_MAX",
    "PIECE_LENGTH_MIN",
    "TARGET_PIECE_COUNT_MAX",
    "export_batch_json_schema",
    "generate_batch_json_schema",
]
