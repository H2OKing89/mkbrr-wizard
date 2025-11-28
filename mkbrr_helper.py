#!/usr/bin/env python3
"""
Interactive wrapper for mkbrr (dockerised).

Workflow:
- Load presets from presets.yaml
- Ask for preset (-P) from that list (or custom)
- Ask for file/folder path (host or container path)
- Convert host path to container path if needed
- Run mkbrr via `docker run ... mkbrr create ...`
"""

import os
import shlex
import subprocess
import sys

import yaml

# ---------------------------------------------------------------------------
# CONFIG: adjust these if your paths ever change
# ---------------------------------------------------------------------------

# Host → container mapping for /data
HOST_DATA_ROOT = "/mnt/user/data"
CONTAINER_DATA_ROOT = "/data"

# Host path for torrent file output
HOST_OUTPUT_DIR = "/mnt/user/data/downloads/torrents/torrentfiles"
# mkbrr will write torrent files here (inside the container)
CONTAINER_OUTPUT_DIR = "/torrentfiles"

# Target ownership for .torrent files (Unraid's nobody:users)
TARGET_UID = 99
TARGET_GID = 100

# Host path for mkbrr config
HOST_CONFIG_DIR = "/mnt/cache/appdata/mkbrr"
# Container path for mkbrr config
CONTAINER_CONFIG_DIR = "/root/.config/mkbrr"

# The mkbrr image
IMAGE = "ghcr.io/autobrr/mkbrr"

# Path to mkbrr presets.yaml on the host
PRESETS_YAML_PATH = os.path.join(HOST_CONFIG_DIR, "presets.yaml")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def host_to_container_path(path: str) -> str:
    """Convert a host path under /mnt/user/data to the container's /data path.

    If it's already a container-style path (/data/...), leave it alone.
    Otherwise, return as-is and let mkbrr complain if it's wrong.
    """
    path = path.strip()

    # Already a container path
    if path.startswith(CONTAINER_DATA_ROOT + "/") or path == CONTAINER_DATA_ROOT:
        return path

    # Normalize to absolute
    abs_path = os.path.abspath(path)

    if abs_path.startswith(HOST_DATA_ROOT):
        suffix = abs_path[len(HOST_DATA_ROOT) :]
        return CONTAINER_DATA_ROOT + suffix

    # Fallback: not under /mnt/user/data and not /data – pass through
    return path


def host_to_container_torrent_path(path: str) -> str:
    """
    Map a host .torrent path under HOST_OUTPUT_DIR to the container's /torrentfiles path.

    If it's already a container-style path (/torrentfiles/...), leave it alone.
    Otherwise, return as-is and let mkbrr complain if it's wrong.
    """
    path = path.strip()

    # Already container path
    if path.startswith(CONTAINER_OUTPUT_DIR + "/") or path == CONTAINER_OUTPUT_DIR:
        return path

    abs_path = os.path.abspath(path)

    if abs_path.startswith(HOST_OUTPUT_DIR):
        suffix = abs_path[len(HOST_OUTPUT_DIR):]
        return CONTAINER_OUTPUT_DIR + suffix

    # Fallback: unknown location, pass through
    return path


def fix_torrent_permissions(root_dir: str = HOST_OUTPUT_DIR) -> None:
    """
    Recursively chown all .torrent files under root_dir to TARGET_UID:TARGET_GID.
    Safe to run as root after mkbrr creates torrents.
    """
    if not os.path.isdir(root_dir):
        print(f"⚠️  Torrent directory does not exist: {root_dir}")
        return

    print(f"🔐 Fixing ownership of .torrent files under {root_dir} ...")

    for dirpath, _, filenames in os.walk(root_dir):
        for name in filenames:
            if not name.lower().endswith(".torrent"):
                continue

            full_path = os.path.join(dirpath, name)
            try:
                stat = os.stat(full_path)
                if stat.st_uid != TARGET_UID or stat.st_gid != TARGET_GID:
                    os.chown(full_path, TARGET_UID, TARGET_GID)
                    print(f"  🔧 chown {TARGET_UID}:{TARGET_GID} -> {full_path}")
            except FileNotFoundError:
                # File may have been removed between listing and chown
                continue
            except PermissionError as e:
                print(f"  ⚠️ Permission error on {full_path}: {e}")


def load_presets_from_yaml(path: str = PRESETS_YAML_PATH) -> list[str]:
    """Load mkbrr preset names from presets.yaml using PyYAML.

    Expects structure like:

        presets:
          btn:
            ...
          mam:
            ...

    Returns a list of preset names, with "btn" first if present.
    """
    if not os.path.exists(path):
        print(f"⚠️  presets.yaml not found at {path}, using fallback presets: ['btn', 'custom']")
        return ["btn", "custom"]

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception as e:
        print(f"⚠️  Failed to parse {path}: {e}. Using fallback presets: ['btn', 'custom']")
        return ["btn", "custom"]

    presets_node = data.get("presets") or {}

    if not isinstance(presets_node, dict) or not presets_node:
        print(f"⚠️  No valid 'presets' mapping in {path}, using fallback presets: ['btn', 'custom']")
        return ["btn", "custom"]

    presets = list(presets_node.keys())

    # Prefer 'btn' first if present
    if "btn" in presets:
        presets = ["btn"] + [p for p in presets if p != "btn"]

    return presets


def pick_preset() -> str:
    presets = load_presets_from_yaml()

    print(f"\n🎛  Preset selection (-P) (from {PRESETS_YAML_PATH}):")
    for idx, p in enumerate(presets, start=1):
        print(f"  [{idx}] {p}")

    print("\nYou can:")
    print("  - Choose by number")
    print("  - Type a preset name directly")
    print("  - Press Enter for the default [btn if available]")

    choice = input(f"\nChoose preset [1-{len(presets)} or name]: ").strip()

    # Numbered selection
    if choice.isdigit():
        idx = int(choice)
        if 1 <= idx <= len(presets):
            return presets[idx - 1]

    # Direct string input (custom or existing)
    if choice:
        if choice not in presets:
            print(f"⚠️  '{choice}' is not in presets.yaml, mkbrr may fail")
        return choice

    # Default when just hitting Enter
    if "btn" in presets:
        return "btn"
    return presets[0]


def ask_path() -> str:
    print("\n📂 Enter the path to the file or folder:")
    print("   - You can paste a *host* path (e.g. /mnt/user/data/...)")
    print("   - Or a *container* path (e.g. /data/downloads/...)")
    raw = input("\nPath: ").strip()

    if not raw:
        print("❌ No path given, aborting.")
        raise SystemExit(1)

    container_path = host_to_container_path(raw)

    # Best-effort check: if it's a host path, verify it exists
    if raw.startswith("/mnt/"):
        if not os.path.exists(raw):
            print(f"⚠️  Warning: host path does not exist: {raw}")
        else:
            print(f"✅ Host path exists: {raw}")

    print(f"🧩 Using container path inside mkbrr: {container_path}")
    return container_path


def ask_torrent_file() -> str:
    """Ask for the .torrent file path to inspect."""
    print("\n📄 Enter the path to the .torrent file to inspect:")
    print(f"   - Host path (e.g. {HOST_OUTPUT_DIR}/my-release.torrent)")
    print(f"   - Or container path (e.g. {CONTAINER_OUTPUT_DIR}/my-release.torrent)")
    raw = input("\nTorrent file path: ").strip()

    if not raw:
        print("❌ No path given, aborting.")
        raise SystemExit(1)

    container_path = host_to_container_torrent_path(raw)

    # Best-effort existence check for host paths
    if raw.startswith("/mnt/"):
        if not os.path.exists(raw):
            print(f"⚠️  Warning: host .torrent does not exist: {raw}")
        else:
            print(f"✅ Host .torrent exists: {raw}")

    print(f"🧩 Using container path inside mkbrr: {container_path}")
    return container_path


def ask_verbose() -> bool:
    """Ask if user wants verbose output for inspect."""
    ans = input("\n🔍 Verbose output? Show all metadata fields? [y/N]: ").strip().lower()
    return ans in ("y", "yes", "v", "verbose")


def ask_quiet() -> bool:
    """Ask if user wants quiet mode (only final status/percent)."""
    ans = input("\n🤫 Quiet mode? Only final status/percent on success. [y/N]: ").strip().lower()
    return ans in ("y", "yes", "q", "quiet")


def ask_workers() -> int | None:
    """Ask for number of workers (or None for automatic)."""
    raw = input("\n⚙️  Workers (leave empty for automatic): ").strip()
    if not raw:
        return None
    try:
        value = int(raw)
        if value <= 0:
            raise ValueError
        return value
    except ValueError:
        print("⚠️  Invalid workers value, ignoring.")
        return None


def build_command(container_path: str, preset: str) -> list[str]:
    """Build the full docker run command as a list of args."""
    return [
        "docker",
        "run",
        "--rm",
        "-it",
        "-w",
        CONTAINER_CONFIG_DIR,
        "-v",
        f"{HOST_DATA_ROOT}:{CONTAINER_DATA_ROOT}",
        "-v",
        f"{HOST_OUTPUT_DIR}:{CONTAINER_OUTPUT_DIR}",
        "-v",
        f"{HOST_CONFIG_DIR}:{CONTAINER_CONFIG_DIR}",
        IMAGE,
        "mkbrr",
        "create",
        container_path,
        "-P",
        preset,
        "--output-dir",
        CONTAINER_OUTPUT_DIR,
    ]


def build_inspect_command(torrent_container_path: str, verbose: bool) -> list[str]:
    """Build the docker run command for `mkbrr inspect`."""
    cmd = [
        "docker",
        "run",
        "--rm",
        "-it",
        "-w",
        CONTAINER_CONFIG_DIR,
        "-v",
        f"{HOST_DATA_ROOT}:{CONTAINER_DATA_ROOT}",
        "-v",
        f"{HOST_OUTPUT_DIR}:{CONTAINER_OUTPUT_DIR}",
        "-v",
        f"{HOST_CONFIG_DIR}:{CONTAINER_CONFIG_DIR}",
        IMAGE,
        "mkbrr",
        "inspect",
        torrent_container_path,
    ]
    if verbose:
        cmd.append("-v")  # mkbrr's --verbose / -v flag
    return cmd


def build_check_command(
    torrent_container_path: str,
    content_container_path: str,
    verbose: bool,
    quiet: bool,
    workers: int | None,
) -> list[str]:
    """Build the docker run command for `mkbrr check`."""
    cmd: list[str] = [
        "docker",
        "run",
        "--rm",
        "-it",
        "-w",
        CONTAINER_CONFIG_DIR,
        "-v",
        f"{HOST_DATA_ROOT}:{CONTAINER_DATA_ROOT}",
        "-v",
        f"{HOST_OUTPUT_DIR}:{CONTAINER_OUTPUT_DIR}",
        "-v",
        f"{HOST_CONFIG_DIR}:{CONTAINER_CONFIG_DIR}",
        IMAGE,
        "mkbrr",
        "check",
        torrent_container_path,
        content_container_path,
    ]

    # Flags come after positional args as per mkbrr docs
    if verbose:
        cmd.append("-v")
    if quiet:
        cmd.append("--quiet")
    if workers is not None:
        cmd.extend(["--workers", str(workers)])

    return cmd


def choose_action() -> str:
    """Ask the user what they want to do with mkbrr."""
    print("\n🧰 What do you want to do?")
    print("  [1] Create a torrent from a file/folder   (mkbrr create)")
    print("  [2] Inspect an existing .torrent file     (mkbrr inspect)")
    print("  [3] Check data against a .torrent file    (mkbrr check)")
    print("  [q] Quit")

    choice = input("\nChoose an option [1/2/3/q]: ").strip().lower()

    if choice in ("2", "i", "inspect"):
        return "inspect"
    if choice in ("3", "c", "check"):
        return "check"
    if choice in ("q", "quit", "exit"):
        print("👋 Bye.")
        raise SystemExit(0)

    # Default to 'create' for anything else (including empty)
    return "create"


def check_docker_available() -> bool:
    """Check if Docker is available on the system."""
    try:
        result = subprocess.run(
            ["docker", "--version"],
            capture_output=True,
            check=False,
        )
        return result.returncode == 0
    except FileNotFoundError:
        return False


def main() -> None:
    print("==========================================")
    print("  🧙 mkbrr Helper – Torrent Creator Wizard")
    print("==========================================")

    try:
        # Check Docker is available
        if not check_docker_available():
            print("❌ Docker is not available")
            sys.exit(1)

        action = choose_action()

        if action == "create":
            preset = pick_preset()
            print(f"\n🎚  Selected preset: {preset}")

            container_path = ask_path()

            cmd = build_command(container_path, preset)

            print("\n🚀 About to run:")
            print("   " + " ".join(shlex.quote(part) for part in cmd))
            confirm = input("\nProceed? [Y/n]: ").strip().lower()
            if confirm not in ("", "y", "yes"):
                print("👉 Cancelled. Nothing was run.")
                return

            # Run mkbrr create
            print("\n🛠  Running mkbrr create... (Ctrl+C to abort)")
            result = subprocess.run(cmd, check=False)

            if result.returncode == 0:
                print("\n✅ mkbrr create finished.")
                # Post-process permissions on created .torrent files
                fix_torrent_permissions()
            else:
                print(f"\n❌ mkbrr exited with code {result.returncode}")
                sys.exit(result.returncode)

        elif action == "inspect":
            torrent_path = ask_torrent_file()
            verbose = ask_verbose()

            cmd = build_inspect_command(torrent_path, verbose)

            print("\n🚀 About to run:")
            print("   " + " ".join(shlex.quote(part) for part in cmd))
            confirm = input("\nProceed? [Y/n]: ").strip().lower()
            if confirm not in ("", "y", "yes"):
                print("👉 Cancelled. Nothing was run.")
                return

            # Run mkbrr inspect
            print("\n🛠  Running mkbrr inspect... (Ctrl+C to abort)")
            result = subprocess.run(cmd, check=False)

            if result.returncode == 0:
                print("\n✅ mkbrr inspect finished.")
            else:
                print(f"\n❌ mkbrr exited with code {result.returncode}")
                sys.exit(result.returncode)

        elif action == "check":
            torrent_path = ask_torrent_file()
            print("\n📂 Now enter the path to the local content to verify.")
            content_path = ask_path()

            verbose = ask_verbose()
            quiet = ask_quiet()
            workers = ask_workers()

            cmd = build_check_command(
                torrent_container_path=torrent_path,
                content_container_path=content_path,
                verbose=verbose,
                quiet=quiet,
                workers=workers,
            )

            print("\n🚀 About to run:")
            print("   " + " ".join(shlex.quote(part) for part in cmd))
            confirm = input("\nProceed? [Y/n]: ").strip().lower()
            if confirm not in ("", "y", "yes"):
                print("👉 Cancelled. Nothing was run.")
                return

            # Run mkbrr check
            print("\n🛠  Running mkbrr check... (Ctrl+C to abort)")
            result = subprocess.run(cmd, check=False)

            if result.returncode == 0:
                print("\n✅ mkbrr check finished (data verified).")
            else:
                print(f"\n❌ mkbrr exited with code {result.returncode}")
                sys.exit(result.returncode)

    except KeyboardInterrupt:
        print("\n⏹  Interrupted by user.")
    except Exception as e:
        print(f"\n💥 Error: {e}")
        raise


if __name__ == "__main__":
    main()

