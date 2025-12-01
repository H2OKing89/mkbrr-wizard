#!/usr/bin/env python3
"""
rip_renamer.py - Auto-discover and rename disc rips to Sonarr-compatible format.

Uses TMDB API for episode titles and MediaInfo for tech specs.
Supports auto-discovery of MakeMKV folder structure or manual config.

Usage:
    python rip_renamer.py                    # Auto-discovery mode (interactive)
    python rip_renamer.py --config jobs.yaml # Config file mode
    python rip_renamer.py --help
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, TypedDict

import httpx
import yaml

# ─────────────────────────── ANSI Colors ───────────────────────────

# Check if output supports colors (not redirected, not Windows without ANSI)
_USE_COLOR = sys.stdout.isatty() and os.name != "nt"


def _c(code: str) -> str:
    """Return ANSI code if colors enabled, else empty string."""
    return code if _USE_COLOR else ""


# Color codes
RED = _c("\033[91m")
GREEN = _c("\033[92m")
YELLOW = _c("\033[93m")
BLUE = _c("\033[94m")
CYAN = _c("\033[96m")
BOLD = _c("\033[1m")
DIM = _c("\033[2m")
RESET = _c("\033[0m")

# Global verbose flag (set by --verbose)
VERBOSE = False


def debug(msg: str) -> None:
    """Print debug message if verbose mode is enabled."""
    if VERBOSE:
        print(f"{DIM}[DEBUG] {msg}{RESET}")


# ─────────────────────────── Type Definitions ───────────────────────────


class RuntimeValidation(TypedDict):
    """Result from validate_runtime()."""

    valid: bool
    warning: str | None
    suggestion: str | None
    multi_episode: int | None


class DiscAnalysis(TypedDict):
    """Result from analyze_disc_files()."""

    episodes: list[Path]
    extras: list[Path]
    all_durations: dict[Path, float]
    avg_episode_runtime: float


class PreflightPlan(TypedDict):
    """Result from build_preflight_plan()."""

    lines: list[str]
    total_episodes: int
    total_extras: int
    missing_episodes: list[str]
    disc_plans: list[dict[str, Any]]
    track_gaps: dict[str, list[int]]


# ─────────────────────────── Config ───────────────────────────

CONFIG_DIR = Path(__file__).parent / "config"
CONFIG_ENV_PATH = CONFIG_DIR / ".env"
CONFIG_YAML_PATH = CONFIG_DIR / "config.yaml"
CACHE_DIR = CONFIG_DIR / "cache"
DEFAULT_MAKEMKV_PATH = Path("/mnt/user/data/downloads/MakeMKV")
TMDB_API_BASE = "https://api.themoviedb.org/3"

# Default TMDB settings
DEFAULT_TMDB_CONFIG = {
    "language": "en",
    "region": "",
    "cache_expiration": 60,  # minutes
}

# Default settings
DEFAULT_SETTINGS = {
    "group": "NOGROUP",
    "source": "Bluray",
    "is_remux": True,
}

# Safety settings
SAFETY_REQUIRE_CONFIRMATION = True  # Require explicit confirmation before execute
SAFETY_CREATE_MANIFEST = True  # Create JSON manifest of all operations

# ─────────────────────────── Config Loading ───────────────────────────


def load_env_file(path: Path) -> dict[str, str]:
    """Load key=value pairs from a .env file."""
    env_vars = {}
    if path.exists():
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    env_vars[key.strip()] = value.strip().strip("\"'")
    return env_vars


def load_config_yaml() -> dict:
    """Load config.yaml if it exists."""
    if CONFIG_YAML_PATH.exists():
        with open(CONFIG_YAML_PATH) as f:
            return yaml.safe_load(f) or {}
    return {}


def get_tmdb_config() -> dict:
    """Get TMDB configuration from config.yaml or defaults."""
    config = load_config_yaml()
    tmdb_config = config.get("tmdb", {})
    return {**DEFAULT_TMDB_CONFIG, **tmdb_config}


def get_default_settings() -> dict:
    """Get default settings from config.yaml or defaults."""
    config = load_config_yaml()
    defaults = config.get("defaults", {})
    return {**DEFAULT_SETTINGS, **defaults}


def get_tmdb_api_key() -> str:
    """Get TMDB API key from environment or config file."""
    key = os.environ.get("TMDB_API_KEY")
    if key:
        return key

    env_vars = load_env_file(CONFIG_ENV_PATH)
    key = env_vars.get("TMDB_API_KEY")
    if key:
        return key

    print("ERROR: TMDB_API_KEY not found.")
    print("Set it in environment or create config/.env with:")
    print("  TMDB_API_KEY=your_key_here")
    sys.exit(1)


# ─────────────────────────── TMDB Cache ───────────────────────────


def get_cache_path(cache_key: str) -> Path:
    """Get the cache file path for a given key."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    # Hash the key to create a safe filename
    key_hash = hashlib.md5(cache_key.encode()).hexdigest()[:16]
    return CACHE_DIR / f"{key_hash}.json"


def get_cached_response(cache_key: str, expiration_minutes: int) -> dict | None:
    """Get cached response if valid."""
    cache_path = get_cache_path(cache_key)
    if not cache_path.exists():
        return None

    try:
        with open(cache_path) as f:
            cached = json.load(f)

        # Check expiration
        cached_time = cached.get("_cached_at", 0)
        age_minutes = (time.time() - cached_time) / 60

        if age_minutes < expiration_minutes:
            result: dict[Any, Any] | None = cached.get("data")
            return result
    except (json.JSONDecodeError, KeyError):
        pass

    return None


def save_to_cache(cache_key: str, data: Any) -> None:
    """Save response to cache."""
    cache_path = get_cache_path(cache_key)
    try:
        with open(cache_path, "w") as f:
            json.dump({"_cached_at": time.time(), "data": data}, f)
    except OSError:
        pass  # Silently fail on cache write errors


def cleanup_expired_cache(max_age_hours: int = 24) -> int:
    """
    Remove expired cache files older than max_age_hours.
    Returns the number of files cleaned up.
    """
    if not CACHE_DIR.exists():
        return 0

    cleaned = 0
    max_age_seconds = max_age_hours * 3600
    now = time.time()

    for cache_file in CACHE_DIR.glob("*.json"):
        try:
            # Check file modification time
            mtime = cache_file.stat().st_mtime
            if now - mtime > max_age_seconds:
                cache_file.unlink()
                cleaned += 1
                debug(f"Cleaned up expired cache: {cache_file.name}")
        except OSError:
            pass  # Ignore errors during cleanup

    return cleaned


# ─────────────────────────── MediaInfo Detection ───────────────────────────

# Audio format priority (higher = better)
AUDIO_PRIORITY = {
    "truehd atmos": 100,
    "truehd": 90,
    "dts-x": 85,
    "dts-hd ma": 80,
    "dts-hd": 75,
    "lpcm": 70,
    "flac": 65,
    "dts": 50,
    "ac3": 40,
    "aac": 30,
    "mp3": 20,
}

# Global cache for MediaInfo results to avoid redundant subprocess calls
_mediainfo_cache: dict[Path, dict[str, Any] | None] = {}


def run_mediainfo_json(file_path: Path) -> dict[str, Any] | None:
    """Run mediainfo --Output=JSON on a file (with caching)."""
    if file_path in _mediainfo_cache:
        return _mediainfo_cache[file_path]

    try:
        result = subprocess.run(
            ["mediainfo", "--Output=JSON", str(file_path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            data: dict[str, Any] = json.loads(result.stdout)
            _mediainfo_cache[file_path] = data
            return data
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
        pass
    _mediainfo_cache[file_path] = None
    return None


def prewarm_mediainfo_cache(files: list[Path], max_workers: int = 8) -> None:
    """Pre-scan multiple files in parallel to warm the cache."""
    # Filter out already cached files
    uncached = [f for f in files if f not in _mediainfo_cache]
    if not uncached:
        debug(f"MediaInfo cache: all {len(files)} files already cached")
        return

    debug(f"MediaInfo cache: scanning {len(uncached)} files (workers={max_workers})")

    def scan_file(file_path: Path) -> tuple[Path, dict[str, Any] | None]:
        """Worker function to scan a single file."""
        try:
            result = subprocess.run(
                ["mediainfo", "--Output=JSON", str(file_path)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0 and result.stdout.strip():
                return file_path, json.loads(result.stdout)
        except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
            pass
        return file_path, None

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(scan_file, f): f for f in uncached}
        for future in as_completed(futures):
            file_path, data = future.result()
            _mediainfo_cache[file_path] = data
            debug(f"  MediaInfo: {file_path.name} - {'OK' if data else 'FAILED'}")


def parse_audio_track(track: dict) -> tuple[str, str, int]:
    """
    Parse an audio track and return (format_name, channel_string, priority).
    """
    # Get format from Commercial name first (more accurate), then Format
    commercial = track.get("Format_Commercial_IfAny", "")
    format_raw = track.get("Format", "")

    # Determine audio format name
    audio_name = "Unknown"
    priority = 0

    if "TrueHD" in commercial:
        # Check for Atmos
        additional = track.get("Format_AdditionalFeatures", "")
        if "Atmos" in additional or "Atmos" in commercial:
            audio_name = "TrueHD Atmos"
            priority = AUDIO_PRIORITY["truehd atmos"]
        else:
            audio_name = "TrueHD"
            priority = AUDIO_PRIORITY["truehd"]
    elif "DTS" in commercial or "DTS" in format_raw.upper():
        if "X" in commercial:
            audio_name = "DTS-X"
            priority = AUDIO_PRIORITY["dts-x"]
        elif "MA" in commercial or "Master Audio" in commercial:
            audio_name = "DTS-HD MA"
            priority = AUDIO_PRIORITY["dts-hd ma"]
        elif "HD" in commercial:
            audio_name = "DTS-HD"
            priority = AUDIO_PRIORITY["dts-hd"]
        else:
            audio_name = "DTS"
            priority = AUDIO_PRIORITY["dts"]
    elif "MLP" in format_raw.upper():
        # MLP without TrueHD commercial name - still TrueHD
        audio_name = "TrueHD"
        priority = AUDIO_PRIORITY["truehd"]
    elif "FLAC" in format_raw.upper():
        audio_name = "FLAC"
        priority = AUDIO_PRIORITY["flac"]
    elif "PCM" in format_raw.upper():
        audio_name = "LPCM"
        priority = AUDIO_PRIORITY["lpcm"]
    elif "AAC" in format_raw.upper():
        audio_name = "AAC"
        priority = AUDIO_PRIORITY["aac"]
    elif "AC-3" in format_raw or "AC3" in format_raw.upper():
        audio_name = "AC3"
        priority = AUDIO_PRIORITY["ac3"]
    elif "MP3" in format_raw.upper() or "MPEG Audio" in format_raw:
        audio_name = "MP3"
        priority = AUDIO_PRIORITY["mp3"]

    # Channel layout
    channels = track.get("Channels", "2")
    ch_count = int(str(channels).split()[0]) if channels else 2

    if ch_count >= 8:
        ch_str = "7.1"
    elif ch_count >= 6:
        ch_str = "5.1"
    elif ch_count >= 2:
        ch_str = "2.0"
    else:
        ch_str = "1.0"

    return audio_name, ch_str, priority


def detect_metadata_from_file(file_path: Path) -> dict[str, Any]:
    """
    Extract resolution, codec, audio, source, languages, and duration from MediaInfo.
    Picks the best audio track and collects all unique languages.
    """
    metadata: dict[str, Any] = {
        "resolution": "1080p",
        "source": "Bluray",
        "codec": "H.264 8bit",
        "audio": "FLAC 2.0",
        "remux": True,
        "languages": [],
        "duration_minutes": None,  # Will be set from General track
    }

    info = run_mediainfo_json(file_path)
    if not info:
        return metadata

    tracks = info.get("media", {}).get("track", [])
    general_track = next((t for t in tracks if t.get("@type") == "General"), None)
    video_track = next((t for t in tracks if t.get("@type") == "Video"), None)
    audio_tracks = [t for t in tracks if t.get("@type") == "Audio"]

    # Duration from General track (in seconds, convert to minutes)
    if general_track:
        duration_str = general_track.get("Duration")
        if duration_str:
            try:
                duration_sec = float(duration_str)
                metadata["duration_minutes"] = round(duration_sec / 60, 1)
            except (ValueError, TypeError):
                pass

    # Source - check OriginalSourceMedium from any track
    for track in tracks:
        extra = track.get("extra", {})
        source_medium = extra.get("OriginalSourceMedium", "")
        if source_medium:
            if "Blu-ray" in source_medium:
                metadata["source"] = "Bluray"
            elif "DVD" in source_medium:
                metadata["source"] = "DVD"
            elif "HDTV" in source_medium:
                metadata["source"] = "HDTV"
            break

    # Resolution & Codec
    if video_track:
        height = video_track.get("Height")
        if height:
            h = int(str(height).replace(" ", ""))
            if h >= 2160:
                metadata["resolution"] = "2160p"
            elif h >= 1080:
                metadata["resolution"] = "1080p"
            elif h >= 720:
                metadata["resolution"] = "720p"
            elif h >= 576:
                metadata["resolution"] = "576p"
            else:
                metadata["resolution"] = "480p"

        # Codec detection
        codec_id = video_track.get("CodecID", "")
        format_name = video_track.get("Format", "")
        bit_depth = video_track.get("BitDepth", "8")

        if "HEVC" in format_name.upper() or "V_MPEGH" in codec_id.upper():
            metadata["codec"] = f"H.265 {bit_depth}bit"
        elif "AVC" in format_name.upper() or "V_MPEG4/ISO/AVC" in codec_id.upper():
            metadata["codec"] = f"H.264 {bit_depth}bit"
        elif "AV1" in format_name.upper():
            metadata["codec"] = f"AV1 {bit_depth}bit"
        elif "VC-1" in format_name.upper() or "VC1" in codec_id.upper():
            metadata["codec"] = f"VC-1 {bit_depth}bit"

    # Audio - find best track and collect all languages
    if audio_tracks:
        best_audio = None
        best_priority = -1

        # Language code mapping
        lang_map = {
            "ja": "JA",
            "jpn": "JA",
            "japanese": "JA",
            "en": "EN",
            "eng": "EN",
            "english": "EN",
            "zh": "ZH",
            "chi": "ZH",
            "chinese": "ZH",
            "ko": "KO",
            "kor": "KO",
            "korean": "KO",
            "fr": "FR",
            "fre": "FR",
            "french": "FR",
            "de": "DE",
            "ger": "DE",
            "german": "DE",
            "es": "ES",
            "spa": "ES",
            "spanish": "ES",
            "it": "IT",
            "ita": "IT",
            "italian": "IT",
            "pt": "PT",
            "por": "PT",
            "portuguese": "PT",
            "ru": "RU",
            "rus": "RU",
            "russian": "RU",
        }

        seen_langs = set()
        for track in audio_tracks:
            audio_name, ch_str, priority = parse_audio_track(track)

            # Track the best audio
            if priority > best_priority:
                best_priority = priority
                best_audio = (audio_name, ch_str)

            # Collect language
            lang = track.get("Language", "").lower()
            if lang in lang_map:
                seen_langs.add(lang_map[lang])

        if best_audio:
            metadata["audio"] = f"{best_audio[0]} {best_audio[1]}"

        # Order languages: JA first if present, then EN, then others
        ordered_langs = []
        if "JA" in seen_langs:
            ordered_langs.append("JA")
            seen_langs.remove("JA")
        if "EN" in seen_langs:
            ordered_langs.append("EN")
            seen_langs.remove("EN")
        ordered_langs.extend(sorted(seen_langs))

        metadata["languages"] = ordered_langs if ordered_langs else ["JA"]

    return metadata


# ─────────────────────────── TMDB API ───────────────────────────

# Retry settings
TMDB_MAX_RETRIES = 3
TMDB_RETRY_DELAY = 1.0  # seconds, will use exponential backoff


def _tmdb_request(
    url: str,
    params: dict[str, Any],
    cache_key: str | None = None,
    expiration_minutes: int = 60,
) -> dict | list | None:
    """
    Make a TMDB API request with retry logic and caching.

    Args:
        url: API endpoint URL
        params: Query parameters
        cache_key: If provided, check/save to cache
        expiration_minutes: Cache expiration time

    Returns:
        Parsed JSON response, or None on failure
    """
    # Check cache first
    if cache_key:
        cached = get_cached_response(cache_key, expiration_minutes)
        if cached is not None:
            debug(f"Cache hit: {cache_key}")
            return cached

    last_error: Exception | None = None

    for attempt in range(TMDB_MAX_RETRIES):
        try:
            with httpx.Client(timeout=10) as client:
                resp = client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()

                # Save to cache if cache_key provided
                if cache_key:
                    save_to_cache(cache_key, data)
                    debug(f"Cached response: {cache_key}")

                result: dict[Any, Any] | list[Any] = data
                return result

        except httpx.TimeoutException as e:
            last_error = e
            delay = TMDB_RETRY_DELAY * (2**attempt)  # Exponential backoff
            debug(
                f"TMDB timeout (attempt {attempt + 1}/{TMDB_MAX_RETRIES}), retrying in {delay}s..."
            )
            if attempt < TMDB_MAX_RETRIES - 1:
                time.sleep(delay)

        except httpx.HTTPStatusError as e:
            last_error = e
            # Don't retry on client errors (4xx) except 429 (rate limit)
            if e.response.status_code == 429:
                delay = TMDB_RETRY_DELAY * (2**attempt)
                debug(
                    f"TMDB rate limited (attempt {attempt + 1}/{TMDB_MAX_RETRIES}), retrying in {delay}s..."
                )
                if attempt < TMDB_MAX_RETRIES - 1:
                    time.sleep(delay)
            elif 400 <= e.response.status_code < 500:
                debug(f"TMDB client error: {e.response.status_code}")
                break  # Don't retry client errors
            else:
                delay = TMDB_RETRY_DELAY * (2**attempt)
                debug(
                    f"TMDB server error (attempt {attempt + 1}/{TMDB_MAX_RETRIES}), retrying in {delay}s..."
                )
                if attempt < TMDB_MAX_RETRIES - 1:
                    time.sleep(delay)

        except httpx.HTTPError as e:
            last_error = e
            delay = TMDB_RETRY_DELAY * (2**attempt)
            debug(f"TMDB error (attempt {attempt + 1}/{TMDB_MAX_RETRIES}): {e}")
            if attempt < TMDB_MAX_RETRIES - 1:
                time.sleep(delay)

    # All retries failed
    if last_error:
        print(f"{YELLOW}TMDB API error after {TMDB_MAX_RETRIES} attempts: {last_error}{RESET}")

    return None


def search_tmdb_tv(query: str, api_key: str) -> list[dict[str, Any]]:
    """Search TMDB for TV shows matching query. Results are cached."""
    tmdb_config = get_tmdb_config()
    cache_key = f"search_tv:{query}:{tmdb_config['language']}"

    url = f"{TMDB_API_BASE}/search/tv"
    params: dict[str, Any] = {
        "api_key": api_key,
        "query": query,
        "language": tmdb_config["language"],
    }
    if tmdb_config.get("region"):
        params["region"] = tmdb_config["region"]

    response = _tmdb_request(url, params, cache_key, tmdb_config["cache_expiration"])
    if response and isinstance(response, dict):
        results: list[dict[str, Any]] = response.get("results", [])
        return results
    return []


def get_tmdb_tv_details(tmdb_id: int, api_key: str) -> dict | None:
    """Get TV show details including seasons. Results are cached."""
    tmdb_config = get_tmdb_config()
    cache_key = f"tv_details:{tmdb_id}:{tmdb_config['language']}"

    url = f"{TMDB_API_BASE}/tv/{tmdb_id}"
    params: dict[str, Any] = {
        "api_key": api_key,
        "language": tmdb_config["language"],
    }

    result = _tmdb_request(url, params, cache_key, tmdb_config["cache_expiration"])
    if result and isinstance(result, dict):
        return result
    return None


def fetch_tmdb_episodes(tmdb_id: int, season: int, api_key: str) -> dict[int, str]:
    """Fetch episode titles for a season. Results are cached."""
    details = fetch_tmdb_episode_details(tmdb_id, season, api_key)
    return {ep_num: ep_info["title"] for ep_num, ep_info in details.items()}


def fetch_tmdb_episode_details(tmdb_id: int, season: int, api_key: str) -> dict[int, dict]:
    """
    Fetch episode details (title, runtime, air_date, type) for a season.
    Results are cached.

    Returns: {episode_number: {"title": str, "runtime": int|None, "air_date": str|None, "type": str}}
    """
    tmdb_config = get_tmdb_config()
    cache_key = f"episode_details:{tmdb_id}:s{season}:{tmdb_config['language']}"

    # Check cache first
    cached = get_cached_response(cache_key, tmdb_config["cache_expiration"])
    if cached is not None:
        return {int(k): v for k, v in cached.items()}  # JSON keys are strings

    url = f"{TMDB_API_BASE}/tv/{tmdb_id}/season/{season}"
    params = {
        "api_key": api_key,
        "language": tmdb_config["language"],
    }

    debug(f"Fetching episode details for TMDB ID {tmdb_id} S{season}")
    data = _tmdb_request(url, params)
    if data is None or not isinstance(data, dict):
        return {}

    episodes = {}
    for ep in data.get("episodes", []):
        ep_num = ep.get("episode_number")
        if ep_num:
            episodes[ep_num] = {
                "title": ep.get("name", f"Episode {ep_num}"),
                "runtime": ep.get("runtime"),  # Minutes or None
                "air_date": ep.get("air_date"),
                "type": ep.get("episode_type", "standard"),
            }
    save_to_cache(cache_key, episodes)
    debug(f"  Retrieved {len(episodes)} episodes from TMDB")
    return episodes


# ─────────────────────────── Runtime Validation ───────────────────────────

# Runtime validation thresholds
RUNTIME_TOLERANCE_PERCENT = 25  # Allow 25% variance
EXTRA_MAX_MINUTES = 5  # Files under 5 min are likely extras/menus
MULTI_EPISODE_THRESHOLD = 1.7  # 1.7x expected = likely double episode
SPECIAL_MIN_MINUTES = 90  # 90+ min often = movie/OVA/special

# File size thresholds (bytes)
FILE_SIZE_MIN_MB = 500  # Files under 500MB likely wrong track
FILE_SIZE_MAX_GB = 15  # Files over 15GB likely multi-episode or wrong rip
FILE_SIZE_MIN = FILE_SIZE_MIN_MB * 1024 * 1024
FILE_SIZE_MAX = FILE_SIZE_MAX_GB * 1024 * 1024 * 1024


def get_file_size(file_path: Path) -> int:
    """Get file size in bytes."""
    try:
        return file_path.stat().st_size
    except OSError:
        return 0


def validate_file_size(file_path: Path) -> dict[str, Any]:
    """
    Check if file size is within expected range for a typical episode.

    Returns:
        {
            "valid": bool,
            "warning": str | None,
            "size_mb": float,
        }
    """
    size = get_file_size(file_path)
    size_mb = size / (1024 * 1024)
    size_gb = size / (1024 * 1024 * 1024)

    result: dict[str, Any] = {
        "valid": True,
        "warning": None,
        "size_mb": size_mb,
    }

    if size < FILE_SIZE_MIN:
        result["valid"] = False
        result["warning"] = (
            f"⚠️  SMALL FILE: {size_mb:.0f} MB (< {FILE_SIZE_MIN_MB} MB) - may be wrong track"
        )
    elif size > FILE_SIZE_MAX:
        result["valid"] = False
        result["warning"] = (
            f"⚠️  LARGE FILE: {size_gb:.1f} GB (> {FILE_SIZE_MAX_GB} GB) - may be multi-episode or wrong rip"
        )

    return result


def validate_runtime(
    file_duration: float | None,
    expected_runtime: int | None,
    filename: str,
) -> RuntimeValidation:
    """
    Validate file duration against expected TMDB runtime.

    Returns a RuntimeValidation dict with:
        - valid: bool - True if duration seems correct
        - warning: str | None - Warning message if any
        - suggestion: str | None - Suggested action
        - multi_episode: int | None - Number of episodes if multi-episode detected
    """
    result: RuntimeValidation = {
        "valid": True,
        "warning": None,
        "suggestion": None,
        "multi_episode": None,
    }

    # If either is missing, we can't validate
    if file_duration is None or expected_runtime is None:
        return result

    # Check for extras (very short files)
    if file_duration < EXTRA_MAX_MINUTES:
        result["valid"] = False
        result["warning"] = f"⚠️  EXTRA/MENU? Only {file_duration:.1f} min"
        result["suggestion"] = "This may be a menu, trailer, or extra - consider skipping"
        return result

    # Calculate variance
    diff_percent = ((file_duration - expected_runtime) / expected_runtime) * 100

    # Check for multi-episode files
    if file_duration >= expected_runtime * MULTI_EPISODE_THRESHOLD:
        # Estimate how many episodes this might be
        estimated_eps = round(file_duration / expected_runtime)
        if estimated_eps >= 2:
            result["warning"] = (
                f"⚠️  MULTI-EPISODE? {file_duration:.1f} min vs expected {expected_runtime} min"
            )
            result["suggestion"] = (
                f"This file may contain {estimated_eps} episodes (E01-E{estimated_eps:02d})"
            )
            result["multi_episode"] = estimated_eps
            return result

    # Check for significant mismatch
    if abs(diff_percent) > RUNTIME_TOLERANCE_PERCENT:
        if diff_percent < 0:
            result["warning"] = (
                f"⚠️  SHORT? {file_duration:.1f} min vs expected {expected_runtime} min ({diff_percent:+.0f}%)"
            )
            result["suggestion"] = "File may be incomplete or wrong episode"
        else:
            result["warning"] = (
                f"⚠️  LONG? {file_duration:.1f} min vs expected {expected_runtime} min ({diff_percent:+.0f}%)"
            )
            result["suggestion"] = "File may have extras or be wrong episode"
        result["valid"] = False
        return result

    return result


def get_file_duration(file_path: Path) -> float | None:
    """Get file duration in minutes from MediaInfo."""
    info = run_mediainfo_json(file_path)
    if not info:
        return None

    tracks = info.get("media", {}).get("track", [])
    general_track = next((t for t in tracks if t.get("@type") == "General"), None)

    if general_track:
        duration_str = general_track.get("Duration")
        if duration_str:
            try:
                return float(duration_str) / 60
            except (ValueError, TypeError):
                pass
    return None


# ─────────────────────────── Folder Discovery ───────────────────────────


def parse_folder_name(folder_name: str) -> dict[str, Any]:
    """
    Parse folder name to extract series name, season, disc info.

    Examples:
        "Gundam Build Divers BD1" -> series="Gundam Build Divers", disc=1
        "MushokuTensei S1P1 D1" -> series="MushokuTensei", season=1, part=1, disc=1
        "Show Name Season 2 Disc 3" -> series="Show Name", season=2, disc=3

    Returns dict with:
        series: str - Series name
        season: int - Season number (default 1)
        disc: int - Disc number (default 1)
        part: int | None - Part number (e.g., P1 in S1P1)
        has_season_tag: bool - True if season was explicitly in folder name
        has_part_tag: bool - True if part was explicitly in folder name
    """
    result: dict[str, Any] = {
        "series": folder_name,
        "season": 1,
        "disc": 1,
        "part": None,
        "has_season_tag": False,
        "has_part_tag": False,
    }

    # Try to extract BD/Disc number (e.g., BD1, D1, Disc 1)
    disc_match = re.search(r"(?:BD|D(?:isc)?)\s*(\d+)", folder_name, re.IGNORECASE)
    if disc_match:
        result["disc"] = int(disc_match.group(1))
        # Remove the disc part from series name
        folder_name = re.sub(
            r"\s*(?:BD|D(?:isc)?)\s*\d+\s*", " ", folder_name, flags=re.IGNORECASE
        ).strip()

    # Try to extract season (e.g., S1, Season 1)
    season_match = re.search(r"S(?:eason)?\s*(\d+)", folder_name, re.IGNORECASE)
    if season_match:
        result["season"] = int(season_match.group(1))
        result["has_season_tag"] = True
        # Remove season part
        folder_name = re.sub(
            r"\s*S(?:eason)?\s*\d+\s*", " ", folder_name, flags=re.IGNORECASE
        ).strip()

    # Try to extract part (e.g., P1, Part 1)
    part_match = re.search(r"P(?:art)?\s*(\d+)", folder_name, re.IGNORECASE)
    if part_match:
        result["part"] = int(part_match.group(1))
        result["has_part_tag"] = True
        folder_name = re.sub(
            r"\s*P(?:art)?\s*\d+\s*", " ", folder_name, flags=re.IGNORECASE
        ).strip()

    result["series"] = folder_name.strip()
    return result


def discover_disc_folders(base_path: Path) -> list[dict[str, Any]]:
    """
    Discover disc folders in MakeMKV directory.

    Handles two structures:
    1. Nested: MakeMKV/SeriesName/SeriesName BD1/
    2. Flat: MakeMKV/SeriesName BD1/
    """
    discovered: list[dict[str, Any]] = []

    if not base_path.exists():
        print(f"Path does not exist: {base_path}")
        return discovered

    for item in sorted(base_path.iterdir()):
        if not item.is_dir():
            continue

        # Check if this folder contains .mkv files directly
        mkv_files = list(item.glob("*.mkv"))
        if mkv_files:
            # This is a disc folder
            parsed = parse_folder_name(item.name)
            discovered.append(
                {
                    "path": item,
                    "folder_path": item,  # Alias for export_config_yaml compatibility
                    "folder_name": item.name,
                    **parsed,
                    "files": sorted(mkv_files),
                }
            )
        else:
            # Check subfolders (nested structure)
            for sub in sorted(item.iterdir()):
                if sub.is_dir():
                    sub_mkv = list(sub.glob("*.mkv"))
                    if sub_mkv:
                        parsed = parse_folder_name(sub.name)
                        # Use parent folder name as series if sub doesn't have clear series
                        if parsed["series"] == sub.name:
                            parsed["series"] = item.name
                        discovered.append(
                            {
                                "path": sub,
                                "folder_path": sub,  # Alias for export_config_yaml compatibility
                                "folder_name": sub.name,
                                "parent_name": item.name,
                                **parsed,
                                "files": sorted(sub_mkv),
                            }
                        )

    return discovered


def extract_track_number(filename: str) -> int:
    """Extract track number from MakeMKV filename like 'Show BD1_t04.mkv'."""
    match = re.search(r"_t(\d+)\.mkv$", filename, re.IGNORECASE)
    if match:
        return int(match.group(1))
    # Fallback: try any number before .mkv
    match = re.search(r"(\d+)\.mkv$", filename, re.IGNORECASE)
    if match:
        return int(match.group(1))
    return 0


# ─────────────────────────── Interactive Mode ───────────────────────────


def normalize_search_query(name: str) -> str:
    """Normalize a series name for TMDB search.

    Handles:
    - CamelCase: 'MushokuTensei' -> 'Mushoku Tensei'
    - Underscores/dots: 'Mushoku_Tensei' -> 'Mushoku Tensei'
    """
    # Add space before uppercase letters in camelCase (but not at start or consecutive caps)
    # MushokuTensei -> Mushoku Tensei
    # DBZ -> DBZ (unchanged)
    name = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name)
    # Replace underscores and dots with spaces
    name = re.sub(r"[_.]", " ", name)
    # Collapse multiple spaces
    name = re.sub(r"\s+", " ", name).strip()
    return name


def confirm_tmdb_match(
    series_name: str, api_key: str, auto_mode: bool = False
) -> tuple[int, str, int | None] | None:
    """
    Search TMDB and let user confirm/select show.
    Returns (tmdb_id, series_title_with_year, first_air_year) or None.

    In auto_mode, automatically selects the first result.
    """
    # Normalize the search query for better TMDB results
    search_query = normalize_search_query(series_name)

    print(f"\nSearching TMDB for: '{search_query}'")
    results = search_tmdb_tv(search_query, api_key)

    if not results:
        print("No results found.")
        if auto_mode:
            print("  [AUTO] Skipping - no TMDB match")
            return None
        sys.stdout.flush()
        manual = input("Enter TMDB ID manually (or press Enter to skip): ").strip()
        if manual.isdigit():
            details = get_tmdb_tv_details(int(manual), api_key)
            if details:
                year = details.get("first_air_date", "")[:4]
                title = details.get("name", "Unknown")
                return (
                    int(manual),
                    f"{title} ({year})" if year else title,
                    int(year) if year else None,
                )
        return None

    # Show results
    print("\nSearch Results:")
    for i, show in enumerate(results[:10], 1):
        year = show.get("first_air_date", "")[:4]
        name = show.get("name", "Unknown")
        tmdb_url = f"https://www.themoviedb.org/tv/{show['id']}"
        print(f"  {i}. {name} ({year})")
        print(f"     {tmdb_url}")

    # In auto mode, select first result
    if auto_mode:
        show = results[0]
        year = show.get("first_air_date", "")[:4]
        name = show.get("name", "Unknown")
        print(f"\n  [AUTO] Selecting: {name} ({year})")
        return show["id"], f"{name} ({year})" if year else name, int(year) if year else None

    print("\n  0. Enter TMDB ID manually")
    print("  s. Skip this series")
    print()
    sys.stdout.flush()

    choice = input("Select number or 's' to skip: ").strip().lower()

    if choice == "s":
        return None
    if choice == "0":
        manual = input("Enter TMDB ID: ").strip()
        if manual.isdigit():
            details = get_tmdb_tv_details(int(manual), api_key)
            if details:
                year = details.get("first_air_date", "")[:4]
                title = details.get("name", "Unknown")
                return (
                    int(manual),
                    f"{title} ({year})" if year else title,
                    int(year) if year else None,
                )
        return None

    try:
        idx = int(choice) - 1
        if 0 <= idx < len(results[:10]):
            show = results[idx]
            year = show.get("first_air_date", "")[:4]
            name = show.get("name", "Unknown")
            return show["id"], f"{name} ({year})" if year else name, int(year) if year else None
    except ValueError:
        pass

    print("Invalid selection.")
    return None


def ask_episodes_per_disc(disc_count: int, total_files: int) -> int:
    """Ask user for episodes per disc or auto-calculate."""
    print(f"\nFound {total_files} files across {disc_count} disc(s).")
    sys.stdout.flush()

    avg = total_files // disc_count if disc_count > 0 else total_files
    guess = input(f"Episodes per disc [{avg}]: ").strip()

    if guess.isdigit():
        return int(guess)
    return avg


def build_btn_filename(
    series_slug: str,
    season: int,
    episode: int,
    episode_title: str,
    metadata: dict,
    group: str = "NOGROUP",
) -> str:
    """
    Build BTN-style filename.
    Format: Series.Name.S01E01.Episode.Title.1080p.BluRay.Remux.Audio.Codec-Group.mkv

    Per BTN rules:
    - Only allowed chars: a-z, A-Z, 0-9, . (dot), - (hyphen before group)
    - Audio: codec + channels with no space (e.g., TrueHD5.1, LPCM2.0)
    """
    # Episode tag
    ep_tag = f"{series_slug}.S{season:02d}E{episode:02d}"

    # Episode title (slugified)
    title_slug = slugify_title(episode_title) if episode_title else ""

    # Resolution
    resolution = metadata.get("resolution", "1080p")

    # Source
    source = metadata.get("source", "BluRay")

    # Remux
    remux = metadata.get("remux", True)

    # Audio - BTN format: codec + channels, no space (e.g., TrueHD5.1, LPCM2.0)
    audio = metadata.get("audio", "FLAC 2.0")
    # Remove space between codec and channels for BTN format
    audio_btn = audio.replace(" ", "")

    # Codec - just the codec name, no bit depth for BTN
    codec = metadata.get("codec", "H.264 8bit")
    # Strip bit depth suffix if present
    codec_btn = re.sub(r"\s*\d+bit$", "", codec)

    # Build filename parts
    parts = [ep_tag]
    if title_slug:
        parts.append(title_slug)
    parts.append(resolution)
    parts.append(source)
    if remux:
        parts.append("Remux")
    parts.append(audio_btn)
    parts.append(codec_btn)

    base = ".".join(parts)
    return f"{base}-{group}.mkv"


def build_bracket_block(
    metadata: dict,
    group: str = "NOGROUP",
    languages_override: list[str] | None = None,
) -> str:
    """
    Build the bracket block for filename.
    Format: [Bluray-1080p Remux][TrueHD 5.1][JA+EN][H.264 8bit]-Group

    Uses languages from metadata unless languages_override is provided.
    """
    parts = []

    # Source and resolution
    source = metadata.get("source", "Bluray")
    resolution = metadata.get("resolution", "1080p")
    remux = metadata.get("remux", True)

    if remux:
        parts.append(f"[{source}-{resolution} Remux]")
    else:
        parts.append(f"[{source}-{resolution}]")

    # Audio
    audio = metadata.get("audio", "FLAC 2.0")
    parts.append(f"[{audio}]")

    # Languages - prefer override, then detected, then default
    if languages_override:
        langs = languages_override
    else:
        langs = metadata.get("languages", ["JA"])
    parts.append(f"[{'+'.join(langs)}]")

    # Codec
    codec = metadata.get("codec", "H.264 8bit")
    parts.append(f"[{codec}]")

    return "".join(parts) + f"-{group}"


def sanitize_filename(name: str) -> str:
    """Remove/replace characters invalid in filenames."""
    # Replace problematic characters
    name = re.sub(r'[<>:"/\\|?*]', "", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name


def slugify_title(title: str) -> str:
    """Convert a title to BTN-friendly dotted form.

    'Surprised to be Dead' -> 'Surprised.to.be.Dead'
    'A New World' -> 'A.New.World'
    "Don't Stop" -> 'Dont.Stop'
    """
    # Strip all apostrophes (handles don't, I'll, we've, etc.)
    s = title.replace("'", "").replace("'", "").replace("'", "")
    s = re.sub(r"[^A-Za-z0-9]+", ".", s)
    s = re.sub(r"\.+", ".", s)  # collapse multiple dots
    result = s.strip(".")
    if result != title:
        debug(f"slugify: '{title}' -> '{result}'")
    return result


def build_sonarr_filename(
    series_title: str,
    season: int,
    episode: int,
    abs_episode: int | None,
    episode_title: str,
    bracket_block: str,
) -> str:
    """
    Build Sonarr-compatible filename.
    Format: Series Name (Year) - S01E01 - 001 - Episode Title [tags]-Group.mkv
    """
    parts = [series_title, "-", f"S{season:02d}E{episode:02d}"]

    if abs_episode:
        parts.extend(["-", f"{abs_episode:03d}"])

    if episode_title:
        parts.extend(["-", sanitize_filename(episode_title)])

    parts.append(bracket_block)
    return " ".join(parts) + ".mkv"


# ─────────────────────────── Smart Extras Detection ───────────────────────────


def analyze_disc_files(
    files: list[Path],
    expected_runtime: int | None,
    quick_mode: bool = False,
) -> DiscAnalysis:
    """
    Analyze disc files to detect extras, validate runtimes, and identify episodes.

    Args:
        files: List of files to analyze
        expected_runtime: Expected episode runtime from TMDB (minutes)
        quick_mode: If True, only sample first and last file for speed

    Returns:
        DiscAnalysis with episodes, extras, all_durations, avg_episode_runtime
    """
    result: DiscAnalysis = {
        "episodes": [],
        "extras": [],
        "all_durations": {},
        "avg_episode_runtime": 0.0,
    }

    # In quick mode, only sample a few files to estimate extras
    if quick_mode and expected_runtime and len(files) >= 2:
        # Sample first and last file to detect pattern
        sample_files = [files[0], files[-1]]
        if len(files) > 4:
            # Also sample middle
            sample_files.append(files[len(files) // 2])

        sample_durations = []
        num_extras = 0
        extra_threshold = expected_runtime * 0.65

        for f in sample_files:
            dur = get_file_duration(f)
            if dur:
                sample_durations.append(dur)
                if dur < extra_threshold:
                    num_extras += 1

        # Estimate: if any sample is an extra, assume ~1 extra per disc
        if num_extras > 0 and len(sample_files) > 0:
            # Rough estimate based on sample ratio
            estimated_extras = max(1, int(len(files) * num_extras / len(sample_files)))
            result["extras"] = files[:estimated_extras]  # Just mark first N as placeholder
            result["episodes"] = files[estimated_extras:]
        else:
            result["episodes"] = files

        if sample_durations:
            result["avg_episode_runtime"] = sum(sample_durations) / len(sample_durations)

        return result

    # Full mode: Get duration for all files
    durations: dict[Path, float] = {}
    for f in files:
        dur = get_file_duration(f)
        if dur:
            durations[f] = dur

    result["all_durations"] = durations

    if not durations:
        # Can't analyze without duration data
        result["episodes"] = files
        return result

    # Determine threshold for extras
    # Use expected runtime if available, otherwise calculate from file durations
    if expected_runtime and expected_runtime > 0:
        # Files less than 65% of expected runtime are likely extras
        # (e.g., 16.25 min threshold for 25 min episodes)
        extra_threshold = expected_runtime * 0.65
    else:
        # Calculate median duration and use 65% of that
        sorted_durations = sorted(durations.values())
        if len(sorted_durations) >= 3:
            median = sorted_durations[len(sorted_durations) // 2]
            extra_threshold = median * 0.65
        else:
            # Not enough data, assume everything is an episode
            result["episodes"] = files
            return result

    # Classify files
    episode_durations = []
    for f in files:
        dur = durations.get(f)
        if dur is None:
            # No duration data, assume it's an episode
            result["episodes"].append(f)
        elif dur < extra_threshold:
            result["extras"].append(f)
        else:
            result["episodes"].append(f)
            episode_durations.append(dur)

    if episode_durations:
        result["avg_episode_runtime"] = sum(episode_durations) / len(episode_durations)

    return result


def prompt_extras_handling(
    disc_name: str,
    episodes: list[Path],
    extras: list[Path],
    durations: dict[Path, float],
    expected_runtime: int | None,
    auto_mode: bool = False,
) -> list[Path]:
    """
    Prompt user about how to handle detected extras.

    Returns the list of files to process as episodes.
    In auto_mode, automatically skips extras.
    """
    if not extras:
        return episodes

    if auto_mode:
        # Automatically skip extras in auto mode
        return sorted(episodes, key=lambda x: extract_track_number(x.name))

    print(f"\n📁 Disc Analysis: {disc_name}")
    print(f"  Found {len(episodes)} likely episodes, {len(extras)} likely extras")

    if expected_runtime:
        print(f"  Expected episode runtime: ~{expected_runtime} min")

    print("\n  Likely EXTRAS (short files):")
    for f in sorted(extras, key=lambda x: extract_track_number(x.name)):
        dur = durations.get(f, 0)
        print(f"    • {f.name} ({dur:.1f} min)")

    print("\n  Likely EPISODES:")
    for f in sorted(episodes, key=lambda x: extract_track_number(x.name))[:5]:
        dur = durations.get(f, 0)
        print(f"    • {f.name} ({dur:.1f} min)")
    if len(episodes) > 5:
        print(f"    ... and {len(episodes) - 5} more")

    print()
    sys.stdout.flush()
    choice = (
        input("  Skip extras and only process episodes? [Y/n/m] (m=manual select): ")
        .strip()
        .lower()
    )

    if choice == "n":
        # Process all files
        return sorted(episodes + extras, key=lambda x: extract_track_number(x.name))
    elif choice == "m":
        # Manual selection
        all_files = sorted(episodes + extras, key=lambda x: extract_track_number(x.name))
        print("\n  Select files to process:")
        for i, f in enumerate(all_files):
            dur = durations.get(f, 0)
            is_extra = f in extras
            marker = "  [EXTRA?]" if is_extra else ""
            print(f"    {i+1}. {f.name} ({dur:.1f} min){marker}")

        sys.stdout.flush()
        selection = input("\n  Enter file numbers to INCLUDE (e.g., 2-11 or 2,3,4,5): ").strip()

        selected: list[Path] = []
        try:
            for part in selection.split(","):
                part = part.strip()
                if "-" in part:
                    start, end = part.split("-")
                    for i in range(int(start), int(end) + 1):
                        if 1 <= i <= len(all_files):
                            selected.append(all_files[i - 1])
                else:
                    i = int(part)
                    if 1 <= i <= len(all_files):
                        selected.append(all_files[i - 1])
        except ValueError:
            print("  Invalid selection, using auto-detected episodes")
            return sorted(episodes, key=lambda x: extract_track_number(x.name))

        return selected if selected else episodes
    else:
        # Default: skip extras
        return sorted(episodes, key=lambda x: extract_track_number(x.name))


# ─────────────────────────── Track Gap Detection ───────────────────────────


def detect_track_gaps(files: list[Path]) -> list[int]:
    """Detect gaps in track numbering that might indicate skipped content."""
    track_nums = sorted(extract_track_number(f.name) for f in files)
    gaps = []
    for i in range(len(track_nums) - 1):
        if track_nums[i + 1] - track_nums[i] > 1:
            for missing in range(track_nums[i] + 1, track_nums[i + 1]):
                gaps.append(missing)
    return gaps


# ─────────────────────────── Undo Log Generation ───────────────────────────


def generate_undo_script(renames: list[tuple[Path, Path]], output_dir: Path) -> Path | None:
    """Generate a bash script to undo all renames."""
    if not renames:
        return None

    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    script_path = output_dir / f"undo_rename_{timestamp}.sh"

    lines = [
        "#!/bin/bash",
        f"# Undo script generated by rip_renamer at {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"# Reverses {len(renames)} file renames",
        "#",
        "# SAFETY: This script will move files back to their original locations.",
        "# If the destination folder no longer exists, files will be restored there.",
        "",
        "set -e  # Exit on error",
        "",
        "# Check for --dry-run flag",
        "DRY_RUN=false",
        'if [[ "$1" == "--dry-run" ]] || [[ "$1" == "-n" ]]; then',
        "    DRY_RUN=true",
        '    echo "DRY RUN - showing what would be reverted:"',
        '    echo ""',
        "fi",
        "",
    ]

    for old_path, new_path in renames:
        # Ensure parent directory exists before moving
        parent_dir = old_path.parent
        lines.append('if [[ "$DRY_RUN" == "true" ]]; then')
        lines.append(f'    echo "  {new_path.name}"')
        lines.append(f'    echo "    -> {old_path}"')
        lines.append("else")
        lines.append(f'    mkdir -p "{parent_dir}"')
        lines.append(f'    mv "{new_path}" "{old_path}"')
        lines.append("fi")
        lines.append("")

    lines.append('if [[ "$DRY_RUN" == "false" ]]; then')
    lines.append(f'    echo "✓ Reverted {len(renames)} files to original locations"')
    lines.append("else")
    lines.append('    echo ""')
    lines.append('    echo "Run without --dry-run to actually revert"')
    lines.append("fi")

    script_path.write_text("\n".join(lines))
    script_path.chmod(0o755)

    return script_path


def generate_manifest(
    renames: list[tuple[Path, Path]],
    output_dir: Path,
) -> Path | None:
    """Generate a JSON manifest of all rename operations for recovery."""
    if not renames:
        return None

    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    manifest_path = output_dir / f"rename_manifest_{timestamp}.json"

    manifest = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total_files": len(renames),
        "operations": [
            {
                "original_path": str(old_path),
                "original_name": old_path.name,
                "original_dir": str(old_path.parent),
                "new_path": str(new_path),
                "new_name": new_path.name,
                "new_dir": str(new_path.parent),
            }
            for old_path, new_path in renames
        ],
    }

    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)

    return manifest_path


def execute_renames_safely(
    renames: list[tuple[Path, Path]],
    auto_mode: bool = False,
    skip_confirm: bool = False,
) -> tuple[int, int, list[tuple[Path, Path]]]:
    """
    Execute rename operations with safety checks.

    Returns tuple of (successful, skipped, successful_renames).

    Safety features:
    - Pre-flight conflict detection
    - Manifest generation before any renames
    - Confirmation prompt (unless skip_confirm=True)
    - Atomic-ish execution with skip-on-conflict
    """
    if not renames:
        return 0, 0, []

    # Pre-flight: detect all conflicts upfront
    conflicts: list[tuple[Path, Path, str]] = []  # (src, dst, reason)
    planned: set[Path] = set()

    for old_path, new_path in renames:
        if new_path.exists() and new_path != old_path:
            conflicts.append((old_path, new_path, "file exists on disk"))
        elif new_path in planned:
            conflicts.append((old_path, new_path, "duplicate target in batch"))
        planned.add(new_path)

    # Group by output directory for summary
    by_dir: dict[Path, list[tuple[Path, Path]]] = {}
    for src, dst in renames:
        parent = dst.parent
        if parent not in by_dir:
            by_dir[parent] = []
        by_dir[parent].append((src, dst))

    # Show pre-execute summary
    print(f"\n{BOLD}{'='*60}")
    print("EXECUTE SUMMARY")
    print(f"{'='*60}{RESET}")

    # Show what will happen
    print(f"\n{CYAN}Operations:{RESET}")
    for output_dir, dir_renames in by_dir.items():
        print(f"  {output_dir}")
        print(f"    {GREEN}→{RESET} {len(dir_renames)} files will be moved here")

    # Show conflicts if any
    if conflicts:
        print(f"\n{RED}⚠️  CONFLICTS DETECTED ({len(conflicts)} files):{RESET}")
        for src, dst, reason in conflicts[:5]:
            print(f"  {src.name}")
            print(f"    {RED}→ {dst.name} ({reason}){RESET}")
        if len(conflicts) > 5:
            print(f"  {DIM}... and {len(conflicts) - 5} more{RESET}")
        print(f"\n{YELLOW}Conflicting files will be SKIPPED.{RESET}")

    valid_renames = len(renames) - len(conflicts)
    print(f"\n{GREEN}✓{RESET} {valid_renames} files will be renamed")
    if conflicts:
        print(f"{YELLOW}!{RESET} {len(conflicts)} files will be skipped due to conflicts")

    # Generate manifest BEFORE any renames
    first_output_dir = next(iter(by_dir.keys())) if by_dir else Path(".")
    manifest_path = generate_manifest(renames, first_output_dir)
    if manifest_path:
        print(f"\n📋 Manifest saved: {manifest_path}")
        print(f"   {DIM}(Contains original paths for recovery){RESET}")

    # Confirmation prompt (unless --yes flag was passed)
    if not skip_confirm and SAFETY_REQUIRE_CONFIRMATION:
        print(f"\n{BOLD}This will MOVE files from their original locations.{RESET}")
        confirm = input(f"{YELLOW}Proceed with rename? [y/N]: {RESET}").strip().lower()
        if confirm != "y":
            print(f"\n{CYAN}Aborted. No files were changed.{RESET}")
            return 0, len(renames), []

    # Execute renames
    successful: list[tuple[Path, Path]] = []
    skipped = 0
    conflict_set = {(src, dst) for src, dst, _ in conflicts}

    for old_path, new_path in renames:
        if (old_path, new_path) in conflict_set:
            print(f"  {YELLOW}SKIP{RESET} {old_path.name} (conflict)")
            skipped += 1
            continue

        # Ensure output directory exists
        new_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            old_path.rename(new_path)
            successful.append((old_path, new_path))
        except Exception as e:
            print(f"  {RED}ERROR{RESET} {old_path.name}: {e}")
            skipped += 1

    print(f"\n{GREEN}✓{RESET} Renamed {len(successful)} files")
    if skipped > 0:
        print(f"{YELLOW}!{RESET} Skipped {skipped} files")

    return len(successful), skipped, successful


# ─────────────────────────── Config Export ───────────────────────────


def export_config_yaml(
    series_configs: list[dict[str, Any]],
    output_path: Path,
    languages: list[str],
    group: str,
) -> None:
    """Export current configuration as YAML for future runs."""
    jobs: list[dict[str, Any]] = []
    for sc in series_configs:
        job: dict[str, Any] = {
            "tmdb_id": sc["tmdb_id"],
            "series_title": sc["series_title"],
            "output_dir": str(sc["output_dir"]),
            "languages": languages,
            "group": group,
            "discs": [],
        }
        for dc in sc["disc_configs"]:
            job["discs"].append(
                {
                    "path": str(dc["disc"]["folder_path"]),
                    "season": dc["season"],
                    "start_episode": dc["start_ep"],
                }
            )
        jobs.append(job)

    config = {"jobs": jobs}

    with open(output_path, "w") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)

    print(f"\n💾 Config saved: {output_path}")
    print(f"   Re-run with: python rip_renamer.py --config {output_path}")


# ─────────────────────────── Pre-flight Summary ───────────────────────────


def build_preflight_plan(
    discs: list[dict],
    disc_configs: list[dict],
    season_episode_counts: dict[int, int],
    api_key: str,
    tmdb_id: int,
) -> PreflightPlan:
    """
    Build a complete pre-flight plan showing what will be renamed.

    Returns:
        PreflightPlan with lines, total_episodes, total_extras, missing_episodes, disc_plans, track_gaps
    """
    plan: PreflightPlan = {
        "lines": [],
        "total_episodes": 0,
        "total_extras": 0,
        "missing_episodes": [],
        "disc_plans": [],
        "track_gaps": {},
    }

    # Track which episodes are covered
    covered_episodes: dict[int, set[int]] = {}  # season -> set of episode numbers

    for config in disc_configs:
        disc = config["disc"]
        disc_name = disc["folder_name"]
        file_count = config["file_count"]
        season = config["season"]
        start_ep = config["start_ep"]

        # Get expected runtime for extras detection
        ep_details = fetch_tmdb_episode_details(tmdb_id, season, api_key)
        sample_runtime = None
        for ep_num in range(start_ep, start_ep + file_count):
            if ep_num in ep_details and ep_details[ep_num].get("runtime"):
                sample_runtime = ep_details[ep_num]["runtime"]
                break

        # Analyze files for extras (use quick mode for speed)
        files = sorted(disc["files"], key=lambda f: extract_track_number(f.name))
        analysis = analyze_disc_files(files, sample_runtime, quick_mode=True)

        num_episodes = len(analysis["episodes"])
        num_extras = len(analysis["extras"])

        # Check for track gaps
        gaps = detect_track_gaps(files)
        if gaps:
            plan["track_gaps"][disc_name] = gaps

        # Build episode range string
        end_ep = start_ep + num_episodes - 1
        ep_range = f"S{season:02d}E{start_ep:02d}-E{end_ep:02d}"

        # Track covered episodes
        if season not in covered_episodes:
            covered_episodes[season] = set()
        for ep in range(start_ep, end_ep + 1):
            covered_episodes[season].add(ep)

        # Build line
        extras_note = (
            f" ({num_extras} extra{'s' if num_extras != 1 else ''} detected)"
            if num_extras > 0
            else ""
        )
        line = f"│ {disc_name}: {file_count} files → {ep_range}{extras_note}"

        plan["lines"].append(line)
        plan["total_episodes"] += num_episodes
        plan["total_extras"] += num_extras
        plan["disc_plans"].append(
            {
                "disc": disc_name,
                "season": season,
                "start_ep": start_ep,
                "end_ep": end_ep,
                "episodes": num_episodes,
                "extras": num_extras,
                "gaps": gaps,
            }
        )

    # Find missing episodes
    for season, ep_count in season_episode_counts.items():
        if season in covered_episodes:
            all_eps = set(range(1, ep_count + 1))
            missing = sorted(all_eps - covered_episodes[season])
            for ep in missing:
                plan["missing_episodes"].append(f"S{season:02d}E{ep:02d}")

    return plan


def display_preflight_summary(plan: PreflightPlan) -> bool:
    """Display the pre-flight summary and get user confirmation."""
    width = 75

    print("\n📋 RENAME PLAN:")
    print("┌" + "─" * (width - 2) + "┐")

    for line in plan["lines"]:
        # Pad line to width
        padded = line + " " * (width - 2 - len(line))
        print(padded + "│")

    print("├" + "─" * (width - 2) + "┤")

    # Summary line
    summary = f"│ Total: {plan['total_episodes']} episodes"
    if plan["total_extras"] > 0:
        summary += f" ({plan['total_extras']} extras to skip)"
    summary += " " * (width - 2 - len(summary)) + "│"
    print(summary)

    # Missing episodes
    if plan["missing_episodes"]:
        missing_str = ", ".join(plan["missing_episodes"][:10])
        if len(plan["missing_episodes"]) > 10:
            missing_str += f" (+{len(plan['missing_episodes']) - 10} more)"
        missing_line = f"│ Missing from TMDB: {missing_str}"
        missing_line += " " * (width - 2 - len(missing_line)) + "│"
        print(missing_line)

    # Track gaps warning
    if plan["track_gaps"]:
        for disc_name, gaps in plan["track_gaps"].items():
            gap_line = f"│ ⚠️  {disc_name}: Missing tracks {gaps}"
            gap_line += " " * (width - 2 - len(gap_line)) + "│"
            print(gap_line)

    print("└" + "─" * (width - 2) + "┘")

    # Confirmation prompt
    sys.stdout.flush()
    choice = input("\nProceed with rename? [Y/n/e] (e=edit configuration): ").strip().lower()

    if choice == "n":
        return False
    elif choice == "e":
        print("  (Edit mode not yet implemented - proceeding with current config)")
        return True
    else:
        return True


# ─────────────────────────── Processing ───────────────────────────


def process_disc_interactive(
    disc_info: dict,
    tmdb_id: int,
    series_title: str,
    season: int,
    start_episode: int,
    api_key: str,
    output_dir: Path,
    languages: list[str],
    group: str,
    abs_start: int | None = None,
    dry_run: bool = True,
    use_btn_format: bool = True,
    skip_extras_prompt: bool = True,
    auto_mode: bool = False,
    planned_paths: set[Path] | None = None,
) -> tuple[int, int, list[tuple[Path, Path]]]:
    """
    Process a single disc's files.
    Returns tuple of (files_processed, extras_skipped, renames_list).

    Args:
        use_btn_format: If True, use BTN-style dotted names. If False, use Sonarr format.
        skip_extras_prompt: If True, prompt user about detected extras.
        auto_mode: If True, auto-skip extras without prompting.
        planned_paths: Set of already-planned output paths (for conflict detection in dry-run).
    """
    files = disc_info.get("files", [])
    renames: list[tuple[Path, Path]] = []

    # Track planned paths for this session
    if planned_paths is None:
        planned_paths = set()

    if not files:
        return 0, 0, renames

    # Fetch episode details (titles + runtimes)
    print(f"\nFetching episode details for Season {season}...")
    ep_details = fetch_tmdb_episode_details(tmdb_id, season, api_key)

    # Get expected runtime from TMDB
    sample_runtime = None
    for ep_num in range(start_episode, start_episode + len(files)):
        if ep_num in ep_details and ep_details[ep_num].get("runtime"):
            sample_runtime = ep_details[ep_num]["runtime"]
            break

    # Sort files by track number first
    original_file_count = len(files)
    files = sorted(files, key=lambda f: extract_track_number(f.name))

    # Smart extras detection - also caches durations to avoid re-scanning
    extras_skipped = 0
    cached_durations: dict[Path, float] = {}

    if skip_extras_prompt and sample_runtime:
        analysis = analyze_disc_files(files, sample_runtime)
        cached_durations = analysis.get("all_durations", {})

        if analysis["extras"]:
            # We found potential extras - prompt user (or auto-skip in auto_mode)
            files = prompt_extras_handling(
                disc_info.get("folder_name", "Unknown"),
                analysis["episodes"],
                analysis["extras"],
                analysis["all_durations"],
                sample_runtime,
                auto_mode=auto_mode,
            )
            extras_skipped = original_file_count - len(files)

            # Show what's happening
            if extras_skipped > 0:
                print(f"\n  ℹ️  Processing {len(files)} files (skipped {extras_skipped} extras)")

                # Warn if file count doesn't match expected episode range
                expected_episodes = original_file_count  # User configured this many
                if len(files) < expected_episodes:
                    print(
                        f"  ⚠️  NOTE: You configured {expected_episodes} episodes (E{start_episode}-E{start_episode + expected_episodes - 1})"
                    )
                    print(f"      But only {len(files)} files remain after skipping extras")
                    print(
                        f"      Episodes will be numbered E{start_episode}-E{start_episode + len(files) - 1}"
                    )

    if sample_runtime:
        print(f"  Expected episode runtime: ~{sample_runtime} min")

    # Get metadata - sample up to 3 files for better language detection
    # (first file might be special/different)
    print("Detecting metadata from MediaInfo...")
    metadata: dict[str, Any] = {}
    all_languages: set[str] = set()

    sample_files = files[:3] if len(files) >= 3 else files
    for i, sample_file in enumerate(sample_files):
        sample_meta = detect_metadata_from_file(sample_file)
        if i == 0:
            metadata = sample_meta
        # Collect all languages found across samples
        for lang in sample_meta.get("languages", []):
            all_languages.add(lang)

    # Use combined languages from all samples
    if all_languages:
        # Order: JA first, then EN, then others
        ordered: list[str] = []
        if "JA" in all_languages:
            ordered.append("JA")
            all_languages.discard("JA")
        if "EN" in all_languages:
            ordered.append("EN")
            all_languages.discard("EN")
        ordered.extend(sorted(all_languages))
        metadata["languages"] = ordered

    detected_langs = metadata.get("languages", [])
    print(
        f"  Resolution: {metadata['resolution']}, Audio: {metadata['audio']}, Codec: {metadata['codec']}"
    )
    print(f"  Detected languages: {'+'.join(detected_langs) if detected_langs else 'none'}")

    # Build series slug for BTN format (dotted, no year)
    # Extract just the series name without year for slug
    series_name_only = re.sub(r"\s*\(\d{4}\)\s*$", "", series_title).strip()
    series_slug = slugify_title(series_name_only)

    count = 0
    warnings_found = []

    for i, file in enumerate(files):
        ep_num = start_episode + i
        abs_num = (abs_start + i) if abs_start else None

        # Get episode details (title + runtime)
        ep_info = ep_details.get(ep_num, {})
        ep_title = ep_info.get("title", "") if ep_info else ""
        expected_runtime = ep_info.get("runtime") if ep_info else None

        # Runtime validation - use cached duration if available
        file_duration = cached_durations.get(file) or get_file_duration(file)
        validation = validate_runtime(file_duration, expected_runtime, file.name)

        # File size validation
        size_check = validate_file_size(file)

        # Specials detection (90+ min runtime might be OVA/special)
        is_potential_special = False
        if file_duration and file_duration >= SPECIAL_MIN_MINUTES:
            is_potential_special = True

        if use_btn_format:
            # BTN-style: Series.Name.S01E01.Episode.Title.1080p.BluRay.Remux.Audio.Codec-Group.mkv
            new_name = build_btn_filename(
                series_slug=series_slug,
                season=season,
                episode=ep_num,
                episode_title=ep_title,
                metadata=metadata,
                group=group,
            )
        else:
            # Sonarr-style: Series Name (Year) - S01E01 - Episode Title [tags]-Group.mkv
            final_langs = detected_langs if detected_langs else languages
            bracket = build_bracket_block(
                metadata, group, languages_override=final_langs if languages else None
            )
            new_name = build_sonarr_filename(
                series_title, season, ep_num, abs_num, ep_title, bracket
            )

        new_path = output_dir / new_name

        # Conflict detection - check both filesystem AND planned paths from this run
        has_conflict = False
        conflict_source = ""
        if new_path.exists() and new_path != file:
            has_conflict = True
            conflict_source = "file exists on disk"
        elif new_path in planned_paths:
            has_conflict = True
            conflict_source = "duplicate in this batch"

        # Add to planned paths for future conflict detection
        planned_paths.add(new_path)

        print(f"\n{DIM}[DRY RUN]{RESET} " if dry_run else "\n")
        print(f"{BOLD}{file.name}{RESET}")

        # Show file size if there's an issue
        if not size_check["valid"]:
            print(f"  {YELLOW}{size_check['warning']}{RESET}")
            warnings_found.append((file.name, {"warning": size_check["warning"]}))

        # Show runtime validation warnings
        if validation["warning"]:
            print(f"  {YELLOW}{validation['warning']}{RESET}")
            if validation["suggestion"]:
                print(f"     {DIM}→ {validation['suggestion']}{RESET}")
            warnings_found.append((file.name, dict(validation)))
        elif file_duration and expected_runtime:
            print(
                f"  {GREEN}✓{RESET} Runtime OK: {file_duration:.1f} min (expected ~{expected_runtime} min)"
            )

        # Specials detection warning
        if is_potential_special:
            print(
                f"  {CYAN}ℹ️  LONG RUNTIME: {file_duration:.0f} min - may be OVA/special (consider S00){RESET}"
            )

        # Conflict warning
        if has_conflict:
            print(f"  {RED}⚠️  CONFLICT: {new_path.name} ({conflict_source})!{RESET}")
            warnings_found.append(
                (file.name, {"warning": f"Conflict: {new_path.name} ({conflict_source})"})
            )

        print(f"  {DIM}->{RESET} {new_name}")

        # Track rename for later execution
        # (We no longer execute here - all renames are batched at the end)
        renames.append((file, new_path))

        count += 1

    # Summary of warnings
    if warnings_found:
        print(f"\n{YELLOW}{'─'*50}")
        print(f"⚠️  {len(warnings_found)} file(s) with warnings:")
        for fname, val in warnings_found:
            print(f"  • {fname}")
            if val.get("multi_episode"):
                print(f"    May be {val['multi_episode']} episodes combined")
        print(f"{'─'*50}{RESET}")

    return count, extras_skipped, renames


def run_auto_discovery(
    makemkv_path: Path,
    output_base: Path,
    languages: list[str],
    group: str,
    dry_run: bool = True,
    auto_mode: bool = False,
    save_config_path: Path | None = None,
    skip_confirm: bool = False,
):
    """Run interactive auto-discovery mode."""
    api_key = get_tmdb_api_key()

    print(f"\n{'='*60}")
    print("RIP RENAMER - Auto Discovery Mode" + (" [AUTO]" if auto_mode else ""))
    print(f"{'='*60}")
    print(f"Scanning: {makemkv_path}")

    discovered = discover_disc_folders(makemkv_path)

    if not discovered:
        print("No disc folders found!")
        return

    print(f"\nFound {len(discovered)} disc folder(s):")
    for d in discovered:
        print(f"  - {d['folder_name']} ({len(d['files'])} files)")

    # Group by series
    series_groups: dict[str, list[dict]] = {}
    for d in discovered:
        series = d.get("parent_name", d["series"])
        if series not in series_groups:
            series_groups[series] = []
        series_groups[series].append(d)

    print(f"\nGrouped into {len(series_groups)} series:")
    for series_name, discs in series_groups.items():
        print(f"  - {series_name}: {len(discs)} disc(s)")

    # Track all renames for dry-run summary
    global_renames: list[tuple[Path, Path]] = []

    # Process each series
    all_series_configs = []  # For config export

    for series_name, discs in series_groups.items():
        print(f"\n{'='*60}")
        print(f"SERIES: {series_name}")
        print(f"{'='*60}")

        match = confirm_tmdb_match(series_name, api_key, auto_mode=auto_mode)
        if not match:
            print(f"Skipping series: {series_name}")
            continue

        tmdb_id, full_title, year = match
        print(f"\nConfirmed: {full_title} (TMDB ID: {tmdb_id})")

        # Get show details for season info
        details = get_tmdb_tv_details(tmdb_id, api_key)
        available_seasons = []
        season_episode_counts = {}
        if details:
            seasons = details.get("seasons", [])
            available_seasons = [s["season_number"] for s in seasons if s["season_number"] > 0]
            season_episode_counts = {
                s["season_number"]: s.get("episode_count", 0)
                for s in seasons
                if s["season_number"] > 0
            }
            print(f"Available seasons: {available_seasons}")
            for sn in available_seasons:
                print(f"  Season {sn}: {season_episode_counts.get(sn, '?')} episodes")

        # Sort discs by (part, disc) for proper ordering
        # e.g., S1P1 D1, S1P1 D2, S1P2 D1, S1P2 D2
        discs = sorted(discs, key=lambda d: (d.get("part") or 0, d["disc"]))
        total_files = sum(len(d["files"]) for d in discs)
        disc_file_counts = [len(d["files"]) for d in discs]

        # Pre-warm MediaInfo cache in parallel for faster processing
        all_files = [f for d in discs for f in d["files"]]
        if auto_mode and all_files:
            print("  Scanning files...", end="", flush=True)
            prewarm_mediainfo_cache(all_files, max_workers=8)
            print(" done")

        # Check if folders have explicit season/part tags
        has_explicit_seasons = any(d.get("has_season_tag") for d in discs)
        has_explicit_parts = any(d.get("has_part_tag") for d in discs)

        # Show disc overview with parsed info
        print(f"\nDisc overview ({total_files} total files):")
        for disc in discs:
            # Build info string from parsed metadata
            info_parts = []
            if disc.get("has_season_tag"):
                info_parts.append(f"S{disc['season']}")
            if disc.get("has_part_tag"):
                info_parts.append(f"P{disc['part']}")
            info_str = " ".join(info_parts)
            if info_str:
                info_str = f" [{info_str}]"
            print(
                f"  Disc {disc['disc']}: {disc['folder_name']} ({len(disc['files'])} files){info_str}"
            )

        # Show detected structure
        if has_explicit_seasons or has_explicit_parts:
            print(f"\n{CYAN}ℹ️  Detected from folder names:{RESET}")
            if has_explicit_seasons:
                seasons_found = sorted({d["season"] for d in discs if d.get("has_season_tag")})
                print(f"  • Season tags: {', '.join(f'S{s}' for s in seasons_found)}")
            if has_explicit_parts:
                parts_found = sorted({d["part"] for d in discs if d.get("has_part_tag")})
                print(f"  • Part tags: {', '.join(f'P{p}' for p in parts_found)}")
                print(
                    f"    {DIM}(Parts typically split a season across multiple BD box sets){RESET}"
                )

        # Smart analysis when we have season info
        if season_episode_counts:
            total_episodes = sum(season_episode_counts.values())

            print("\n📊 Analysis:")
            print(f"  Your files: {total_files}")
            print(
                f"  TMDB total: {total_episodes} ({' + '.join(f'S{s}:{c}' for s, c in season_episode_counts.items())})"
            )

            if total_files == total_episodes:
                print("  ✅ Perfect match! Files = Episodes")
            elif total_files < total_episodes:
                print(f"  ⚠️  You have {total_episodes - total_files} fewer files than episodes")
                print("     (Missing episodes, or some discs not ripped yet?)")
            else:
                print(f"  ⚠️  You have {total_files - total_episodes} more files than episodes")
                print("     (Extras, specials, or duplicate tracks?)")

            # Try to find likely season splits
            if len(available_seasons) > 1:
                print("\n🔍 Possible season splits:")

                # Check if files match single season exactly
                for sn, ep_count in season_episode_counts.items():
                    if total_files == ep_count:
                        print(f"  ✓ All {total_files} files = Season {sn} only ({ep_count} eps)")

                # Check if files match cumulative seasons exactly
                cumulative = 0
                for sn in sorted(season_episode_counts.keys()):
                    cumulative += season_episode_counts[sn]
                    if total_files == cumulative and sn > 1:
                        seasons_str = "-".join([f"S{s}" for s in [1, sn]])
                        print(
                            f"  ✓ All {total_files} files = {seasons_str} ({cumulative} eps total)"
                        )

                # Try to find split point based on disc boundaries
                running_total = 0
                found_split = False
                for i, count in enumerate(disc_file_counts):
                    running_total += count
                    # Check if this matches end of a season
                    cumulative_check = 0
                    for sn in sorted(season_episode_counts.keys()):
                        cumulative_check += season_episode_counts[sn]
                        if running_total == cumulative_check:
                            remaining = total_files - running_total
                            remaining_eps = total_episodes - cumulative_check
                            if remaining > 0:
                                discs_so_far = "+".join(
                                    f"D{discs[d]['disc']}" for d in range(i + 1)
                                )
                                discs_after = "+".join(
                                    f"D{discs[d]['disc']}"
                                    for d in range(i + 1, len(disc_file_counts))
                                )
                                seasons_covered = f"S1-S{sn}" if sn > 1 else f"S{sn}"
                                print(
                                    f"  ✓ {discs_so_far} ({running_total} files) = {seasons_covered} ✓"
                                )
                                if remaining == remaining_eps:
                                    print(
                                        f"  ✓ {discs_after} ({remaining} files) = S{sn+1} ({remaining_eps} eps) ✓"
                                    )
                                else:
                                    diff = remaining_eps - remaining
                                    print(
                                        f"  ? {discs_after} ({remaining} files) vs S{sn+1} ({remaining_eps} eps) - {diff} missing"
                                    )
                                found_split = True
                                break
                    if found_split:
                        break

                if not found_split:
                    # No exact match found, show closest suggestions
                    running_total = 0
                    for i, count in enumerate(disc_file_counts):
                        running_total += count
                        # Check how close we are to season boundaries
                        for sn, ep_count in season_episode_counts.items():
                            if abs(running_total - ep_count) <= 3:  # Within 3 episodes
                                discs_so_far = "+".join(
                                    f"D{discs[d]['disc']}" for d in range(i + 1)
                                )
                                diff = running_total - ep_count
                                if diff == 0:
                                    print(
                                        f"  ✓ {discs_so_far} ({running_total} files) ≈ S{sn} ({ep_count} eps)"
                                    )
                                elif diff > 0:
                                    print(
                                        f"  ? {discs_so_far} ({running_total} files) ≈ S{sn} ({ep_count} eps) +{diff} extra"
                                    )
                                else:
                                    print(
                                        f"  ? {discs_so_far} ({running_total} files) ≈ S{sn} ({ep_count} eps) {diff} short"
                                    )

        # Determine if this is multi-season based on folder naming or file counts
        # Check if folders have explicit season info that tells us everything is one season
        explicit_seasons_in_folders = {d["season"] for d in discs if d.get("has_season_tag")}

        if len(available_seasons) > 1:
            if has_explicit_seasons and len(explicit_seasons_in_folders) == 1:
                # All folders are tagged with same season (e.g., S1P1, S1P2)
                detected_season = list(explicit_seasons_in_folders)[0]
                print(f"\n{GREEN}✓ All discs tagged as Season {detected_season}{RESET}")
                if has_explicit_parts:
                    parts_in_season = sorted(
                        {
                            d["part"]
                            for d in discs
                            if d.get("has_part_tag") and d["season"] == detected_season
                        }
                    )
                    print(
                        f"  Parts {', '.join(str(p) for p in parts_in_season)} of Season {detected_season}"
                    )
                multi_season = False
                # Override the first disc's season for later processing
                for disc in discs:
                    disc["season"] = detected_season
            elif has_explicit_seasons and len(explicit_seasons_in_folders) > 1:
                # Folders are tagged with multiple seasons - definitely multi-season
                print(
                    f"\n{YELLOW}ℹ️  Folder names indicate seasons: {', '.join(f'S{s}' for s in sorted(explicit_seasons_in_folders))}{RESET}"
                )
                multi_season = True
                if auto_mode:
                    print("  [AUTO] Multi-season: yes (from folder names)")
            else:
                # No explicit season tags - use heuristics
                print(f"\n⚠️  This series has {len(available_seasons)} seasons.")
                if auto_mode:
                    # Auto-detect based on file counts vs season counts
                    multi_season = total_files > max(season_episode_counts.values())
                    print(f"  [AUTO] Multi-season: {'yes' if multi_season else 'no'}")
                    sys.stdout.flush()
                else:
                    multi_season = (
                        input("Do discs span multiple seasons? [y/N]: ").strip().lower() == "y"
                    )
        else:
            multi_season = False

        if multi_season:
            # Multi-season mode: configure each disc individually
            if not auto_mode:
                print("\n📀 Configure each disc's season and starting episode:")
            disc_configs: list[dict[str, Any]] = []

            for disc in discs:
                file_count = len(disc["files"])
                if not auto_mode:
                    print(
                        f"\n--- Disc {disc['disc']}: {disc['folder_name']} ({file_count} files) ---"
                    )

                # Suggest season based on previous disc or default
                if disc_configs:
                    last_config = disc_configs[-1]
                    last_end_ep = last_config["start_ep"] + last_config["file_count"] - 1
                    last_season = last_config["season"]
                    last_season_count = season_episode_counts.get(last_season, 999)

                    # Calculate what the next episode would be
                    next_ep = last_end_ep + 1

                    # Smart season detection:
                    # If next episode would exceed current season, switch to next season
                    if next_ep > last_season_count:
                        # We've gone past this season, move to next
                        suggested_season = last_season + 1
                        suggested_start = 1
                        # Show what happened
                        if not auto_mode:
                            print(f"  ℹ️  Previous disc ended at S{last_season}E{last_end_ep}")
                            print(
                                f"      Season {last_season} has {last_season_count} eps → switching to Season {suggested_season}"
                            )
                    elif next_ep == last_season_count:
                        # This disc will finish the season exactly or go slightly over
                        # Check if this disc's files would overflow into next season
                        remaining_in_season = last_season_count - last_end_ep
                        if file_count > remaining_in_season + 2:  # More than 2 episodes overflow
                            # Likely this disc starts a new season
                            suggested_season = last_season + 1
                            suggested_start = 1
                            if not auto_mode:
                                print(
                                    f"  ℹ️  Only {remaining_in_season} ep(s) left in S{last_season}, but disc has {file_count} files"
                                )
                                print(f"      Suggesting Season {suggested_season} instead")
                        else:
                            suggested_season = last_season
                            suggested_start = next_ep
                    else:
                        # Continue same season
                        suggested_season = last_season
                        suggested_start = next_ep

                        # But also check: would this disc overflow the season?
                        projected_end = suggested_start + file_count - 1
                        if projected_end > last_season_count:
                            overflow = projected_end - last_season_count
                            if not auto_mode:
                                print(
                                    f"  ⚠️  This would end at E{projected_end}, but S{last_season} only has {last_season_count} eps"
                                )
                            if overflow >= file_count // 2:
                                # More than half overflow - probably wrong season
                                suggested_season = last_season + 1
                                suggested_start = 1
                                if not auto_mode:
                                    print(f"      Suggesting Season {suggested_season} instead")
                else:
                    suggested_season = discs[0].get("season", 1)
                    suggested_start = 1

                # In auto mode, use suggested values
                if auto_mode:
                    disc_season = suggested_season
                    disc_start = suggested_start
                else:
                    # Show season episode count if available
                    if suggested_season in season_episode_counts:
                        print(
                            f"  (Season {suggested_season} has {season_episode_counts[suggested_season]} episodes)"
                        )

                    season_input = input(f"  Season [{suggested_season}]: ").strip()
                    disc_season = int(season_input) if season_input.isdigit() else suggested_season

                    # If user changed season, reset suggested start to 1
                    if disc_season != suggested_season:
                        suggested_start = 1
                        if disc_season in season_episode_counts:
                            print(
                                f"  (Season {disc_season} has {season_episode_counts[disc_season]} episodes)"
                            )

                    start_input = input(f"  Start episode [{suggested_start}]: ").strip()
                    disc_start = int(start_input) if start_input.isdigit() else suggested_start

                    # Validate: warn if episodes would exceed season count
                    disc_end_ep = disc_start + len(disc["files"]) - 1
                    season_max = season_episode_counts.get(disc_season, 999)
                    if disc_end_ep > season_max:
                        print(
                            f"  ⚠️  WARNING: Episodes {disc_start}-{disc_end_ep} exceeds Season {disc_season} ({season_max} eps)"
                        )
                        print("     This will result in missing episode titles!")
                        confirm = input("  Continue anyway? [y/N]: ").strip().lower()
                        if confirm != "y":
                            # Let them re-enter
                            season_input = input(f"  Season [{disc_season}]: ").strip()
                            disc_season = (
                                int(season_input) if season_input.isdigit() else disc_season
                            )
                            if disc_season in season_episode_counts:
                                print(
                                    f"  (Season {disc_season} has {season_episode_counts[disc_season]} episodes)"
                                )
                            start_input = input("  Start episode [1]: ").strip()
                            disc_start = int(start_input) if start_input.isdigit() else 1

                disc_configs.append(
                    {
                        "disc": disc,
                        "season": disc_season,
                        "start_ep": disc_start,
                        "file_count": len(disc["files"]),
                    }
                )

            # Output directory
            output_dir = output_base / sanitize_filename(full_title)

            # Build and show pre-flight summary
            if auto_mode:
                print("  Analyzing disc files for extras detection...", end="", flush=True)
            plan = build_preflight_plan(
                discs=discs,
                disc_configs=disc_configs,
                season_episode_counts=season_episode_counts,
                api_key=api_key,
                tmdb_id=tmdb_id,
            )
            if auto_mode:
                print(" done")

            if not auto_mode:
                if not display_preflight_summary(plan):
                    print("Skipping this series.")
                    continue
            else:
                # In auto mode, just show brief summary
                print(
                    f"\n[AUTO] Plan: {plan['total_episodes']} episodes, {plan['total_extras']} extras to skip"
                )

            print(f"\nOutput directory: {output_dir}")

            # Save config if requested
            if save_config_path:
                all_series_configs.append(
                    {
                        "tmdb_id": tmdb_id,
                        "series_title": full_title,
                        "output_dir": output_dir,
                        "disc_configs": disc_configs,
                    }
                )

            # Process each disc with its config
            # Track actual episodes processed to adjust for extras
            actual_next_episode: dict[int, int] = {}
            total_extras_skipped = 0
            all_renames: list[tuple[Path, Path]] = []  # For undo script
            planned_paths: set[Path] = set()  # For conflict detection in dry-run

            for config in disc_configs:
                disc = config["disc"]
                disc_season = config["season"]
                configured_start = config["start_ep"]
                configured_count = config["file_count"]

                # Check if we need to adjust start episode due to extras skipped on previous disc
                if disc_season in actual_next_episode:
                    expected_start = actual_next_episode[disc_season]
                    if configured_start != expected_start:
                        print("\n  ⚠️  EPISODE ADJUSTMENT: Previous disc(s) skipped extras")
                        print(
                            f"      Configured start: E{configured_start}, Actual next: E{expected_start}"
                        )
                        print(f"      Adjusting start episode to E{expected_start}")
                        configured_start = expected_start

                print(f"\n--- Disc {disc['disc']}: {disc['folder_name']} ---")
                print(
                    f"Season {disc_season}, Episodes: {configured_start} - {configured_start + configured_count - 1}"
                )

                files_processed, extras_skipped, disc_renames = process_disc_interactive(
                    disc_info=disc,
                    tmdb_id=tmdb_id,
                    series_title=full_title,
                    season=disc_season,
                    start_episode=configured_start,
                    api_key=api_key,
                    output_dir=output_dir,
                    languages=languages,
                    group=group,
                    abs_start=None,
                    dry_run=dry_run,
                    auto_mode=auto_mode,
                    planned_paths=planned_paths,
                )

                # Track where the next episode should start for this season
                actual_next_episode[disc_season] = configured_start + files_processed
                total_extras_skipped += extras_skipped
                all_renames.extend(disc_renames)
                global_renames.extend(disc_renames)  # Track globally for summary

            # Execute renames with safety checks (if not dry-run)
            if not dry_run and all_renames:
                success_count, skip_count, successful_renames = execute_renames_safely(
                    all_renames,
                    auto_mode=auto_mode,
                    skip_confirm=skip_confirm,
                )
                # Generate undo script only for successfully renamed files
                if successful_renames:
                    undo_script = generate_undo_script(successful_renames, output_dir)
                    if undo_script:
                        print(f"\n📝 Undo script: {undo_script}")

            # Summary of adjustments
            if total_extras_skipped > 0:
                print(f"\n📊 Total extras skipped: {total_extras_skipped} files")
        else:
            # Single season mode (original behavior)
            # Use season from folder if explicitly tagged, otherwise default to 1
            if has_explicit_seasons and len(explicit_seasons_in_folders) == 1:
                season_guess = list(explicit_seasons_in_folders)[0]
            else:
                season_guess = discs[0].get("season", 1)

            # Check if we have parts (e.g., S1P1, S1P2)
            has_parts = any(d.get("part") for d in discs)

            # In auto mode, use defaults; otherwise prompt
            if auto_mode:
                season = season_guess
                start_ep = 1
                abs_start = None
                eps_per_disc = 0  # Not used in auto mode - we use running_episode instead

                # More informative auto message
                if has_explicit_seasons:
                    season_source = f"from folder names (S{season})"
                else:
                    season_source = "default"

                if has_parts:
                    parts_list = sorted({d["part"] for d in discs if d.get("has_part_tag")})
                    parts_str = ", ".join(f"P{p}" for p in parts_list)
                    print(f"  [AUTO] Season {season} ({season_source}), Parts: {parts_str}")
                    print(f"         {total_files} episodes total, sequential numbering")
                else:
                    print(
                        f"  [AUTO] Season {season} ({season_source}), {total_files} eps, starting E1"
                    )
                sys.stdout.flush()
            else:
                # Ask for season
                season_input = input(f"\nSeason number [{season_guess}]: ").strip()
                season = int(season_input) if season_input.isdigit() else season_guess

                # Calculate episodes per disc
                eps_per_disc = ask_episodes_per_disc(len(discs), total_files)

                # Ask for start episode of first disc
                start_ep_input = input("Start episode for first disc [1]: ").strip()
                start_ep = int(start_ep_input) if start_ep_input.isdigit() else 1

                # Ask about absolute numbering
                use_abs = input("Use absolute episode numbering? [y/N]: ").strip().lower() == "y"
                abs_start = None
                if use_abs:
                    abs_input = input(f"Absolute start number [{start_ep}]: ").strip()
                    abs_start = int(abs_input) if abs_input.isdigit() else start_ep

            # Output directory
            output_dir = output_base / sanitize_filename(full_title)
            print(f"\nOutput directory: {output_dir}")

            # Process each disc - track running episode count
            single_season_renames: list[tuple[Path, Path]] = []
            single_season_planned: set[Path] = set()  # For conflict detection in dry-run
            current_ep = start_ep if not auto_mode else 1

            for disc in discs:
                disc_num = disc["disc"]
                disc_part = disc.get("part")
                file_count = len(disc["files"])

                # Use sequential counting when parts detected OR in auto mode
                # This prevents episode resets between S1P1 and S1P2
                if auto_mode or has_parts:
                    disc_start_ep = current_ep
                else:
                    disc_start_ep = start_ep + (disc_num - 1) * eps_per_disc

                disc_abs_start = abs_start + (disc_start_ep - start_ep) if abs_start else None

                part_str = f" (Part {disc_part})" if disc_part else ""
                print(f"\n--- Disc {disc_num}{part_str}: {disc['folder_name']} ---")
                print(f"Episodes: {disc_start_ep} - {disc_start_ep + file_count - 1}")

                files_processed, _, disc_renames = process_disc_interactive(
                    disc_info=disc,
                    tmdb_id=tmdb_id,
                    series_title=full_title,
                    season=season,
                    start_episode=disc_start_ep,
                    api_key=api_key,
                    output_dir=output_dir,
                    languages=languages,
                    group=group,
                    abs_start=disc_abs_start,
                    dry_run=dry_run,
                    auto_mode=auto_mode,
                    planned_paths=single_season_planned,
                )
                single_season_renames.extend(disc_renames)
                global_renames.extend(disc_renames)  # Track globally for summary

                # Update running count for next disc (accounts for extras skipped)
                current_ep = disc_start_ep + files_processed

            # Execute renames with safety checks (if not dry-run)
            if not dry_run and single_season_renames:
                success_count, skip_count, successful_renames = execute_renames_safely(
                    single_season_renames,
                    auto_mode=auto_mode,
                    skip_confirm=skip_confirm,
                )
                # Generate undo script only for successfully renamed files
                if successful_renames:
                    undo_script = generate_undo_script(successful_renames, output_dir)
                    if undo_script:
                        print(f"\n📝 Undo script: {undo_script}")

    # Dry-run summary table
    if dry_run and global_renames:
        print(f"\n{BOLD}{'='*60}")
        print("DRY RUN SUMMARY")
        print(f"{'='*60}{RESET}")
        print(f"\n{GREEN}✓{RESET} {len(global_renames)} files would be renamed:\n")

        # Show compact summary grouped by output directory
        by_dir: dict[Path, list[tuple[Path, Path]]] = {}
        for src, dst in global_renames:
            parent = dst.parent
            if parent not in by_dir:
                by_dir[parent] = []
            by_dir[parent].append((src, dst))

        for output_dir, renames in by_dir.items():
            print(f"{CYAN}{output_dir}{RESET}")
            for src, dst in renames[:3]:  # Show first 3
                print(f"  {DIM}{src.name}{RESET}")
                print(f"  {GREEN}→{RESET} {dst.name}")
            if len(renames) > 3:
                print(f"  {DIM}... and {len(renames) - 3} more{RESET}")
            print()

        print(f"{YELLOW}Use --execute to apply these changes{RESET}")

    # Export config if requested
    if save_config_path and all_series_configs:
        export_config_yaml(all_series_configs, save_config_path, languages, group)

    print(f"\n{'='*60}")
    print("Done!" + (" (DRY RUN - no files were renamed)" if dry_run else ""))
    print(f"{'='*60}")


# ─────────────────────────── Config File Mode ───────────────────────────


def load_config(config_path: Path) -> dict[str, Any]:
    """Load YAML config file."""
    with open(config_path) as f:
        result: dict[str, Any] = yaml.safe_load(f) or {}
        return result


def process_config_job(job: dict, api_key: str, dry_run: bool = True) -> list[tuple[Path, Path]]:
    """Process a single job from config file. Returns list of renames."""
    tmdb_id = job["tmdb_id"]
    series_title = job["series_title"]
    season = job.get("season", 1)
    source_dir = Path(job["source_dir"])
    output_dir = Path(job.get("output_dir", source_dir.parent / "renamed"))
    start_ep = job.get("start_episode", 1)
    abs_start = job.get("abs_start")
    languages = job.get("languages", ["JA"])
    group = job.get("group", "NOGROUP")

    print(f"\nProcessing: {series_title} Season {season}")
    print(f"Source: {source_dir}")

    files = sorted(source_dir.glob("*.mkv"), key=lambda f: extract_track_number(f.name))
    if not files:
        print("No .mkv files found!")
        return []

    disc_info = {"files": files}
    planned_paths: set[Path] = set()
    _, _, renames = process_disc_interactive(
        disc_info=disc_info,
        tmdb_id=tmdb_id,
        series_title=series_title,
        season=season,
        start_episode=start_ep,
        api_key=api_key,
        output_dir=output_dir,
        languages=languages,
        group=group,
        abs_start=abs_start,
        dry_run=dry_run,
        planned_paths=planned_paths,
    )
    return renames


def run_config_mode(config_path: Path, dry_run: bool = True, skip_confirm: bool = False):
    """Run in config file mode."""
    api_key = get_tmdb_api_key()
    config = load_config(config_path)

    jobs = config.get("jobs", [])
    print(f"Loaded {len(jobs)} job(s) from {config_path}")

    all_renames: list[tuple[Path, Path]] = []
    for job in jobs:
        job_renames = process_config_job(job, api_key, dry_run=True)  # Always plan first
        all_renames.extend(job_renames)

    # Execute renames with safety checks (if not dry-run)
    if not dry_run and all_renames:
        success_count, skip_count, successful_renames = execute_renames_safely(
            all_renames,
            auto_mode=False,
            skip_confirm=skip_confirm,
        )
        # Generate undo script for all jobs
        if successful_renames:
            # Use first job's output dir for undo script
            first_output = Path(jobs[0].get("output_dir", "."))
            undo_script = generate_undo_script(successful_renames, first_output)
            if undo_script:
                print(f"\n📝 Undo script: {undo_script}")
    elif dry_run and all_renames:
        print(f"\n{BOLD}{'='*60}")
        print("DRY RUN SUMMARY")
        print(f"{'='*60}{RESET}")
        print(f"\n{GREEN}✓{RESET} {len(all_renames)} files would be renamed")
        print(f"{YELLOW}Use --execute to apply these changes{RESET}")

    print("\nDone!" + (" (DRY RUN - no files were renamed)" if dry_run else ""))


# ─────────────────────────── Main ───────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Auto-discover and rename disc rips to Sonarr-compatible format",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python rip_renamer.py                          # Auto-discovery (interactive)
    python rip_renamer.py --path /mnt/discs        # Custom MakeMKV path
    python rip_renamer.py --config jobs.yaml       # Config file mode
    python rip_renamer.py --execute                # Actually rename files
        """,
    )

    parser.add_argument(
        "--config",
        "-c",
        type=Path,
        help="YAML config file with batch jobs",
    )
    parser.add_argument(
        "--path",
        "-p",
        type=Path,
        default=DEFAULT_MAKEMKV_PATH,
        help=f"MakeMKV folder path (default: {DEFAULT_MAKEMKV_PATH})",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        help="Output base directory (default: same as source)",
    )
    parser.add_argument(
        "--languages",
        "-l",
        nargs="+",
        default=["JA"],
        help="Language tags (default: JA)",
    )
    parser.add_argument(
        "--group",
        "-g",
        help="Release group tag (default: from config.yaml or NOGROUP)",
    )
    parser.add_argument(
        "--execute",
        "-x",
        action="store_true",
        help="Actually rename files (default: dry run)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Show detailed debug output",
    )
    parser.add_argument(
        "--auto",
        "-a",
        action="store_true",
        help="Auto mode: skip prompts, use best-guess defaults",
    )
    parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Skip confirmation prompt before execute (use with caution)",
    )
    parser.add_argument(
        "--save-config",
        type=Path,
        metavar="FILE",
        help="Save configuration to YAML file for future runs",
    )

    args = parser.parse_args()

    # Set global verbose flag
    global VERBOSE
    VERBOSE = args.verbose

    # Cleanup old cache files at startup (older than 7 days)
    cleaned = cleanup_expired_cache(max_age_hours=7 * 24)
    if cleaned > 0:
        debug(f"Cleaned up {cleaned} expired cache files")

    # Apply defaults from config
    defaults = get_default_settings()
    group = args.group if args.group else defaults["group"]

    dry_run = not args.execute

    if args.config:
        run_config_mode(args.config, dry_run, skip_confirm=args.yes)
    else:
        output_base = args.output or args.path
        run_auto_discovery(
            makemkv_path=args.path,
            output_base=output_base,
            languages=args.languages,
            group=group,
            dry_run=dry_run,
            auto_mode=args.auto,
            save_config_path=args.save_config,
            skip_confirm=args.yes,
        )


if __name__ == "__main__":
    main()
