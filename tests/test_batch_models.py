"""Focused tests for the package-native batch manifest source of truth."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from mkbrr_wizard.batch_models import (
    JSON_SCHEMA_DIALECT,
    TARGET_PIECE_COUNT_MAX,
    BatchJob,
    BatchManifest,
    export_batch_json_schema,
    generate_batch_json_schema,
)


def _job(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "path": "/data/media/movie.mkv",
        "output": "/torrentfiles/movie.torrent",
    }
    values.update(overrides)
    return values


def test_manifest_supports_current_and_extended_job_fields() -> None:
    manifest = BatchManifest.model_validate(
        {
            "version": 1,
            "jobs": [
                _job(
                    trackers=[
                        "https://tracker.example/announce?passkey=abc",
                        "udp://tracker.example:6969/announce",
                    ],
                    webseeds=["https://seed.example/movie.mkv"],
                    private=True,
                    no_date=False,
                    entropy=True,
                    skip_prefix=True,
                    fail_on_season_warning=False,
                    no_creator=True,
                    piece_length=16,
                    max_piece_length=27,
                    name="Display Name",
                    comment="release comment",
                    source="BLURAY",
                    exclude_patterns=["*.nfo"],
                    include_patterns=["*.mkv"],
                ),
                _job(
                    path="/mnt/user/media/second.mkv",
                    output="/mnt/user/torrents/second.torrent",
                    target_piece_count=900,
                    max_piece_length=24,
                ),
            ],
        }
    )

    first, second = manifest.jobs
    assert isinstance(first, BatchJob)
    assert first.trackers == (
        "https://tracker.example/announce?passkey=abc",
        "udp://tracker.example:6969/announce",
    )
    assert first.webseeds == ("https://seed.example/movie.mkv",)
    assert first.name == "Display Name"
    assert first.comment == "release comment"
    assert first.source == "BLURAY"
    assert first.exclude_patterns == ("*.nfo",)
    assert first.include_patterns == ("*.mkv",)
    assert first.no_creator is True
    assert first.piece_length == 16
    assert first.max_piece_length == 27
    assert second.target_piece_count == 900


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(
            {"version": 1, "jobs": [_job(unknown_option=True)]},
            id="unknown-job-field",
        ),
        pytest.param(
            {"version": 1, "jobs": [_job()], "unexpected": True},
            id="unknown-manifest-field",
        ),
    ],
)
def test_manifest_forbids_unknown_fields(payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        BatchManifest.model_validate(payload)


@pytest.mark.parametrize("version", [True, 1.0, "1", 2])
def test_manifest_version_is_strictly_integer_one(version: Any) -> None:
    with pytest.raises(ValidationError, match="version must be the integer 1"):
        BatchManifest.model_validate({"version": version, "jobs": [_job()]})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("path", "relative/movie.mkv", id="relative-content"),
        pytest.param("output", "movie.torrent", id="relative-output"),
        pytest.param("output", "/torrentfiles/movie", id="missing-torrent-suffix"),
        pytest.param("path", "   ", id="blank-content"),
        pytest.param("output", "\x00/bad.torrent", id="nul-output"),
    ],
)
def test_job_requires_nonempty_absolute_paths(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        BatchJob.model_validate(_job(**{field: value}))


def test_path_validation_is_lexical_and_does_not_require_existing_paths() -> None:
    job = BatchJob.model_validate(
        _job(
            path="/host/path/that/does/not/exist",
            output="/container/output/that/does/not/exist.torrent",
        )
    )

    assert job.path == "/host/path/that/does/not/exist"
    assert job.output == "/container/output/that/does/not/exist.torrent"


def test_every_model_entry_point_trims_supported_strings() -> None:
    raw = _job(
        path=" /data/media/movie.mkv ",
        output=" /torrentfiles/movie.torrent ",
        trackers=[" https://tracker.example/announce "],
        webseeds=[" https://seed.example/movie.mkv "],
        name=" Display Name ",
        comment=" release comment ",
        source=" BLURAY ",
        exclude_patterns=[" *.nfo "],
        include_patterns=[" *.mkv "],
    )

    direct = BatchJob.model_validate(raw)
    nested = BatchManifest.model_validate({"version": 1, "jobs": [raw]}).jobs[0]
    legacy = BatchJob.from_mapping(raw)

    assert direct == nested == legacy
    job = direct

    assert job.path == "/data/media/movie.mkv"
    assert job.output == "/torrentfiles/movie.torrent"
    assert job.trackers == ("https://tracker.example/announce",)
    assert job.webseeds == ("https://seed.example/movie.mkv",)
    assert job.name == "Display Name"
    assert job.comment == "release comment"
    assert job.source == "BLURAY"
    assert job.exclude_patterns == ("*.nfo",)
    assert job.include_patterns == ("*.mkv",)


@pytest.mark.parametrize("field", ["trackers", "webseeds", "exclude_patterns", "include_patterns"])
def test_blank_sequence_entries_fail_every_model_entry_point(field: str) -> None:
    raw = _job(**{field: [" "]})

    with pytest.raises(ValidationError):
        BatchJob.model_validate(raw)
    with pytest.raises(ValidationError):
        BatchManifest.model_validate({"version": 1, "jobs": [raw]})
    with pytest.raises(ValueError):
        BatchJob.from_mapping(raw)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("piece_length", 15, id="piece-too-small"),
        pytest.param("piece_length", 28, id="piece-too-large"),
        pytest.param("max_piece_length", 15, id="max-piece-too-small"),
        pytest.param("max_piece_length", 28, id="max-piece-too-large"),
        pytest.param("target_piece_count", 0, id="target-not-positive"),
        pytest.param(
            "target_piece_count",
            TARGET_PIECE_COUNT_MAX + 1,
            id="target-overflows-mkbrr-uint",
        ),
        pytest.param("piece_length", "18", id="piece-string-is-not-coerced"),
        pytest.param("private", 1, id="boolean-is-strict"),
    ],
)
def test_job_rejects_invalid_or_coerced_scalar_values(field: str, value: Any) -> None:
    with pytest.raises(ValidationError):
        BatchJob.model_validate(_job(**{field: value}))


def test_explicit_piece_length_and_target_count_are_mutually_exclusive() -> None:
    with pytest.raises(ValidationError, match="piece_length and target_piece_count"):
        BatchJob.model_validate(_job(piece_length=20, target_piece_count=800))


def test_mapping_constructor_reports_whole_job_validation_errors() -> None:
    with pytest.raises(
        ValueError,
        match=r"^Invalid batch job: .*piece_length and target_piece_count",
    ):
        BatchJob.from_mapping(_job(piece_length=20, target_piece_count=800))


@pytest.mark.parametrize(
    ("field", "uri", "message"),
    [
        pytest.param("trackers", "not-a-uri", "tracker URI scheme", id="tracker-relative"),
        pytest.param(
            "trackers",
            "ftp://tracker.example/announce",
            "tracker URI scheme",
            id="tracker-protocol",
        ),
        pytest.param(
            "trackers",
            "https:///announce",
            "tracker URI must include a host",
            id="tracker-host",
        ),
        pytest.param(
            "webseeds",
            "udp://seed.example/file",
            "webseed URI scheme",
            id="webseed-protocol",
        ),
        pytest.param(
            "webseeds",
            "https://seed.example/a file",
            "must not contain whitespace",
            id="webseed-whitespace",
        ),
    ],
)
def test_job_validates_tracker_and_webseed_uris(field: str, uri: str, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        BatchJob.model_validate(_job(**{field: [uri]}))


def test_strict_manifest_rejects_blank_sequence_items() -> None:
    with pytest.raises(ValidationError):
        BatchManifest.model_validate(
            {"version": 1, "jobs": [_job(trackers=["https://tracker.example/announce", ""])]}
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("name", "bad\x00name", id="name"),
        pytest.param("comment", "bad\x00comment", id="comment"),
        pytest.param("source", "bad\x00source", id="source"),
        pytest.param("exclude_patterns", ["bad\x00pattern"], id="exclude-pattern"),
        pytest.param("include_patterns", ["bad\x00pattern"], id="include-pattern"),
    ],
)
def test_job_rejects_nul_in_argv_bound_text(field: str, value: Any) -> None:
    with pytest.raises(ValidationError, match="NUL"):
        BatchJob.model_validate(_job(**{field: value}))


def test_manifest_rejects_lexically_duplicate_output_paths() -> None:
    payload = {
        "version": 1,
        "jobs": [
            _job(output="/torrentfiles/show.torrent"),
            _job(
                path="/data/media/other.mkv",
                output="//torrentfiles/releases/../show.torrent",
            ),
        ],
    }

    with pytest.raises(ValidationError, match=r"jobs\.1\.output duplicates jobs\.0\.output"):
        BatchManifest.model_validate(payload)


def test_generated_schema_describes_strict_models_and_new_fields() -> None:
    schema = generate_batch_json_schema()
    job_schema = schema["$defs"]["BatchJob"]
    properties = job_schema["properties"]

    assert schema["$schema"] == JSON_SCHEMA_DIALECT
    assert schema["additionalProperties"] is False
    assert job_schema["additionalProperties"] is False
    assert {"name", "max_piece_length", "target_piece_count", "no_creator"} <= properties.keys()
    assert properties["path"] == {
        "description": "Absolute host or container source path",
        "format": "absolute-path",
        "minLength": 1,
        "not": {"pattern": r"(?:\u0000|\s$)"},
        "pattern": r"^/(?:[\s\S]*\S)?$",
        "title": "Path",
        "type": "string",
    }
    assert properties["trackers"]["items"]["format"] == "uri"
    assert "[Uu][Dd][Pp]" in properties["trackers"]["items"]["pattern"]
    assert properties["webseeds"]["items"]["format"] == "uri"
    assert properties["webseeds"]["items"]["pattern"].startswith("^[Hh][Tt][Tt][Pp][Ss]?://")
    assert properties["output"]["pattern"] == r"^/[\s\S]*\.torrent$"
    assert properties["output"]["not"] == {"pattern": r"(?:\u0000|\s$)"}
    assert properties["name"]["pattern"] == r"^(?:$|\S(?:[\s\S]*\S)?)$"
    assert properties["include_patterns"]["items"]["pattern"] == (r"^\S(?:[\s\S]*\S)?$")
    target_schema = properties["target_piece_count"]["anyOf"][0]
    assert target_schema["maximum"] == TARGET_PIECE_COUNT_MAX
    assert job_schema["allOf"] == [
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


def test_export_batch_json_schema_writes_generated_schema(tmp_path: Path) -> None:
    destination = tmp_path / "nested" / "batch.schema.json"

    exported = export_batch_json_schema(destination)

    assert exported == destination.resolve()
    assert json.loads(destination.read_text(encoding="utf-8")) == generate_batch_json_schema()
    assert destination.read_bytes().endswith(b"\n")


def test_tracked_batch_schemas_match_generated_model() -> None:
    root = Path(__file__).parents[1]
    expected = f"{json.dumps(generate_batch_json_schema(), indent=2)}\n".encode()

    assert (root / "schema" / "batch.json").read_bytes() == expected
    assert (root / "src" / "mkbrr_wizard" / "schema" / "batch.json").read_bytes() == expected
