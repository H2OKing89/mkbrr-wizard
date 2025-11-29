#!/usr/bin/env python3
"""
Interactive hardlink wizard for BTN-style season packs.

Flow:
- Ask for source series directory (e.g. /mnt/user/data/videos/anime-shows/...).
- Detect "Season 01", "Season 02", ... directories.
- Let you choose which season(s) to hardlink.
- Guess a BTN-style series slug from the folder name (e.g. Yu.Yu.Hakusho) and let you override.
- Ask for destination root (default: Unraid seedvault path).
- Ask dry-run vs live.
- For each episode:
  - Parse SxxExx + title from filename.
  - Build BTN-esque filename:
      Series.SxxExx.Episode.Title.Resolution.Source[.Remux].Audio.Codec-Group.mkv
  - Hardlink into a BTN-style season pack folder under the destination root.
"""

import os
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# CONFIG DEFAULTS (edit these to taste)
# ---------------------------------------------------------------------------

DEFAULT_DST_ROOT = Path("/mnt/user/data/downloads/torrents/qbittorrent/seedvault/anime-shows")
DEFAULT_GROUP = "H2OKing"
DEFAULT_DRY_RUN = True  # script will ask, this is just the default


# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------


def log(msg: str) -> None:
    print(msg)


def warn(msg: str) -> None:
    print(f"[WARN] {msg}", file=sys.stderr)


def error(msg: str) -> None:
    print(f"[ERROR] {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Episode parsing & naming
# ---------------------------------------------------------------------------

# Slightly more flexible: allow 2 or 3 digit episode numbers
EP_REGEX = re.compile(r"S(?P<season>\d{2})E(?P<ep>\d{2,3})\s*-\s*\d{3}\s*-\s*(?P<title>.+?)\s*\[")


def parse_episode_info(name: str) -> tuple[int, int, str] | None:
    """
    Extract (season, episode, title) from a filename stem.

    Example:
        'Yu Yu Hakusho (1992) - S01E01 - 001 - Surprised to be Dead [Bluray-1080p ...'
        -> (1, 1, 'Surprised to be Dead')
    """
    m = EP_REGEX.search(name)
    if not m:
        return None
    season = int(m.group("season"))
    ep = int(m.group("ep"))
    title = m.group("title").strip()
    return season, ep, title


def slugify_title(title: str) -> str:
    """
    Convert an episode title to BTN-friendly dotted form.

    'Surprised to be Dead' -> 'Surprised.to.be.Dead'
    'Yusuke vs. Rando 99 Attacks' -> 'Yusuke.vs.Rando.99.Attacks'
    """
    s = re.sub(r"[^A-Za-z0-9]+", ".", title)
    s = re.sub(r"\.+", ".", s)  # collapse multiple dots
    return s.strip(".")


def detect_resolution(name: str) -> str:
    m = re.search(r"(\d{3,4}p)", name, re.IGNORECASE)
    if m:
        return m.group(1)
    # Fallback
    return "1080p"


def detect_source(name: str) -> str:
    lower = name.lower()
    if "bluray" in lower or "blu-ray" in lower:
        return "BluRay"
    if "web" in lower:
        return "WEB"
    # Fallback
    return "BluRay"


def detect_remux(name: str) -> bool:
    return "remux" in name.lower()


def detect_audio(name: str) -> str:
    """
    Map whatever is in the filename to a compact label:
    e.g. 'TrueHD 5.1' -> 'TrueHD', 'DTS-HD MA' -> 'DTS-HD.MA'
    """
    candidates = [
        ("DTS-HD.MA", "DTS-HD.MA"),
        ("DTS-HD MA", "DTS-HD.MA"),
        ("DTS-HD", "DTS-HD"),
        ("TrueHD", "TrueHD"),
        ("FLAC", "FLAC"),
        ("EAC3", "EAC3"),
        ("DDP", "DDP"),
        ("AC3", "AC3"),
        ("AAC", "AAC"),
    ]
    lower = name.lower()
    for needle, label in candidates:
        if needle.lower() in lower:
            return label
    # Fallback
    return "TrueHD"


def detect_codec(name: str) -> str:
    lower = name.lower()
    if "hevc" in lower or "x265" in lower or "h265" in lower:
        return "H.265"
    if "h264" in lower or "x264" in lower:
        return "H.264"
    # Fallback
    return "H.264"


def build_dest_filename(
    series_slug: str,
    group: str,
    season: int,
    episode: int,
    title: str,
    src_name_for_meta: str,
) -> str:
    """
    Build BTN-esque filename based on parsed metadata.

    Series.SxxExx.Ep.Title.Resolution.Source[.Remux].Audio.Codec-Group.mkv
    """
    ep_title_slug = slugify_title(title)
    resolution = detect_resolution(src_name_for_meta)
    source = detect_source(src_name_for_meta)
    remux = detect_remux(src_name_for_meta)
    audio = detect_audio(src_name_for_meta)
    codec = detect_codec(src_name_for_meta)

    parts = [
        f"{series_slug}.S{season:02d}E{episode:02d}",
        ep_title_slug,
        resolution,
        source,
    ]
    if remux:
        parts.append("Remux")
    base = ".".join(parts)
    return f"{base}.{audio}.{codec}-{group}.mkv"


def season_pack_dir(dst_root: Path, series_slug: str, season: int, group: str) -> Path:
    """
    Compute the season pack directory name used for BTN-style folders.

    Example:
      Yu.Yu.Hakusho.S01.BluRay.1080p.BluRay.Remux.TrueHD.H.264-H2OKing
    """
    # These are basically fixed for this release set
    resolution = "1080p"
    source = "BluRay"
    audio = "TrueHD"
    codec = "H.264"

    folder_name = (
        f"{series_slug}.S{season:02d}."
        f"{source}.{resolution}.{source}.Remux."
        f"{audio}.{codec}-{group}"
    )
    return dst_root / folder_name


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------


def ensure_hardlink(src: Path, dst: Path, dry_run: bool) -> str:
    """
    Create a hardlink from src -> dst (if needed).

    Returns a status string: 'linked', 'exists-same', 'exists-different',
    'would-link', 'exists', 'missing-src', or 'error'.
    """
    if not src.exists():
        return "missing-src"

    if dst.exists():
        try:
            if not dry_run:
                src_stat = src.stat()
                dst_stat = dst.stat()
                if src_stat.st_ino == dst_stat.st_ino and src_stat.st_dev == dst_stat.st_dev:
                    return "exists-same"
                else:
                    return "exists-different"
            else:
                # In dry-run we just report that something exists
                return "exists"
        except OSError:
            return "error"

    if dry_run:
        return "would-link"

    try:
        os.link(src, dst)
        return "linked"
    except OSError:
        return "error"


# ---------------------------------------------------------------------------
# Interactive helpers
# ---------------------------------------------------------------------------


def ask_src_root() -> Path:
    print("\n📂 Enter the *series* directory path.")
    print("   Example:")
    print("   /mnt/user/data/videos/anime-shows/Yu Yu Hakusho (1992) {imdb-tt0185133}")
    raw = input("\nSeries directory: ").strip()
    if not raw:
        error("No path given, aborting.")
        raise SystemExit(1)

    p = Path(raw)
    if not p.is_dir():
        error(f"Source root does not exist or is not a directory: {p}")
        raise SystemExit(1)

    return p


def find_season_dirs(src_root: Path) -> list[tuple[int, Path]]:
    """
    Return list of (season_number, path) for dirs like 'Season 01', 'Season 02', etc.
    """
    results: list[tuple[int, Path]] = []
    for entry in sorted(src_root.iterdir()):
        if not entry.is_dir():
            continue
        m = re.match(r"Season\s+(\d+)", entry.name, re.IGNORECASE)
        if not m:
            continue
        season = int(m.group(1))
        results.append((season, entry))
    return results


def choose_seasons(seasons: list[tuple[int, Path]]) -> list[tuple[int, Path]]:
    """
    Let the user pick one or more seasons from the detected list.
    """
    if not seasons:
        error("No 'Season XX' directories found under the source root.")
        raise SystemExit(1)

    print("\n📁 Detected seasons:")
    for idx, (season, path) in enumerate(seasons, start=1):
        print(f"  [{idx}] Season {season:02d}  ->  {path.name}")

    print("\nYou can:")
    print("  - Enter a single number (e.g. 1)")
    print("  - Enter multiple numbers separated by commas (e.g. 1,3,4)")
    print("  - Type 'all' to process all detected seasons")
    choice = input("\nWhich season(s) to hardlink? [all]: ").strip().lower()

    if not choice or choice in ("all", "a"):
        return seasons

    # Parse comma-separated indices
    selected: list[tuple[int, Path]] = []
    parts = [part.strip() for part in choice.split(",") if part.strip()]
    for part in parts:
        if not part.isdigit():
            warn(f"Ignoring invalid selection: {part!r}")
            continue
        idx = int(part)
        if 1 <= idx <= len(seasons):
            selected.append(seasons[idx - 1])
        else:
            warn(f"Ignoring out-of-range index: {idx}")

    if not selected:
        error("No valid seasons selected, aborting.")
        raise SystemExit(1)

    return selected


def guess_series_slug(src_root: Path) -> str:
    """
    Guess a BTN-style slug from the series folder name.

    'Yu Yu Hakusho (1992) {imdb-tt0185133}' -> 'Yu.Yu.Hakusho'
    """
    name = src_root.name
    # Drop year in parentheses and imdb/extra in braces
    name = re.sub(r"\(.*?\)", "", name)
    name = re.sub(r"\{.*?\}", "", name)
    name = name.strip()
    slug = re.sub(r"[^A-Za-z0-9]+", ".", name)
    slug = re.sub(r"\.+", ".", slug).strip(".")
    return slug


def ask_series_slug(src_root: Path) -> str:
    guess = guess_series_slug(src_root)
    ans = input(f"\n🏷  Series slug (BTN-style) [{guess}]: ").strip()
    return ans or guess


def ask_group(default_group: str = DEFAULT_GROUP) -> str:
    ans = input(f"\n👥 Release group tag [{default_group}]: ").strip()
    return ans or default_group


def ask_dst_root(default_dst: Path = DEFAULT_DST_ROOT) -> Path:
    ans = input(f"\n📦 Destination root for season packs [{default_dst}]: ").strip()
    if not ans:
        return default_dst
    p = Path(ans)
    return p


def ask_dry_run(default: bool = DEFAULT_DRY_RUN) -> bool:
    default_label = "Y/n" if default else "y/N"
    ans = input(f"\n🧪 Dry-run only (no changes)? [{default_label}]: ").strip().lower()
    if not ans:
        return default
    return ans in ("y", "yes")


# ---------------------------------------------------------------------------
# Main processing
# ---------------------------------------------------------------------------


def process_seasons(
    src_root: Path,
    seasons: list[tuple[int, Path]],
    dst_root: Path,
    series_slug: str,
    group: str,
    dry_run: bool,
) -> None:
    total = 0
    linked = 0
    already = 0
    skipped = 0
    errors = 0
    would_link = 0

    for season_num, season_dir in seasons:
        log(f"\n--- Processing {season_dir.name} (Season {season_num:02d}) ---")

        for src_file in sorted(season_dir.glob("*.mkv")):
            stem = src_file.stem

            parsed = parse_episode_info(stem)
            if not parsed:
                warn(f"Skipping (unparsed) {src_file.name}")
                skipped += 1
                continue

            file_season, ep_num, title = parsed

            # Sanity: season from filename should match directory season
            if file_season != season_num:
                warn(
                    f"Season mismatch for {src_file.name} "
                    f"(dir season {season_num}, file season {file_season})"
                )

            dest_season_dir = season_pack_dir(dst_root, series_slug, file_season, group)
            if not dry_run:
                dest_season_dir.mkdir(parents=True, exist_ok=True)

            dest_name = build_dest_filename(
                series_slug=series_slug,
                group=group,
                season=file_season,
                episode=ep_num,
                title=title,
                src_name_for_meta=stem,
            )
            dest_path = dest_season_dir / dest_name

            status = ensure_hardlink(src_file, dest_path, dry_run=dry_run)
            total += 1

            tag = f"S{file_season:02d}E{ep_num:02d}"

            if status == "linked":
                linked += 1
                log(f"[LINKED]   {tag} -> {dest_path.name}")

            elif status in ("exists-same", "exists"):
                already += 1
                log(f"[EXISTS]   {tag} -> {dest_path.name}")

            elif status == "would-link":
                would_link += 1
                log(f"[DRY-RUN]  {tag} -> {dest_path.name}")

            elif status == "exists-different":
                errors += 1
                warn(
                    f"[CLASH]    Dest exists with different inode: {dest_path} " f"(src={src_file})"
                )

            elif status == "missing-src":
                errors += 1
                error(f"[MISSING]  Source file disappeared: {src_file}")

            else:
                errors += 1
                error(
                    f"[ERROR]   Unexpected status for {src_file} -> {dest_path} "
                    f"(status={status})"
                )

    log("\n========== Summary ==========")
    log(f"Total episodes seen:   {total}")
    if dry_run:
        log(f"Would link (dry-run): {would_link}")
    else:
        log(f"New hardlinks made:   {linked}")
    log(f"Already present:       {already}")
    log(f"Skipped (unparsed):    {skipped}")
    log(f"Errors / clashes:      {errors}")
    log(f"Mode:                  {'DRY-RUN' if dry_run else 'LIVE'}")
    log("=============================")


def main() -> None:
    log("==============================================")
    log("  BTN Season Pack Hardlink Wizard (H2OKing)")
    log("==============================================")

    try:
        src_root = ask_src_root()
        seasons_all = find_season_dirs(src_root)
        seasons_selected = choose_seasons(seasons_all)
        series_slug = ask_series_slug(src_root)
        group = ask_group()
        dst_root = ask_dst_root()
        dry_run = ask_dry_run()

        log("\n------ Configuration ------")
        log(f"Source root:      {src_root}")
        log(f"Destination root: {dst_root}")
        log(f"Series slug:      {series_slug}")
        log(f"Group:            {group}")
        log(f"Seasons:          {', '.join(f'S{s:02d}' for s, _ in seasons_selected)}")
        log(f"Mode:             {'DRY-RUN' if dry_run else 'LIVE (creating hardlinks!)'}")
        log("---------------------------")

        confirm = input("\nProceed with hardlinking? [Y/n]: ").strip().lower()
        if confirm not in ("", "y", "yes"):
            print("👉 Cancelled. Nothing was changed.")
            return

        process_seasons(
            src_root=src_root,
            seasons=seasons_selected,
            dst_root=dst_root,
            series_slug=series_slug,
            group=group,
            dry_run=dry_run,
        )

    except KeyboardInterrupt:
        print("\n⏹  Interrupted by user.")


if __name__ == "__main__":
    main()
