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
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
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
    from prompt_toolkit.history import InMemoryHistory

    _content_history: InMemoryHistory | None = InMemoryHistory()
    _torrent_history: InMemoryHistory | None = InMemoryHistory()
    _has_prompt_toolkit = True
except ImportError:
    # prompt_toolkit is optional; fall back to basic input
    _content_history = None
    _torrent_history = None
    _has_prompt_toolkit = False


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
class AppCfg:
    runtime: str  # auto|docker|native
    docker_support: bool
    chown: bool
    docker_user: str | None

    mkbrr: MkbrrCfg
    paths: PathsCfg
    ownership: OwnershipCfg

    presets_yaml_host: str  # absolute host path to presets.yaml
    presets_yaml_container: str  # container path to presets.yaml (docker runtime)


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
        host_data_root=_expand_path(
            str(paths_node.get("host_data_root", "/mnt/user/data"))
        ).rstrip("/"),
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

    return AppCfg(
        runtime=runtime,
        docker_support=docker_support,
        chown=chown,
        docker_user=docker_user,
        mkbrr=mkbrr,
        paths=paths,
        ownership=ownership,
        presets_yaml_host=presets_host,
        presets_yaml_container=presets_container,
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
        if raw.startswith(cfg.paths.container_data_root + "/") or raw == cfg.paths.container_data_root:
            return raw
        abs_path = os.path.abspath(raw)
        if abs_path.startswith(cfg.paths.host_data_root + "/") or abs_path == cfg.paths.host_data_root:
            return cfg.paths.container_data_root + abs_path[len(cfg.paths.host_data_root) :]
        return raw
    else:
        # container -> host
        if raw.startswith(cfg.paths.host_data_root + "/") or raw == cfg.paths.host_data_root:
            return raw
        if raw.startswith(cfg.paths.container_data_root + "/") or raw == cfg.paths.container_data_root:
            return cfg.paths.host_data_root + raw[len(cfg.paths.container_data_root) :]
        return os.path.abspath(raw)


def map_torrent_path(cfg: AppCfg, runtime: str, raw: str) -> str:
    raw = raw.strip()
    if runtime == "docker":
        # host output -> container output
        if raw.startswith(cfg.paths.container_output_dir + "/") or raw == cfg.paths.container_output_dir:
            return raw
        abs_path = os.path.abspath(raw)
        if abs_path.startswith(cfg.paths.host_output_dir + "/") or abs_path == cfg.paths.host_output_dir:
            return cfg.paths.container_output_dir + abs_path[len(cfg.paths.host_output_dir) :]
        return raw
    else:
        # container output -> host output
        if raw.startswith(cfg.paths.host_output_dir + "/") or raw == cfg.paths.host_output_dir:
            return raw
        if raw.startswith(cfg.paths.container_output_dir + "/") or raw == cfg.paths.container_output_dir:
            return cfg.paths.host_output_dir + raw[len(cfg.paths.container_output_dir) :]
        return os.path.abspath(raw)


# ----------------------------
# Docker command builder
# ----------------------------


def docker_run_base(cfg: AppCfg, workdir: str) -> list[str]:
    cmd = ["docker", "run", "--rm"]

    # Only add -it when interactive; cron/log files hate TTY
    if sys.stdin.isatty():
        cmd += ["-it"]

    if cfg.docker_user:
        cmd += ["--user", cfg.docker_user]

    cmd += [
        "-w",
        workdir,
        "-v",
        f"{cfg.paths.host_data_root}:{cfg.paths.container_data_root}",
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
        console.print(f"[warn]⚠ presets.yaml not found at {host_presets_yaml}. Using fallback: ['btn', 'custom'][/]")
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

    choice = Prompt.ask(f"Choose preset [cyan][1-{len(presets)} or name][/]", default="1")
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
        "[cyan][q][/] Quit",
        title="🧰 Action",
        border_style="cyan",
        box=box.ROUNDED,
    )
    console.print(panel)

    choice = Prompt.ask("Choose", choices=["1", "2", "3", "q"], default="1")
    if choice == "2":
        return "inspect"
    if choice == "3":
        return "check"
    if choice == "q":
        raise SystemExit(0)
    return "create"


def ask_path(prompt: str, history: InMemoryHistory | None = None) -> str:
    """Ask for a path, with optional ↑/↓ history via prompt_toolkit."""
    if _has_prompt_toolkit and history is not None:
        from prompt_toolkit import PromptSession as PS

        session: PS[str] = PS(history=history)
        try:
            raw = session.prompt(f"{prompt}: ")
        except (EOFError, KeyboardInterrupt):
            raise SystemExit(0)
    else:
        raw = Prompt.ask(prompt)

    raw = _clean_user_path(raw)
    if not raw:
        console.print("[err]❌ No path provided.[/]")
        raise SystemExit(1)
    return raw


def ask_verbose(mode: str) -> bool:
    return Confirm.ask(f"Verbose output for {mode}?", default=False)


def ask_quiet() -> bool:
    return Confirm.ask("Quiet mode for check?", default=False)


def ask_workers() -> int | None:
    s = Prompt.ask("Workers", default="auto")
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
    return Confirm.ask("Proceed?", default=True)


# ----------------------------
# Main
# ----------------------------


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--config", default="config.yaml", help="Path to config.yaml (default: ./config.yaml)"
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


def render_header(cfg: AppCfg, runtime: str) -> None:
    """Render a stylish startup header using Rich."""
    table = Table(show_header=False, box=None, padding=(0, 1))
    table.add_column("key", style="cyan")
    table.add_column("val")
    table.add_row("Runtime", f"[bold]{runtime}[/]")
    table.add_row("Docker", f"{cfg.docker_support} (user={cfg.docker_user or 'none'})")
    table.add_row("Presets", cfg.presets_yaml_host)
    table.add_row("Output", cfg.paths.host_output_dir)
    table.add_row("chown", f"{cfg.chown} ({cfg.ownership.uid}:{cfg.ownership.gid})")

    console.rule("[title]mkbrr Wizard[/]")
    console.print(Panel(table, title="🧙 Config", border_style="magenta", box=box.ROUNDED))


def main() -> None:
    args = parse_args()
    cfg = load_config(Path(args.config))
    sanity_checks(cfg)

    forced = "docker" if args.docker else "native" if args.native else None
    runtime = pick_runtime(cfg, forced)

    render_header(cfg, runtime)

    try:
        while True:
            console.print()  # breathing room
            action = choose_action()

            if action == "create":
                preset = pick_preset(cfg)
                raw = ask_path("📂 Content path", history=_content_history)
                content_path = map_content_path(cfg, runtime, raw)

                # Check existence for native mode before calling mkbrr
                if runtime == "native" and not os.path.exists(content_path):
                    console.print(f"[err]❌ Content path does not exist:[/] {content_path}")
                    console.print("[dim]Tip: don't wrap the path in quotes (or let the wizard strip them).[/]")
                    continue

                # Build command.
                # We avoid output flags entirely and rely on cwd / -w output_dir.
                if runtime == "docker":
                    cmd = docker_run_base(cfg, cfg.paths.container_output_dir) + [
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

                if confirm_cmd(cmd, cwd=cwd):
                    r = subprocess.run(cmd, cwd=cwd, check=False)
                    if r.returncode == 0:
                        console.print("[ok]✅ mkbrr create finished.[/]")
                        maybe_fix_torrent_permissions(cfg)
                    else:
                        console.print(f"[err]❌ mkbrr exited with code {r.returncode}[/]")

            elif action == "inspect":
                raw = ask_path("📄 Torrent file path", history=_torrent_history)
                torrent_path = map_torrent_path(cfg, runtime, raw)
                verbose = ask_verbose("inspect")

                if runtime == "docker":
                    cmd = docker_run_base(cfg, cfg.paths.container_config_dir) + ["inspect", torrent_path]
                else:
                    cmd = [cfg.mkbrr.binary, "inspect", torrent_path]

                if verbose:
                    cmd.append("-v")

                if confirm_cmd(cmd):
                    r = subprocess.run(cmd, check=False)
                    if r.returncode == 0:
                        console.print("[ok]✅ done.[/]")
                    else:
                        console.print(f"[err]❌ mkbrr exited with code {r.returncode}[/]")

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
                    r = subprocess.run(cmd, check=False)
                    if r.returncode == 0:
                        console.print("[ok]✅ data verified.[/]")
                    else:
                        console.print(f"[err]❌ mkbrr exited with code {r.returncode}[/]")

            console.rule(style="dim")
            if not Confirm.ask("Do another operation?", default=False):
                console.print("[dim]👋 Bye.[/]")
                break

    except (KeyboardInterrupt, EOFError):
        console.print("\n[dim]⏹ Interrupted. Bye.[/]")


if __name__ == "__main__":
    main()
