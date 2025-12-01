"""Tests for hardlink_wizard.py functions."""

import os
import sys

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pathlib import Path

from hardlink_wizard import (
    SeasonMeta,
    build_dest_filename,
    detect_audio,
    detect_codec,
    detect_remux,
    detect_resolution,
    detect_source,
    detect_source_and_resolution,
    guess_series_slug,
    parse_episode_info,
    season_pack_dir,
    slugify_title,
)


class TestParseEpisodeInfo:
    """Tests for parse_episode_info function."""

    def test_standard_episode(self):
        """Standard Sonarr episode naming."""
        name = "Yu Yu Hakusho (1992) - S01E01 - Surprised to be Dead [Bluray-1080p]"
        result = parse_episode_info(name)
        assert result == (1, 1, "Surprised to be Dead")

    def test_anime_with_absolute_number(self):
        """Anime naming with absolute episode number."""
        name = "Yu Yu Hakusho (1992) - S01E01 - 001 - Surprised to be Dead [Bluray-1080p]"
        result = parse_episode_info(name)
        assert result == (1, 1, "Surprised to be Dead")

    def test_multi_episode(self):
        """Multi-episode files."""
        name = "Show Name - S01E01-E03 - 001-003 - Episode Title [HDTV-720p]"
        result = parse_episode_info(name)
        assert result == (1, 1, "Episode Title")

    def test_three_digit_episode(self):
        """Three-digit episode numbers (long-running anime)."""
        name = "One Piece - S01E100 - 100 - Episode Title [Bluray-1080p]"
        result = parse_episode_info(name)
        assert result == (1, 100, "Episode Title")

    def test_no_match_returns_none(self):
        """Non-matching filenames return None."""
        name = "random_file.mkv"
        result = parse_episode_info(name)
        assert result is None

    def test_title_with_special_chars(self):
        """Title with special characters."""
        name = "Show - S01E05 - What's Up, Doc? [Bluray-1080p]"
        result = parse_episode_info(name)
        assert result == (1, 5, "What's Up, Doc?")


class TestSlugifyTitle:
    """Tests for slugify_title function."""

    def test_basic_title(self):
        """Basic title with spaces."""
        assert slugify_title("Surprised to be Dead") == "Surprised.to.be.Dead"

    def test_title_with_punctuation(self):
        """Title with various punctuation."""
        assert slugify_title("Yusuke vs. Rando 99 Attacks") == "Yusuke.vs.Rando.99.Attacks"

    def test_apostrophe_removal(self):
        """Apostrophes should be stripped."""
        assert slugify_title("Don't Stop") == "Dont.Stop"
        assert slugify_title("I'll Be There") == "Ill.Be.There"
        assert slugify_title("We've Got") == "Weve.Got"

    def test_curly_apostrophes(self):
        """Curly/smart apostrophes should also be stripped."""
        assert slugify_title("Don't Stop") == "Dont.Stop"
        assert slugify_title("It's Time") == "Its.Time"

    def test_multiple_special_chars(self):
        """Multiple special characters collapse to single dot."""
        assert slugify_title("Hello --- World!!!") == "Hello.World"

    def test_leading_trailing_special(self):
        """Leading/trailing special chars are stripped."""
        assert slugify_title("...Title...") == "Title"


class TestDetectResolution:
    """Tests for detect_resolution function."""

    def test_1080p(self):
        assert detect_resolution("[Bluray-1080p]") == "1080p"

    def test_720p(self):
        assert detect_resolution("[HDTV-720p]") == "720p"

    def test_2160p(self):
        assert detect_resolution("[Bluray-2160p]") == "2160p"

    def test_fallback(self):
        """No resolution found should return fallback."""
        result = detect_resolution("no resolution here")
        assert result == "1080p"  # FALLBACK_META default


class TestDetectSource:
    """Tests for detect_source function."""

    def test_bluray(self):
        assert detect_source("[Bluray-1080p]") == "BluRay"
        assert detect_source("Blu-Ray") == "BluRay"

    def test_web(self):
        assert detect_source("[WEBDL-1080p]") == "WEB"
        assert detect_source("WEB-DL") == "WEB"

    def test_fallback(self):
        result = detect_source("unknown source")
        assert result == "BluRay"  # FALLBACK_META default


class TestDetectRemux:
    """Tests for detect_remux function."""

    def test_remux_detected(self):
        assert detect_remux("[Bluray-1080p Remux]") is True
        assert detect_remux("REMUX") is True

    def test_not_remux(self):
        assert detect_remux("[Bluray-1080p]") is False


class TestDetectAudio:
    """Tests for detect_audio function."""

    def test_truehd_51(self):
        assert detect_audio("TrueHD 5.1") == "TrueHD5.1"

    def test_truehd_atmos(self):
        assert detect_audio("TrueHD 7.1 Atmos") == "TrueHDA7.1"
        assert detect_audio("Atmos TrueHD 7.1") == "TrueHDA7.1"

    def test_dts_hd_ma(self):
        assert detect_audio("DTS-HD MA 5.1") == "DTS-HD.MA5.1"
        assert detect_audio("DTS-HD.MA 5.1") == "DTS-HD.MA5.1"

    def test_ddp(self):
        assert detect_audio("DD+ 5.1") == "DDP5.1"
        assert detect_audio("EAC3 5.1") == "DDP5.1"
        assert detect_audio("E-AC-3 5.1") == "DDP5.1"

    def test_ddp_atmos(self):
        assert detect_audio("DDP 5.1 Atmos") == "DDPA5.1"

    def test_ac3_to_dd(self):
        """AC3 should map to DD (BTN naming)."""
        assert detect_audio("AC3 5.1") == "DD5.1"

    def test_aac(self):
        assert detect_audio("AAC 2.0") == "AAC2.0"

    def test_flac(self):
        assert detect_audio("FLAC 2.0") == "FLAC2.0"

    def test_default_channels(self):
        """When no channel info, default to 2.0."""
        assert detect_audio("AAC") == "AAC2.0"

    def test_fallback(self):
        """Unknown audio should use fallback."""
        result = detect_audio("unknown audio format")
        assert "2.0" in result  # Should have default channels


class TestDetectCodec:
    """Tests for detect_codec function."""

    def test_h264(self):
        assert detect_codec("x264") == "H.264"
        assert detect_codec("H264") == "H.264"
        assert detect_codec("h264") == "H.264"

    def test_h265(self):
        assert detect_codec("HEVC") == "H.265"
        assert detect_codec("x265") == "H.265"
        assert detect_codec("H265") == "H.265"

    def test_fallback(self):
        result = detect_codec("unknown")
        assert result == "H.264"  # FALLBACK_META default


class TestDetectSourceAndResolution:
    """Tests for detect_source_and_resolution function."""

    def test_bluray_remux(self):
        source, res, remux = detect_source_and_resolution("[Bluray-1080p Remux]")
        assert source == "BluRay"
        assert res == "1080p"
        assert remux is True

    def test_webdl(self):
        source, res, remux = detect_source_and_resolution("[WEBDL-1080p]")
        assert source == "WEB-DL"
        assert res == "1080p"
        assert remux is False

    def test_webrip(self):
        source, res, remux = detect_source_and_resolution("[WEBRip-720p]")
        assert source == "WEBRip"
        assert res == "720p"
        assert remux is False

    def test_hdtv(self):
        source, res, remux = detect_source_and_resolution("[HDTV-1080p]")
        assert source == "HDTV"
        assert res == "1080p"
        assert remux is False

    def test_proper_tag(self):
        """Proper tag should not affect parsing."""
        source, res, remux = detect_source_and_resolution("[WEBDL-1080p Proper]")
        assert source == "WEB-DL"
        assert res == "1080p"
        assert remux is False


class TestGuessSeriesSlug:
    """Tests for guess_series_slug function."""

    def test_basic_series(self):
        path = Path("/mnt/user/data/videos/anime-shows/Yu Yu Hakusho (1992) {imdb-tt0185133}")
        assert guess_series_slug(path) == "Yu.Yu.Hakusho"

    def test_series_with_hyphen(self):
        path = Path("/mnt/user/data/videos/Mushoku Tensei - Jobless Reincarnation (2021)")
        assert guess_series_slug(path) == "Mushoku.Tensei.Jobless.Reincarnation"

    def test_series_no_year(self):
        path = Path("/mnt/user/data/videos/Breaking Bad")
        assert guess_series_slug(path) == "Breaking.Bad"

    def test_series_with_the(self):
        path = Path("/mnt/user/data/videos/The Office (US) (2005)")
        # Note: (US) is in parentheses so it gets stripped along with year
        assert guess_series_slug(path) == "The.Office"

    def test_series_with_country_code_outside_parens(self):
        path = Path("/mnt/user/data/videos/The Office US (2005)")
        assert guess_series_slug(path) == "The.Office.US"


class TestBuildDestFilename:
    """Tests for build_dest_filename function."""

    def test_basic_filename(self):
        meta: SeasonMeta = {
            "resolution": "1080p",
            "source": "BluRay",
            "audio": "TrueHD5.1",
            "codec": "H.264",
            "remux": True,
        }
        result = build_dest_filename(
            series_slug="Yu.Yu.Hakusho",
            group="H2OKing",
            season=1,
            episode=1,
            title="Surprised to be Dead",
            season_meta=meta,
        )
        expected = "Yu.Yu.Hakusho.S01E01.Surprised.to.be.Dead.1080p.BluRay.Remux.TrueHD5.1.H.264-H2OKing.mkv"
        assert result == expected

    def test_no_remux(self):
        meta: SeasonMeta = {
            "resolution": "720p",
            "source": "WEB-DL",
            "audio": "AAC2.0",
            "codec": "H.264",
            "remux": False,
        }
        result = build_dest_filename(
            series_slug="Breaking.Bad",
            group="LOL",
            season=1,
            episode=5,
            title="Gray Matter",
            season_meta=meta,
        )
        expected = "Breaking.Bad.S01E05.Gray.Matter.720p.WEB-DL.AAC2.0.H.264-LOL.mkv"
        assert result == expected

    def test_three_digit_episode(self):
        meta: SeasonMeta = {
            "resolution": "1080p",
            "source": "BluRay",
            "audio": "DD5.1",
            "codec": "H.265",
            "remux": False,
        }
        result = build_dest_filename(
            series_slug="One.Piece",
            group="GRP",
            season=1,
            episode=100,
            title="Legend Begins",
            season_meta=meta,
        )
        # Episode 100+ should use 3-digit format
        assert "S01E100" in result


class TestSeasonPackDir:
    """Tests for season_pack_dir function."""

    def test_remux_folder(self):
        meta: SeasonMeta = {
            "resolution": "1080p",
            "source": "BluRay",
            "audio": "TrueHD5.1",
            "codec": "H.264",
            "remux": True,
        }
        result = season_pack_dir(
            dst_root=Path("/mnt/user/data/seedvault"),
            series_slug="Yu.Yu.Hakusho",
            season=1,
            group="H2OKing",
            season_meta=meta,
        )
        expected = Path(
            "/mnt/user/data/seedvault/Yu.Yu.Hakusho.S01.1080p.BluRay.Remux.TrueHD5.1.H.264-H2OKing"
        )
        assert result == expected

    def test_no_remux_folder(self):
        meta: SeasonMeta = {
            "resolution": "720p",
            "source": "HDTV",
            "audio": "AAC2.0",
            "codec": "H.264",
            "remux": False,
        }
        result = season_pack_dir(
            dst_root=Path("/data/seedvault"),
            series_slug="The.Office",
            season=2,
            group="LOL",
            season_meta=meta,
        )
        expected = Path("/data/seedvault/The.Office.S02.720p.HDTV.AAC2.0.H.264-LOL")
        assert result == expected
