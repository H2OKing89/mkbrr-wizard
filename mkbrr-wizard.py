#!/usr/bin/env python3
"""
Interactive wrapper for mkbrr (Docker OR native), driven by config.yaml.

Rich UI edition ✨

Key points:
- runtime: auto|docker|native
- docker_support: true/false (also tolerates "ture")
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
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

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
        if s in ("true", "ture", "yes", "y", "1", "on", "enabled"):
            return True
        if s in ("false", "no", "n", "0", "off", "disabled"):
            return False
    return default


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
    split_share_preflight: str = "fail"  # off|warn|fail
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
        image=str(mkbrr_node.get("image", "ghcr.io/autobrr/mkbrr")).strip(),
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
    preflight_mode = str(unraid_node.get("split_share_preflight", "fail")).strip().lower()
    if preflight_mode not in ("off", "warn", "fail"):
        raise ValueError("unraid.split_share_preflight must be one of: off, warn, fail")

    split_share_max_entries = int(unraid_node.get("split_share_max_entries", 20000))
    if split_share_max_entries <= 0:
        raise ValueError("unraid.split_share_max_entries must be a positive integer")

    unraid = UnraidCfg(
        enabled=_coerce_bool(unraid_node.get("enabled", False), False),
        fuse_root=_expand_path(str(unraid_node.get("fuse_root", "/mnt/user"))).rstrip("/"),
        split_share_preflight=preflight_mode,
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


def _natural_disk_sort_key(path: str) -> tuple[int, str]:
    name = os.path.basename(path)
    match = re.fullmatch(r"disk(\d+)", name)
    if not match:
        return (sys.maxsize, name)
    return (int(match.group(1)), name)


def _unraid_candidate_roots() -> list[str]:
    try:
        entries = [entry.path for entry in os.scandir("/mnt") if entry.is_dir()]
    except OSError:
        return []

    disk_roots = [p for p in entries if re.fullmatch(r"disk\d+", os.path.basename(p))]
    cache_roots = [p for p in entries if re.fullmatch(r"cache(?:-.+)?", os.path.basename(p))]
    disk_roots.sort(key=_natural_disk_sort_key)
    cache_roots.sort()
    return disk_roots + cache_roots


# ----------------------------
# Storage type detection (HDD vs SSD)
# ----------------------------

_RE_UNRAID_HDD = re.compile(r"^/mnt/disk\d+(/|$)")
_RE_UNRAID_SSD = re.compile(r"^/mnt/cache(?:-.+)?(/|$)")
_RE_UNRAID_FUSE = re.compile(r"^/mnt/user(/|$)")


def _resolve_fuse_path(fuse_root: str, path: str) -> str | None:
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

    for root in _unraid_candidate_roots():
        candidate = f"{root}{relative}"
        if os.path.exists(candidate):
            return candidate

    return None


def _resolve_unraid_fuse_path(path: str, fuse_root: str = "/mnt/user") -> str | None:
    """Resolve Unraid FUSE paths to physical /mnt/diskN or /mnt/cache* mounts."""
    return _resolve_fuse_path(fuse_root, path)


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


def detect_storage_type(path: str) -> str:
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

    # Tier 1.5: Unraid FUSE path (/mnt/user) -> physical mount resolution
    if _RE_UNRAID_FUSE.match(abs_path):
        resolved = _resolve_unraid_fuse_path(abs_path)
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

    resolved = _resolve_fuse_path(fuse_root, abs_path)
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

# Matches S01E02, s01e02, S01E01E02 (captures first episode number only)
_EPISODE_RE = re.compile(r"S(\d{2,})E(\d{2,})", re.IGNORECASE)


def scan_episodes(directory: str) -> list[tuple[int, str]]:
    """Scan *directory* for video files with S##E## names.

    Returns a sorted list of ``(episode_number, filename)`` tuples.
    Only the **first** episode number in each filename is used (multi-episode
    files like ``S01E01E02`` map to the first ``E##``).
    Non-video files and files without an episode tag are silently skipped.
    """
    results: list[tuple[int, str]] = []
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
        m = _EPISODE_RE.search(name)
        if m:
            ep_num = int(m.group(2))
            results.append((ep_num, name))

    results.sort(key=lambda t: t[0])
    return results


def format_episode_ranges(episode_numbers: list[int]) -> str:
    """Format a list of episode numbers into a compact range string.

    Example: ``[1, 2, 3, 5, 6, 8]`` → ``"E01-E03, E05-E06, E08"``.
    """
    if not episode_numbers:
        return ""
    nums = sorted(episode_numbers)
    ranges: list[str] = []
    start = end = nums[0]
    for n in nums[1:]:
        if n == end + 1:
            end = n
        else:
            if start == end:
                ranges.append(f"E{start:02d}")
            else:
                ranges.append(f"E{start:02d}-E{end:02d}")
            start = end = n
    if start == end:
        ranges.append(f"E{start:02d}")
    else:
        ranges.append(f"E{start:02d}-E{end:02d}")
    return ", ".join(ranges)


def parse_split_ranges(input_str: str, available: list[int]) -> list[list[int]]:
    """Parse a user-supplied split specification into episode-number lists.

    *input_str* uses range notation separated by ``,`` or ``;`` where each
    range is ``start-end`` (inclusive).  Example: ``"1-11, 12-22"``.

    Returns a list of lists — one per part — containing the episode numbers
    that actually exist in *available*.

    Raises ``ValueError`` on:
    * overlapping ranges
    * a range that references zero available episodes
    * unparseable tokens
    """
    available_set = set(available)
    parts: list[list[int]] = []
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

        part_eps = sorted(ep for ep in range(lo, hi + 1) if ep in available_set)
        if not part_eps:
            raise ValueError(f"Range {lo}-{hi} contains no episodes found in folder")
        parts.append(part_eps)

    return parts


def build_split_include_patterns(
    episodes: list[tuple[int, str]], part_episodes: list[int]
) -> list[str]:
    """Build ``--include`` glob patterns that select exactly *part_episodes*.

    Uses the ``S##E##`` tag extracted from each filename so the pattern is
    precise (e.g. ``*S01E03*`` rather than a bare ``*E03*``).
    """
    # Build lookup: ep_num -> first matching filename
    ep_map: dict[int, str] = {}
    for ep_num, fname in episodes:
        if ep_num not in ep_map:
            ep_map[ep_num] = fname

    patterns: list[str] = []
    for ep in sorted(part_episodes):
        ep_fname = ep_map.get(ep)
        if ep_fname is None:
            continue
        m = _EPISODE_RE.search(ep_fname)
        if m:
            # e.g. "*S01E03*"
            patterns.append(f"*{m.group(0)}*")
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
    parts: list[list[int]],
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
) -> tuple[list[str], str | None]:
    """Return (cmd, cwd) for create action depending on runtime."""
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
    return cmd, cwd


def _append_bool_flag(cmd: list[str], flag: str, *, value: bool) -> None:
    if value:
        cmd.append(flag)


def build_batch_job_create_command(
    cfg: AppCfg,
    runtime: str,
    preset: str,
    job: dict[str, Any],
    host_data_root_override: str | None = None,
) -> tuple[list[str], str | None]:
    """Return (cmd, cwd) for a single batch job executed via mkbrr create."""
    content_path = str(job.get("path", "")).strip()
    output_path = str(job.get("output", "")).strip()
    if not output_path:
        raise ValueError("Batch job output path cannot be empty")

    cmd, cwd = build_create_command(
        cfg,
        runtime,
        content_path,
        preset,
        host_data_root_override=host_data_root_override,
    )
    cmd += ["--output", output_path]

    trackers = job.get("trackers")
    if isinstance(trackers, list):
        for tracker in trackers:
            tracker_text = str(tracker).strip()
            if tracker_text:
                cmd += ["--tracker", tracker_text]

    webseeds = job.get("webseeds")
    if isinstance(webseeds, list):
        for seed in webseeds:
            seed_text = str(seed).strip()
            if seed_text:
                cmd += ["--web-seed", seed_text]

    if isinstance(job.get("private"), bool):
        _append_bool_flag(cmd, "--private", value=job["private"])

    if isinstance(job.get("no_date"), bool):
        _append_bool_flag(cmd, "--no-date", value=job["no_date"])

    if isinstance(job.get("entropy"), bool):
        _append_bool_flag(cmd, "--entropy", value=job["entropy"])

    if isinstance(job.get("skip_prefix"), bool):
        _append_bool_flag(cmd, "--skip-prefix", value=job["skip_prefix"])

    if isinstance(job.get("fail_on_season_warning"), bool):
        _append_bool_flag(
            cmd,
            "--fail-on-season-warning",
            value=job["fail_on_season_warning"],
        )

    piece_length = job.get("piece_length")
    if isinstance(piece_length, int):
        cmd += ["--piece-length", str(piece_length)]

    comment = str(job.get("comment", "")).strip()
    if comment:
        cmd += ["--comment", comment]

    source = str(job.get("source", "")).strip()
    if source:
        cmd += ["--source", source]

    exclude_patterns = job.get("exclude_patterns")
    if isinstance(exclude_patterns, list):
        for pattern in exclude_patterns:
            pattern_text = str(pattern).strip()
            if pattern_text:
                cmd += ["--exclude", pattern_text]

    include_patterns = job.get("include_patterns")
    if isinstance(include_patterns, list):
        for pattern in include_patterns:
            pattern_text = str(pattern).strip()
            if pattern_text:
                cmd += ["--include", pattern_text]

    return cmd, cwd


def build_inspect_command(
    cfg: AppCfg, runtime: str, torrent_path: str, verbose: bool = False
) -> list[str]:
    """Return cmd for inspect action depending on runtime."""
    if runtime == "docker":
        cmd = docker_run_base(cfg, cfg.paths.container_config_dir) + ["inspect", torrent_path]
    else:
        cmd = [cfg.mkbrr.binary, "inspect", torrent_path]
    if verbose:
        cmd.append("-v")
    return cmd


def build_check_command(
    cfg: AppCfg,
    runtime: str,
    torrent_path: str,
    content_path: str,
    verbose: bool = False,
    quiet: bool = False,
    workers: int | None = None,
) -> list[str]:
    """Return cmd for check action depending on runtime."""
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
    return cmd


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


def maybe_fix_torrent_permissions(cfg: AppCfg) -> None:
    if not cfg.chown:
        return

    outdir = cfg.paths.host_output_dir
    if not os.path.isdir(outdir):
        console.print(f"[warn]⚠ Output dir does not exist:[/] {outdir}")
        return

    # Only try chown as root (Unraid root: yes; Ubuntu user: maybe no)
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        console.print("[warn]⚠ chown=true but not running as root; skipping chown.[/]")
        return

    uid, gid = cfg.ownership.uid, cfg.ownership.gid
    changed = 0

    for dirpath, _, files in os.walk(outdir):
        for f in files:
            if not f.lower().endswith(".torrent"):
                continue
            p = os.path.join(dirpath, f)
            try:
                st = os.stat(p)
                if st.st_uid != uid or st.st_gid != gid:
                    os.chown(p, uid, gid)
                    changed += 1
            except FileNotFoundError:
                continue
            except PermissionError as e:
                console.print(f"[warn]⚠ Permission error on {p}: {e}[/]")

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


def confirm_cmd(cmd: list[str], cwd: str | None = None) -> bool:
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
        "Piece length exponent [14-24] (blank to skip)",
        14,
        24,
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


def _is_under_root(path: str, root: str) -> bool:
    return path == root or path.startswith(root + "/")


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
        if (
            runtime == "docker"
            and original_path
            and mapped_path == original_path
            and not _is_under_root(original_path, cfg.paths.container_data_root)
        ):
            console.print(f"[warn]⚠ Job {idx} path was not remapped for docker: {original_path}[/]")

        original_output = str(job.get("output", "")).strip()
        mapped_output = original_output
        if original_output:
            mapped_output = map_torrent_path(cfg, runtime, original_output)
            if mapped_output == original_output:
                content_fallback = map_content_path(cfg, runtime, original_output)
                if content_fallback != original_output:
                    mapped_output = content_fallback
        job["output"] = mapped_output

        if (
            runtime == "docker"
            and original_output
            and mapped_output == original_output
            and not _is_under_root(original_output, cfg.paths.container_output_dir)
            and not _is_under_root(original_output, cfg.paths.container_data_root)
        ):
            console.print(
                f"[warn]⚠ Job {idx} output was not remapped for docker: {original_output}[/]"
            )

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

        asyncio.run_coroutine_threadsafe(self._dispatch(event), self._loop)

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

        asyncio.run_coroutine_threadsafe(_drain(), loop)
        thread.join(timeout=timeout)


def render_header(cfg: AppCfg, runtime: str) -> None:
    """Render a stylish startup header using Rich."""
    table = Table(show_header=False, box=None, padding=(0, 1))
    table.add_column("key", style="cyan")
    table.add_column("val")
    table.add_row("Runtime", f"[bold]{runtime}[/]")
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


def main() -> None:
    args = parse_args()
    cfg = load_config(Path(args.config))
    sanity_checks(cfg)

    forced = "docker" if args.docker else "native" if args.native else None
    runtime = pick_runtime(cfg, forced)

    render_header(cfg, runtime)

    notifier = NotificationManager(cfg.notifications)

    try:
        while True:
            console.print()  # breathing room
            action = choose_action()

            if action == "create":
                preset = pick_preset(cfg)
                raw = ask_path("📂 Content path", history=_content_history)
                content_path, host_data_root_override = resolve_unraid_content_path(
                    cfg, runtime, raw
                )

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
                if episodes and len(episodes) >= 2:
                    ep_nums = [ep for ep, _ in episodes]
                    console.print(
                        f"[info]ℹ Found {len(episodes)} episode(s): "
                        f"{format_episode_ranges(ep_nums)}[/]"
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
                                parts = parse_split_ranges(range_input, ep_nums)
                                break
                            except ValueError as e:
                                console.print(f"[err]❌ {e}[/]")

                        # --- Build include patterns for each part ---
                        all_patterns: list[list[str]] = []
                        for part_eps in parts:
                            pats = build_split_include_patterns(episodes, part_eps)
                            all_patterns.append(pats)

                        output_dir = cfg.paths.host_output_dir
                        folder_name = Path(raw.rstrip("/").rstrip("\\")).name

                        render_split_summary(folder_name, parts, all_patterns, output_dir)

                        # --- Build batch jobs and preview first command ---
                        split_jobs: list[dict[str, Any]] = []
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
                                {
                                    "path": content_path,
                                    "output": out_path,
                                    "include_patterns": pats,
                                    "fail_on_season_warning": False,
                                }
                            )

                        try:
                            preview_cmd, preview_cwd = build_batch_job_create_command(
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
                        if not confirm_cmd(preview_cmd, cwd=preview_cwd):
                            continue

                        # --- Execute each part ---
                        succeeded = 0
                        failed = 0
                        result_rows: list[tuple[int, str, str, int]] = []
                        split_t0 = time.monotonic()

                        for idx, job in enumerate(split_jobs, 1):
                            try:
                                cmd, cwd = build_batch_job_create_command(
                                    cfg,
                                    runtime,
                                    preset,
                                    job,
                                    host_data_root_override=host_data_root_override,
                                )
                            except ValueError as e:
                                failed += 1
                                result_rows.append((idx, str(job["path"]), str(job["output"]), 2))
                                console.print(f"[err]❌ Part {idx} invalid: {e}[/]")
                                continue

                            # Auto-tune workers
                            job_host_path = _resolve_host_path_for_detection(
                                cfg, runtime, raw, host_data_root_override
                            )
                            job_storage = detect_storage_type(job_host_path)
                            job_workers = resolve_workers(job_storage, cfg.workers)
                            if job_workers is not None:
                                cmd += ["--workers", str(job_workers)]

                            try:
                                r = subprocess.run(
                                    cmd,
                                    cwd=cwd,
                                    check=False,
                                    timeout=cfg.batch.job_timeout_seconds,
                                )
                                result_rows.append(
                                    (idx, str(job["path"]), str(job["output"]), r.returncode)
                                )
                                if r.returncode == 0:
                                    succeeded += 1
                                else:
                                    failed += 1
                                    console.print(
                                        f"[err]❌ Part {idx} failed with exit code"
                                        f" {r.returncode}[/]"
                                    )
                            except subprocess.TimeoutExpired:
                                failed += 1
                                result_rows.append((idx, str(job["path"]), str(job["output"]), 124))
                                console.print(f"[err]❌ Part {idx} timed out[/]")

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

                        for idx, cp, op, code in result_rows:
                            code_style = "ok" if code == 0 else "err"
                            results_table.add_row(str(idx), cp, op, f"[{code_style}]{code}[/]")
                        console.print(results_table)

                        if succeeded > 0:
                            console.print(
                                f"[ok]✅ Split series completed with {succeeded}"
                                f" successful part(s).[/]"
                            )
                            maybe_fix_torrent_permissions(cfg)
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
                                    "result_rows": result_rows,
                                    "elapsed": split_elapsed,
                                },
                            )
                        )
                        _did_split = True

                if not _did_split:
                    # Build command inline rather than via build_create_command because
                    # single-create relies on cwd (native) / -w (docker) for output
                    # placement and intentionally omits --output.  Splitting uses
                    # build_batch_job_create_command which passes an explicit --output
                    # path per part to avoid filename collisions.
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

                    # Auto-tune workers based on storage type
                    host_path = _resolve_host_path_for_detection(
                        cfg, runtime, raw, host_data_root_override
                    )
                    storage_type = detect_storage_type(host_path)
                    workers = resolve_workers(storage_type, cfg.workers)
                    if workers is not None:
                        cmd += ["--workers", str(workers)]
                        console.print(
                            f"[info]ℹ Storage detected as {storage_type.upper()} "
                            f"→ --workers {workers}[/]"
                        )
                    else:
                        console.print(
                            f"[info]ℹ Storage detected as {storage_type.upper()} "
                            f"→ workers auto[/]"
                        )

                    if confirm_cmd(cmd, cwd=cwd):
                        t0 = time.monotonic()
                        r = subprocess.run(cmd, cwd=cwd, check=False)
                        elapsed = time.monotonic() - t0
                        if r.returncode == 0:
                            console.print("[ok]✅ mkbrr create finished.[/]")
                            maybe_fix_torrent_permissions(cfg)
                        else:
                            console.print(f"[err]❌ mkbrr exited with code {r.returncode}[/]")
                        notifier.notify(
                            NotifyEvent(
                                event_type="create",
                                success=r.returncode == 0,
                                title=("Torrent Created" if r.returncode == 0 else "Create Failed"),
                                details={
                                    "path": raw,
                                    "preset": preset,
                                    "exit_code": r.returncode,
                                    "elapsed": elapsed,
                                },
                            )
                        )

            elif action == "batch":
                preset = pick_preset(cfg)
                if cfg.batch.mode == "simple":
                    console.print("[info]Using simple mode (preset-driven).[/]")
                else:
                    console.print("[info]Using advanced mode (per-job optional fields).[/]")
                payload = collect_batch_jobs_interactive(cfg)
                payload = map_batch_job_paths(cfg, runtime, payload)

                try:
                    schema = load_batch_schema()
                except (FileNotFoundError, ValueError) as e:
                    console.print(f"[err]❌ {e}[/]")
                    continue

                validation_errors = validate_batch_payload(payload, schema)
                if validation_errors:
                    console.print("[err]❌ Batch config failed schema validation:[/]")
                    for err in validation_errors:
                        console.print(f"[err]  - {err}[/]")
                    continue

                jobs = payload.get("jobs")
                if not isinstance(jobs, list) or not jobs:
                    console.print("[err]❌ No valid jobs found after validation.[/]")
                    continue

                typed_jobs: list[dict[str, Any]] = [
                    cast(dict[str, Any], job) for job in jobs if isinstance(job, dict)
                ]
                if not typed_jobs:
                    console.print("[err]❌ No valid job objects found after validation.[/]")
                    continue

                render_batch_summary(payload)

                try:
                    preview_override = None
                    if runtime == "docker":
                        preview_override = resolve_unraid_content_path(
                            cfg, runtime, str(typed_jobs[0].get("path", ""))
                        )[1]
                    preview_cmd, preview_cwd = build_batch_job_create_command(
                        cfg,
                        runtime,
                        preset,
                        typed_jobs[0],
                        host_data_root_override=preview_override,
                    )
                except ValueError as e:
                    console.print(f"[err]❌ Invalid batch job: {e}[/]")
                    continue
                console.print(
                    f"[info]About to run {len(typed_jobs)} batch job(s). Showing first job command preview.[/]"
                )
                if not confirm_cmd(preview_cmd, cwd=preview_cwd):
                    continue

                succeeded = 0
                failed = 0
                batch_result_rows: list[tuple[int, str, str, int]] = []
                batch_t0 = time.monotonic()

                for idx, job in enumerate(typed_jobs, 1):
                    output_path = str(job.get("output", "")).strip()
                    content_path = str(job.get("path", "")).strip()
                    job_override = None
                    if runtime == "docker":
                        job_override = resolve_unraid_content_path(cfg, runtime, content_path)[1]

                    try:
                        preflight_unraid_split_share(
                            cfg,
                            runtime=runtime,
                            content_path=content_path,
                            host_data_root_override=job_override,
                            context=f"batch job {idx}",
                        )
                    except ValueError as e:
                        failed += 1
                        batch_result_rows.append((idx, content_path, output_path, 2))
                        console.print(f"[err]❌ Job {idx} preflight failed: {e}[/]")
                        continue

                    try:
                        cmd, cwd = build_batch_job_create_command(
                            cfg,
                            runtime,
                            preset,
                            job,
                            host_data_root_override=job_override,
                        )
                    except ValueError as e:
                        failed += 1
                        batch_result_rows.append((idx, content_path, output_path, 2))
                        console.print(f"[err]❌ Job {idx} invalid: {e}[/]")
                        continue

                    # Auto-tune workers per job based on storage type
                    job_host_path = _resolve_host_path_for_detection(
                        cfg, runtime, content_path, job_override
                    )
                    job_storage = detect_storage_type(job_host_path)
                    job_workers = resolve_workers(job_storage, cfg.workers)
                    if job_workers is not None:
                        cmd += ["--workers", str(job_workers)]

                    try:
                        r = subprocess.run(
                            cmd,
                            cwd=cwd,
                            check=False,
                            timeout=cfg.batch.job_timeout_seconds,
                        )
                        batch_result_rows.append((idx, content_path, output_path, r.returncode))

                        if r.returncode == 0:
                            succeeded += 1
                        else:
                            failed += 1
                            console.print(
                                f"[err]❌ Job {idx} failed with exit code {r.returncode}[/]"
                            )
                    except subprocess.TimeoutExpired:
                        failed += 1
                        batch_result_rows.append((idx, content_path, output_path, 124))
                        timeout_msg = (
                            f" after {cfg.batch.job_timeout_seconds}s"
                            if cfg.batch.job_timeout_seconds is not None
                            else ""
                        )
                        console.print(f"[err]❌ Job {idx} timed out{timeout_msg}[/]")

                results_table = Table(
                    title=f"Batch Results (success={succeeded}, failed={failed})",
                    box=box.SIMPLE,
                    show_lines=False,
                )
                results_table.add_column("#", style="cyan", justify="right")
                results_table.add_column("Path", style="path")
                results_table.add_column("Output", style="path")
                results_table.add_column("Code", justify="right")

                for idx, content_path, output_path, code in batch_result_rows:
                    code_style = "ok" if code == 0 else "err"
                    results_table.add_row(
                        str(idx),
                        content_path,
                        output_path,
                        f"[{code_style}]{code}[/]",
                    )

                console.print(results_table)

                if succeeded > 0:
                    console.print(
                        f"[ok]✅ mkbrr batch create completed with {succeeded} successful job(s).[/]"
                    )
                    maybe_fix_torrent_permissions(cfg)
                else:
                    console.print("[err]❌ mkbrr batch create failed for all jobs.[/]")

                batch_elapsed = time.monotonic() - batch_t0
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
                            "result_rows": batch_result_rows,
                            "elapsed": batch_elapsed,
                        },
                    )
                )

            elif action == "inspect":
                raw = ask_path("📄 Torrent file path", history=_torrent_history)
                torrent_path = map_torrent_path(cfg, runtime, raw)
                verbose = ask_verbose("inspect")

                if runtime == "docker":
                    cmd = docker_run_base(cfg, cfg.paths.container_config_dir) + [
                        "inspect",
                        torrent_path,
                    ]
                else:
                    cmd = [cfg.mkbrr.binary, "inspect", torrent_path]

                if verbose:
                    cmd.append("-v")

                if confirm_cmd(cmd):
                    t0 = time.monotonic()
                    r = subprocess.run(cmd, check=False)
                    elapsed = time.monotonic() - t0
                    if r.returncode == 0:
                        console.print("[ok]✅ done.[/]")
                    else:
                        console.print(f"[err]❌ mkbrr exited with code {r.returncode}[/]")
                    notifier.notify(
                        NotifyEvent(
                            event_type="inspect",
                            success=r.returncode == 0,
                            title="Inspect Complete" if r.returncode == 0 else "Inspect Failed",
                            details={
                                "path": raw,
                                "exit_code": r.returncode,
                                "elapsed": elapsed,
                            },
                        )
                    )

            elif action == "check":
                raw_t = ask_path("📄 Torrent file path", history=_torrent_history)
                raw_c = ask_path("📂 Content path to verify", history=_content_history)

                torrent_path = map_torrent_path(cfg, runtime, raw_t)
                content_path = map_content_path(cfg, runtime, raw_c)

                # Validate paths before running mkbrr
                if runtime == "native":
                    if not os.path.isfile(torrent_path):
                        console.print(f"[err]❌ Torrent file not found:[/] {torrent_path}")
                        continue
                    if not os.path.exists(content_path):
                        console.print(f"[err]❌ Content path not found:[/] {content_path}")
                        continue

                verbose = ask_verbose("check")
                quiet = ask_quiet()
                workers = ask_workers()

                # If user chose auto, apply storage-type detection
                if workers is None:
                    check_host_path = _resolve_host_path_for_detection(cfg, runtime, raw_c, None)
                    check_storage = detect_storage_type(check_host_path)
                    workers = resolve_workers(check_storage, cfg.workers)
                    if workers is not None:
                        console.print(
                            f"[info]ℹ Storage detected as {check_storage.upper()} "
                            f"→ --workers {workers}[/]"
                        )
                    else:
                        console.print(
                            f"[info]ℹ Storage detected as {check_storage.upper()} "
                            f"→ workers auto[/]"
                        )

                if quiet and verbose:
                    console.print("[warn]⚠ Both verbose and quiet selected; preferring quiet.[/]")
                    verbose = False

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

                if confirm_cmd(cmd):
                    t0 = time.monotonic()
                    r = subprocess.run(cmd, check=False)
                    elapsed = time.monotonic() - t0
                    if r.returncode == 0:
                        console.print("[ok]✅ data verified.[/]")
                    else:
                        console.print(f"[err]❌ mkbrr exited with code {r.returncode}[/]")
                    notifier.notify(
                        NotifyEvent(
                            event_type="check",
                            success=r.returncode == 0,
                            title="Data Verified" if r.returncode == 0 else "Check Failed",
                            details={
                                "path": raw_t,
                                "exit_code": r.returncode,
                                "elapsed": elapsed,
                            },
                        )
                    )

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
