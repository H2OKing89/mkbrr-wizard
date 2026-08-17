#!/usr/bin/env python3
"""
Interactive wrapper for mkbrr (Docker OR native), driven by config.yaml.

Rich UI edition ✨

Key points:
- runtime: auto|docker|native
- docker_support: true/false
- chown: true/false
- Accepts either /mnt/... or /data/... paths (maps depending on runtime)
- Always passes --preset-file
- Avoids mkbrr output flag mismatch by using:
    - native: cwd = host_output_dir
    - docker : -w  = container_output_dir
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import uuid
import warnings
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol, cast

from .batch_models import (
    BatchJob,
    BatchManifest,
    generate_batch_json_schema,
)
from .execution.docker_cleanup import cleanup_named_docker_container

try:
    from pydantic import (
        BaseModel,
        ConfigDict,
        Field,
        ValidationError,
        ValidationInfo,
        computed_field,
        field_validator,
        model_validator,
    )
except ImportError as e:
    print("❌ pydantic is not installed. Install it with:\n   pip install 'pydantic>=2.7,<3'")
    raise SystemExit(1) from e

try:
    import yaml
except ImportError as e:
    print("❌ PyYAML is not installed. Install it with:\n   pip install pyyaml")
    raise SystemExit(1) from e

try:
    from rich import box
    from rich.console import Console, Group
    from rich.panel import Panel
    from rich.prompt import Confirm, Prompt
    from rich.syntax import Syntax
    from rich.table import Table
    from rich.text import Text
    from rich.theme import Theme
    from rich.traceback import install as install_rich_traceback

    install_rich_traceback(show_locals=False)
except ImportError as e:
    print("❌ rich is not installed. Install it with:\n   pip install rich")
    raise SystemExit(1) from e

try:
    from prompt_toolkit.history import InMemoryHistory

    _content_history: InMemoryHistory | None = InMemoryHistory()
    _torrent_history: InMemoryHistory | None = InMemoryHistory()
    _has_prompt_toolkit = True
except ImportError:
    # prompt_toolkit is optional; fall back to basic input
    _content_history = None
    _torrent_history = None
    _has_prompt_toolkit = False

try:
    import httpx

    _has_httpx = True
except ImportError:
    httpx = None  # type: ignore[assignment]
    _has_httpx = False

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None  # type: ignore[assignment]


THEME = Theme(
    {
        "title": "bold cyan",
        "accent": "cyan",
        "info": "bright_cyan",
        "ok": "bold green",
        "warn": "bold yellow",
        "err": "bold red",
        "dim": "dim",
        "path": "bright_white",
        "k": "dim",
        "v": "bright_white",
    }
)
console = Console(theme=THEME, highlight=False)

MKBRR_TESTED_VERSION = "1.24.1"
MKBRR_MIN_SUPPORTED_VERSION = (1, 24, 0)
MKBRR_NEXT_UNSUPPORTED_VERSION = (2, 0, 0)
DEFAULT_MKBRR_IMAGE = f"ghcr.io/autobrr/mkbrr:v{MKBRR_TESTED_VERSION}"


# ----------------------------
# Config + parsing
# ----------------------------


def _coerce_bool(v: Any) -> Any:
    if isinstance(v, bool):
        return v
    if isinstance(v, int | float):
        return bool(v)
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("true", "yes", "y", "1", "on", "enabled"):
            return True
        if s in ("false", "no", "n", "0", "off", "disabled"):
            return False
    return v


class _StrictConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, validate_default=True)


def _normalize_choice(value: str, field_name: str, choices: tuple[str, ...]) -> str:
    normalized = value.strip().lower()
    if normalized not in choices:
        raise ValueError(f"{field_name} must be one of: {', '.join(choices)}")
    return normalized


def _coerce_legacy_int(value: Any) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, float) and not value.is_integer():
        return value
    with contextlib.suppress(TypeError, ValueError, OverflowError):
        return int(value)
    return value


class _MkbrrInput(_StrictConfigModel):
    binary: str = "mkbrr"
    image: str = DEFAULT_MKBRR_IMAGE

    @field_validator("binary", "image")
    @classmethod
    def _strip_text(cls, value: str) -> str:
        return value.strip()


class _PathsInput(_StrictConfigModel):
    host_data_root: str = "/mnt/user/data"
    container_data_root: str = "/data"
    host_output_dir: str = "/mnt/user/data/downloads/torrents/torrentfiles"
    container_output_dir: str = "/torrentfiles"
    host_config_dir: str = "/mnt/cache/appdata/mkbrr"
    container_config_dir: str = "/root/.config/mkbrr"

    @field_validator("host_data_root", "host_output_dir", "host_config_dir")
    @classmethod
    def _expand_host_path(cls, value: str) -> str:
        return _expand_path(value).rstrip("/")

    @field_validator("container_data_root", "container_output_dir", "container_config_dir")
    @classmethod
    def _normalize_container_path(cls, value: str) -> str:
        return value.strip().rstrip("/")


class _OwnershipInput(_StrictConfigModel):
    uid: int = 99
    gid: int = 100

    _normalize_legacy_ints = field_validator("uid", "gid", mode="before")(_coerce_legacy_int)


class _BatchInput(_StrictConfigModel):
    mode: str = "simple"
    job_timeout_seconds: int | None = None
    max_parallel_jobs: int = 1
    hdd_parallel_per_device: int = 1
    ssd_parallel_per_device: int = 2
    max_total_workers: int | None = None

    _normalize_legacy_ints = field_validator(
        "job_timeout_seconds",
        "max_parallel_jobs",
        "hdd_parallel_per_device",
        "ssd_parallel_per_device",
        "max_total_workers",
        mode="before",
    )(_coerce_legacy_int)

    @field_validator("mode")
    @classmethod
    def _validate_mode(cls, value: str) -> str:
        return _normalize_choice(value, "batch.mode", ("simple", "advanced"))

    @field_validator("job_timeout_seconds")
    @classmethod
    def _validate_timeout(cls, value: int | None) -> int | None:
        if value is not None and value <= 0:
            raise ValueError("batch.job_timeout_seconds must be a positive integer")
        return value

    @field_validator(
        "max_parallel_jobs",
        "hdd_parallel_per_device",
        "ssd_parallel_per_device",
    )
    @classmethod
    def _validate_positive_limits(cls, value: int, info: ValidationInfo) -> int:
        if value <= 0:
            raise ValueError(f"batch.{info.field_name} must be a positive integer")
        return value

    @field_validator("max_total_workers")
    @classmethod
    def _validate_worker_budget(cls, value: int | None) -> int | None:
        if value is not None and value <= 0:
            raise ValueError("batch.max_total_workers must be a positive integer")
        return value


class _UnraidInput(_StrictConfigModel):
    enabled: bool = False
    fuse_root: str = "/mnt/user"
    mount_priority: str = "disk_first"
    split_share_preflight: str = "fail"
    split_share_unmapped_docker_path: str = "warn"
    split_share_max_entries: int = 20000
    split_share_follow_symlinks: bool = False

    _coerce_legacy_bools = field_validator("enabled", "split_share_follow_symlinks", mode="before")(
        _coerce_bool
    )
    _normalize_legacy_max_entries = field_validator("split_share_max_entries", mode="before")(
        _coerce_legacy_int
    )

    @field_validator("fuse_root")
    @classmethod
    def _expand_fuse_root(cls, value: str) -> str:
        return _expand_path(value).rstrip("/")

    @field_validator("mount_priority")
    @classmethod
    def _validate_mount_priority(cls, value: str) -> str:
        return _normalize_choice(value, "unraid.mount_priority", ("disk_first", "cache_first"))

    @field_validator("split_share_preflight")
    @classmethod
    def _validate_preflight_mode(cls, value: str) -> str:
        return _normalize_choice(value, "unraid.split_share_preflight", ("off", "warn", "fail"))

    @field_validator("split_share_unmapped_docker_path")
    @classmethod
    def _validate_unmapped_docker_path_mode(cls, value: str) -> str:
        return _normalize_choice(
            value,
            "unraid.split_share_unmapped_docker_path",
            ("off", "warn", "fail"),
        )

    @field_validator("split_share_max_entries")
    @classmethod
    def _validate_max_entries(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("unraid.split_share_max_entries must be a positive integer")
        return value


class _WorkersInput(_StrictConfigModel):
    hdd: int | None = 1
    ssd: int | None = None
    default: int | None = None

    @field_validator("hdd", "ssd", "default", mode="before")
    @classmethod
    def _normalize_workers(cls, value: Any, info: ValidationInfo) -> int | None:
        if value is None:
            return None
        if isinstance(value, str):
            value = value.strip().lower()
            if value in ("auto", ""):
                return None
            with contextlib.suppress(ValueError):
                value = int(value)
        if isinstance(value, int) and value > 0:
            return value
        raise ValueError(f"workers.{info.field_name} must be a positive integer or 'auto'")


class _PushoverInput(_StrictConfigModel):
    enabled: bool = False
    app_token: str = ""
    user_key: str = ""
    priority: int = 0
    failure_priority: int = 1
    device: str = ""

    _coerce_legacy_enabled = field_validator("enabled", mode="before")(_coerce_bool)
    _normalize_legacy_priorities = field_validator("priority", "failure_priority", mode="before")(
        _coerce_legacy_int
    )

    @field_validator("app_token", "user_key")
    @classmethod
    def _expand_tokens(cls, value: str) -> str:
        return _expand_env(value)

    @field_validator("device")
    @classmethod
    def _strip_device(cls, value: str) -> str:
        return value.strip()

    @field_validator("priority", "failure_priority")
    @classmethod
    def _validate_priority(cls, value: int, info: ValidationInfo) -> int:
        if not -2 <= value <= 1:
            raise ValueError(
                f"notifications.pushover.{info.field_name} must be between -2 and 1; "
                "priority 2 requires retry and expire settings, which are not supported."
            )
        return value


class _DiscordInput(_StrictConfigModel):
    enabled: bool = False
    webhook_url: str = ""
    username: str = "mkbrr-wizard"
    avatar_url: str = ""
    color_success: int = 0x2ECC71
    color_failure: int = 0xE74C3C
    color_partial: int = 0xF39C12

    _coerce_legacy_enabled = field_validator("enabled", mode="before")(_coerce_bool)

    @field_validator("webhook_url")
    @classmethod
    def _expand_webhook_url(cls, value: str) -> str:
        return _expand_env(value)

    @field_validator("username", "avatar_url")
    @classmethod
    def _strip_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("color_success", "color_failure", "color_partial", mode="before")
    @classmethod
    def _parse_color(cls, value: int | str) -> int | str:
        if isinstance(value, str):
            return int(value, 0)
        return value


class _NotificationsInput(_StrictConfigModel):
    enabled: bool = False
    policy: str = "summary"
    pushover: _PushoverInput = Field(default_factory=_PushoverInput)
    discord: _DiscordInput = Field(default_factory=_DiscordInput)
    timeout_seconds: int = 10

    _coerce_legacy_enabled = field_validator("enabled", mode="before")(_coerce_bool)
    _normalize_legacy_timeout = field_validator("timeout_seconds", mode="before")(
        _coerce_legacy_int
    )

    @field_validator("policy")
    @classmethod
    def _validate_policy(cls, value: str) -> str:
        return _normalize_choice(value, "notifications.policy", ("summary", "failures_only", "off"))


class _AppConfigInput(_StrictConfigModel):
    runtime: str = "auto"
    docker_support: bool = True
    chown: bool = True
    docker_user: str | None = None
    mkbrr: _MkbrrInput = Field(default_factory=_MkbrrInput)
    paths: _PathsInput = Field(default_factory=_PathsInput)
    ownership: _OwnershipInput = Field(default_factory=_OwnershipInput)
    batch: _BatchInput = Field(default_factory=_BatchInput)
    unraid: _UnraidInput = Field(default_factory=_UnraidInput)
    notifications: _NotificationsInput = Field(default_factory=_NotificationsInput)
    workers: _WorkersInput = Field(default_factory=_WorkersInput)
    presets_yaml: str = "presets.yaml"

    _coerce_legacy_bools = field_validator("docker_support", "chown", mode="before")(_coerce_bool)

    @model_validator(mode="before")
    @classmethod
    def _normalize_sections(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        raw = dict(value)
        for section in (
            "mkbrr",
            "paths",
            "ownership",
            "batch",
            "unraid",
            "notifications",
            "workers",
        ):
            if raw.get(section) is None:
                raw[section] = {}
        notifications = raw.get("notifications")
        if isinstance(notifications, dict):
            for provider in ("pushover", "discord"):
                if notifications.get(provider) is None:
                    notifications[provider] = {}
        return raw

    @field_validator("runtime")
    @classmethod
    def _validate_runtime(cls, value: str) -> str:
        return _normalize_choice(value, "runtime", ("auto", "docker", "native"))

    @field_validator("docker_user")
    @classmethod
    def _normalize_docker_user(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None

    @field_validator("presets_yaml")
    @classmethod
    def _normalize_presets_yaml(cls, value: str) -> str:
        return value.strip()


MkbrrCfg = _MkbrrInput
PathsCfg = _PathsInput
OwnershipCfg = _OwnershipInput
BatchCfg = _BatchInput
UnraidCfg = _UnraidInput
WorkersCfg = _WorkersInput
PushoverCfg = _PushoverInput
DiscordCfg = _DiscordInput
NotificationsCfg = _NotificationsInput


class AppCfg(_AppConfigInput):
    @model_validator(mode="before")
    @classmethod
    def _migrate_derived_preset_fields(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        raw = dict(value)
        presets_host = raw.pop("presets_yaml_host", None)
        raw.pop("presets_yaml_container", None)
        if "presets_yaml" not in raw and presets_host:
            raw["presets_yaml"] = presets_host
        return raw

    @computed_field  # type: ignore[prop-decorator, misc]
    @property
    def presets_yaml_host(self) -> str:
        presets_yaml = _expand_path(self.presets_yaml)
        if os.path.isabs(presets_yaml):
            return presets_yaml
        return str(Path(self.paths.host_config_dir) / self.presets_yaml)

    @computed_field  # type: ignore[prop-decorator, misc]
    @property
    def presets_yaml_container(self) -> str:
        host_preset = Path(os.path.abspath(self.presets_yaml_host))
        host_config = Path(os.path.abspath(self.paths.host_config_dir))
        try:
            relative = host_preset.relative_to(host_config)
        except ValueError:
            relative = Path(f".external-{host_preset.name}")
        return str(Path(self.paths.container_config_dir) / relative)


_LEGACY_BOOL_PATHS = (
    ("docker_support",),
    ("chown",),
    ("unraid", "enabled"),
    ("unraid", "split_share_follow_symlinks"),
    ("notifications", "enabled"),
    ("notifications", "pushover", "enabled"),
    ("notifications", "discord", "enabled"),
)


def _resolve_config_path(
    raw: dict[str, Any], path_parts: tuple[str, ...]
) -> tuple[dict[str, Any], str] | None:
    node = raw
    for key in path_parts[:-1]:
        child = node.get(key)
        if not isinstance(child, dict):
            return None
        node = child
    return node, path_parts[-1]


def _migrate_legacy_ture(raw: dict[str, Any]) -> None:
    migrated_paths: list[str] = []
    for path_parts in _LEGACY_BOOL_PATHS:
        resolved = _resolve_config_path(raw, path_parts)
        if resolved is None:
            continue
        node, field_name = resolved
        value = node.get(field_name)
        if isinstance(value, str) and value.strip().lower() == "ture":
            node[field_name] = True
            migrated_paths.append(".".join(path_parts))

    if migrated_paths:
        warnings.warn(
            "Migrated legacy 'ture' boolean value(s) at "
            f"{', '.join(migrated_paths)}; update config.yaml to use true.",
            UserWarning,
            stacklevel=2,
        )


def _expand_env(s: str) -> str:
    """Expand $VARS / ${VAR} in a string without Path normalization (for URLs, tokens)."""
    s = (s or "").strip()
    if not s:
        return s
    return os.path.expandvars(s)


def _expand_path(p: str) -> str:
    """Expand ~ and $VARS and return a normalized path string (doesn't require existence)."""
    p = (p or "").strip()
    if not p:
        return p
    p = os.path.expandvars(p)
    return str(Path(p).expanduser())


def _clean_user_path(s: str) -> str:
    """
    Clean up user input from interactive prompts:
    - trims whitespace
    - strips one pair of matching surrounding quotes ('...' or "...")
    - expands ~ and $VARS
    """
    s = (s or "").strip()
    if not s:
        return s

    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        s = s[1:-1].strip()

    return _expand_path(s)


def load_config_environment(config_path: Path) -> None:
    """Load an optional ``.env`` adjacent to the selected configuration file."""
    if load_dotenv is not None:
        load_dotenv(config_path.expanduser().resolve(strict=False).parent / ".env")


@dataclass(frozen=True)
class CommandSpec:
    argv: tuple[str, ...]
    cwd: str | None = None

    def with_args(self, *args: str) -> CommandSpec:
        return CommandSpec(argv=(*self.argv, *args), cwd=self.cwd)


class RuntimeBackend(Protocol):
    runtime: str

    def run(
        self, command: CommandSpec, *, timeout: int | None = None
    ) -> subprocess.CompletedProcess[Any]: ...


class _SubprocessBackend:
    def run(
        self, command: CommandSpec, *, timeout: int | None = None
    ) -> subprocess.CompletedProcess[Any]:
        try:
            return subprocess.run(command.argv, cwd=command.cwd, check=False, timeout=timeout)
        except OSError:
            return subprocess.CompletedProcess(command.argv, returncode=127)


class NativeBackend(_SubprocessBackend):
    runtime = "native"


class DockerBackend(_SubprocessBackend):
    runtime = "docker"

    def run(
        self, command: CommandSpec, *, timeout: int | None = None
    ) -> subprocess.CompletedProcess[Any]:
        try:
            return super().run(command, timeout=timeout)
        except subprocess.TimeoutExpired:
            cleanup_named_docker_container(
                command.argv,
                capture_diagnostics=False,
            )
            raise


def backend_for_runtime(runtime: str) -> RuntimeBackend:
    if runtime == "native":
        return NativeBackend()
    if runtime == "docker":
        return DockerBackend()
    raise ValueError(f"Unsupported runtime: {runtime}")


@dataclass(frozen=True)
class ExecutionResult:
    returncode: int
    elapsed: float
    timed_out: bool = False


class CommandExecutor:
    def __init__(self, backend: RuntimeBackend) -> None:
        self._backend = backend

    def run(self, command: CommandSpec, *, timeout: int | None = None) -> ExecutionResult:
        started = time.monotonic()
        try:
            completed = self._backend.run(command, timeout=timeout)
        except subprocess.TimeoutExpired:
            return ExecutionResult(
                returncode=124,
                elapsed=time.monotonic() - started,
                timed_out=True,
            )
        return ExecutionResult(
            returncode=completed.returncode,
            elapsed=time.monotonic() - started,
        )


@dataclass(frozen=True)
class JobResult:
    index: int
    content_path: str
    output_path: str
    exit_code: int

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0

    def as_tuple(self) -> tuple[int, str, str, int]:
        return self.index, self.content_path, self.output_path, self.exit_code


def load_config(path: Path) -> AppCfg:
    raw: dict[str, Any] = {}
    if path.exists():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        if loaded is None:
            raw = {}
        elif not isinstance(loaded, dict):
            raise ValueError("config.yaml root must be a mapping")
        else:
            raw = cast(dict[str, Any], loaded)
    else:
        raise FileNotFoundError(f"Config not found: {path}")

    _migrate_legacy_ture(raw)
    try:
        return cast(AppCfg, AppCfg.model_validate(raw))
    except ValidationError as e:
        errors = e.errors(include_url=False, include_input=False)
        raise ValueError(
            f"Invalid configuration:\n{json.dumps(errors, indent=2, default=str)}"
        ) from e


# ----------------------------
# Runtime detection
# ----------------------------


def docker_available() -> bool:
    try:
        r = subprocess.run(["docker", "--version"], capture_output=True, text=True, check=False)
        return r.returncode == 0
    except FileNotFoundError:
        return False


def native_available(binary: str) -> bool:
    return shutil.which(binary) is not None


def pick_runtime(cfg: AppCfg, forced: str | None) -> str:
    if forced:
        return forced

    if cfg.runtime in ("docker", "native"):
        return cfg.runtime

    # auto
    if cfg.docker_support and docker_available():
        return "docker"
    if native_available(cfg.mkbrr.binary):
        return "native"
    # last chance: if docker exists but docker_support false, still allow native only
    raise RuntimeError(
        "No usable runtime found.\n"
        "- Docker not available (or docker_support=false)\n"
        "- Native mkbrr not found on PATH\n"
    )


def detect_mkbrr_version(cfg: AppCfg, runtime: str) -> str:
    """Best-effort mkbrr version string for the active runtime."""

    def _shorten_version_line(line: str) -> str:
        m = re.search(r"(?<![0-9A-Za-z])[vV]?(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)", line)
        if m:
            return m.group(1)
        return line

    if runtime == "docker":
        base_cmd: list[str] = ["docker", "run", "--rm"]
        if cfg.docker_user:
            base_cmd += ["--user", cfg.docker_user]
        candidates = [
            base_cmd + [cfg.mkbrr.image, "mkbrr", "version"],
            base_cmd + [cfg.mkbrr.image, "mkbrr", "--version"],
        ]
    else:
        candidates = [
            [cfg.mkbrr.binary, "version"],
            [cfg.mkbrr.binary, "--version"],
        ]

    for cmd in candidates:
        try:
            r = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
                timeout=8,
            )
        except Exception:
            continue

        if int(getattr(r, "returncode", 1)) != 0:
            continue

        stdout_text = str(getattr(r, "stdout", "") or "")
        stderr_text = str(getattr(r, "stderr", "") or "")
        output = stdout_text.strip() or stderr_text.strip()
        if not output:
            continue

        first_line = output.splitlines()[0].strip()
        if first_line:
            return _shorten_version_line(first_line)

    return "unknown"


def verify_mkbrr_compatibility(version: str) -> None:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:[-+][0-9A-Za-z.-]+)?", version.strip())
    if match is None:
        raise RuntimeError(
            "Could not verify the mkbrr version. "
            f"This wizard supports mkbrr >=1.24.0,<2.0.0 and tests against {MKBRR_TESTED_VERSION}."
        )

    parsed = tuple(int(part) for part in match.groups())
    if not MKBRR_MIN_SUPPORTED_VERSION <= parsed < MKBRR_NEXT_UNSUPPORTED_VERSION:
        raise RuntimeError(
            f"mkbrr {version} is not supported. "
            f"Use mkbrr >=1.24.0,<2.0.0; tested version: {MKBRR_TESTED_VERSION}."
        )


# ----------------------------
# Path mapping (content + torrent files)
# ----------------------------


def map_content_path(cfg: AppCfg, runtime: str, raw: str) -> str:
    raw = raw.strip()
    if runtime == "docker":
        # host -> container
        if (
            raw.startswith(cfg.paths.container_data_root + "/")
            or raw == cfg.paths.container_data_root
        ):
            return raw
        abs_path = os.path.abspath(raw)
        if (
            abs_path.startswith(cfg.paths.host_data_root + "/")
            or abs_path == cfg.paths.host_data_root
        ):
            return cfg.paths.container_data_root + abs_path[len(cfg.paths.host_data_root) :]
        return raw
    else:
        # container -> host
        if raw.startswith(cfg.paths.host_data_root + "/") or raw == cfg.paths.host_data_root:
            return raw
        if (
            raw.startswith(cfg.paths.container_data_root + "/")
            or raw == cfg.paths.container_data_root
        ):
            return cfg.paths.host_data_root + raw[len(cfg.paths.container_data_root) :]
        return os.path.abspath(raw)


def map_torrent_path(cfg: AppCfg, runtime: str, raw: str) -> str:
    raw = raw.strip()
    if runtime == "docker":
        # host output -> container output
        if (
            raw.startswith(cfg.paths.container_output_dir + "/")
            or raw == cfg.paths.container_output_dir
        ):
            return raw
        abs_path = os.path.abspath(raw)
        if (
            abs_path.startswith(cfg.paths.host_output_dir + "/")
            or abs_path == cfg.paths.host_output_dir
        ):
            return cfg.paths.container_output_dir + abs_path[len(cfg.paths.host_output_dir) :]
        return raw
    else:
        # container output -> host output
        if raw.startswith(cfg.paths.host_output_dir + "/") or raw == cfg.paths.host_output_dir:
            return raw
        if (
            raw.startswith(cfg.paths.container_output_dir + "/")
            or raw == cfg.paths.container_output_dir
        ):
            return cfg.paths.host_output_dir + raw[len(cfg.paths.container_output_dir) :]
        return os.path.abspath(raw)


def _is_under_root(path: str, root: str) -> bool:
    normalized_path = os.path.normpath(path)
    normalized_root = os.path.normpath(root)
    try:
        return os.path.commonpath((normalized_path, normalized_root)) == normalized_root
    except ValueError:
        return False


def _require_mapped_docker_path(
    path: str,
    *,
    context: str,
    configured_roots: tuple[tuple[str, str, str], ...],
) -> None:
    if any(_is_under_root(path, container_root) for _, _, container_root in configured_roots):
        return

    expected = " or ".join(
        f"{name} ({host_root} on host, {container_root} in container)"
        for name, host_root, container_root in configured_roots
    )
    raise ValueError(
        f"{context} is outside configured Docker mounts: {path}. Use a path under {expected}."
    )


def resolve_mounted_torrent_path(
    cfg: AppCfg,
    runtime: str,
    raw: str,
    *,
    context: str,
) -> str:
    mapped = map_torrent_path(cfg, runtime, raw)
    if runtime != "docker":
        return mapped

    if not _is_under_root(mapped, cfg.paths.container_output_dir):
        mapped = map_content_path(cfg, runtime, raw)

    _require_mapped_docker_path(
        mapped,
        context=context,
        configured_roots=(
            (
                "paths.host_output_dir",
                cfg.paths.host_output_dir,
                cfg.paths.container_output_dir,
            ),
            ("paths.host_data_root", cfg.paths.host_data_root, cfg.paths.container_data_root),
        ),
    )
    return mapped


def _natural_disk_sort_key(path: str) -> tuple[int, str]:
    name = os.path.basename(path)
    match = re.fullmatch(r"disk(\d+)", name)
    if not match:
        return (sys.maxsize, name)
    return (int(match.group(1)), name)


def _unraid_candidate_roots(cache_first: bool = False) -> list[str]:
    try:
        entries = [entry.path for entry in os.scandir("/mnt") if entry.is_dir()]
    except OSError:
        return []

    disk_roots = [p for p in entries if re.fullmatch(r"disk\d+", os.path.basename(p))]
    cache_roots = [p for p in entries if re.fullmatch(r"cache(?:-.+)?", os.path.basename(p))]
    disk_roots.sort(key=_natural_disk_sort_key)
    cache_roots.sort()
    if cache_first:
        return cache_roots + disk_roots
    return disk_roots + cache_roots


# ----------------------------
# Storage type detection (HDD vs SSD)
# ----------------------------

_RE_UNRAID_HDD = re.compile(r"^/mnt/disk\d+(/|$)")
_RE_UNRAID_SSD = re.compile(r"^/mnt/cache(?:-.+)?(/|$)")
_RE_UNRAID_FUSE = re.compile(r"^/mnt/user(/|$)")


def _resolve_fuse_path(fuse_root: str, path: str, mount_priority: str = "disk_first") -> str | None:
    """Resolve a FUSE-mounted path to physical /mnt/diskN or /mnt/cache* mounts.

    Returns the resolved physical path when found, otherwise None.
    """
    abs_path = os.path.abspath(path)
    fuse_root = fuse_root.rstrip("/") or "/mnt/user"

    if abs_path != fuse_root and not abs_path.startswith(f"{fuse_root}/"):
        return None

    relative = abs_path[len(fuse_root) :]
    if not relative.startswith("/"):
        relative = f"/{relative}"

    if mount_priority == "cache_first":
        roots = _unraid_candidate_roots(cache_first=True)
    else:
        roots = _unraid_candidate_roots()

    for root in roots:
        candidate = f"{root}{relative}"
        if os.path.exists(candidate):
            return candidate

    return None


def _resolve_unraid_fuse_path(
    path: str, fuse_root: str = "/mnt/user", mount_priority: str = "disk_first"
) -> str | None:
    """Resolve Unraid FUSE paths to physical /mnt/diskN or /mnt/cache* mounts."""
    return _resolve_fuse_path(fuse_root, path, mount_priority=mount_priority)


def _detect_storage_type_sysblock(path: str) -> str:
    """Detect storage type via /sys/block/*/queue/rotational for arbitrary Linux paths.

    Returns "hdd", "ssd", or "unknown".
    """
    try:
        stat_result = os.stat(path)
    except OSError:
        return "unknown"

    major = os.major(stat_result.st_dev)
    minor = os.minor(stat_result.st_dev)

    # For device-mapper / md / lvm we need the actual parent block device.
    # Try /sys/dev/block/<major>:<minor> → follow chain up to a real disk.
    sys_path = f"/sys/dev/block/{major}:{minor}"
    try:
        real = os.path.realpath(sys_path)
        # Walk up until we find a queue/rotational file
        parts = real.split("/")
        for i in range(len(parts), 2, -1):
            candidate = "/".join(parts[:i]) + "/queue/rotational"
            try:
                val = Path(candidate).read_text().strip()
            except (FileNotFoundError, OSError):
                continue
            return "hdd" if val == "1" else "ssd"
    except OSError:
        pass

    return "unknown"


def detect_storage_type(
    path: str,
    fuse_root: str = "/mnt/user",
    mount_priority: str = "disk_first",
) -> str:
    """Detect whether *path* resides on HDD or SSD.

    Detection tiers:
    1. Unraid path pattern: /mnt/diskN → hdd, /mnt/cache* → ssd
    2. /sys/block rotational flag (generic Linux fallback)
    3. "unknown" if nothing matches

    Returns "hdd", "ssd", or "unknown".
    """
    abs_path = os.path.abspath(path)

    # Tier 1: Unraid path patterns (fast, no I/O)
    if _RE_UNRAID_HDD.match(abs_path):
        return "hdd"
    if _RE_UNRAID_SSD.match(abs_path):
        return "ssd"

    # Tier 1.5: Unraid FUSE path -> physical mount resolution
    normalized_fuse_root = fuse_root.rstrip("/") or "/mnt/user"
    if abs_path == normalized_fuse_root or abs_path.startswith(f"{normalized_fuse_root}/"):
        resolved = _resolve_unraid_fuse_path(
            abs_path,
            fuse_root=normalized_fuse_root,
            mount_priority=mount_priority,
        )
        if resolved:
            if _RE_UNRAID_HDD.match(resolved):
                return "hdd"
            if _RE_UNRAID_SSD.match(resolved):
                return "ssd"
            return _detect_storage_type_sysblock(resolved)

    # Tier 2: /sys/block rotational flag (generic Linux)
    return _detect_storage_type_sysblock(abs_path)


def resolve_workers(storage_type: str, workers_cfg: WorkersCfg) -> int | None:
    """Map a storage type to the configured --workers value.

    Returns an int (explicit worker count) or None (let mkbrr auto-detect).
    """
    if storage_type == "hdd":
        return workers_cfg.hdd
    if storage_type == "ssd":
        return workers_cfg.ssd
    return workers_cfg.default


def _resolve_host_path_for_detection(
    cfg: AppCfg, runtime: str, raw_input: str, host_data_root_override: str | None
) -> str:
    """Derive the host-side content path for storage type detection.

    In native mode the content_path IS the host path.
    In docker mode we need to map back from the container path.
    """
    if runtime == "native":
        return map_content_path(cfg, "native", raw_input)
    # Docker mode: map container path -> host path.
    # If Unraid provided an override root, preserve the subpath under
    # container_data_root so we return the full host-side content path.
    if host_data_root_override:
        container_root = cfg.paths.container_data_root.rstrip("/")
        override_root = host_data_root_override.rstrip("/")
        if raw_input == container_root:
            return override_root
        if raw_input.startswith(container_root + "/"):
            suffix = raw_input[len(container_root) :]
            return f"{override_root}{suffix}"
    # Fallback: map container → host
    return map_content_path(cfg, "native", raw_input)


def resolve_unraid_disk_path(
    cfg: AppCfg,
    raw: str,
    *,
    emit_messages: bool = True,
) -> str:
    """Resolve /mnt/user paths to physical /mnt/diskN or /mnt/cache* paths on Unraid."""
    raw = (raw or "").strip()
    if not raw or not cfg.unraid.enabled:
        return raw

    abs_path = os.path.abspath(raw)
    fuse_root = cfg.unraid.fuse_root
    normalized_fuse_root = fuse_root.rstrip("/") or "/mnt/user"

    resolved = _resolve_fuse_path(fuse_root, abs_path, mount_priority=cfg.unraid.mount_priority)
    if (
        resolved is None
        and abs_path != normalized_fuse_root
        and not abs_path.startswith(f"{normalized_fuse_root}/")
    ):
        return abs_path
    if resolved:
        if emit_messages:
            console.print(f"[info][i] Unraid resolved content path to:[/] {resolved}")
        return resolved

    if emit_messages:
        console.print(f"[warn]⚠ Unraid path not found on disk/cache mounts:[/] {abs_path}")
    return abs_path


def _resolve_unraid_host_data_root(cfg: AppCfg, resolved_host_path: str) -> str | None:
    """Return host_data_root override (e.g. /mnt/disk5/data) for docker bind mount."""
    if not cfg.unraid.enabled:
        return None

    host_data_root = cfg.paths.host_data_root.rstrip("/")
    fuse_root = cfg.unraid.fuse_root.rstrip("/") or "/mnt/user"
    if host_data_root != fuse_root and not host_data_root.startswith(f"{fuse_root}/"):
        return None

    relative_from_mnt = resolved_host_path.removeprefix("/mnt/")
    if "/" not in relative_from_mnt:
        return None
    mount_root_name = relative_from_mnt.split("/", 1)[0]
    if not re.fullmatch(r"disk\d+|cache(?:-.+)?", mount_root_name):
        return None

    suffix = host_data_root[len(fuse_root) :]
    if suffix and not suffix.startswith("/"):
        suffix = f"/{suffix}"

    mount_root = f"/mnt/{mount_root_name}"
    return f"{mount_root}{suffix}" if suffix else mount_root


@dataclass(frozen=True)
class ResolvedContent:
    runtime: str
    runtime_path: str
    host_path: str
    fuse_host_path: str
    host_mount_override: str | None
    used_fuse_fallback: bool = False


def _resolved_content_for_host_path(
    cfg: AppCfg,
    runtime: str,
    host_path: str,
    fuse_host_path: str,
    *,
    used_fuse_fallback: bool,
) -> ResolvedContent:
    host_mount_override = (
        None if used_fuse_fallback else _resolve_unraid_host_data_root(cfg, host_path)
    )
    if runtime == "docker":
        runtime_path = map_content_path(cfg, "docker", host_path)
        if host_mount_override and (
            host_path.startswith(host_mount_override + "/") or host_path == host_mount_override
        ):
            runtime_path = cfg.paths.container_data_root + host_path[len(host_mount_override) :]
        _require_mapped_docker_path(
            runtime_path,
            context="Content path",
            configured_roots=(
                (
                    "paths.host_data_root",
                    host_mount_override or cfg.paths.host_data_root,
                    cfg.paths.container_data_root,
                ),
            ),
        )
    else:
        runtime_path = host_path
        host_mount_override = None

    return ResolvedContent(
        runtime=runtime,
        runtime_path=runtime_path,
        host_path=host_path,
        fuse_host_path=fuse_host_path,
        host_mount_override=host_mount_override,
        used_fuse_fallback=used_fuse_fallback,
    )


def resolve_unraid_content_path(
    cfg: AppCfg,
    runtime: str,
    raw: str,
    *,
    emit_messages: bool = True,
) -> ResolvedContent:
    """Resolve content into a runtime plan while retaining its FUSE source path."""
    mapped = map_content_path(cfg, runtime, raw)
    fuse_host_path = map_content_path(cfg, "native", mapped)
    if not cfg.unraid.enabled:
        if runtime == "docker":
            _require_mapped_docker_path(
                mapped,
                context="Content path",
                configured_roots=(
                    (
                        "paths.host_data_root",
                        cfg.paths.host_data_root,
                        cfg.paths.container_data_root,
                    ),
                ),
            )
        return ResolvedContent(
            runtime=runtime,
            runtime_path=mapped,
            host_path=fuse_host_path,
            fuse_host_path=fuse_host_path,
            host_mount_override=None,
        )

    resolved_host = resolve_unraid_disk_path(
        cfg,
        fuse_host_path,
        emit_messages=emit_messages,
    )
    return _resolved_content_for_host_path(
        cfg,
        runtime,
        resolved_host,
        fuse_host_path,
        used_fuse_fallback=False,
    )


def _detect_split_share_mismatch(
    original_host_path: str,
    resolved_host_path: str,
    *,
    max_entries: int,
    follow_symlinks: bool,
) -> tuple[int, list[str], int, bool]:
    """Return (missing_count, sample_missing_relpaths, permission_errors, capped_scan)."""
    missing_count = 0
    missing_examples: list[str] = []
    permission_errors = 0
    scanned = 0
    capped_scan = False

    if os.path.isfile(original_host_path):
        if not os.path.exists(resolved_host_path):
            return (1, [os.path.basename(original_host_path)], 0, False)
        return (0, [], 0, False)

    if not os.path.isdir(original_host_path):
        return (0, [], 0, False)

    def _onerror(_: OSError) -> None:
        nonlocal permission_errors
        permission_errors += 1

    stop_scan = False
    for root, _, files in os.walk(
        original_host_path,
        topdown=True,
        onerror=_onerror,
        followlinks=follow_symlinks,
    ):
        for filename in files:
            scanned += 1
            if scanned > max_entries:
                capped_scan = True
                stop_scan = True
                break

            source_file = os.path.join(root, filename)
            rel = os.path.relpath(source_file, original_host_path)
            target_file = os.path.join(resolved_host_path, rel)
            if not os.path.exists(target_file):
                missing_count += 1
                if len(missing_examples) < 5:
                    missing_examples.append(rel)
        if stop_scan:
            break

    return (missing_count, missing_examples, permission_errors, capped_scan)


def preflight_unraid_split_share(
    cfg: AppCfg,
    resolved: ResolvedContent,
    *,
    context: str,
    warning_sink: Callable[[str], None] | None = None,
) -> ResolvedContent:
    """Validate a physical Unraid plan and fall back to FUSE when configured to warn."""
    if not cfg.unraid.enabled:
        return resolved

    mode = cfg.unraid.split_share_preflight
    if mode == "off":
        return resolved
    if resolved.used_fuse_fallback or resolved.fuse_host_path == resolved.host_path:
        return resolved
    if not os.path.exists(resolved.fuse_host_path):
        return resolved

    missing_count, missing_examples, permission_errors, capped_scan = _detect_split_share_mismatch(
        resolved.fuse_host_path,
        resolved.host_path,
        max_entries=cfg.unraid.split_share_max_entries,
        follow_symlinks=cfg.unraid.split_share_follow_symlinks,
    )

    if missing_count == 0 and permission_errors == 0 and not capped_scan:
        return resolved

    details: list[str] = []
    if missing_count > 0:
        details.append(f"missing {missing_count} file(s) on resolved mount")
    if permission_errors > 0:
        details.append(f"{permission_errors} permission error(s) while scanning")
    if capped_scan:
        details.append(f"scan capped at {cfg.unraid.split_share_max_entries} entries")

    base_msg = (
        f"Unraid preflight ({context}) detected possible split-share content: "
        f"{'; '.join(details)}\n"
        f"  original: {resolved.fuse_host_path}\n"
        f"  resolved: {resolved.host_path}"
    )
    if missing_examples:
        base_msg += "\n  examples: " + ", ".join(missing_examples)

    if mode == "warn":
        try:
            fallback = _resolved_content_for_host_path(
                cfg,
                resolved.runtime,
                resolved.fuse_host_path,
                resolved.fuse_host_path,
                used_fuse_fallback=True,
            )
        except ValueError:
            message = f"{base_msg}\nUsing resolved mount for this operation."
            if warning_sink is None:
                console.print(f"[warn]⚠ {message}[/]")
            else:
                warning_sink(message)
            return resolved
        message = f"{base_msg}\nUsing FUSE path for this operation."
        if warning_sink is None:
            console.print(f"[warn]⚠ {message}[/]")
        else:
            warning_sink(message)
        return fallback

    raise ValueError(
        base_msg
        + "\nUse /mnt/user (FUSE) for this content, or gather files onto a single disk/pool path first."
    )


# ----------------------------
# Split series helpers
# ----------------------------

_VIDEO_EXTENSIONS = frozenset((".mkv", ".mp4", ".avi", ".ts", ".m2ts"))
EpisodeKey = tuple[int, int]

# Matches S01E02 plus chained/ranged forms: S01E01E02, S01E01-E02, S01E01-02.
_EPISODE_RE = re.compile(r"S(\d{2,})E(\d{2,})((?:(?:-?E|-)\d{2,})*)", re.IGNORECASE)
_ADDITIONAL_EPISODE_RE = re.compile(r"(-?E|-)(\d{2,})", re.IGNORECASE)


def scan_episodes(directory: str) -> list[tuple[EpisodeKey, str]]:
    """Scan *directory* for video files with S##E## names.

    Returns one ``((season, episode), filename)`` tuple per episode identity.
    Multi-episode files therefore produce multiple entries with the same filename.
    Non-video files and files without an episode tag are silently skipped.
    """
    results: list[tuple[EpisodeKey, str]] = []
    try:
        entries = os.listdir(directory)
    except OSError:
        return results

    for name in entries:
        full = os.path.join(directory, name)
        if not os.path.isfile(full):
            continue
        ext = os.path.splitext(name)[1].lower()
        if ext not in _VIDEO_EXTENSIONS:
            continue
        seen_keys: set[EpisodeKey] = set()
        for match in _EPISODE_RE.finditer(name):
            season = int(match.group(1))
            first_episode = int(match.group(2))
            episode_numbers = [first_episode]
            previous_episode = first_episode
            for separator, number in _ADDITIONAL_EPISODE_RE.findall(match.group(3)):
                episode = int(number)
                if separator.startswith("-") and episode > previous_episode:
                    episode_numbers.extend(range(previous_episode + 1, episode + 1))
                else:
                    episode_numbers.append(episode)
                previous_episode = episode
            for episode in episode_numbers:
                key = (season, episode)
                if key not in seen_keys:
                    results.append((key, name))
                    seen_keys.add(key)

    results.sort(key=lambda item: (item[0], item[1].casefold()))
    return results


def _format_episode_number_ranges(episode_numbers: list[int], season: int | None) -> list[str]:
    ranges: list[str] = []
    start = end = episode_numbers[0]
    prefix = f"S{season:02d}" if season is not None else ""
    for number in episode_numbers[1:]:
        if number == end + 1:
            end = number
            continue
        ranges.append(
            f"{prefix}E{start:02d}" if start == end else f"{prefix}E{start:02d}-E{end:02d}"
        )
        start = end = number
    ranges.append(f"{prefix}E{start:02d}" if start == end else f"{prefix}E{start:02d}-E{end:02d}")
    return ranges


def format_episode_ranges(episode_keys: list[EpisodeKey]) -> str:
    """Format episode identities into compact season-aware ranges.

    A single season keeps the compact ``E01-E03`` display. Mixed seasons are
    rendered as ``S01E01-E03, S02E01-E03``.
    """
    if not episode_keys:
        return ""

    episodes_by_season: dict[int, set[int]] = {}
    for season, episode in episode_keys:
        episodes_by_season.setdefault(season, set()).add(episode)

    show_season = len(episodes_by_season) > 1
    ranges: list[str] = []
    for season, episode_numbers in sorted(episodes_by_season.items()):
        ranges.extend(
            _format_episode_number_ranges(
                sorted(episode_numbers),
                season if show_season else None,
            )
        )
    return ", ".join(ranges)


def parse_split_ranges(input_str: str, available: list[EpisodeKey]) -> list[list[EpisodeKey]]:
    """Parse a single-season split specification into episode-key lists.

    *input_str* uses range notation separated by ``,`` or ``;`` where each
    range is ``start-end`` (inclusive).  Example: ``"1-11, 12-22"``.

    Returns a list of lists — one per part — containing the episode numbers
    that actually exist in *available*.

    Raises ``ValueError`` on:
    * overlapping ranges
    * ranges that omit any available episode
    * a range that references zero available episodes
    * unparseable tokens
    """
    seasons = {season for season, _ in available}
    if len(seasons) > 1:
        raise ValueError(
            "Cannot split a folder containing multiple seasons; use a separate source folder "
            "for each season"
        )
    if not seasons:
        raise ValueError("No episodes available to split")

    season = next(iter(seasons))
    available_numbers = {episode for _, episode in available}
    parts: list[list[EpisodeKey]] = []
    seen: set[int] = set()

    # Normalize separators: "1-11; 12-22" -> "1-11, 12-22"
    tokens = [t.strip() for t in re.split(r"[,;]+", input_str) if t.strip()]
    if not tokens:
        raise ValueError("No ranges provided")

    for token in tokens:
        m = re.fullmatch(r"(\d+)\s*-\s*(\d+)", token)
        if not m:
            raise ValueError(f"Invalid range token: '{token}' — expected e.g. '1-11'")
        lo, hi = int(m.group(1)), int(m.group(2))
        if lo > hi:
            raise ValueError(f"Invalid range: {lo}-{hi} (start > end)")

        overlap = seen & set(range(lo, hi + 1))
        if overlap:
            raise ValueError(
                f"Overlapping range: {token} — episode(s) {sorted(overlap)} already assigned"
            )
        seen.update(range(lo, hi + 1))

        part_numbers = sorted(ep for ep in range(lo, hi + 1) if ep in available_numbers)
        if not part_numbers:
            raise ValueError(f"Range {lo}-{hi} contains no episodes found in folder")
        parts.append([(season, episode) for episode in part_numbers])

    assigned = {episode_key for part in parts for episode_key in part}
    omitted = sorted(set(available) - assigned)
    if omitted:
        raise ValueError(f"Split ranges omit episode(s): {format_episode_ranges(omitted)}")

    return parts


def build_split_include_patterns(
    episodes: list[tuple[EpisodeKey, str]], part_episodes: list[EpisodeKey]
) -> list[str]:
    """Build ``--include`` glob patterns that select exactly *part_episodes*.

    Uses the ``S##E##`` tag extracted from each filename so the pattern is
    precise (e.g. ``*S01E03*`` rather than a bare ``*E03*``).
    """
    episode_keys_by_filename: dict[str, set[EpisodeKey]] = {}
    for episode_key, filename in episodes:
        episode_keys_by_filename.setdefault(filename, set()).add(episode_key)

    selected = set(part_episodes)
    patterns: list[str] = []
    for filename, file_episode_keys in sorted(
        episode_keys_by_filename.items(),
        key=lambda item: (min(item[1]), item[0].casefold()),
    ):
        selected_file_keys = selected & file_episode_keys
        if not selected_file_keys:
            continue
        if selected_file_keys != file_episode_keys:
            formatted = format_episode_ranges(sorted(file_episode_keys))
            raise ValueError(
                f"Multi-episode file '{filename}' ({formatted}) must remain in one split part"
            )
        match = _EPISODE_RE.search(filename)
        if match:
            pattern = f"*{match.group(0)}*"
            if pattern not in patterns:
                patterns.append(pattern)
    return patterns


def split_output_name(folder_name: str, part_index: int) -> str:
    """Generate an output ``.torrent`` filename for a split-series part.

    Inserts ``.Part{N}`` before the ``.torrent`` extension.
    *part_index* is 1-based.
    """
    base = folder_name.rstrip("/").rstrip("\\")
    base = Path(base).name if base else "split"
    return f"{base}.Part{part_index}.torrent"


def render_split_summary(
    folder_name: str,
    parts: list[list[EpisodeKey]],
    include_patterns: list[list[str]],
    output_dir: str,
) -> None:
    """Print a rich summary table of the planned split-series jobs."""
    table = Table(
        title=f"Split Series — {len(parts)} parts",
        box=box.SIMPLE,
        show_lines=False,
    )
    table.add_column("Part", style="cyan", justify="right")
    table.add_column("Episodes", style="bright_white")
    table.add_column("# Files", justify="right")
    table.add_column("Output", style="path")

    for idx, (part_eps, patterns) in enumerate(zip(parts, include_patterns, strict=True), 1):
        out_name = split_output_name(folder_name, idx)
        out_path = str(Path(output_dir) / out_name)
        table.add_row(
            str(idx),
            format_episode_ranges(part_eps),
            str(len(patterns)),
            out_path,
        )

    console.print(table)


# ----------------------------
# Docker command builder
# ----------------------------


# ----------------------------
# Command builders (testable)
# ----------------------------


def build_create_command(
    cfg: AppCfg,
    runtime: str,
    content_path: str,
    preset: str,
    host_data_root_override: str | None = None,
    *,
    include_output_dir: bool = True,
    interactive: bool = True,
) -> CommandSpec:
    """Return the create command plan for the selected runtime."""
    if runtime == "docker":
        cmd = docker_run_base(
            cfg,
            cfg.paths.container_output_dir,
            host_data_root_override=host_data_root_override,
            interactive=interactive,
        ) + [
            "create",
            content_path,
            "-P",
            preset,
            "--preset-file",
            cfg.presets_yaml_container,
        ]
        cwd = None
    else:
        cmd = [
            cfg.mkbrr.binary,
            "create",
            content_path,
            "-P",
            preset,
            "--preset-file",
            cfg.presets_yaml_host,
        ]
        cwd = cfg.paths.host_output_dir
    if include_output_dir:
        output_dir = (
            cfg.paths.container_output_dir if runtime == "docker" else cfg.paths.host_output_dir
        )
        cmd += ["--output-dir", output_dir]
    return CommandSpec(argv=tuple(cmd), cwd=cwd)


def _append_optional_bool(cmd: list[str], flag: str, value: bool | None) -> None:
    if value is not None:
        cmd.append(f"{flag}={str(value).lower()}")


def build_batch_job_create_command(
    cfg: AppCfg,
    runtime: str,
    preset: str,
    job: BatchJob | Mapping[str, Any],
    host_data_root_override: str | None = None,
    *,
    interactive: bool = True,
) -> CommandSpec:
    """Return the command plan for a single batch job executed via mkbrr create."""
    job = job if isinstance(job, BatchJob) else BatchJob.from_mapping(job)

    base_spec = build_create_command(
        cfg,
        runtime,
        job.path,
        preset,
        host_data_root_override=host_data_root_override,
        include_output_dir=False,
        interactive=interactive,
    )
    cmd = list(base_spec.argv)
    # Mark output-dir as explicitly empty so a preset cannot override the
    # per-job output path. mkbrr gives a non-empty OutputDir precedence over
    # OutputPath, even when --output is present.
    cmd += ["--output-dir=", "--output", job.output]

    for tracker in job.trackers:
        cmd += ["--tracker", tracker]

    for seed in job.webseeds:
        cmd += ["--web-seed", seed]

    _append_optional_bool(cmd, "--private", job.private)

    _append_optional_bool(cmd, "--no-date", job.no_date)
    _append_optional_bool(cmd, "--entropy", job.entropy)
    _append_optional_bool(cmd, "--skip-prefix", job.skip_prefix)
    _append_optional_bool(cmd, "--fail-on-season-warning", job.fail_on_season_warning)
    _append_optional_bool(cmd, "--no-creator", job.no_creator)

    if job.piece_length is not None:
        cmd += ["--piece-length", str(job.piece_length)]
    if job.max_piece_length is not None:
        cmd += ["--max-piece-length", str(job.max_piece_length)]
    if job.target_piece_count is not None:
        cmd += ["--target-piece-count", str(job.target_piece_count)]

    if job.name:
        cmd += ["--name", job.name]

    if job.comment is not None:
        cmd += ["--comment", job.comment]

    if job.source is not None:
        cmd += ["--source", job.source]

    for pattern in job.exclude_patterns:
        cmd += ["--exclude", pattern]

    for pattern in job.include_patterns:
        cmd += ["--include", pattern]

    # Explicit output paths are independent of the configured default output
    # directory. A missing default directory must not prevent a valid batch
    # job from launching.
    return CommandSpec(argv=tuple(cmd), cwd=None)


def build_inspect_command(
    cfg: AppCfg,
    runtime: str,
    torrent_path: str,
    verbose: bool = False,
    *,
    interactive: bool = True,
) -> CommandSpec:
    """Return the inspect command plan for the selected runtime."""
    if runtime == "docker":
        cmd = docker_run_base(
            cfg,
            cfg.paths.container_config_dir,
            interactive=interactive,
        ) + ["inspect", torrent_path]
    else:
        cmd = [cfg.mkbrr.binary, "inspect", torrent_path]
    if verbose:
        cmd.append("-v")
    return CommandSpec(argv=tuple(cmd))


def build_check_command(
    cfg: AppCfg,
    runtime: str,
    torrent_path: str,
    content_path: str,
    verbose: bool = False,
    quiet: bool = False,
    workers: int | None = None,
    host_data_root_override: str | None = None,
    extra_mounts: tuple[tuple[str, str], ...] = (),
    *,
    interactive: bool = True,
) -> CommandSpec:
    """Return the check command plan for the selected runtime."""
    if runtime == "docker":
        cmd = docker_run_base(
            cfg,
            cfg.paths.container_config_dir,
            host_data_root_override=host_data_root_override,
            interactive=interactive,
            extra_mounts=extra_mounts,
        ) + [
            "check",
            torrent_path,
            content_path,
        ]
    else:
        cmd = [cfg.mkbrr.binary, "check", torrent_path, content_path]

    if verbose:
        cmd.append("-v")
    if quiet:
        cmd.append("--quiet")
    if workers:
        cmd += ["--workers", str(workers)]
    return CommandSpec(argv=tuple(cmd))


def docker_run_base(
    cfg: AppCfg,
    workdir: str,
    host_data_root_override: str | None = None,
    *,
    interactive: bool = True,
    extra_mounts: tuple[tuple[str, str], ...] = (),
) -> list[str]:
    cmd = ["docker", "run", "--rm", "--name", f"mkbrr-wizard-{uuid.uuid4().hex}"]

    # Only add -it when interactive; cron/log files hate TTY
    if interactive and sys.stdin.isatty():
        cmd += ["-it"]

    if cfg.docker_user:
        cmd += ["--user", cfg.docker_user]

    data_root = host_data_root_override or cfg.paths.host_data_root

    cmd += [
        "-w",
        workdir,
        "-v",
        f"{data_root}:{cfg.paths.container_data_root}",
        "-v",
        f"{cfg.paths.host_output_dir}:{cfg.paths.container_output_dir}",
        "-v",
        f"{cfg.paths.host_config_dir}:{cfg.paths.container_config_dir}",
    ]
    if not _is_under_root(cfg.presets_yaml_host, cfg.paths.host_config_dir):
        cmd += ["-v", f"{cfg.presets_yaml_host}:{cfg.presets_yaml_container}:ro"]
    # Bind paths left unreachable by a data_root override (e.g. a torrent file
    # outside the physical disk substituted for the FUSE union mount).
    for host_path, container_path in extra_mounts:
        cmd += ["-v", f"{host_path}:{container_path}:ro"]
    cmd += [cfg.mkbrr.image, "mkbrr"]
    return cmd


# ----------------------------
# Permissions
# ----------------------------


def host_torrent_output_path(cfg: AppCfg, path: str) -> str:
    """Resolve a runtime torrent output path to its host representation."""
    if _is_under_root(path, cfg.paths.container_output_dir):
        return map_torrent_path(cfg, "native", path)
    if _is_under_root(path, cfg.paths.container_data_root):
        return map_content_path(cfg, "native", path)
    return os.path.abspath(path)


# Compatibility alias for existing callers of the former private helper.
_host_torrent_output_path = host_torrent_output_path


def _snapshot_torrent_outputs(output_dir: str) -> dict[str, tuple[int, int, int]]:
    snapshot: dict[str, tuple[int, int, int]] = {}
    if not os.path.isdir(output_dir):
        return snapshot

    for dirpath, _, files in os.walk(output_dir):
        for filename in files:
            if not filename.lower().endswith(".torrent"):
                continue
            path = os.path.join(dirpath, filename)
            try:
                stat = os.stat(path)
            except (FileNotFoundError, PermissionError):
                continue
            snapshot[path] = (stat.st_ino, stat.st_size, stat.st_mtime_ns)
    return snapshot


def _changed_torrent_outputs(
    before: dict[str, tuple[int, int, int]],
    after: dict[str, tuple[int, int, int]],
) -> list[str]:
    return [path for path, metadata in after.items() if before.get(path) != metadata]


def maybe_fix_torrent_permissions(
    cfg: AppCfg,
    torrent_paths: list[str],
    *,
    emit_messages: bool = True,
) -> None:
    if not cfg.chown or not torrent_paths:
        return

    # Only try chown as root (Unraid root: yes; Ubuntu user: maybe no)
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        if emit_messages:
            console.print("[warn]⚠ chown=true but not running as root; skipping chown.[/]")
        return

    uid, gid = cfg.ownership.uid, cfg.ownership.gid
    changed = 0

    for path in dict.fromkeys(os.path.abspath(path) for path in torrent_paths):
        if not path.lower().endswith(".torrent"):
            continue
        try:
            stat = os.stat(path)
            if stat.st_uid != uid or stat.st_gid != gid:
                os.chown(path, uid, gid)
                changed += 1
        except FileNotFoundError:
            continue
        except PermissionError as e:
            if emit_messages:
                console.print(f"[warn]⚠ Permission error on {path}: {e}[/]")

    if emit_messages:
        if changed:
            console.print(f"[ok]✅ chown fixed ownership on {changed} .torrent file(s).[/]")
        else:
            console.print("[dim]ownership already correct (or nothing new to chown).[/]")


# ----------------------------
# Presets menu
# ----------------------------


def load_presets(host_presets_yaml: str) -> list[str]:
    p = Path(host_presets_yaml)
    if not p.exists():
        console.print(
            f"[warn]⚠ presets.yaml not found at {host_presets_yaml}. Using fallback: ['btn', 'custom'][/]"
        )
        return ["btn", "custom"]

    loaded = yaml.safe_load(p.read_text(encoding="utf-8"))
    data: dict[str, Any] = cast(dict[str, Any], loaded) if isinstance(loaded, dict) else {}
    presets_node: dict[str, Any] = cast(dict[str, Any], data.get("presets") or {})

    if not presets_node:
        return ["btn", "custom"]

    presets: list[str] = [str(k) for k in presets_node.keys()]
    if "btn" in presets:
        presets = ["btn"] + [x for x in presets if x != "btn"]
    return presets


def preset_include_patterns(host_presets_yaml: str, preset: str) -> tuple[str, ...]:
    """Return include patterns inherited by *preset* from its defaults and definition."""
    preset_path = Path(host_presets_yaml)
    if not preset_path.exists():
        return ()

    try:
        loaded = yaml.safe_load(preset_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ValueError(f"Could not read presets file {preset_path}: {error}") from error
    if not isinstance(loaded, Mapping):
        return ()

    patterns: list[str] = []
    presets_node = loaded.get("presets")
    preset_node = presets_node.get(preset) if isinstance(presets_node, Mapping) else None
    for node in (loaded.get("default"), preset_node):
        if not isinstance(node, Mapping):
            continue
        include_patterns = node.get("include_patterns")
        if isinstance(include_patterns, list):
            if not all(isinstance(pattern, str) for pattern in include_patterns):
                raise ValueError(
                    f"Invalid include_patterns in presets file {preset_path}: expected strings"
                )
            patterns.extend(pattern.strip() for pattern in include_patterns if pattern.strip())
        elif include_patterns:
            raise ValueError(
                f"Invalid include_patterns in presets file {preset_path}: expected a list"
            )
    return tuple(patterns)


def pick_preset(cfg: AppCfg) -> str:
    presets = load_presets(cfg.presets_yaml_host)

    table = Table(title="Presets (-P)", show_header=False, box=None, padding=(0, 2))
    table.add_column("idx", style="cyan")
    table.add_column("name")
    for i, p in enumerate(presets, 1):
        table.add_row(f"[{i}]", p)
    console.print(table)
    console.print(f"[dim](from {cfg.presets_yaml_host})[/]")

    choice = cast(
        str, Prompt.ask(f"Choose preset [cyan][1-{len(presets)} or name][/]", default="1")
    )
    if choice.isdigit():
        idx = int(choice)
        if 1 <= idx <= len(presets):
            return presets[idx - 1]
    if choice:
        if choice not in presets:
            console.print(f"[warn]⚠ '{choice}' not found in presets.yaml; mkbrr may fail.[/]")
        return choice
    return "btn" if "btn" in presets else presets[0]


# ----------------------------
# Prompts
# ----------------------------


def choose_action() -> str:
    panel = Panel(
        "[cyan][1][/] Create a torrent from a file/folder   [dim](mkbrr create)[/]\n"
        "[cyan][2][/] Inspect an existing .torrent file     [dim](mkbrr inspect)[/]\n"
        "[cyan][3][/] Check data against a .torrent file    [dim](mkbrr check)[/]\n"
        "[cyan][4][/] Batch create torrents                [dim](mkbrr create per-job)[/]\n"
        "[cyan][q][/] Quit",
        title="🧰 Action",
        border_style="cyan",
        box=box.ROUNDED,
    )
    console.print(panel)

    choice = cast(str, Prompt.ask("Choose", choices=["1", "2", "3", "4", "q"], default="1"))
    if choice == "2":
        return "inspect"
    if choice == "3":
        return "check"
    if choice == "4":
        return "batch"
    if choice == "q":
        raise SystemExit(0)
    return "create"


def ask_path(
    prompt: str, history: InMemoryHistory | None = None, default: str | None = None
) -> str:
    """Ask for a path, with optional ↑/↓ history via prompt_toolkit."""
    if _has_prompt_toolkit and history is not None:
        from prompt_toolkit import PromptSession as PS

        session: PS[str] = PS(history=history)
        try:
            suffix = f" [{default}]" if default else ""
            raw = cast(str, session.prompt(f"{prompt}{suffix}: "))
        except (EOFError, KeyboardInterrupt) as e:
            raise SystemExit(0) from e
    else:
        if default:
            raw = cast(str, Prompt.ask(prompt, default=default))
        else:
            raw = cast(str, Prompt.ask(prompt))

    raw = _clean_user_path(raw)
    if not raw and default:
        raw = _clean_user_path(default)
    if not raw:
        console.print("[err]❌ No path provided.[/]")
        raise SystemExit(1)
    return raw


def ask_verbose(mode: str) -> bool:
    return cast(bool, Confirm.ask(f"Verbose output for {mode}?", default=False))


def ask_quiet() -> bool:
    return cast(bool, Confirm.ask("Quiet mode for check?", default=False))


def ask_workers() -> int | None:
    s = cast(str, Prompt.ask("Workers", default="auto"))
    if s == "auto" or not s:
        return None
    try:
        v = int(s)
        return v if v > 0 else None
    except ValueError:
        console.print("[warn]⚠ Invalid workers; using auto.[/]")
        return None


def confirm_cmd(cmd: Sequence[str], cwd: str | None = None) -> bool:
    cmd_str = " ".join(shlex.quote(x) for x in cmd)

    parts: list[Text | Syntax] = []
    if cwd:
        parts.append(Text(f"cwd: {cwd}", style="dim"))
    parts.append(Syntax(cmd_str, "bash", word_wrap=True))

    console.print(
        Panel(
            Group(*parts),
            title="🚀 Command Preview",
            border_style="green",
            box=box.ROUNDED,
        )
    )
    return cast(bool, Confirm.ask("Proceed?", default=True))


def _script_dir() -> Path:
    """Return the source checkout root, or the installed package directory."""
    package_dir = Path(__file__).resolve().parent
    source_root = package_dir.parents[1]
    if (source_root / "schema" / "batch.json").is_file():
        return source_root
    return package_dir


def _batch_schema_path() -> Path:
    return _script_dir() / "schema" / "batch.json"


def load_batch_schema() -> dict[str, Any]:
    """Return JSON Schema generated from the strict batch models."""
    return generate_batch_json_schema()


def _error_path(path_parts: list[Any]) -> str:
    if not path_parts:
        return "root"
    return ".".join(str(p) for p in path_parts)


def validate_batch_payload(payload: dict[str, Any], schema: dict[str, Any]) -> list[str]:
    """Validate through :class:`BatchManifest`; ``schema`` remains for API compatibility."""
    del schema
    msgs: list[str] = []
    jobs = payload.get("jobs")
    if isinstance(jobs, list):
        output_jobs: dict[str, int] = {}
        for idx, raw_job in enumerate(jobs):
            if not isinstance(raw_job, dict):
                continue
            output = raw_job.get("output")
            if not isinstance(output, str) or not output.strip():
                continue
            normalized_output = os.path.normpath(output.strip())
            first_idx = output_jobs.get(normalized_output)
            if first_idx is not None:
                msgs.append(
                    f"jobs.{idx}.output: duplicates jobs.{first_idx}.output "
                    f"after path resolution: {normalized_output}"
                )
            else:
                output_jobs[normalized_output] = idx

    if msgs:
        return msgs

    try:
        BatchManifest.model_validate(payload)
    except ValidationError as error:
        for item in error.errors(include_url=False, include_input=False):
            path = _error_path(list(item.get("loc", ())))
            msgs.append(f"{path}: {item['msg']}")
    return msgs


def ask_positive_int(prompt: str, default: int = 1) -> int:
    while True:
        raw = cast(str, Prompt.ask(prompt, default=str(default))).strip()
        try:
            value = int(raw)
            if value > 0:
                return value
        except ValueError:
            pass
        console.print("[warn]⚠ Please enter a positive integer.[/]")


def ask_csv_list(prompt: str, default: list[str] | None = None) -> list[str] | None:
    default_text = ",".join(default) if default else ""
    raw = cast(str, Prompt.ask(prompt, default=default_text)).strip()
    if not raw:
        return None
    values = [x.strip() for x in raw.split(",") if x.strip()]
    return values or None


def ask_optional_int_range(
    prompt: str, min_value: int, max_value: int, default: int | None = None
) -> int | None:
    default_text = str(default) if default is not None else ""
    raw = cast(str, Prompt.ask(prompt, default=default_text)).strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        console.print(f"[warn]⚠ Invalid number '{raw}'. Skipping.[/]")
        return None
    if value < min_value or value > max_value:
        console.print(f"[warn]⚠ Value must be between {min_value} and {max_value}. Skipping.[/]")
        return None
    return value


def ask_optional_text(prompt: str, default: str | None = None) -> str | None:
    default_text = default or ""
    raw = cast(str, Prompt.ask(prompt, default=default_text)).strip()
    return raw or None


def ask_optional_bool(prompt: str, *, default: bool | None = None) -> bool | None:
    default_choice = "skip"
    if default is True:
        default_choice = "y"
    elif default is False:
        default_choice = "n"

    choice = cast(
        str,
        Prompt.ask(
            f"{prompt} [y/n/skip]",
            choices=["y", "n", "skip"],
            default=default_choice,
        ),
    )
    if choice == "skip":
        return None
    return choice == "y"


def _collect_job_optional_settings(
    previous: dict[str, Any] | None, job_index: int
) -> dict[str, Any]:
    if previous is not None and job_index > 1:
        if cast(bool, Confirm.ask("Reuse optional settings from previous job?", default=True)):
            return deepcopy(previous)

    table = Table(title=f"Job {job_index} Optional Settings", show_header=False, box=None)
    table.add_column("field", style="dim")
    table.add_column("value")
    table.add_row(
        "Common",
        "trackers, private, piece_length, comment, source, name",
    )
    table.add_row(
        "Advanced",
        "max_piece_length, target_piece_count, entropy, no_date, no_creator",
    )
    table.add_row(
        "Filtering / safety",
        "webseeds, exclude_patterns, include_patterns, skip_prefix, " "fail_on_season_warning",
    )
    console.print(table)

    trackers_default = cast(list[str] | None, previous.get("trackers")) if previous else None
    private_default = cast(bool | None, previous.get("private")) if previous else None
    piece_length_default = cast(int | None, previous.get("piece_length")) if previous else None
    comment_default = cast(str | None, previous.get("comment")) if previous else None
    source_default = cast(str | None, previous.get("source")) if previous else None
    entropy_default = cast(bool | None, previous.get("entropy")) if previous else None
    no_date_default = cast(bool | None, previous.get("no_date")) if previous else None
    webseeds_default = cast(list[str] | None, previous.get("webseeds")) if previous else None
    exclude_default = cast(list[str] | None, previous.get("exclude_patterns")) if previous else None
    include_default = cast(list[str] | None, previous.get("include_patterns")) if previous else None
    name_default = cast(str | None, previous.get("name")) if previous else None
    max_piece_length_default = (
        cast(int | None, previous.get("max_piece_length")) if previous else None
    )
    target_piece_count_default = (
        cast(int | None, previous.get("target_piece_count")) if previous else None
    )
    no_creator_default = cast(bool | None, previous.get("no_creator")) if previous else None
    skip_prefix_default = cast(bool | None, previous.get("skip_prefix")) if previous else None
    fail_on_season_warning_default = (
        cast(bool | None, previous.get("fail_on_season_warning")) if previous else None
    )

    result: dict[str, Any] = {}

    trackers = ask_csv_list("Trackers (comma-separated, blank to skip)", default=trackers_default)
    if trackers is not None:
        result["trackers"] = trackers

    private = ask_optional_bool("Private torrent?", default=private_default)
    if private is not None:
        result["private"] = private

    piece_length = ask_optional_int_range(
        "Piece length exponent [16-27] (blank to skip)",
        16,
        27,
        default=piece_length_default,
    )
    if piece_length is not None:
        result["piece_length"] = piece_length

    comment = ask_optional_text("Comment (blank to skip)", default=comment_default)
    if comment is not None:
        result["comment"] = comment

    source = ask_optional_text("Source (blank to skip)", default=source_default)
    if source is not None:
        result["source"] = source

    entropy = ask_optional_bool("Randomize info hash (entropy)?", default=entropy_default)
    if entropy is not None:
        result["entropy"] = entropy

    no_date = ask_optional_bool("Omit creation date (no_date)?", default=no_date_default)
    if no_date is not None:
        result["no_date"] = no_date

    webseeds = ask_csv_list("Webseeds (comma-separated, blank to skip)", default=webseeds_default)
    if webseeds is not None:
        result["webseeds"] = webseeds

    exclude_patterns = ask_csv_list(
        "Exclude patterns (comma-separated, blank to skip)",
        default=exclude_default,
    )
    if exclude_patterns is not None:
        result["exclude_patterns"] = exclude_patterns

    include_patterns = ask_csv_list(
        "Include patterns (comma-separated, blank to skip)",
        default=include_default,
    )
    if include_patterns is not None:
        result["include_patterns"] = include_patterns

    # Keep these newer questions after the original optional-settings sequence.
    # Besides making the progression feel like a second, less frequently used
    # tier, this lets prompt adapters that intentionally stop after the classic
    # fields retain their existing behaviour.
    try:
        name = ask_optional_text("Torrent name override (blank to skip)", default=name_default)
        if name is not None:
            result["name"] = name

        max_piece_length = ask_optional_int_range(
            "Maximum piece length exponent [16-27] (blank to skip)",
            16,
            27,
            default=max_piece_length_default,
        )
        if max_piece_length is not None:
            result["max_piece_length"] = max_piece_length

        # mkbrr treats an explicit piece length and a target piece count as
        # alternative strategies.  Avoid constructing an invalid manifest by
        # offering target_piece_count only when no explicit exponent was set.
        if piece_length is None:
            target_piece_count = ask_optional_int_range(
                "Target piece count (positive integer, blank to skip)",
                1,
                (1 << 64) - 1,
                default=target_piece_count_default,
            )
            if target_piece_count is not None:
                result["target_piece_count"] = target_piece_count
        elif target_piece_count_default is not None:
            console.print(
                "[warn]⚠ Ignoring the previous target piece count because an explicit "
                "piece length is set.[/]"
            )

        no_creator = ask_optional_bool(
            "Omit creator string (no_creator)?", default=no_creator_default
        )
        if no_creator is not None:
            result["no_creator"] = no_creator

        skip_prefix = ask_optional_bool(
            "Skip tracker prefix in output name (skip_prefix)?",
            default=skip_prefix_default,
        )
        if skip_prefix is not None:
            result["skip_prefix"] = skip_prefix

        fail_on_season_warning = ask_optional_bool(
            "Fail on incomplete-season warning?",
            default=fail_on_season_warning_default,
        )
        if fail_on_season_warning is not None:
            result["fail_on_season_warning"] = fail_on_season_warning
    except StopIteration:
        # Lightweight prompt adapters may signal that their optional input is
        # complete with StopIteration.  Everything collected so far remains a
        # valid job, so treat the newer tail fields as skipped.
        pass

    return result


def _default_batch_output_path(cfg: AppCfg, content_raw: str) -> str:
    trimmed = content_raw.rstrip("/").rstrip("\\")
    name = Path(trimmed).name if trimmed else ""
    suffix = Path(name).suffix if name else ""
    looks_like_file_ext = bool(suffix) and suffix[1:].isalpha() and len(suffix[1:]) <= 5
    base = Path(name).stem if looks_like_file_ext else name
    base = base or "batch-job"
    return str(Path(cfg.paths.host_output_dir) / f"{base}.torrent")


def collect_batch_jobs_interactive_simple(cfg: AppCfg) -> dict[str, Any]:
    num_jobs = ask_positive_int("How many batch jobs?", default=1)
    jobs: list[dict[str, Any]] = []

    for idx in range(1, num_jobs + 1):
        console.rule(f"[accent]Batch Job {idx}[/]")
        content_raw = ask_path(f"📂 Job {idx} content path", history=_content_history)
        output_default = _default_batch_output_path(cfg, content_raw)
        output_raw = ask_path(
            f"📄 Job {idx} output .torrent path",
            history=_torrent_history,
            default=output_default,
        )
        jobs.append({"path": content_raw, "output": output_raw})

    return {"version": 1, "jobs": jobs}


def collect_batch_jobs_interactive_advanced(cfg: AppCfg) -> dict[str, Any]:
    num_jobs = ask_positive_int("How many batch jobs?", default=1)
    jobs: list[dict[str, Any]] = []
    previous_optional: dict[str, Any] | None = None

    for idx in range(1, num_jobs + 1):
        console.rule(f"[accent]Batch Job {idx}[/]")
        content_raw = ask_path(f"📂 Job {idx} content path", history=_content_history)
        output_default = _default_batch_output_path(cfg, content_raw)
        output_raw = ask_path(
            f"📄 Job {idx} output .torrent path",
            history=_torrent_history,
            default=output_default,
        )

        optional = _collect_job_optional_settings(previous_optional, idx)
        job: dict[str, Any] = {"path": content_raw, "output": output_raw, **optional}
        jobs.append(job)
        previous_optional = deepcopy(optional)

    return {"version": 1, "jobs": jobs}


def collect_batch_jobs_interactive(cfg: AppCfg) -> dict[str, Any]:
    if cfg.batch.mode == "advanced":
        return collect_batch_jobs_interactive_advanced(cfg)
    return collect_batch_jobs_interactive_simple(cfg)


def map_batch_job_paths(cfg: AppCfg, runtime: str, payload: dict[str, Any]) -> dict[str, Any]:
    mapped = deepcopy(payload)
    jobs = mapped.get("jobs")
    if not isinstance(jobs, list):
        return mapped

    for idx, raw_job in enumerate(jobs, 1):
        if not isinstance(raw_job, dict):
            continue
        job = cast(dict[str, Any], raw_job)

        original_path = str(job.get("path", "")).strip()
        mapped_path = original_path
        if original_path:
            mapped_path = map_content_path(cfg, runtime, original_path)
            if runtime == "docker":
                _require_mapped_docker_path(
                    mapped_path,
                    context=f"Batch job {idx} content path",
                    configured_roots=(
                        (
                            "paths.host_data_root",
                            cfg.paths.host_data_root,
                            cfg.paths.container_data_root,
                        ),
                    ),
                )
        job["path"] = mapped_path

        original_output = str(job.get("output", "")).strip()
        mapped_output = original_output
        if original_output:
            mapped_output = resolve_mounted_torrent_path(
                cfg,
                runtime,
                original_output,
                context=f"Batch job {idx} output path",
            )
        job["output"] = mapped_output

    return mapped


def render_batch_summary(payload: dict[str, Any]) -> None:
    jobs = payload.get("jobs")
    if not isinstance(jobs, list):
        console.print("[warn]⚠ No jobs to summarize.[/]")
        return

    table = Table(title=f"Batch Jobs ({len(jobs)})", box=box.SIMPLE, show_lines=False)
    table.add_column("#", style="cyan", justify="right")
    table.add_column("Path", style="path")
    table.add_column("Output", style="path")

    for idx, raw_job in enumerate(jobs, 1):
        if not isinstance(raw_job, dict):
            continue
        job = cast(dict[str, Any], raw_job)
        table.add_row(str(idx), str(job.get("path", "")), str(job.get("output", "")))

    console.print(table)


# ----------------------------
# Main
# ----------------------------


def _default_config_path() -> str:
    """Resolve a source-checkout or user configuration without using site-packages."""
    configured = os.environ.get("MKBRR_WIZARD_CONFIG", "").strip()
    if configured:
        return _expand_path(configured)

    source_config = _script_dir() / "config.yaml"
    if source_config.is_file():
        return str(source_config)

    config_home = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(_expand_path(config_home)) if config_home else Path.home() / ".config"
    return str(base / "mkbrr-wizard" / "config.yaml")


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--config",
        default=_default_config_path(),
        help="Path to config.yaml (default: <script_dir>/config.yaml)",
    )
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--docker", action="store_true", help="Force docker runtime")
    g.add_argument("--native", action="store_true", help="Force native runtime")
    return ap.parse_args()


def sanity_checks(cfg: AppCfg) -> None:
    Path(cfg.paths.host_output_dir).mkdir(parents=True, exist_ok=True)

    # presets must exist on host for menu
    if not Path(cfg.presets_yaml_host).exists():
        console.print(f"[warn]⚠ presets.yaml not found at: {cfg.presets_yaml_host}[/]")
        console.print("[dim]    The preset menu will fall back to ['btn', 'custom'].[/]")

    # Docker runtime requires config dir mount to include presets.yaml
    if cfg.docker_support and Path(cfg.paths.host_config_dir).exists():
        # friendly reminder only
        pass


# ----------------------------
# Notification system
# ----------------------------


@dataclass
class NotifyEvent:
    """Lightweight payload for a notification-worthy event."""

    event_type: str  # create|batch|inspect|check
    success: bool
    title: str
    details: dict[str, Any] = field(default_factory=dict)


def _format_duration(seconds: float) -> str:
    """Human-friendly duration string."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    mins, secs = divmod(int(seconds), 60)
    if mins < 60:
        return f"{mins}m {secs}s"
    hours, mins = divmod(mins, 60)
    return f"{hours}h {mins}m {secs}s"


def _format_pushover_html(event: NotifyEvent) -> str:
    """Build an HTML body for a Pushover notification."""
    lines: list[str] = []

    if event.event_type == "create":
        path = event.details.get("path", "")
        preset = event.details.get("preset", "")
        elapsed = event.details.get("elapsed")
        exit_code = event.details.get("exit_code", 0)

        if event.success:
            lines.append('<font color="green"><b>✅ Torrent Created</b></font>')
        else:
            lines.append(f'<font color="red"><b>❌ Create Failed</b> (exit {exit_code})</font>')

        lines.append(f"<b>Path:</b> {path}")
        if preset:
            lines.append(f"<b>Preset:</b> {preset}")
        if elapsed is not None:
            lines.append(f"<b>Duration:</b> {_format_duration(elapsed)}")

    elif event.event_type == "batch":
        succeeded = event.details.get("succeeded", 0)
        failed = event.details.get("failed", 0)
        total = succeeded + failed
        elapsed = event.details.get("elapsed")
        result_rows = event.details.get("result_rows", [])

        if failed == 0:
            lines.append('<font color="green"><b>✅ Batch Complete</b></font>')
        elif succeeded == 0:
            lines.append('<font color="red"><b>❌ Batch Failed</b></font>')
        else:
            lines.append('<font color="#F39C12"><b>⚠ Batch Partial</b></font>')

        lines.append(
            f'<font color="green">✅ {succeeded}</font> / '
            f'<font color="red">❌ {failed}</font> of {total} job(s)'
        )

        if elapsed is not None:
            lines.append(f"<b>Duration:</b> {_format_duration(elapsed)}")

        # List failed jobs
        failed_rows = [r for r in result_rows if r[3] != 0]
        if failed_rows:
            lines.append("")
            lines.append("<b>Failed jobs:</b>")
            for idx, content_path, _output_path, code in failed_rows[:10]:
                lines.append(f"• Job {idx}: {content_path} (exit {code})")
            if len(failed_rows) > 10:
                lines.append(f"  … and {len(failed_rows) - 10} more")

    elif event.event_type in ("inspect", "check"):
        path = event.details.get("path", "")
        exit_code = event.details.get("exit_code", 0)
        elapsed = event.details.get("elapsed")
        label = "Inspect" if event.event_type == "inspect" else "Check"

        if event.success:
            lines.append(f'<font color="green"><b>✅ {label} Complete</b></font>')
        else:
            lines.append(f'<font color="red"><b>❌ {label} Failed</b> (exit {exit_code})</font>')

        lines.append(f"<b>Path:</b> {path}")
        if elapsed is not None:
            lines.append(f"<b>Duration:</b> {_format_duration(elapsed)}")

    return "<br>".join(lines)


def _format_discord_embed(event: NotifyEvent, discord_cfg: DiscordCfg) -> dict[str, Any]:
    """Build a Discord embed dict for a notification event."""
    from datetime import datetime, timezone

    fields: list[dict[str, Any]] = []
    description_lines: list[str] = []

    if event.event_type == "create":
        path = event.details.get("path", "")
        preset = event.details.get("preset", "")
        elapsed = event.details.get("elapsed")
        exit_code = event.details.get("exit_code", 0)

        color = discord_cfg.color_success if event.success else discord_cfg.color_failure
        title = "✅ Torrent Created" if event.success else f"❌ Create Failed (exit {exit_code})"

        fields.append({"name": "Path", "value": f"`{path}`", "inline": False})
        if preset:
            fields.append({"name": "Preset", "value": preset, "inline": True})
        if elapsed is not None:
            fields.append({"name": "Duration", "value": _format_duration(elapsed), "inline": True})

    elif event.event_type == "batch":
        succeeded = event.details.get("succeeded", 0)
        failed = event.details.get("failed", 0)
        total = succeeded + failed
        elapsed = event.details.get("elapsed")
        result_rows = event.details.get("result_rows", [])

        if failed == 0:
            color = discord_cfg.color_success
            title = "✅ Batch Complete"
        elif succeeded == 0:
            color = discord_cfg.color_failure
            title = "❌ Batch Failed"
        else:
            color = discord_cfg.color_partial
            title = "⚠ Batch Partial"

        fields.append(
            {
                "name": "Results",
                "value": f"✅ {succeeded} / ❌ {failed} of {total} job(s)",
                "inline": True,
            }
        )
        if elapsed is not None:
            fields.append({"name": "Duration", "value": _format_duration(elapsed), "inline": True})

        failed_rows = [r for r in result_rows if r[3] != 0]
        if failed_rows:
            fail_lines = []
            for idx, content_path, _output_path, code in failed_rows[:10]:
                fail_lines.append(f"**Job {idx}:** `{content_path}` (exit {code})")
            if len(failed_rows) > 10:
                fail_lines.append(f"… and {len(failed_rows) - 10} more")
            fields.append({"name": "Failed Jobs", "value": "\n".join(fail_lines), "inline": False})

    elif event.event_type in ("inspect", "check"):
        path = event.details.get("path", "")
        exit_code = event.details.get("exit_code", 0)
        elapsed = event.details.get("elapsed")
        label = "Inspect" if event.event_type == "inspect" else "Check"

        color = discord_cfg.color_success if event.success else discord_cfg.color_failure
        title = f"✅ {label} Complete" if event.success else f"❌ {label} Failed (exit {exit_code})"

        fields.append({"name": "Path", "value": f"`{path}`", "inline": False})
        if elapsed is not None:
            fields.append({"name": "Duration", "value": _format_duration(elapsed), "inline": True})

    else:
        color = discord_cfg.color_success if event.success else discord_cfg.color_failure
        title = event.title

    embed: dict[str, Any] = {
        "title": title,
        "color": color,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "mkbrr-wizard"},
    }
    if description_lines:
        embed["description"] = "\n".join(description_lines)
    if fields:
        embed["fields"] = fields

    return embed


class NotificationManager:
    """Fire-and-forget notification dispatcher with Pushover + Discord support.

    Runs an asyncio event loop in a daemon thread so notification HTTP calls
    never block the interactive TUI.
    """

    def __init__(self, cfg: NotificationsCfg) -> None:
        self._cfg = cfg
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._http_client: Any | None = None
        self._active = False

        if not cfg.enabled or cfg.policy == "off":
            return
        if not _has_httpx:
            console.print(
                "[warn]⚠ httpx is not installed — notifications disabled. "
                "Install with: pip install 'httpx[http2]'[/]"
            )
            return
        if not cfg.pushover.enabled and not cfg.discord.enabled:
            return

        # Spin up a background event loop
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, daemon=True, name="notify-loop"
        )
        self._thread.start()
        self._active = True

    async def _get_http_client(self) -> Any:
        """Return a shared AsyncClient instance for this manager."""
        if self._http_client is None:
            if not _has_httpx or httpx is None:
                raise RuntimeError("httpx is not available")
            self._http_client = httpx.AsyncClient(
                http2=True,
                timeout=self._cfg.timeout_seconds,
            )
        return self._http_client

    def notify(self, event: NotifyEvent) -> None:
        """Schedule a notification (fire-and-forget). Returns immediately."""
        if not self._active or self._loop is None:
            return

        # Policy filtering
        policy = self._cfg.policy
        if policy == "off":
            return
        if policy == "failures_only" and event.success:
            return
        # "summary" = always send
        dispatch_coro = self._dispatch(event)
        try:
            asyncio.run_coroutine_threadsafe(dispatch_coro, self._loop)
        except RuntimeError:
            dispatch_coro.close()
            # Loop is shutting down; ignore late notifications quietly.
            return

    async def _dispatch(self, event: NotifyEvent) -> None:
        """Gather provider tasks concurrently."""
        tasks: list[asyncio.Task[None]] = []
        if self._cfg.pushover.enabled:
            tasks.append(asyncio.ensure_future(self._send_pushover(event)))
        if self._cfg.discord.enabled:
            tasks.append(asyncio.ensure_future(self._send_discord(event)))
        if not tasks:
            return
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                console.print(f"[warn]⚠ Notification provider error: {result}[/]")

    async def _send_pushover(self, event: NotifyEvent) -> None:
        """Send an HTML notification via Pushover API."""
        po = self._cfg.pushover
        if not po.app_token or not po.user_key:
            return

        priority = po.failure_priority if not event.success else po.priority
        body = _format_pushover_html(event)

        data: dict[str, Any] = {
            "token": po.app_token,
            "user": po.user_key,
            "title": event.title,
            "message": body,
            "html": "1",
            "priority": str(priority),
        }
        if po.device:
            data["device"] = po.device

        client = await self._get_http_client()
        resp = await client.post("https://api.pushover.net/1/messages.json", data=data)
        resp.raise_for_status()

    async def _send_discord(self, event: NotifyEvent) -> None:
        """Send an embed notification via Discord webhook."""
        dc = self._cfg.discord
        if not dc.webhook_url:
            return

        embed = _format_discord_embed(event, dc)
        payload: dict[str, Any] = {"embeds": [embed]}
        if dc.username:
            payload["username"] = dc.username
        if dc.avatar_url:
            payload["avatar_url"] = dc.avatar_url

        client = await self._get_http_client()
        resp = await client.post(dc.webhook_url, json=payload)
        resp.raise_for_status()

    def shutdown(self, timeout: float = 5.0) -> None:
        """Gracefully drain pending notifications and stop the background loop."""
        if not self._active or self._loop is None or self._thread is None:
            return
        self._active = False
        loop = self._loop
        thread = self._thread

        # Drain all pending tasks before stopping — prevents the last
        # notification (e.g. a success summary) from being silently dropped.
        async def _drain() -> None:
            pending = [t for t in asyncio.all_tasks(loop) if t is not asyncio.current_task()]
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            if self._http_client is not None:
                await self._http_client.aclose()
                self._http_client = None

        drain_coro = _drain()
        try:
            drain_future = asyncio.run_coroutine_threadsafe(drain_coro, loop)
            drain_future.result(timeout=timeout)
        except RuntimeError:
            drain_coro.close()
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(loop.stop)
        except TimeoutError:
            drain_future.cancel()
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(loop.stop)
        finally:
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=timeout)
            if not thread.is_alive():
                loop.close()


def render_header(cfg: AppCfg, runtime: str, mkbrr_version: str = "unknown") -> None:
    """Render a stylish startup header using Rich."""
    table = Table(show_header=False, box=None, padding=(0, 1))
    table.add_column("key", style="cyan")
    table.add_column("val")
    table.add_row("Runtime", f"[bold]{runtime}[/]")
    table.add_row("mkbrr", mkbrr_version)
    table.add_row("Docker", f"{cfg.docker_support} (user={cfg.docker_user or 'none'})")
    table.add_row("Unraid", f"{cfg.unraid.enabled} (fuse_root={cfg.unraid.fuse_root})")
    table.add_row("Presets", cfg.presets_yaml_host)
    table.add_row("Output", cfg.paths.host_output_dir)
    table.add_row("chown", f"{cfg.chown} ({cfg.ownership.uid}:{cfg.ownership.gid})")
    w = cfg.workers
    workers_info = f"hdd={w.hdd or 'auto'}, ssd={w.ssd or 'auto'}, default={w.default or 'auto'}"
    table.add_row("Workers", workers_info)

    console.rule("[title]mkbrr Wizard[/]")
    console.print(Panel(table, title="🧙 Config", border_style="magenta", box=box.ROUNDED))


@dataclass(frozen=True)
class PlannedJob:
    index: int
    job: BatchJob
    command_spec: CommandSpec
    host_path: str


@dataclass(frozen=True)
class JobBatchRun:
    succeeded: int
    failed: int
    results: tuple[JobResult, ...]
    elapsed: float


def _validate_planned_job_paths(
    cfg: AppCfg, resolved_content: ResolvedContent, job: BatchJob
) -> None:
    if not os.path.exists(resolved_content.host_path):
        raise ValueError(f"Content path does not exist: {resolved_content.host_path}")

    host_output_path = host_torrent_output_path(cfg, job.output)
    output_dir = Path(host_output_path).parent
    if os.path.exists(host_output_path):
        raise ValueError(f"Output file already exists: {host_output_path}")
    if not output_dir.is_dir():
        raise ValueError(f"Output directory does not exist: {output_dir}")
    if not os.access(output_dir, os.W_OK):
        raise ValueError(f"Output directory is not writable: {output_dir}")


def plan_job_batch(
    cfg: AppCfg,
    jobs: Sequence[BatchJob],
    prepare_job: Callable[[int, BatchJob], PlannedJob | JobResult],
) -> tuple[tuple[PlannedJob, ...], tuple[JobResult, ...]]:
    plans: list[PlannedJob] = []
    errors: list[JobResult] = []
    for index, job in enumerate(jobs, 1):
        prepared = prepare_job(index, job)
        if isinstance(prepared, JobResult):
            errors.append(prepared)
            continue

        storage_type = detect_storage_type(
            prepared.host_path,
            fuse_root=cfg.unraid.fuse_root,
            mount_priority=cfg.unraid.mount_priority,
        )
        workers = resolve_workers(storage_type, cfg.workers)
        plans.append(
            replace(
                prepared,
                command_spec=prepared.command_spec.with_args("--workers", str(workers or 0)),
            )
        )
    return tuple(plans), tuple(errors)


def render_planning_errors(errors: Sequence[JobResult], *, item_label: str) -> None:
    console.print(f"[err]❌ {item_label} planning failed; no jobs were executed.[/]")
    for result in errors:
        console.print(f"[err]  - {item_label} {result.index}: {result.content_path}[/]")


def run_job_batch(
    cfg: AppCfg,
    executor: CommandExecutor,
    plans: Sequence[PlannedJob],
    *,
    item_label: str,
    started: float,
    show_timeout_limit: bool = False,
) -> JobBatchRun:
    succeeded = 0
    failed = 0
    results: list[JobResult] = []
    for plan in plans:
        execution = executor.run(plan.command_spec, timeout=cfg.batch.job_timeout_seconds)
        result = JobResult(plan.index, plan.job.path, plan.job.output, execution.returncode)
        results.append(result)
        if execution.timed_out:
            failed += 1
            timeout_message = (
                f" after {cfg.batch.job_timeout_seconds}s"
                if show_timeout_limit and cfg.batch.job_timeout_seconds is not None
                else ""
            )
            console.print(f"[err]❌ {item_label} {plan.index} timed out{timeout_message}[/]")
        elif result.succeeded:
            succeeded += 1
        else:
            failed += 1
            console.print(
                f"[err]❌ {item_label} {plan.index} failed with exit code {result.exit_code}[/]"
            )

    return JobBatchRun(
        succeeded=succeeded,
        failed=failed,
        results=tuple(results),
        elapsed=time.monotonic() - started,
    )


def render_job_results(summary: JobBatchRun, *, title: str, index_label: str) -> None:
    results_table = Table(
        title=f"{title} (success={summary.succeeded}, failed={summary.failed})",
        box=box.SIMPLE,
        show_lines=False,
    )
    results_table.add_column(index_label, style="cyan", justify="right")
    results_table.add_column("Path", style="path")
    results_table.add_column("Output", style="path")
    results_table.add_column("Code", justify="right")
    for result in summary.results:
        style = "ok" if result.succeeded else "err"
        results_table.add_row(
            str(result.index),
            result.content_path,
            result.output_path,
            f"[{style}]{result.exit_code}[/]",
        )
    console.print(results_table)


def handle_inspect(
    cfg: AppCfg,
    runtime: str,
    executor: CommandExecutor,
    notifier: NotificationManager,
) -> bool:
    raw = ask_path("📄 Torrent file path", history=_torrent_history)
    try:
        torrent_path = resolve_mounted_torrent_path(
            cfg,
            runtime,
            raw,
            context="Inspect torrent path",
        )
    except ValueError as e:
        console.print(f"[err]❌ {e}[/]")
        return False

    if runtime == "native" and not os.path.isfile(torrent_path):
        console.print(f"[err]❌ Torrent file not found:[/] {torrent_path}")
        return False

    verbose = ask_verbose("inspect")
    command_spec = build_inspect_command(cfg, runtime, torrent_path, verbose=verbose)
    if confirm_cmd(command_spec.argv, cwd=command_spec.cwd):
        execution = executor.run(command_spec)
        if execution.returncode == 0:
            console.print("[ok]✅ done.[/]")
        else:
            console.print(f"[err]❌ mkbrr exited with code {execution.returncode}[/]")
        notifier.notify(
            NotifyEvent(
                event_type="inspect",
                success=execution.returncode == 0,
                title="Inspect Complete" if execution.returncode == 0 else "Inspect Failed",
                details={
                    "path": raw,
                    "exit_code": execution.returncode,
                    "elapsed": execution.elapsed,
                },
            )
        )
    return True


def handle_check(
    cfg: AppCfg,
    runtime: str,
    executor: CommandExecutor,
    notifier: NotificationManager,
) -> bool:
    raw_torrent_path = ask_path("📄 Torrent file path", history=_torrent_history)
    raw_content_path = ask_path("📂 Content path to verify", history=_content_history)
    try:
        torrent_path = resolve_mounted_torrent_path(
            cfg,
            runtime,
            raw_torrent_path,
            context="Check torrent path",
        )
    except ValueError as e:
        console.print(f"[err]❌ {e}[/]")
        return False

    content_path = map_content_path(cfg, runtime, raw_content_path)
    if runtime == "docker":
        try:
            _require_mapped_docker_path(
                content_path,
                context="Check content path",
                configured_roots=(
                    (
                        "paths.host_data_root",
                        cfg.paths.host_data_root,
                        cfg.paths.container_data_root,
                    ),
                ),
            )
        except ValueError as e:
            console.print(f"[err]❌ {e}[/]")
            return False

    if runtime == "native":
        if not os.path.isfile(torrent_path):
            console.print(f"[err]❌ Torrent file not found:[/] {torrent_path}")
            return False
        if not os.path.exists(content_path):
            console.print(f"[err]❌ Content path not found:[/] {content_path}")
            return False

    verbose = ask_verbose("check")
    quiet = ask_quiet()
    workers = ask_workers()
    if workers is None:
        check_host_path = _resolve_host_path_for_detection(cfg, runtime, raw_content_path, None)
        check_storage = detect_storage_type(
            check_host_path,
            fuse_root=cfg.unraid.fuse_root,
            mount_priority=cfg.unraid.mount_priority,
        )
        workers = resolve_workers(check_storage, cfg.workers)
        if workers is not None:
            console.print(
                f"[info]ℹ Storage detected as {check_storage.upper()} → --workers {workers}[/]"
            )
        else:
            console.print(f"[info]ℹ Storage detected as {check_storage.upper()} → workers auto[/]")

    if quiet and verbose:
        console.print("[warn]⚠ Both verbose and quiet selected; preferring quiet.[/]")
        verbose = False

    command_spec = build_check_command(
        cfg,
        runtime,
        torrent_path,
        content_path,
        verbose=verbose,
        quiet=quiet,
        workers=workers,
    )
    if confirm_cmd(command_spec.argv, cwd=command_spec.cwd):
        execution = executor.run(command_spec)
        if execution.returncode == 0:
            console.print("[ok]✅ data verified.[/]")
        else:
            console.print(f"[err]❌ mkbrr exited with code {execution.returncode}[/]")
        notifier.notify(
            NotifyEvent(
                event_type="check",
                success=execution.returncode == 0,
                title="Data Verified" if execution.returncode == 0 else "Check Failed",
                details={
                    "path": raw_torrent_path,
                    "exit_code": execution.returncode,
                    "elapsed": execution.elapsed,
                },
            )
        )
    return True


def handle_batch(
    cfg: AppCfg,
    runtime: str,
    executor: CommandExecutor,
    notifier: NotificationManager,
) -> bool:
    preset = pick_preset(cfg)
    console.print(
        "[info]Using simple mode (preset-driven).[/]"
        if cfg.batch.mode == "simple"
        else "[info]Using advanced mode (per-job optional fields).[/]"
    )
    payload = collect_batch_jobs_interactive(cfg)
    try:
        payload = map_batch_job_paths(cfg, runtime, payload)
        schema = load_batch_schema()
    except (FileNotFoundError, ValueError) as e:
        console.print(f"[err]❌ {e}[/]")
        return False

    validation_errors = validate_batch_payload(payload, schema)
    if validation_errors:
        console.print("[err]❌ Batch config failed schema validation:[/]")
        for error in validation_errors:
            console.print(f"[err]  - {error}[/]")
        return False

    jobs = payload.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        console.print("[err]❌ No valid jobs found after validation.[/]")
        return False
    try:
        typed_jobs: list[BatchJob] = []
        for index, job in enumerate(jobs, 1):
            if not isinstance(job, Mapping):
                raise ValueError(f"Batch job {index} must be a mapping")
            typed_jobs.append(BatchJob.from_mapping(job))
    except ValueError as e:
        console.print(f"[err]❌ Invalid batch job: {e}[/]")
        return False
    if not typed_jobs:
        console.print("[err]❌ No valid job objects found after validation.[/]")
        return False

    render_batch_summary(payload)

    def prepare_batch_job(index: int, job: BatchJob) -> PlannedJob | JobResult:
        try:
            resolved_content = preflight_unraid_split_share(
                cfg,
                resolve_unraid_content_path(cfg, runtime, job.path),
                context=f"batch job {index}",
            )
            _validate_planned_job_paths(cfg, resolved_content, job)
        except ValueError as e:
            console.print(f"[err]❌ Job {index} planning failed: {e}[/]")
            return JobResult(index, job.path, job.output, 2)

        try:
            command_spec = build_batch_job_create_command(
                cfg,
                runtime,
                preset,
                job.model_copy(update={"path": resolved_content.runtime_path}),
                host_data_root_override=resolved_content.host_mount_override,
            )
        except ValueError as e:
            console.print(f"[err]❌ Job {index} invalid: {e}[/]")
            return JobResult(index, job.path, job.output, 2)

        return PlannedJob(
            index=index,
            job=job,
            command_spec=command_spec,
            host_path=resolved_content.host_path,
        )

    plans, planning_errors = plan_job_batch(cfg, typed_jobs, prepare_batch_job)
    if planning_errors:
        render_planning_errors(planning_errors, item_label="Job")
        return False

    preview_spec = plans[0].command_spec
    console.print(
        f"[info]About to run {len(plans)} batch job(s). Showing first job command preview.[/]"
    )
    if not confirm_cmd(preview_spec.argv, cwd=preview_spec.cwd):
        return False

    summary = run_job_batch(
        cfg,
        executor,
        plans,
        item_label="Job",
        started=time.monotonic(),
        show_timeout_limit=True,
    )
    render_job_results(summary, title="Batch Results", index_label="#")

    if summary.succeeded > 0:
        console.print(
            f"[ok]✅ mkbrr batch create completed with {summary.succeeded} successful job(s).[/]"
        )
        maybe_fix_torrent_permissions(
            cfg,
            [
                host_torrent_output_path(cfg, result.output_path)
                for result in summary.results
                if result.succeeded
            ],
        )
    else:
        console.print("[err]❌ mkbrr batch create failed for all jobs.[/]")

    notifier.notify(
        NotifyEvent(
            event_type="batch",
            success=summary.failed == 0,
            title=(
                "Batch Complete"
                if summary.failed == 0
                else "Batch Failed" if summary.succeeded == 0 else "Batch Partial"
            ),
            details={
                "succeeded": summary.succeeded,
                "failed": summary.failed,
                "result_rows": [result.as_tuple() for result in summary.results],
                "elapsed": summary.elapsed,
            },
        )
    )
    return True


def handle_split_series(
    cfg: AppCfg,
    runtime: str,
    executor: CommandExecutor,
    notifier: NotificationManager,
    *,
    preset: str,
    raw: str,
    content_path: str,
    host_data_root_override: str | None,
    episodes: list[tuple[EpisodeKey, str]],
    episode_keys: list[EpisodeKey],
) -> bool:
    try:
        inherited_include_patterns = preset_include_patterns(cfg.presets_yaml_host, preset)
    except ValueError as error:
        console.print(f"[err]❌ {error}[/]")
        return False
    if inherited_include_patterns:
        console.print(
            "[err]❌ Split series cannot use a preset with include_patterns because mkbrr "
            "combines preset and split filters. Remove the preset include_patterns first.[/]"
        )
        return False

    while True:
        range_input = cast(
            str,
            Prompt.ask("Enter episode ranges [dim](e.g. 1-11, 12-22)[/]"),
        )
        try:
            parts = parse_split_ranges(range_input, episode_keys)
            break
        except ValueError as e:
            console.print(f"[err]❌ {e}[/]")

    all_patterns: list[list[str]] = []
    try:
        for part_eps in parts:
            all_patterns.append(build_split_include_patterns(episodes, part_eps))
    except ValueError as e:
        console.print(f"[err]❌ Split plan invalid: {e}[/]")
        return False

    output_dir = cfg.paths.host_output_dir
    folder_name = Path(raw.rstrip("/").rstrip("\\")).name
    render_split_summary(folder_name, parts, all_patterns, output_dir)

    split_jobs: list[BatchJob] = []
    for index, (_part_eps, patterns) in enumerate(zip(parts, all_patterns, strict=True), 1):
        output_name = split_output_name(folder_name, index)
        host_output_path = str(Path(output_dir) / output_name)
        output_path = map_torrent_path(cfg, runtime, host_output_path)
        if output_path == host_output_path:
            content_fallback = map_content_path(cfg, runtime, host_output_path)
            if content_fallback != host_output_path:
                output_path = content_fallback
        split_jobs.append(
            BatchJob(
                path=content_path,
                output=output_path,
                include_patterns=tuple(patterns),
                fail_on_season_warning=False,
            )
        )

    def prepare_split_job(index: int, job: BatchJob) -> PlannedJob | JobResult:
        try:
            resolved_content = resolve_unraid_content_path(cfg, runtime, job.path)
            _validate_planned_job_paths(cfg, resolved_content, job)
            command_spec = build_batch_job_create_command(
                cfg,
                runtime,
                preset,
                job,
                host_data_root_override=host_data_root_override,
            )
        except ValueError as e:
            console.print(f"[err]❌ Part {index} invalid: {e}[/]")
            return JobResult(index, job.path, job.output, 2)

        return PlannedJob(
            index=index,
            job=job,
            command_spec=command_spec,
            host_path=resolved_content.host_path,
        )

    plans, planning_errors = plan_job_batch(cfg, split_jobs, prepare_split_job)
    if planning_errors:
        render_planning_errors(planning_errors, item_label="Part")
        return False

    preview_spec = plans[0].command_spec
    console.print(
        f"[info]About to run {len(plans)} split-series job(s). "
        "Showing Part 1 command preview.[/]"
    )
    if not confirm_cmd(preview_spec.argv, cwd=preview_spec.cwd):
        return False

    summary = run_job_batch(
        cfg,
        executor,
        plans,
        item_label="Part",
        started=time.monotonic(),
    )
    render_job_results(summary, title="Split Series Results", index_label="Part")

    if summary.succeeded > 0:
        console.print(
            f"[ok]✅ Split series completed with {summary.succeeded}" f" successful part(s).[/]"
        )
        successful_outputs = [
            host_torrent_output_path(cfg, result.output_path)
            for result in summary.results
            if result.succeeded
        ]
        maybe_fix_torrent_permissions(cfg, successful_outputs)
    else:
        console.print("[err]❌ Split series failed for all parts.[/]")

    notifier.notify(
        NotifyEvent(
            event_type="batch",
            success=summary.failed == 0,
            title=(
                "Split Series Complete"
                if summary.failed == 0
                else "Split Series Failed" if summary.succeeded == 0 else "Split Series Partial"
            ),
            details={
                "succeeded": summary.succeeded,
                "failed": summary.failed,
                "result_rows": [result.as_tuple() for result in summary.results],
                "elapsed": summary.elapsed,
            },
        )
    )
    return True


def handle_create(
    cfg: AppCfg,
    runtime: str,
    executor: CommandExecutor,
    notifier: NotificationManager,
) -> bool:
    preset = pick_preset(cfg)
    raw = ask_path("📂 Content path", history=_content_history)
    try:
        resolved_content = resolve_unraid_content_path(cfg, runtime, raw)
    except ValueError as e:
        console.print(f"[err]❌ {e}[/]")
        return False

    if runtime == "native" and not os.path.exists(resolved_content.runtime_path):
        console.print(f"[err]❌ Content path does not exist:[/] {resolved_content.runtime_path}")
        console.print("[dim]Tip: don't wrap the path in quotes (or let the wizard strip them).[/]")
        return False

    try:
        resolved_content = preflight_unraid_split_share(
            cfg,
            resolved_content,
            context="create",
        )
    except ValueError as e:
        console.print(f"[err]❌ {e}[/]")
        return False

    content_path = resolved_content.runtime_path
    host_data_root_override = resolved_content.host_mount_override
    scan_dir = resolved_content.host_path
    episodes = scan_episodes(scan_dir) if os.path.isdir(scan_dir) else []
    episode_keys = sorted({episode_key for episode_key, _ in episodes})
    seasons = {season for season, _ in episode_keys}
    if len(seasons) > 1:
        season_labels = ", ".join(f"S{season:02d}" for season in sorted(seasons))
        console.print(
            f"[warn]⚠ Split skipped: found multiple seasons ({season_labels}). "
            "Use a separate source folder for each season.[/]"
        )
    elif len(episode_keys) >= 2:
        console.print(
            f"[info]ℹ Found {len(episode_keys)} episode(s): "
            f"{format_episode_ranges(episode_keys)}[/]"
        )
        do_split = cast(bool, Confirm.ask("Split this season into parts?", default=False))
        if do_split:
            return handle_split_series(
                cfg,
                runtime,
                executor,
                notifier,
                preset=preset,
                raw=raw,
                content_path=content_path,
                host_data_root_override=host_data_root_override,
                episodes=episodes,
                episode_keys=episode_keys,
            )

    command_spec = build_create_command(
        cfg,
        runtime,
        content_path,
        preset,
        host_data_root_override=host_data_root_override,
    )

    host_path = _resolve_host_path_for_detection(cfg, runtime, raw, host_data_root_override)
    storage_type = detect_storage_type(
        host_path,
        fuse_root=cfg.unraid.fuse_root,
        mount_priority=cfg.unraid.mount_priority,
    )
    workers = resolve_workers(storage_type, cfg.workers)
    if workers is not None:
        command_spec = command_spec.with_args("--workers", str(workers))
        console.print(
            f"[info]ℹ Storage detected as {storage_type.upper()} → --workers {workers}[/]"
        )
    else:
        console.print(f"[info]ℹ Storage detected as {storage_type.upper()} → workers auto[/]")

    if workers is None:
        command_spec = command_spec.with_args("--workers", "0")

    if confirm_cmd(command_spec.argv, cwd=command_spec.cwd):
        outputs_before = _snapshot_torrent_outputs(cfg.paths.host_output_dir) if cfg.chown else {}
        execution = executor.run(command_spec)
        if execution.returncode == 0:
            console.print("[ok]✅ mkbrr create finished.[/]")
            outputs_after = _snapshot_torrent_outputs(cfg.paths.host_output_dir)
            maybe_fix_torrent_permissions(
                cfg,
                _changed_torrent_outputs(outputs_before, outputs_after),
            )
        else:
            console.print(f"[err]❌ mkbrr exited with code {execution.returncode}[/]")
        notifier.notify(
            NotifyEvent(
                event_type="create",
                success=execution.returncode == 0,
                title="Torrent Created" if execution.returncode == 0 else "Create Failed",
                details={
                    "path": raw,
                    "preset": preset,
                    "exit_code": execution.returncode,
                    "elapsed": execution.elapsed,
                },
            )
        )
    return True


def main() -> None:
    args = parse_args()
    config_path = Path(args.config)
    load_config_environment(config_path)
    cfg = load_config(config_path)
    sanity_checks(cfg)

    forced = "docker" if args.docker else "native" if args.native else None
    runtime = pick_runtime(cfg, forced)
    mkbrr_version = "unknown"
    if sys.stdin.isatty():
        mkbrr_version = detect_mkbrr_version(cfg, runtime)
        try:
            verify_mkbrr_compatibility(mkbrr_version)
        except RuntimeError as e:
            console.print(f"[err]❌ {e}[/]")
            raise SystemExit(2) from e

    render_header(cfg, runtime, mkbrr_version=mkbrr_version)

    notifier = NotificationManager(cfg.notifications)
    executor = CommandExecutor(backend_for_runtime(runtime))

    try:
        while True:
            console.print()  # breathing room
            action = choose_action()

            if action == "create":
                if not handle_create(cfg, runtime, executor, notifier):
                    continue

            elif action == "batch":
                if not handle_batch(cfg, runtime, executor, notifier):
                    continue

            elif action == "inspect":
                if not handle_inspect(cfg, runtime, executor, notifier):
                    continue

            elif action == "check":
                if not handle_check(cfg, runtime, executor, notifier):
                    continue

            console.rule(style="dim")
            if not Confirm.ask("Do another operation?", default=False):
                console.print("[dim]👋 Bye.[/]")
                break

    except (KeyboardInterrupt, EOFError):
        console.print("\n[dim]⏹ Interrupted. Bye.[/]")
    finally:
        notifier.shutdown()


if __name__ == "__main__":
    main()
