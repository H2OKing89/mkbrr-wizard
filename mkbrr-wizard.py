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
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import warnings
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, cast

try:
    from pydantic import BaseModel, ConfigDict, Field, ValidationError
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
    from jsonschema import Draft7Validator
except ImportError as e:
    print("❌ jsonschema is not installed. Install it with:\n   pip install jsonschema")
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

    load_dotenv()  # loads .env from cwd automatically
except ImportError:
    pass  # python-dotenv is optional


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


def _coerce_bool(v: Any, default: bool) -> bool:
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
    return default


class _StrictConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class _MkbrrInput(_StrictConfigModel):
    binary: str = "mkbrr"
    image: str = DEFAULT_MKBRR_IMAGE


class _PathsInput(_StrictConfigModel):
    host_data_root: str = "/mnt/user/data"
    container_data_root: str = "/data"
    host_output_dir: str = "/mnt/user/data/downloads/torrents/torrentfiles"
    container_output_dir: str = "/torrentfiles"
    host_config_dir: str = "/mnt/cache/appdata/mkbrr"
    container_config_dir: str = "/root/.config/mkbrr"


class _OwnershipInput(_StrictConfigModel):
    uid: int = 99
    gid: int = 100


class _BatchInput(_StrictConfigModel):
    mode: str = "simple"
    job_timeout_seconds: int | None = None


class _UnraidInput(_StrictConfigModel):
    enabled: bool = False
    fuse_root: str = "/mnt/user"
    mount_priority: str = "disk_first"
    split_share_preflight: str = "fail"
    split_share_unmapped_docker_path: str = "warn"
    split_share_max_entries: int = 20000
    split_share_follow_symlinks: bool = False


class _WorkersInput(_StrictConfigModel):
    hdd: int | str | None = 1
    ssd: int | str | None = "auto"
    default: int | str | None = "auto"


class _PushoverInput(_StrictConfigModel):
    enabled: bool = False
    app_token: str = ""
    user_key: str = ""
    priority: int = 0
    failure_priority: int = 1
    device: str = ""


class _DiscordInput(_StrictConfigModel):
    enabled: bool = False
    webhook_url: str = ""
    username: str = "mkbrr-wizard"
    avatar_url: str = ""
    color_success: int | str = 0x2ECC71
    color_failure: int | str = 0xE74C3C
    color_partial: int | str = 0xF39C12


class _NotificationsInput(_StrictConfigModel):
    enabled: bool = False
    policy: str = "summary"
    pushover: _PushoverInput = Field(default_factory=_PushoverInput)
    discord: _DiscordInput = Field(default_factory=_DiscordInput)
    timeout_seconds: int = 10


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


_LEGACY_TURE_PATHS = (
    ("docker_support",),
    ("chown",),
    ("unraid", "enabled"),
    ("unraid", "split_share_follow_symlinks"),
    ("notifications", "enabled"),
    ("notifications", "pushover", "enabled"),
    ("notifications", "discord", "enabled"),
)

_LEGACY_BOOL_PATHS = (
    (("docker_support",), True),
    (("chown",), True),
    (("unraid", "enabled"), False),
    (("unraid", "split_share_follow_symlinks"), False),
    (("notifications", "enabled"), False),
    (("notifications", "pushover", "enabled"), False),
    (("notifications", "discord", "enabled"), False),
)

_LEGACY_INT_PATHS = (
    ("ownership", "uid"),
    ("ownership", "gid"),
    ("batch", "job_timeout_seconds"),
    ("unraid", "split_share_max_entries"),
    ("notifications", "timeout_seconds"),
    ("notifications", "pushover", "priority"),
    ("notifications", "pushover", "failure_priority"),
)


def _migrate_legacy_ture(raw: dict[str, Any]) -> None:
    migrated_paths: list[str] = []
    for path_parts in _LEGACY_TURE_PATHS:
        node: dict[str, Any] = raw
        for key in path_parts[:-1]:
            child = node.get(key)
            if not isinstance(child, dict):
                break
            node = child
        else:
            field_name = path_parts[-1]
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


def _normalize_legacy_config_scalars(raw: dict[str, Any]) -> None:
    for path_parts, default in _LEGACY_BOOL_PATHS:
        resolved = _resolve_config_path(raw, path_parts)
        if resolved is None:
            continue
        node, field_name = resolved
        if field_name in node:
            node[field_name] = _coerce_bool(node[field_name], default)

    for path_parts in _LEGACY_INT_PATHS:
        resolved = _resolve_config_path(raw, path_parts)
        if resolved is None:
            continue
        node, field_name = resolved
        value = node.get(field_name)
        if value is None:
            continue
        try:
            node[field_name] = int(value)
        except (TypeError, ValueError):
            pass


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


@dataclass(frozen=True)
class PathsCfg:
    host_data_root: str
    container_data_root: str
    host_output_dir: str
    container_output_dir: str
    host_config_dir: str
    container_config_dir: str


@dataclass(frozen=True)
class OwnershipCfg:
    uid: int
    gid: int


@dataclass(frozen=True)
class MkbrrCfg:
    binary: str
    image: str


@dataclass(frozen=True)
class BatchCfg:
    mode: str  # simple|advanced
    job_timeout_seconds: int | None = None


@dataclass(frozen=True)
class UnraidCfg:
    enabled: bool = False
    fuse_root: str = "/mnt/user"
    mount_priority: str = "disk_first"  # disk_first|cache_first
    split_share_preflight: str = "fail"  # off|warn|fail
    split_share_unmapped_docker_path: str = "warn"  # off|warn|fail
    split_share_max_entries: int = 20000
    split_share_follow_symlinks: bool = False


@dataclass(frozen=True)
class WorkersCfg:
    hdd: int | None = 1  # --workers value for spinning disks (None = auto)
    ssd: int | None = None  # --workers value for SSDs/NVMe (None = auto)
    default: int | None = None  # fallback when storage type can't be determined


@dataclass(frozen=True)
class PushoverCfg:
    enabled: bool = False
    app_token: str = ""
    user_key: str = ""
    priority: int = 0  # -2 to 2
    failure_priority: int = 1
    device: str = ""


@dataclass(frozen=True)
class DiscordCfg:
    enabled: bool = False
    webhook_url: str = ""
    username: str = "mkbrr-wizard"
    avatar_url: str = ""
    color_success: int = 0x2ECC71
    color_failure: int = 0xE74C3C
    color_partial: int = 0xF39C12


@dataclass(frozen=True)
class NotificationsCfg:
    enabled: bool = False
    policy: str = "summary"  # summary|failures_only|off
    pushover: PushoverCfg = field(default_factory=PushoverCfg)
    discord: DiscordCfg = field(default_factory=DiscordCfg)
    timeout_seconds: int = 10


@dataclass(frozen=True)
class AppCfg:
    runtime: str  # auto|docker|native
    docker_support: bool
    chown: bool
    docker_user: str | None

    mkbrr: MkbrrCfg
    paths: PathsCfg
    ownership: OwnershipCfg
    batch: BatchCfg

    presets_yaml_host: str  # absolute host path to presets.yaml
    presets_yaml_container: str  # container path to presets.yaml (docker runtime)
    unraid: UnraidCfg = field(default_factory=UnraidCfg)
    notifications: NotificationsCfg = field(default_factory=NotificationsCfg)
    workers: WorkersCfg = field(default_factory=WorkersCfg)


@dataclass(frozen=True)
class BatchJob:
    path: str
    output: str
    trackers: tuple[str, ...] = ()
    webseeds: tuple[str, ...] = ()
    private: bool | None = None
    no_date: bool | None = None
    entropy: bool | None = None
    skip_prefix: bool | None = None
    fail_on_season_warning: bool | None = None
    piece_length: int | None = None
    comment: str = ""
    source: str = ""
    exclude_patterns: tuple[str, ...] = ()
    include_patterns: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> BatchJob:
        def required_text(field_name: str) -> str:
            value = raw.get(field_name)
            if not isinstance(value, str) or not value.strip():
                label = "content path" if field_name == "path" else field_name
                raise ValueError(f"Batch job {label} cannot be empty")
            return value.strip()

        def optional_text(field_name: str) -> str:
            value = raw.get(field_name)
            if value is None:
                return ""
            if not isinstance(value, str):
                raise ValueError(f"Batch job {field_name} must be a string")
            return value.strip()

        def optional_bool(field_name: str) -> bool | None:
            value = raw.get(field_name)
            if value is None:
                return None
            if not isinstance(value, bool):
                raise ValueError(f"Batch job {field_name} must be a boolean")
            return value

        def string_tuple(field_name: str) -> tuple[str, ...]:
            value = raw.get(field_name)
            if value is None:
                return ()
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise ValueError(f"Batch job {field_name} must be a list of strings")
            return tuple(item.strip() for item in value if item.strip())

        piece_length = raw.get("piece_length")
        if piece_length is not None:
            if isinstance(piece_length, bool) or not isinstance(piece_length, int):
                raise ValueError("Batch job piece_length must be an integer")
            if not 16 <= piece_length <= 27:
                raise ValueError("Batch job piece_length must be between 16 and 27")

        return cls(
            path=required_text("path"),
            output=required_text("output"),
            trackers=string_tuple("trackers"),
            webseeds=string_tuple("webseeds"),
            private=optional_bool("private"),
            no_date=optional_bool("no_date"),
            entropy=optional_bool("entropy"),
            skip_prefix=optional_bool("skip_prefix"),
            fail_on_season_warning=optional_bool("fail_on_season_warning"),
            piece_length=piece_length,
            comment=optional_text("comment"),
            source=optional_text("source"),
            exclude_patterns=string_tuple("exclude_patterns"),
            include_patterns=string_tuple("include_patterns"),
        )


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
        return subprocess.run(command.argv, cwd=command.cwd, check=False, timeout=timeout)


class NativeBackend(_SubprocessBackend):
    runtime = "native"


class DockerBackend(_SubprocessBackend):
    runtime = "docker"


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
    _normalize_legacy_config_scalars(raw)
    try:
        raw = cast(dict[str, Any], _AppConfigInput.model_validate(raw).model_dump())
    except ValidationError as e:
        errors = e.errors(include_url=False, include_input=False)
        raise ValueError(f"Invalid configuration:\n{json.dumps(errors, indent=2)}") from e

    runtime = str(raw.get("runtime", "auto")).strip().lower()
    if runtime not in ("auto", "docker", "native"):
        raise ValueError("runtime must be one of: auto, docker, native")

    docker_support = _coerce_bool(raw.get("docker_support", True), True)
    chown = _coerce_bool(raw.get("chown", True), True)
    docker_user = raw.get("docker_user")
    docker_user = str(docker_user).strip() if docker_user else None

    mkbrr_node: dict[str, Any] = cast(dict[str, Any], raw.get("mkbrr") or {})
    mkbrr = MkbrrCfg(
        binary=str(mkbrr_node.get("binary", "mkbrr")).strip(),
        image=str(mkbrr_node.get("image", DEFAULT_MKBRR_IMAGE)).strip(),
    )

    paths_node: dict[str, Any] = cast(dict[str, Any], raw.get("paths") or {})
    paths = PathsCfg(
        host_data_root=_expand_path(str(paths_node.get("host_data_root", "/mnt/user/data"))).rstrip(
            "/"
        ),
        container_data_root=str(paths_node.get("container_data_root", "/data")).rstrip("/"),
        host_output_dir=_expand_path(
            str(paths_node.get("host_output_dir", "/mnt/user/data/downloads/torrents/torrentfiles"))
        ).rstrip("/"),
        container_output_dir=str(paths_node.get("container_output_dir", "/torrentfiles")).rstrip(
            "/"
        ),
        host_config_dir=_expand_path(
            str(paths_node.get("host_config_dir", "/mnt/cache/appdata/mkbrr"))
        ).rstrip("/"),
        container_config_dir=str(
            paths_node.get("container_config_dir", "/root/.config/mkbrr")
        ).rstrip("/"),
    )

    ownership_node: dict[str, Any] = cast(dict[str, Any], raw.get("ownership") or {})
    ownership = OwnershipCfg(
        uid=int(ownership_node.get("uid", 99)),
        gid=int(ownership_node.get("gid", 100)),
    )

    batch_node: dict[str, Any] = cast(dict[str, Any], raw.get("batch") or {})
    batch_mode = str(batch_node.get("mode", "simple")).strip().lower()
    if batch_mode not in ("simple", "advanced"):
        raise ValueError("batch.mode must be one of: simple, advanced")

    timeout_raw = batch_node.get("job_timeout_seconds")
    job_timeout_seconds: int | None = None
    if timeout_raw is not None:
        timeout_val = int(timeout_raw)
        if timeout_val <= 0:
            raise ValueError("batch.job_timeout_seconds must be a positive integer")
        job_timeout_seconds = timeout_val

    batch = BatchCfg(mode=batch_mode, job_timeout_seconds=job_timeout_seconds)

    unraid_node: dict[str, Any] = cast(dict[str, Any], raw.get("unraid") or {})
    mount_priority = str(unraid_node.get("mount_priority", "disk_first")).strip().lower()
    if mount_priority not in ("disk_first", "cache_first"):
        raise ValueError("unraid.mount_priority must be one of: disk_first, cache_first")

    preflight_mode = str(unraid_node.get("split_share_preflight", "fail")).strip().lower()
    if preflight_mode not in ("off", "warn", "fail"):
        raise ValueError("unraid.split_share_preflight must be one of: off, warn, fail")

    unmapped_docker_path_mode = (
        str(unraid_node.get("split_share_unmapped_docker_path", "warn")).strip().lower()
    )
    if unmapped_docker_path_mode not in ("off", "warn", "fail"):
        raise ValueError("unraid.split_share_unmapped_docker_path must be one of: off, warn, fail")

    split_share_max_entries = int(unraid_node.get("split_share_max_entries", 20000))
    if split_share_max_entries <= 0:
        raise ValueError("unraid.split_share_max_entries must be a positive integer")

    unraid = UnraidCfg(
        enabled=_coerce_bool(unraid_node.get("enabled", False), False),
        fuse_root=_expand_path(str(unraid_node.get("fuse_root", "/mnt/user"))).rstrip("/"),
        mount_priority=mount_priority,
        split_share_preflight=preflight_mode,
        split_share_unmapped_docker_path=unmapped_docker_path_mode,
        split_share_max_entries=split_share_max_entries,
        split_share_follow_symlinks=_coerce_bool(
            unraid_node.get("split_share_follow_symlinks", False), False
        ),
    )

    presets_yaml_raw = str(raw.get("presets_yaml", "presets.yaml")).strip()

    # Expand first (handles ~/ and $HOME/ etc)
    presets_yaml_expanded = _expand_path(presets_yaml_raw)

    # If it's still not absolute after expansion, treat it as relative to host_config_dir
    if os.path.isabs(presets_yaml_expanded):
        presets_host = presets_yaml_expanded
    else:
        presets_host = str(Path(paths.host_config_dir) / presets_yaml_raw)

    # In docker, we expect presets.yaml to be available under container_config_dir
    presets_container = str(Path(paths.container_config_dir) / Path(presets_host).name)

    # ---- notifications ----
    notif_node: dict[str, Any] = cast(dict[str, Any], raw.get("notifications") or {})
    notif_enabled = _coerce_bool(notif_node.get("enabled", False), False)

    notif_policy = str(notif_node.get("policy", "summary")).strip().lower()
    if notif_policy not in ("summary", "failures_only", "off"):
        raise ValueError("notifications.policy must be one of: summary, failures_only, off")

    po_node: dict[str, Any] = cast(dict[str, Any], notif_node.get("pushover") or {})
    pushover = PushoverCfg(
        enabled=_coerce_bool(po_node.get("enabled", False), False),
        app_token=_expand_env(str(po_node.get("app_token", ""))),
        user_key=_expand_env(str(po_node.get("user_key", ""))),
        priority=int(po_node.get("priority", 0)),
        failure_priority=int(po_node.get("failure_priority", 1)),
        device=str(po_node.get("device", "")).strip(),
    )

    dc_node: dict[str, Any] = cast(dict[str, Any], notif_node.get("discord") or {})
    discord_color_success = dc_node.get("color_success", 0x2ECC71)
    discord_color_failure = dc_node.get("color_failure", 0xE74C3C)
    discord_color_partial = dc_node.get("color_partial", 0xF39C12)
    # Handle hex strings from YAML (0x... is parsed as string by YAML)
    if isinstance(discord_color_success, str):
        discord_color_success = int(discord_color_success, 0)
    if isinstance(discord_color_failure, str):
        discord_color_failure = int(discord_color_failure, 0)
    if isinstance(discord_color_partial, str):
        discord_color_partial = int(discord_color_partial, 0)

    discord = DiscordCfg(
        enabled=_coerce_bool(dc_node.get("enabled", False), False),
        webhook_url=_expand_env(str(dc_node.get("webhook_url", ""))),
        username=str(dc_node.get("username", "mkbrr-wizard")).strip(),
        avatar_url=str(dc_node.get("avatar_url", "")).strip(),
        color_success=int(discord_color_success),
        color_failure=int(discord_color_failure),
        color_partial=int(discord_color_partial),
    )

    notifications = NotificationsCfg(
        enabled=notif_enabled,
        policy=notif_policy,
        pushover=pushover,
        discord=discord,
        timeout_seconds=int(notif_node.get("timeout_seconds", 10)),
    )

    # ---- workers auto-tune ----
    workers_node: dict[str, Any] = cast(dict[str, Any], raw.get("workers") or {})

    def _parse_workers_val(v: Any, field_name: str) -> int | None:
        if v is None:
            return None
        s = str(v).strip().lower()
        if s in ("auto", ""):
            return None
        try:
            val = int(s)
            if val <= 0:
                raise ValueError(f"workers.{field_name} must be a positive integer or 'auto'")
            return val
        except (ValueError, TypeError) as err:
            raise ValueError(f"workers.{field_name} must be a positive integer or 'auto'") from err

    workers_cfg = WorkersCfg(
        hdd=_parse_workers_val(workers_node.get("hdd", 1), "hdd"),
        ssd=_parse_workers_val(workers_node.get("ssd", "auto"), "ssd"),
        default=_parse_workers_val(workers_node.get("default", "auto"), "default"),
    )

    return AppCfg(
        runtime=runtime,
        docker_support=docker_support,
        chown=chown,
        docker_user=docker_user,
        mkbrr=mkbrr,
        paths=paths,
        ownership=ownership,
        batch=batch,
        presets_yaml_host=presets_host,
        presets_yaml_container=presets_container,
        unraid=unraid,
        notifications=notifications,
        workers=workers_cfg,
    )


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


def resolve_unraid_disk_path(cfg: AppCfg, raw: str) -> str:
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
        console.print(f"[info][i] Unraid resolved content path to:[/] {resolved}")
        return resolved

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


def resolve_unraid_content_path(cfg: AppCfg, runtime: str, raw: str) -> tuple[str, str | None]:
    """Return (content_path_for_runtime, host_data_root_override_for_docker)."""
    mapped = map_content_path(cfg, runtime, raw)
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
        return mapped, None

    host_view = map_content_path(cfg, "native", mapped)
    resolved_host = resolve_unraid_disk_path(cfg, host_view)

    if runtime == "docker":
        host_override = _resolve_unraid_host_data_root(cfg, resolved_host)
        mapped_resolved = map_content_path(cfg, "docker", resolved_host)
        if host_override and (
            resolved_host.startswith(host_override + "/") or resolved_host == host_override
        ):
            mapped_resolved = cfg.paths.container_data_root + resolved_host[len(host_override) :]
        _require_mapped_docker_path(
            mapped_resolved,
            context="Content path",
            configured_roots=(
                (
                    "paths.host_data_root",
                    host_override or cfg.paths.host_data_root,
                    cfg.paths.container_data_root,
                ),
            ),
        )
        return mapped_resolved, host_override

    return resolved_host, None


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
    *,
    runtime: str,
    content_path: str,
    host_data_root_override: str | None,
    original_input_path: str | None = None,
    context: str,
) -> None:
    """Detect split-share file layouts and optionally fail before invoking mkbrr."""
    if not cfg.unraid.enabled:
        return

    mode = cfg.unraid.split_share_preflight
    if mode == "off":
        return

    fuse_root = cfg.unraid.fuse_root.rstrip("/") or "/mnt/user"
    host_data_root = cfg.paths.host_data_root.rstrip("/")
    original_host_path: str | None = None
    resolved_host_path: str | None = None

    if runtime == "docker":
        mapped = content_path.strip()
        container_root = cfg.paths.container_data_root.rstrip("/")
        if mapped != container_root and not mapped.startswith(container_root + "/"):
            mode_unmapped = cfg.unraid.split_share_unmapped_docker_path
            if mode_unmapped == "off":
                return

            msg = (
                f"Unraid preflight ({context}) skipped: docker content path is outside "
                f"{container_root}: {mapped}\n"
                "Use a mapped container path under the data root to enable split-share validation."
            )
            if mode_unmapped == "fail":
                raise ValueError(msg)
            console.print(f"[warn]⚠ {msg}[/]")
            return

        relative = mapped[len(container_root) :]
        original_host_path = f"{host_data_root}{relative}"
        base = host_data_root_override or host_data_root
        resolved_host_path = f"{base}{relative}"
    else:
        resolved_host_path = os.path.abspath(content_path)
        if original_input_path:
            mapped_original = map_content_path(cfg, "native", original_input_path)
            if mapped_original == fuse_root or mapped_original.startswith(f"{fuse_root}/"):
                original_host_path = mapped_original

        if original_host_path is None:
            suffix = (
                host_data_root[len(fuse_root) :] if host_data_root.startswith(fuse_root) else ""
            )
            if suffix and not suffix.startswith("/"):
                suffix = f"/{suffix}"

            match = re.match(r"^/mnt/(disk\d+|cache(?:-.+)?)(/.*)?$", resolved_host_path)
            if match and suffix:
                candidate_root = f"/mnt/{match.group(1)}{suffix}"
                if resolved_host_path == candidate_root or resolved_host_path.startswith(
                    candidate_root + "/"
                ):
                    relative = resolved_host_path[len(candidate_root) :]
                    original_host_path = f"{host_data_root}{relative}"

    if not original_host_path or not resolved_host_path:
        return
    if original_host_path == resolved_host_path:
        return
    if not os.path.exists(original_host_path):
        return

    missing_count, missing_examples, permission_errors, capped_scan = _detect_split_share_mismatch(
        original_host_path,
        resolved_host_path,
        max_entries=cfg.unraid.split_share_max_entries,
        follow_symlinks=cfg.unraid.split_share_follow_symlinks,
    )

    if missing_count == 0 and permission_errors == 0:
        if capped_scan:
            console.print(
                "[warn]⚠ Unraid preflight scan reached max entries; full split-share validation was not exhaustive.[/]"
            )
        return

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
        f"  original: {original_host_path}\n"
        f"  resolved: {resolved_host_path}"
    )
    if missing_examples:
        base_msg += "\n  examples: " + ", ".join(missing_examples)

    if mode == "warn":
        console.print(f"[warn]⚠ {base_msg}[/]")
        return

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
) -> CommandSpec:
    """Return the create command plan for the selected runtime."""
    if runtime == "docker":
        cmd = docker_run_base(
            cfg,
            cfg.paths.container_output_dir,
            host_data_root_override=host_data_root_override,
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
    return CommandSpec(argv=tuple(cmd), cwd=cwd)


def _append_bool_flag(cmd: list[str], flag: str, *, value: bool) -> None:
    if value:
        cmd.append(flag)


def build_batch_job_create_command(
    cfg: AppCfg,
    runtime: str,
    preset: str,
    job: BatchJob | Mapping[str, Any],
    host_data_root_override: str | None = None,
) -> CommandSpec:
    """Return the command plan for a single batch job executed via mkbrr create."""
    job = job if isinstance(job, BatchJob) else BatchJob.from_mapping(job)

    base_spec = build_create_command(
        cfg,
        runtime,
        job.path,
        preset,
        host_data_root_override=host_data_root_override,
    )
    cmd = list(base_spec.argv)
    cmd += ["--output", job.output]

    for tracker in job.trackers:
        cmd += ["--tracker", tracker]

    for seed in job.webseeds:
        cmd += ["--web-seed", seed]

    if job.private is not None:
        cmd.append(f"--private={str(job.private).lower()}")

    if job.no_date is not None:
        _append_bool_flag(cmd, "--no-date", value=job.no_date)

    if job.entropy is not None:
        _append_bool_flag(cmd, "--entropy", value=job.entropy)

    if job.skip_prefix is not None:
        _append_bool_flag(cmd, "--skip-prefix", value=job.skip_prefix)

    if job.fail_on_season_warning is not None:
        _append_bool_flag(
            cmd,
            "--fail-on-season-warning",
            value=job.fail_on_season_warning,
        )

    if job.piece_length is not None:
        cmd += ["--piece-length", str(job.piece_length)]

    if job.comment:
        cmd += ["--comment", job.comment]

    if job.source:
        cmd += ["--source", job.source]

    for pattern in job.exclude_patterns:
        cmd += ["--exclude", pattern]

    for pattern in job.include_patterns:
        cmd += ["--include", pattern]

    return CommandSpec(argv=tuple(cmd), cwd=base_spec.cwd)


def build_inspect_command(
    cfg: AppCfg, runtime: str, torrent_path: str, verbose: bool = False
) -> CommandSpec:
    """Return the inspect command plan for the selected runtime."""
    if runtime == "docker":
        cmd = docker_run_base(cfg, cfg.paths.container_config_dir) + ["inspect", torrent_path]
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
) -> CommandSpec:
    """Return the check command plan for the selected runtime."""
    if runtime == "docker":
        cmd = docker_run_base(cfg, cfg.paths.container_config_dir) + [
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
    cfg: AppCfg, workdir: str, host_data_root_override: str | None = None
) -> list[str]:
    cmd = ["docker", "run", "--rm"]

    # Only add -it when interactive; cron/log files hate TTY
    if sys.stdin.isatty():
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
        cfg.mkbrr.image,
        "mkbrr",
    ]
    return cmd


# ----------------------------
# Permissions
# ----------------------------


def _host_torrent_output_path(cfg: AppCfg, path: str) -> str:
    if _is_under_root(path, cfg.paths.container_output_dir):
        return map_torrent_path(cfg, "native", path)
    if _is_under_root(path, cfg.paths.container_data_root):
        return map_content_path(cfg, "native", path)
    return os.path.abspath(path)


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


def maybe_fix_torrent_permissions(cfg: AppCfg, torrent_paths: list[str]) -> None:
    if not cfg.chown or not torrent_paths:
        return

    # Only try chown as root (Unraid root: yes; Ubuntu user: maybe no)
    if hasattr(os, "geteuid") and os.geteuid() != 0:
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
            console.print(f"[warn]⚠ Permission error on {path}: {e}[/]")

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
    return Path(__file__).resolve().parent


def _batch_schema_path() -> Path:
    return _script_dir() / "schema" / "batch.json"


def load_batch_schema() -> dict[str, Any]:
    schema_path = _batch_schema_path()
    if not schema_path.exists():
        raise FileNotFoundError(f"Batch schema not found: {schema_path}")

    try:
        loaded = json.loads(schema_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in batch schema at {schema_path}: {e}") from e

    if not isinstance(loaded, dict):
        raise ValueError(f"Batch schema must be a JSON object at: {schema_path}")
    return cast(dict[str, Any], loaded)


def _error_path(path_parts: list[Any]) -> str:
    if not path_parts:
        return "root"
    return ".".join(str(p) for p in path_parts)


def validate_batch_payload(payload: dict[str, Any], schema: dict[str, Any]) -> list[str]:
    validator = Draft7Validator(schema)
    errors = sorted(validator.iter_errors(payload), key=lambda e: list(e.absolute_path))
    msgs: list[str] = []
    for err in errors:
        path = _error_path(list(err.absolute_path))
        msgs.append(f"{path}: {err.message}")

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
    table.add_row("Common", "trackers, private, piece_length, comment, source")
    table.add_row("Advanced", "entropy, no_date, webseeds, exclude_patterns, include_patterns")
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
            mapped_path, _ = resolve_unraid_content_path(cfg, runtime, original_path)
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
    """Return default config.yaml path relative to the script's location."""
    return str(_script_dir() / "config.yaml")


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
            loop.stop()

        drain_coro = _drain()
        try:
            drain_future = asyncio.run_coroutine_threadsafe(drain_coro, loop)
            drain_future.result(timeout=timeout)
        except RuntimeError:
            drain_coro.close()
            try:
                loop.call_soon_threadsafe(loop.stop)
            except RuntimeError:
                pass
        except TimeoutError:
            drain_future.cancel()
            try:
                loop.call_soon_threadsafe(loop.stop)
            except RuntimeError:
                pass
        finally:
            thread.join(timeout=timeout)


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
    try:
        preview_override = None
        if runtime == "docker":
            preview_override = resolve_unraid_content_path(cfg, runtime, typed_jobs[0].path)[1]
        preview_spec = build_batch_job_create_command(
            cfg,
            runtime,
            preset,
            typed_jobs[0],
            host_data_root_override=preview_override,
        )
    except ValueError as e:
        console.print(f"[err]❌ Invalid batch job: {e}[/]")
        return False
    console.print(
        f"[info]About to run {len(typed_jobs)} batch job(s). Showing first job command preview.[/]"
    )
    if not confirm_cmd(preview_spec.argv, cwd=preview_spec.cwd):
        return False

    succeeded = 0
    failed = 0
    results: list[JobResult] = []
    started = time.monotonic()
    for index, job in enumerate(typed_jobs, 1):
        job_override = None
        if runtime == "docker":
            job_override = resolve_unraid_content_path(cfg, runtime, job.path)[1]
        try:
            preflight_unraid_split_share(
                cfg,
                runtime=runtime,
                content_path=job.path,
                host_data_root_override=job_override,
                context=f"batch job {index}",
            )
        except ValueError as e:
            failed += 1
            results.append(JobResult(index, job.path, job.output, 2))
            console.print(f"[err]❌ Job {index} preflight failed: {e}[/]")
            continue

        try:
            command_spec = build_batch_job_create_command(
                cfg,
                runtime,
                preset,
                job,
                host_data_root_override=job_override,
            )
        except ValueError as e:
            failed += 1
            results.append(JobResult(index, job.path, job.output, 2))
            console.print(f"[err]❌ Job {index} invalid: {e}[/]")
            continue

        job_host_path = _resolve_host_path_for_detection(cfg, runtime, job.path, job_override)
        job_storage = detect_storage_type(
            job_host_path,
            fuse_root=cfg.unraid.fuse_root,
            mount_priority=cfg.unraid.mount_priority,
        )
        job_workers = resolve_workers(job_storage, cfg.workers)
        if job_workers is not None:
            command_spec = command_spec.with_args("--workers", str(job_workers))

        execution = executor.run(command_spec, timeout=cfg.batch.job_timeout_seconds)
        results.append(JobResult(index, job.path, job.output, execution.returncode))
        if execution.timed_out:
            failed += 1
            timeout_msg = (
                f" after {cfg.batch.job_timeout_seconds}s"
                if cfg.batch.job_timeout_seconds is not None
                else ""
            )
            console.print(f"[err]❌ Job {index} timed out{timeout_msg}[/]")
        elif execution.returncode == 0:
            succeeded += 1
        else:
            failed += 1
            console.print(f"[err]❌ Job {index} failed with exit code {execution.returncode}[/]")

    results_table = Table(
        title=f"Batch Results (success={succeeded}, failed={failed})",
        box=box.SIMPLE,
        show_lines=False,
    )
    results_table.add_column("#", style="cyan", justify="right")
    results_table.add_column("Path", style="path")
    results_table.add_column("Output", style="path")
    results_table.add_column("Code", justify="right")
    for result in results:
        style = "ok" if result.succeeded else "err"
        results_table.add_row(
            str(result.index),
            result.content_path,
            result.output_path,
            f"[{style}]{result.exit_code}[/]",
        )
    console.print(results_table)

    if succeeded > 0:
        console.print(f"[ok]✅ mkbrr batch create completed with {succeeded} successful job(s).[/]")
        maybe_fix_torrent_permissions(
            cfg,
            [
                _host_torrent_output_path(cfg, result.output_path)
                for result in results
                if result.succeeded
            ],
        )
    else:
        console.print("[err]❌ mkbrr batch create failed for all jobs.[/]")

    notifier.notify(
        NotifyEvent(
            event_type="batch",
            success=failed == 0,
            title=(
                "Batch Complete"
                if failed == 0
                else "Batch Failed" if succeeded == 0 else "Batch Partial"
            ),
            details={
                "succeeded": succeeded,
                "failed": failed,
                "result_rows": [result.as_tuple() for result in results],
                "elapsed": time.monotonic() - started,
            },
        )
    )
    return True


def main() -> None:
    args = parse_args()
    cfg = load_config(Path(args.config))
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
                preset = pick_preset(cfg)
                raw = ask_path("📂 Content path", history=_content_history)
                try:
                    content_path, host_data_root_override = resolve_unraid_content_path(
                        cfg, runtime, raw
                    )
                except ValueError as e:
                    console.print(f"[err]❌ {e}[/]")
                    continue

                # Check existence for native mode before calling mkbrr
                if runtime == "native" and not os.path.exists(content_path):
                    console.print(f"[err]❌ Content path does not exist:[/] {content_path}")
                    console.print(
                        "[dim]Tip: don't wrap the path in quotes (or let the wizard strip them).[/]"
                    )
                    continue

                try:
                    preflight_unraid_split_share(
                        cfg,
                        runtime=runtime,
                        content_path=content_path,
                        host_data_root_override=host_data_root_override,
                        original_input_path=raw,
                        context="create",
                    )
                except ValueError as e:
                    console.print(f"[err]❌ {e}[/]")
                    continue

                # --------------------------------------------------
                # Split-series detection: scan for S##E## video files
                # --------------------------------------------------
                _did_split = False
                # Use the host-side path for scanning — in Docker mode `raw` is
                # a container path that doesn't exist on the host filesystem.
                scan_dir = _resolve_host_path_for_detection(
                    cfg, runtime, raw, host_data_root_override
                )
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
                    do_split = cast(
                        bool,
                        Confirm.ask("Split this season into parts?", default=False),
                    )
                    if do_split:
                        # --- Collect split ranges ---
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

                        # --- Build include patterns for each part ---
                        all_patterns: list[list[str]] = []
                        try:
                            for part_eps in parts:
                                pats = build_split_include_patterns(episodes, part_eps)
                                all_patterns.append(pats)
                        except ValueError as e:
                            console.print(f"[err]❌ Split plan invalid: {e}[/]")
                            continue

                        output_dir = cfg.paths.host_output_dir
                        folder_name = Path(raw.rstrip("/").rstrip("\\")).name

                        render_split_summary(folder_name, parts, all_patterns, output_dir)

                        # --- Build batch jobs and preview first command ---
                        split_jobs: list[BatchJob] = []
                        for idx, (_part_eps, pats) in enumerate(
                            zip(parts, all_patterns, strict=True), 1
                        ):
                            out_name = split_output_name(folder_name, idx)
                            host_out_path = str(Path(output_dir) / out_name)
                            out_path = map_torrent_path(cfg, runtime, host_out_path)
                            if out_path == host_out_path:
                                content_fallback = map_content_path(cfg, runtime, host_out_path)
                                if content_fallback != host_out_path:
                                    out_path = content_fallback
                            split_jobs.append(
                                BatchJob(
                                    path=content_path,
                                    output=out_path,
                                    include_patterns=tuple(pats),
                                    fail_on_season_warning=False,
                                )
                            )

                        try:
                            preview_spec = build_batch_job_create_command(
                                cfg,
                                runtime,
                                preset,
                                split_jobs[0],
                                host_data_root_override=host_data_root_override,
                            )
                        except ValueError as e:
                            console.print(f"[err]❌ {e}[/]")
                            continue

                        console.print(
                            f"[info]About to run {len(split_jobs)} split-series job(s). "
                            f"Showing Part 1 command preview.[/]"
                        )
                        if not confirm_cmd(preview_spec.argv, cwd=preview_spec.cwd):
                            continue

                        # --- Execute each part ---
                        succeeded = 0
                        failed = 0
                        results: list[JobResult] = []
                        split_t0 = time.monotonic()

                        for idx, job in enumerate(split_jobs, 1):
                            try:
                                command_spec = build_batch_job_create_command(
                                    cfg,
                                    runtime,
                                    preset,
                                    job,
                                    host_data_root_override=host_data_root_override,
                                )
                            except ValueError as e:
                                failed += 1
                                results.append(JobResult(idx, job.path, job.output, 2))
                                console.print(f"[err]❌ Part {idx} invalid: {e}[/]")
                                continue

                            # Auto-tune workers
                            job_host_path = _resolve_host_path_for_detection(
                                cfg, runtime, raw, host_data_root_override
                            )
                            job_storage = detect_storage_type(
                                job_host_path,
                                fuse_root=cfg.unraid.fuse_root,
                                mount_priority=cfg.unraid.mount_priority,
                            )
                            job_workers = resolve_workers(job_storage, cfg.workers)
                            if job_workers is not None:
                                command_spec = command_spec.with_args("--workers", str(job_workers))

                            execution = executor.run(
                                command_spec,
                                timeout=cfg.batch.job_timeout_seconds,
                            )
                            results.append(
                                JobResult(idx, job.path, job.output, execution.returncode)
                            )
                            if execution.timed_out:
                                failed += 1
                                console.print(f"[err]❌ Part {idx} timed out[/]")
                            elif execution.returncode == 0:
                                succeeded += 1
                            else:
                                failed += 1
                                console.print(
                                    f"[err]❌ Part {idx} failed with exit code"
                                    f" {execution.returncode}[/]"
                                )

                        # --- Results table ---
                        results_table = Table(
                            title=f"Split Series Results"
                            f" (success={succeeded}, failed={failed})",
                            box=box.SIMPLE,
                            show_lines=False,
                        )
                        results_table.add_column("Part", style="cyan", justify="right")
                        results_table.add_column("Path", style="path")
                        results_table.add_column("Output", style="path")
                        results_table.add_column("Code", justify="right")

                        for result in results:
                            code_style = "ok" if result.succeeded else "err"
                            results_table.add_row(
                                str(result.index),
                                result.content_path,
                                result.output_path,
                                f"[{code_style}]{result.exit_code}[/]",
                            )
                        console.print(results_table)

                        if succeeded > 0:
                            console.print(
                                f"[ok]✅ Split series completed with {succeeded}"
                                f" successful part(s).[/]"
                            )
                            successful_outputs = [
                                _host_torrent_output_path(cfg, result.output_path)
                                for result in results
                                if result.succeeded
                            ]
                            maybe_fix_torrent_permissions(cfg, successful_outputs)
                        else:
                            console.print("[err]❌ Split series failed for all parts.[/]")

                        split_elapsed = time.monotonic() - split_t0
                        notifier.notify(
                            NotifyEvent(
                                event_type="batch",
                                success=failed == 0,
                                title=(
                                    "Split Series Complete"
                                    if failed == 0
                                    else (
                                        "Split Series Failed"
                                        if succeeded == 0
                                        else "Split Series Partial"
                                    )
                                ),
                                details={
                                    "succeeded": succeeded,
                                    "failed": failed,
                                    "result_rows": [result.as_tuple() for result in results],
                                    "elapsed": split_elapsed,
                                },
                            )
                        )
                        _did_split = True

                if not _did_split:
                    command_spec = build_create_command(
                        cfg,
                        runtime,
                        content_path,
                        preset,
                        host_data_root_override=host_data_root_override,
                    )

                    # Auto-tune workers based on storage type
                    host_path = _resolve_host_path_for_detection(
                        cfg, runtime, raw, host_data_root_override
                    )
                    storage_type = detect_storage_type(
                        host_path,
                        fuse_root=cfg.unraid.fuse_root,
                        mount_priority=cfg.unraid.mount_priority,
                    )
                    workers = resolve_workers(storage_type, cfg.workers)
                    if workers is not None:
                        command_spec = command_spec.with_args("--workers", str(workers))
                        console.print(
                            f"[info]ℹ Storage detected as {storage_type.upper()} "
                            f"→ --workers {workers}[/]"
                        )
                    else:
                        console.print(
                            f"[info]ℹ Storage detected as {storage_type.upper()} "
                            f"→ workers auto[/]"
                        )

                    if confirm_cmd(command_spec.argv, cwd=command_spec.cwd):
                        outputs_before = (
                            _snapshot_torrent_outputs(cfg.paths.host_output_dir)
                            if cfg.chown
                            else {}
                        )
                        execution = executor.run(command_spec)
                        if execution.returncode == 0:
                            console.print("[ok]✅ mkbrr create finished.[/]")
                            outputs_after = _snapshot_torrent_outputs(cfg.paths.host_output_dir)
                            maybe_fix_torrent_permissions(
                                cfg,
                                _changed_torrent_outputs(outputs_before, outputs_after),
                            )
                        else:
                            console.print(
                                f"[err]❌ mkbrr exited with code {execution.returncode}[/]"
                            )
                        notifier.notify(
                            NotifyEvent(
                                event_type="create",
                                success=execution.returncode == 0,
                                title=(
                                    "Torrent Created"
                                    if execution.returncode == 0
                                    else "Create Failed"
                                ),
                                details={
                                    "path": raw,
                                    "preset": preset,
                                    "exit_code": execution.returncode,
                                    "elapsed": execution.elapsed,
                                },
                            )
                        )

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
