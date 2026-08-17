"""Stable, serializable contracts for planning and executing mkbrr work.

The interactive UI, a future headless CLI, and execution backends can all exchange
these models without importing one another.  They deliberately contain effective
values (resolved paths and options), rather than prompt-specific state.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Literal, TypeVar
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    field_validator,
    model_validator,
)

NonEmptyText = Annotated[
    str,
    StringConstraints(strict=True, strip_whitespace=True, min_length=1),
]
NonNegativeInt = Annotated[int, Field(ge=0)]
PositiveInt = Annotated[int, Field(gt=0)]
PieceLength = Annotated[int, Field(ge=16, le=27)]
CommandArgument = Annotated[str, StringConstraints(strict=True)]
Command = Annotated[tuple[CommandArgument, ...], Field(min_length=1)]

SerializableModelT = TypeVar("SerializableModelT", bound="SerializableModel")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class SerializableModel(BaseModel):
    """Immutable-by-assignment Pydantic model with explicit JSON helpers."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    def to_json(self, *, indent: int | None = 2) -> str:
        """Return a portable JSON representation of the model."""

        return self.model_dump_json(indent=indent)

    @classmethod
    def from_json(
        cls: type[SerializableModelT], data: str | bytes | bytearray
    ) -> SerializableModelT:
        """Restore a model previously produced by :meth:`to_json`."""

        return cls.model_validate_json(data)


class OperationKind(str, Enum):
    """User-facing mkbrr operation represented by a plan item."""

    CREATE = "create"
    CHECK = "check"
    INSPECT = "inspect"
    MODIFY = "modify"


class RuntimeKind(str, Enum):
    """Execution environment for an effective plan."""

    NATIVE = "native"
    DOCKER = "docker"


class StorageKind(str, Enum):
    """Storage class used to select workers and schedule concurrent work."""

    HDD = "hdd"
    SSD = "ssd"
    UNKNOWN = "unknown"


class OperationStatus(str, Enum):
    """Lifecycle state shared by progress events and terminal results."""

    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    BLOCKED = "blocked"

    @property
    def is_terminal(self) -> bool:
        return self in {
            OperationStatus.SUCCEEDED,
            OperationStatus.FAILED,
            OperationStatus.SKIPPED,
            OperationStatus.CANCELLED,
            OperationStatus.TIMED_OUT,
            OperationStatus.BLOCKED,
        }


class ProgressEventKind(str, Enum):
    """Purpose of an execution event, independent of its lifecycle state."""

    QUEUED = "queued"
    STARTED = "started"
    PROGRESS = "progress"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    MESSAGE = "message"


class ResolvedPath(SerializableModel):
    """A user-entered path and every relevant resolved representation."""

    entered: NonEmptyText
    host: NonEmptyText
    runtime: NonEmptyText
    physical: NonEmptyText | None = None


class EffectiveOptions(SerializableModel):
    """Effective mkbrr settings after preset defaults and overrides are applied."""

    preset: NonEmptyText | None = None
    trackers: tuple[NonEmptyText, ...] = ()
    webseeds: tuple[NonEmptyText, ...] = ()
    private: bool | None = None
    source: NonEmptyText | None = None
    comment: str | None = None
    piece_length: PieceLength | None = None
    max_piece_length: PieceLength | None = None
    target_piece_count: PositiveInt | None = None
    workers: NonNegativeInt | None = None
    include_patterns: tuple[NonEmptyText, ...] = ()
    exclude_patterns: tuple[NonEmptyText, ...] = ()
    extra: dict[str, JsonValue] = Field(default_factory=dict)

    @property
    def piece_strategy(self) -> str:
        """Return a concise description of the selected piece strategy."""

        if self.piece_length is not None:
            return f"fixed 2^{self.piece_length} bytes"
        if self.target_piece_count is not None:
            return f"target {self.target_piece_count:,} pieces"
        if self.max_piece_length is not None:
            return f"automatic (max 2^{self.max_piece_length} bytes)"
        return "automatic"


class PlannedOperation(SerializableModel):
    """One fully resolved, executable item in an :class:`ExecutionPlan`."""

    operation_id: NonEmptyText
    position: PositiveInt
    kind: OperationKind
    source_path: ResolvedPath
    output_path: ResolvedPath | None = None
    command: Command
    cwd: NonEmptyText | None = None
    options: EffectiveOptions = Field(default_factory=EffectiveOptions)
    storage: StorageKind = StorageKind.UNKNOWN
    storage_key: NonEmptyText | None = None
    estimated_file_count: NonNegativeInt | None = None
    estimated_size_bytes: NonNegativeInt | None = None
    warnings: tuple[NonEmptyText, ...] = ()
    metadata: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("command")
    @classmethod
    def _command_must_be_executable(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value[0].strip():
            raise ValueError("command executable must not be blank")
        if any("\x00" in argument for argument in value):
            raise ValueError("command arguments must not contain a NUL byte")
        return value


class ExecutionPlan(SerializableModel):
    """A versioned plan that can be previewed, persisted, and executed later."""

    schema_version: Literal[1] = 1
    plan_id: NonEmptyText = Field(default_factory=lambda: uuid4().hex)
    created_at: datetime = Field(default_factory=_utc_now)
    runtime: RuntimeKind
    dry_run: bool = False
    operations: tuple[PlannedOperation, ...] = ()
    warnings: tuple[NonEmptyText, ...] = ()
    metadata: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("created_at")
    @classmethod
    def _created_at_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("created_at must include a timezone")
        return value

    @model_validator(mode="after")
    def _operations_must_be_unambiguous(self) -> ExecutionPlan:
        operation_ids = [operation.operation_id for operation in self.operations]
        if len(operation_ids) != len(set(operation_ids)):
            raise ValueError("operation_id values must be unique within a plan")

        positions = [operation.position for operation in self.operations]
        if len(positions) != len(set(positions)):
            raise ValueError("operation positions must be unique within a plan")
        return self


class OperationResult(SerializableModel):
    """Terminal result for a planned operation."""

    schema_version: Literal[1] = 1
    plan_id: NonEmptyText
    operation_id: NonEmptyText
    status: OperationStatus
    exit_code: int | None = None
    elapsed_seconds: Annotated[float, Field(ge=0)] = 0.0
    started_at: datetime | None = None
    finished_at: datetime = Field(default_factory=_utc_now)
    output_path: NonEmptyText | None = None
    stdout: str | None = None
    stderr: str | None = None
    error_message: NonEmptyText | None = None
    details: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("started_at", "finished_at")
    @classmethod
    def _timestamps_must_be_timezone_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError("result timestamps must include a timezone")
        return value

    @model_validator(mode="after")
    def _validate_terminal_result(self) -> OperationResult:
        if not self.status.is_terminal:
            raise ValueError("OperationResult status must be terminal")
        if self.started_at is not None and self.finished_at < self.started_at:
            raise ValueError("finished_at cannot precede started_at")
        if self.status is OperationStatus.SUCCEEDED and self.exit_code not in (None, 0):
            raise ValueError("a succeeded result cannot have a non-zero exit_code")
        return self

    @property
    def succeeded(self) -> bool:
        return self.status is OperationStatus.SUCCEEDED


class ProgressEvent(SerializableModel):
    """Ordered snapshot emitted while a plan or operation is executing."""

    schema_version: Literal[1] = 1
    event_id: NonEmptyText = Field(default_factory=lambda: uuid4().hex)
    plan_id: NonEmptyText
    operation_id: NonEmptyText | None = None
    sequence: NonNegativeInt
    occurred_at: datetime = Field(default_factory=_utc_now)
    kind: ProgressEventKind = ProgressEventKind.QUEUED
    status: OperationStatus
    completed_units: NonNegativeInt | None = None
    total_units: NonNegativeInt | None = None
    message: NonEmptyText | None = None
    metrics: dict[str, float] = Field(default_factory=dict)

    @field_validator("occurred_at")
    @classmethod
    def _occurred_at_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("occurred_at must include a timezone")
        return value

    @model_validator(mode="after")
    def _completed_units_cannot_exceed_total(self) -> ProgressEvent:
        if (
            self.completed_units is not None
            and self.total_units is not None
            and self.completed_units > self.total_units
        ):
            raise ValueError("completed_units cannot exceed total_units")
        return self

    @property
    def fraction_complete(self) -> float | None:
        if self.completed_units is None or not self.total_units:
            return None
        return self.completed_units / self.total_units
