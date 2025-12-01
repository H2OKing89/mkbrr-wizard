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

import errno
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import TypedDict

# ---------------------------------------------------------------------------
# CONFIG DEFAULTS (edit these to taste)
# ---------------------------------------------------------------------------

DEFAULT_DST_ROOT = Path("/mnt/user/data/downloads/torrents/qbittorrent/seedvault/anime-shows")
DEFAULT_GROUP = "H2OKing"
DEFAULT_DRY_RUN = True  # script will ask, this is just the default
DEFAULT_LOG_DIR = Path.home() / ".local" / "share" / "hardlink-wizard"
USE_MEDIAINFO = True  # Use mediainfo for accurate metadata detection if available
VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".m2ts", ".ts"}  # Supported video extensions


# ---------------------------------------------------------------------------
# TypedDict for metadata
# ---------------------------------------------------------------------------


class SeasonMeta(TypedDict):
    resolution: str
    source: str
    audio: str
    codec: str
    remux: bool


# Centralized fallback metadata values
FALLBACK_META: SeasonMeta = {
    "resolution": "1080p",
    "source": "BluRay",
    "audio": "TrueHD",  # Channel info added dynamically by detect_audio()
    "codec": "H.264",
    "remux": True,
}


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------


def setup_logging(log_dir: Path = DEFAULT_LOG_DIR) -> Path | None:
    """Set up file logging in addition to console output.

    Returns the log file path if successful, None otherwise.
    """
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_file = log_dir / f"hardlink_wizard_{timestamp}.log"

        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(message)s",
            handlers=[
                logging.FileHandler(log_file),
            ],
        )
        return log_file
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------


def log(msg: str) -> None:
    print(msg)
    logging.info(msg)


def warn(msg: str) -> None:
    print(f"[WARN] {msg}", file=sys.stderr)
    logging.warning(msg)


def error(msg: str) -> None:
    print(f"[ERROR] {msg}", file=sys.stderr)
    logging.error(msg)


# ---------------------------------------------------------------------------
# Episode parsing & naming
# ---------------------------------------------------------------------------

# Handles both standard and anime Sonarr naming, including multi-episode
# Matches:
#   S01E01 - 001 - Surprised to be Dead [    (anime single)
#   S01E01-E03 - 001-003 - Episode Title [   (anime multi)
#   S01E01 - Episode Title [                 (standard single)
#   S01E01-E03 - Episode Title [             (standard multi)
EP_REGEX = re.compile(
    r"S(?P<season>\d{2})E(?P<ep>\d{2,3})(?:-E\d{2,3})?"  # S01E01 or S01E01-E03
    r"\s*-\s*"  # separator
    r"(?:\d+(?:-\d+)?\s*-\s*)?"  # optional absolute: 001 or 001-003
    r"(?P<title>.+?)"  # episode title
    r"\s*(?:\[|$)"  # until [ or end
)


def parse_episode_info(name: str) -> tuple[int, int, str] | None:
    """Extract (season, episode, title) from a filename stem.

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
    """Convert an episode title to BTN-friendly dotted form.

    'Surprised to be Dead' -> 'Surprised.to.be.Dead'
    'Yusuke vs. Rando 99 Attacks' -> 'Yusuke.vs.Rando.99.Attacks'
    "Don't Stop" -> 'Dont.Stop'
    "I'll Be There" -> 'Ill.Be.There'
    "We've Got" -> 'Weve.Got'

    All apostrophes are stripped to avoid BTN naming issues.
    """
    # Strip all apostrophes first (handles don't, I'll, we've, 'twas, rock'n'roll, etc.)
    s = title.replace("'", "").replace("'", "").replace("'", "")  # ASCII and curly apostrophes
    s = re.sub(r"[^A-Za-z0-9]+", ".", s)
    s = re.sub(r"\.+", ".", s)  # collapse multiple dots
    return s.strip(".")


def detect_resolution(name: str) -> str:
    m = re.search(r"(\d{3,4}p)", name, re.IGNORECASE)
    if m:
        return m.group(1)
    return FALLBACK_META["resolution"]


def detect_source(name: str) -> str:
    lower = name.lower()
    if "bluray" in lower or "blu-ray" in lower:
        return "BluRay"
    if "web" in lower:
        return "WEB"
    return FALLBACK_META["source"]


def detect_remux(name: str) -> bool:
    return "remux" in name.lower()


def detect_audio(name: str) -> str:
    """Map whatever is in the filename to BTN-style audio token (codec + channels).

    BTN format: codec followed by channels with no separator.

    BTN Audio Codecs:
        AAC, DD (AC-3), DDP (E-AC-3/DD+), DTS, DTS-HD.HRA, DTS-HD.MA,
        FLAC, LPCM, TrueHD

    Atmos: Append 'A' to codec (DDPA, TrueHDA)

    Channels: 1.0 (mono), 2.0 (stereo), 5.1 (surround), 7.1 (surround)

    Examples:
        'TrueHD 5.1' -> 'TrueHD5.1'
        'TrueHD 7.1 Atmos' -> 'TrueHDA7.1'
        'DTS-HD MA 5.1' -> 'DTS-HD.MA5.1'
        'DD+ 5.1' -> 'DDP5.1'
        'AC3 5.1' -> 'DD5.1'
        'AAC 2.0' -> 'AAC2.0'
        'Atmos TrueHD 7.1' -> 'TrueHDA7.1'
    """
    # Try to extract channels (e.g., 5.1, 7.1, 2.0, 1.0)
    channels_match = re.search(r"(\d\.\d)", name)
    channels = channels_match.group(1) if channels_match else "2.0"  # default to stereo

    # Check for Atmos (adds 'A' suffix to codec)
    has_atmos = bool(re.search(r"atmos", name, re.IGNORECASE))

    # Audio codec detection (order matters - check specific before general)
    # Returns (pattern, base_label, supports_atmos)
    audio_codecs = [
        (r"DTS[-\s]?HD[\s.]?MA", "DTS-HD.MA", False),
        (r"DTS[-\s]?HD[\s.]?HRA?", "DTS-HD.HRA", False),
        (r"DTS[-\s]?HD", "DTS-HD", False),
        (r"DTS", "DTS", False),
        (r"TrueHD", "TrueHD", True),  # TrueHD can have Atmos
        (r"FLAC", "FLAC", False),
        (r"EAC3|E-AC-?3|DDP|DD\+|Dolby\s*Digital\s*Plus", "DDP", True),  # DDP can have Atmos
        (r"AC3|Dolby\s*Digital(?!\s*Plus)", "DD", False),  # BTN uses DD for AC-3
        (r"AAC", "AAC", False),
        (r"LPCM|PCM", "LPCM", False),
        (r"Opus", "Opus", False),
    ]

    for pattern, label, supports_atmos in audio_codecs:
        if re.search(pattern, name, re.IGNORECASE):
            if has_atmos and supports_atmos:
                return f"{label}A{channels}"  # e.g., TrueHDA7.1, DDPA5.1
            return f"{label}{channels}"

    # Fallback
    return f"{FALLBACK_META['audio']}{channels}"


def detect_codec(name: str) -> str:
    lower = name.lower()
    if "hevc" in lower or "x265" in lower or "h265" in lower:
        return "H.265"
    if "h264" in lower or "x264" in lower:
        return "H.264"
    return FALLBACK_META["codec"]


def detect_source_and_resolution(name: str) -> tuple[str, str, bool]:
    """Parse Sonarr's [Quality Full] block more precisely.

    Sonarr quality profiles:
        HDTV-720p, HDTV-1080p, HDTV-2160p
        WEBRip-720p, WEBRip-1080p, WEBRip-2160p
        WEBDL-720p, WEBDL-1080p, WEBDL-2160p
        Bluray-720p, Bluray-1080p, Bluray-2160p
        Bluray-1080p Remux, Bluray-2160p Remux

    BTN-style mapping:
        HDTV      -> HDTV
        WEBRip    -> WEBRip
        WEBDL     -> WEB-DL
        Bluray    -> BluRay

    Examples:
        [Bluray-1080p Remux] -> ('BluRay', '1080p', True)
        [WEBDL-1080p Proper] -> ('WEB-DL', '1080p', False)
        [WEBRip-720p]        -> ('WEBRip', '720p', False)
        [HDTV-1080p]         -> ('HDTV', '1080p', False)

    Falls back to loose detection if precise pattern not found.
    """
    # Look for the quality block pattern matching Sonarr's format
    m = re.search(
        r"\[(?P<source>Bluray|HDTV|WEBDL|WEBRip)"
        r"-(?P<res>720p|1080p|2160p)"
        r"(?P<remux>\s+Remux)?"
        r"[^\]]*\]",
        name,
        re.IGNORECASE,
    )
    if m:
        source_raw = m.group("source").lower()
        # Map Sonarr quality names to BTN-style source tags
        source_map = {
            "bluray": "BluRay",
            "hdtv": "HDTV",
            "webdl": "WEB-DL",
            "webrip": "WEBRip",
        }
        return (
            source_map.get(source_raw, "BluRay"),
            m.group("res"),
            bool(m.group("remux")),
        )
    # Fall back to loose detection
    return (detect_source(name), detect_resolution(name), detect_remux(name))


# ---------------------------------------------------------------------------
# MediaInfo-based metadata detection (more accurate than filename parsing)
# ---------------------------------------------------------------------------


def has_mediainfo() -> bool:
    """Check if mediainfo is available on the system."""
    return shutil.which("mediainfo") is not None


def get_mediainfo_json(file_path: Path) -> dict[str, object] | None:
    """Run mediainfo and return parsed JSON output."""
    try:
        result = subprocess.run(
            ["mediainfo", "--Output=JSON", str(file_path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            data: dict[str, object] = json.loads(result.stdout)
            return data
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as e:
        logging.warning("MediaInfo failed for %s: %s", file_path, e)
    return None


def get_track_by_type(
    media_info: dict[str, object], track_type: str, order: int = 1
) -> dict[str, object] | None:
    """Get a specific track from mediainfo output by type and order."""
    media = media_info.get("media")
    if not isinstance(media, dict):
        return None
    tracks = media.get("track", [])
    if not isinstance(tracks, list):
        return None
    count = 0
    for track in tracks:
        if isinstance(track, dict) and track.get("@type") == track_type:
            count += 1
            if count == order:
                return track
    return None


def _get_str(track: dict[str, object], key: str, default: str = "") -> str:
    """Safely extract a string value from a mediainfo track."""
    value = track.get(key, default)
    return str(value) if value is not None else default


def detect_metadata_from_mediainfo(file_path: Path) -> SeasonMeta | None:
    """Extract BTN-compliant metadata directly from file using mediainfo.

    Returns None if mediainfo fails or isn't available.
    """
    if not USE_MEDIAINFO or not has_mediainfo():
        return None

    info = get_mediainfo_json(file_path)
    if not info:
        return None

    # Get video track
    video = get_track_by_type(info, "Video")
    if not video:
        return None

    # Get primary (default) audio track
    audio = get_track_by_type(info, "Audio")

    # --- Resolution ---
    height = _get_str(video, "Height")
    scan_type = _get_str(video, "ScanType", "Progressive")
    if height:
        height_int = int(height.replace(" ", ""))
        # Determine interlaced vs progressive
        suffix = "i" if scan_type.lower() == "interlaced" else "p"
        # Normalize to standard resolutions
        if height_int >= 2160:
            resolution = f"2160{suffix}"
        elif height_int >= 1080:
            resolution = f"1080{suffix}"
        elif height_int >= 720:
            resolution = f"720{suffix}"
        elif height_int >= 576:
            resolution = "576p"
        elif height_int >= 480:
            resolution = "480p"
        else:
            resolution = f"{height_int}{suffix}"
    else:
        resolution = FALLBACK_META["resolution"]

    # --- Video Codec ---
    format_name = _get_str(video, "Format").upper()
    codec_map = {
        "AVC": "H.264",
        "H.264": "H.264",
        "HEVC": "H.265",
        "H.265": "H.265",
        "VP9": "VP9",
        "AV1": "AV1",
        "MPEG-2": "MPEG2",
        "MPEG2": "MPEG2",
        "XVID": "Xvid",
        "DIVX": "DivX",
    }
    codec = codec_map.get(format_name, format_name if format_name else FALLBACK_META["codec"])

    # --- Source (from filename/metadata, mediainfo can't reliably detect this) ---
    general = get_track_by_type(info, "General")
    title = _get_str(general, "Title") if general else ""
    extra = video.get("extra")
    original_source = ""
    if isinstance(extra, dict):
        original_source = str(extra.get("OriginalSourceMedium", ""))

    if "blu-ray" in original_source.lower() or "bluray" in title.lower():
        source = "BluRay"
    elif "web" in title.lower():
        source = "WEB-DL"
    elif "hdtv" in title.lower():
        source = "HDTV"
    else:
        # Fall back to filename-based detection
        source = detect_source(file_path.stem)

    # --- Remux detection ---
    remux = "remux" in title.lower() or "remux" in file_path.stem.lower()

    # --- Audio ---
    if audio:
        audio_format = _get_str(audio, "Format")
        audio_commercial = _get_str(audio, "Format_Commercial_IfAny")
        channels = _get_str(audio, "Channels", "2")
        audio_title = _get_str(audio, "Title")

        # Convert channel count to BTN format (6 -> 5.1, 8 -> 7.1, 2 -> 2.0, 1 -> 1.0)
        try:
            ch_count = int(channels)
            if ch_count >= 8:
                ch_str = "7.1"
            elif ch_count >= 6:
                ch_str = "5.1"
            elif ch_count == 1:
                ch_str = "1.0"
            else:
                ch_str = "2.0"
        except ValueError:
            ch_str = "2.0"

        # Check for Atmos
        has_atmos = "atmos" in audio_commercial.lower() or "atmos" in audio_title.lower()

        # Map audio format to BTN label
        audio_format_upper = audio_format.upper()
        audio_commercial_upper = audio_commercial.upper()
        if "MLP FBA" in audio_format_upper or "TRUEHD" in audio_commercial_upper:
            audio_label = "TrueHDA" if has_atmos else "TrueHD"
        elif (
            "E-AC-3" in audio_format_upper
            or "DDP" in audio_commercial_upper
            or "DD+" in audio_commercial
        ):
            audio_label = "DDPA" if has_atmos else "DDP"
        elif "AC-3" in audio_format_upper or audio_format_upper == "AC3":
            audio_label = "DD"
        elif "DTS-HD MA" in audio_commercial_upper:
            audio_label = "DTS-HD.MA"
        elif "DTS-HD" in audio_commercial_upper:
            audio_label = "DTS-HD"
        elif "DTS" in audio_format_upper:
            audio_label = "DTS"
        elif "AAC" in audio_format_upper:
            audio_label = "AAC"
        elif "FLAC" in audio_format_upper:
            audio_label = "FLAC"
        elif "PCM" in audio_format_upper or "LPCM" in audio_format_upper:
            audio_label = "LPCM"
        elif "OPUS" in audio_format_upper:
            audio_label = "Opus"
        else:
            audio_label = FALLBACK_META["audio"]

        audio_token = f"{audio_label}{ch_str}"
    else:
        audio_token = f"{FALLBACK_META['audio']}2.0"

    return {
        "resolution": resolution,
        "source": source,
        "audio": audio_token,
        "codec": codec,
        "remux": remux,
    }


def detect_season_metadata(season_dir: Path, quiet: bool = False) -> SeasonMeta:
    """Detect common metadata for a season from the first few video files.

    Uses mediainfo for accurate detection if available, otherwise falls back
    to filename parsing.

    Checks up to 3 files to verify consistency and warns if metadata differs.
    Falls back to centralized FALLBACK_META if nothing is found.

    Args:
        season_dir: Path to the season directory.
        quiet: If True, suppress the "Using mediainfo" log message.
    """
    metadata_samples: list[SeasonMeta] = []
    video_files = sorted(f for f in season_dir.iterdir() if f.suffix.lower() in VIDEO_EXTS)[
        :3
    ]  # Check first 3 files

    # Check if mediainfo is available (only check once)
    use_mi = USE_MEDIAINFO and has_mediainfo()
    if use_mi and video_files and not quiet:
        log("📊 Using mediainfo for metadata detection...")

    for src_file in video_files:
        # Try mediainfo first
        if use_mi:
            mi_meta = detect_metadata_from_mediainfo(src_file)
            if mi_meta:
                metadata_samples.append(mi_meta)
                continue

        # Fall back to filename parsing
        stem = src_file.stem
        source, resolution, remux = detect_source_and_resolution(stem)
        metadata_samples.append(
            {
                "resolution": resolution,
                "source": source,
                "audio": detect_audio(stem),
                "codec": detect_codec(stem),
                "remux": remux,
            }
        )

    if not metadata_samples:
        return FALLBACK_META.copy()

    # Verify consistency
    first = metadata_samples[0]
    if len(metadata_samples) > 1:
        for sample in metadata_samples[1:]:
            if sample != first:
                warn(f"Inconsistent metadata detected in {season_dir.name}")
                break

    return first


def build_dest_filename(
    series_slug: str,
    group: str,
    season: int,
    episode: int,
    title: str,
    season_meta: SeasonMeta,
) -> str:
    """Build BTN-compliant filename based on season metadata.

    BTN Format: Series.Name.SXXEXX.Episode.Title.Resolution.Source[.Remux].Audio.Codec-Group.mkv

    Per BTN rules:
    - Only allowed chars: a-z, A-Z, 0-9, . (dot), - (hyphen before group)
    - Resolution: 1080p/1080i/720p required; 576p/480p optional
    - Audio: codec + channels (e.g., TrueHD5.1, DD5.1, AAC2.0)
    - Group: Must have hyphen before group name

    Uses season_meta for consistent naming across all episodes in a season.
    """
    ep_title_slug = slugify_title(title)
    resolution = season_meta["resolution"]
    source = season_meta["source"]
    remux = season_meta["remux"]
    audio = season_meta["audio"]
    codec = season_meta["codec"]

    # Keep 3-digit formatting for >=100 episodes, otherwise 2-digit
    ep_width = 3 if episode >= 100 else 2
    ep_tag = f"{series_slug}.S{season:02d}E{episode:0{ep_width}d}"

    parts = [
        ep_tag,
        ep_title_slug,
        resolution,
        source,
    ]
    if remux:
        parts.append("Remux")
    base = ".".join(parts)
    return f"{base}.{audio}.{codec}-{group}.mkv"


def season_pack_dir(
    dst_root: Path,
    series_slug: str,
    season: int,
    group: str,
    season_meta: SeasonMeta,
) -> Path:
    """Compute the season pack directory name per BTN rules.

    BTN Format: Series.Name.SXX.Resolution.Source[.Remux].Audio.Codec-Group

    Examples:
        Yu.Yu.Hakusho.S01.1080p.BluRay.Remux.TrueHD5.1.H.264-H2OKing
        Breaking.Bad.S01.720p.BluRay.DD5.1.H.264-H2OKing
        The.Office.US.S01.HDTV.AAC2.0.H.264-LOL

    Per BTN: Folder names should be the same as the pack's release name.
    """
    resolution = season_meta["resolution"]
    source = season_meta["source"]
    audio = season_meta["audio"]
    codec = season_meta["codec"]
    remux = season_meta["remux"]

    folder_name = f"{series_slug}.S{season:02d}.{resolution}.{source}"
    if remux:
        folder_name += ".Remux"
    folder_name += f".{audio}.{codec}-{group}"
    return dst_root / folder_name


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------


def validate_same_filesystem(src: Path, dst_root: Path) -> bool:
    """Verify src and dst are on same filesystem (required for hardlinks)."""
    try:
        src_dev = src.stat().st_dev
        dst_dev = dst_root.stat().st_dev
        return src_dev == dst_dev
    except OSError:
        return False


def ensure_hardlink(src: Path, dst: Path, dry_run: bool) -> str:
    """Create a hardlink from src -> dst (if needed).

    Returns a status string: 'linked', 'exists-same', 'exists-different',
    'would-link', 'missing-src', or 'error'.
    """
    if not src.exists():
        return "missing-src"

    if dst.exists():
        try:
            # Always compare inodes for accurate reporting (even in dry-run)
            src_stat = src.stat()
            dst_stat = dst.stat()
            if src_stat.st_ino == dst_stat.st_ino and src_stat.st_dev == dst_stat.st_dev:
                return "exists-same"
            else:
                return "exists-different"
        except OSError:
            return "error"

    if dry_run:
        return "would-link"

    try:
        os.link(src, dst)
        return "linked"
    except OSError as e:
        if e.errno == errno.EXDEV:
            logging.error(
                "Cross-device link error: %s -> %s. "
                "Source and destination must be on the same filesystem.",
                src,
                dst,
            )
        else:
            logging.error("OSError [%s] linking %s -> %s: %s", e.errno, src, dst, e)
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

    # Strip surrounding quotes (handles copy-paste from file managers/terminals)
    raw = raw.strip("\"'")

    p = Path(os.path.expanduser(raw)).resolve()
    if not p.is_dir():
        error(f"Source root does not exist or is not a directory: {p}")
        raise SystemExit(1)

    return p


def find_season_dirs(src_root: Path) -> list[tuple[int, Path]]:
    """Return list of (season_number, path) for dirs like 'Season 01', 'Season 02', etc."""
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
    """Let the user pick one or more seasons from the detected list."""
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
    """Guess a BTN-style slug from the series folder name.

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
    """Auto-generate the series slug from the source folder name."""
    slug = guess_series_slug(src_root)
    print(f"\n🏷  Series slug: {slug}")
    return slug


def ask_group(default_group: str = DEFAULT_GROUP) -> str:
    ans = input(f"\n👥 Release group tag [{default_group}]: ").strip()
    return ans or default_group


def ask_dst_root(default_dst: Path = DEFAULT_DST_ROOT) -> Path:
    ans = input(f"\n📦 Destination root for season packs [{default_dst}]: ").strip()
    if not ans:
        return default_dst
    p = Path(os.path.expanduser(ans)).resolve()
    return p


def find_existing_parent(path: Path) -> Path:
    """Find the first existing parent directory of a path.

    Used to validate filesystem compatibility when destination doesn't exist yet.
    """
    while not path.exists():
        parent = path.parent
        if parent == path:  # Hit filesystem root
            break
        path = parent
    return path


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

        video_files = sorted(f for f in season_dir.iterdir() if f.suffix.lower() in VIDEO_EXTS)
        if not video_files:
            warn(f"No video files found in {season_dir}, skipping this season.")
            continue

        # Always suppress mediainfo message during processing (already shown in preview)
        season_meta = detect_season_metadata(season_dir, quiet=True)

        for src_file in video_files:
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

            dest_season_dir = season_pack_dir(
                dst_root=dst_root,
                series_slug=series_slug,
                season=file_season,
                group=group,
                season_meta=season_meta,
            )

            if not dry_run:
                try:
                    dest_season_dir.mkdir(parents=True, exist_ok=True)
                except OSError as e:
                    errors += 1
                    error(f"[ERROR] Failed to create directory {dest_season_dir}: {e}")
                    continue

            dest_name = build_dest_filename(
                series_slug=series_slug,
                group=group,
                season=file_season,
                episode=ep_num,
                title=title,
                season_meta=season_meta,
            )
            dest_path = dest_season_dir / dest_name

            status = ensure_hardlink(src_file, dest_path, dry_run=dry_run)
            total += 1

            # Episode tag in logs: still SxxEyy (human-friendly), not necessarily 3-digit
            ep_width = 3 if ep_num >= 100 else 2
            tag = f"S{file_season:02d}E{ep_num:0{ep_width}d}"

            if status == "linked":
                linked += 1
                log(f"[LINKED]   {tag} -> {dest_path.name}")

            elif status == "exists-same":
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

            elif status == "error":
                errors += 1
                error(f"[ERROR]    Failed to link {tag} -> {dest_path.name} (see log for details)")

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
    log_file = setup_logging()

    log("==============================================")
    log("  BTN Season Pack Hardlink Wizard (H2OKing)")
    log("==============================================")

    if log_file:
        log(f"📝 Logging to: {log_file}")

    # Check for mediainfo
    if USE_MEDIAINFO:
        if has_mediainfo():
            log("✅ mediainfo detected - using accurate file-based metadata")
        else:
            log("⚠️  mediainfo not found - falling back to filename parsing")

    try:
        src_root = ask_src_root()
        seasons_all = find_season_dirs(src_root)
        seasons_selected = choose_seasons(seasons_all)
        series_slug = ask_series_slug(src_root)
        group = ask_group()
        dst_root = ask_dst_root()

        # Validate filesystem before processing (check existing parent if dest doesn't exist)
        check_path = dst_root if dst_root.exists() else find_existing_parent(dst_root)
        if not validate_same_filesystem(src_root, check_path):
            error(
                f"Source ({src_root}) and destination ({dst_root}) are on different filesystems.\n"
                f"       Hardlinks require both paths to be on the same filesystem."
            )
            raise SystemExit(1)

        dry_run = ask_dry_run()

        # Preview detected metadata from first selected season
        if seasons_selected:
            first_season_dir = seasons_selected[0][1]
            preview_meta = detect_season_metadata(first_season_dir)
            log("\n------ Detected Metadata ------")
            log(f"Resolution:       {preview_meta['resolution']}")
            log(f"Source:           {preview_meta['source']}")
            log(f"Audio:            {preview_meta['audio']}")
            log(f"Codec:            {preview_meta['codec']}")
            log(f"Remux:            {'Yes' if preview_meta['remux'] else 'No'}")
            log("-------------------------------")

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
    if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0)
    main()
