"""Tests for split-series helpers (scan, parse, pattern generation, naming)."""

from types import ModuleType, SimpleNamespace
from typing import Any

import pytest  # type: ignore[import-untyped]

from .conftest import _Seq

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def sample_cfg(mkbrr_wizard: ModuleType) -> Any:
    return mkbrr_wizard.AppCfg(
        runtime="auto",
        docker_support=False,
        chown=False,
        docker_user=None,
        mkbrr=mkbrr_wizard.MkbrrCfg(binary="mkbrr", image="ghcr.io/autobrr/mkbrr"),
        paths=mkbrr_wizard.PathsCfg(
            host_data_root="/mnt/user/data",
            container_data_root="/data",
            host_output_dir="/mnt/user/data/downloads/torrents/torrentfiles",
            container_output_dir="/torrentfiles",
            host_config_dir="/mnt/cache/appdata/mkbrr",
            container_config_dir="/root/.config/mkbrr",
        ),
        ownership=mkbrr_wizard.OwnershipCfg(uid=99, gid=100),
        batch=mkbrr_wizard.BatchCfg(mode="simple"),
        presets_yaml_host="/mnt/cache/appdata/mkbrr/presets.yaml",
        presets_yaml_container="/root/.config/mkbrr/presets.yaml",
    )


# ---------------------------------------------------------------------------
# scan_episodes
# ---------------------------------------------------------------------------


class TestScanEpisodes:
    def test_basic_season_folder(self, tmp_path, mkbrr_wizard: ModuleType) -> None:
        """Standard season folder with sequential episodes."""
        for ep in range(1, 13):
            (tmp_path / f"Show.S01E{ep:02d}.1080p.mkv").write_text("x")
        result = mkbrr_wizard.scan_episodes(str(tmp_path))
        assert len(result) == 12
        assert result[0] == (1, "Show.S01E01.1080p.mkv")
        assert result[-1] == (12, "Show.S01E12.1080p.mkv")

    def test_mixed_extensions(self, tmp_path, mkbrr_wizard: ModuleType) -> None:
        """Only video files are returned."""
        (tmp_path / "Show.S01E01.mkv").write_text("x")
        (tmp_path / "Show.S01E02.mp4").write_text("x")
        (tmp_path / "Show.S01E03.avi").write_text("x")
        (tmp_path / "Show.S01E04.nfo").write_text("x")
        (tmp_path / "Show.S01E05.txt").write_text("x")
        (tmp_path / "Show.S01E06.jpg").write_text("x")
        result = mkbrr_wizard.scan_episodes(str(tmp_path))
        assert len(result) == 3
        assert [ep for ep, _ in result] == [1, 2, 3]

    def test_non_episode_video_files_skipped(self, tmp_path, mkbrr_wizard: ModuleType) -> None:
        """Video files without S##E## naming are skipped."""
        (tmp_path / "Movie.2024.1080p.mkv").write_text("x")
        (tmp_path / "Bonus.Feature.mp4").write_text("x")
        result = mkbrr_wizard.scan_episodes(str(tmp_path))
        assert result == []

    def test_empty_directory(self, tmp_path, mkbrr_wizard: ModuleType) -> None:
        result = mkbrr_wizard.scan_episodes(str(tmp_path))
        assert result == []

    def test_nonexistent_directory(self, mkbrr_wizard: ModuleType) -> None:
        result = mkbrr_wizard.scan_episodes("/nonexistent/path/12345")
        assert result == []

    def test_case_insensitive(self, tmp_path, mkbrr_wizard: ModuleType) -> None:
        """Episode regex is case-insensitive."""
        (tmp_path / "show.s01e01.mkv").write_text("x")
        (tmp_path / "SHOW.S01E02.MKV").write_text("x")
        result = mkbrr_wizard.scan_episodes(str(tmp_path))
        assert len(result) == 2

    def test_subdirectories_ignored(self, tmp_path, mkbrr_wizard: ModuleType) -> None:
        """Only files in the top-level directory are scanned."""
        sub = tmp_path / "Extras"
        sub.mkdir()
        (sub / "Show.S01E01.mkv").write_text("x")
        (tmp_path / "Show.S01E02.mkv").write_text("x")
        result = mkbrr_wizard.scan_episodes(str(tmp_path))
        assert len(result) == 1
        assert result[0][0] == 2

    def test_multi_episode_takes_first(self, tmp_path, mkbrr_wizard: ModuleType) -> None:
        """S01E01E02 files use the first episode number."""
        (tmp_path / "Show.S01E01E02.mkv").write_text("x")
        result = mkbrr_wizard.scan_episodes(str(tmp_path))
        assert len(result) == 1
        assert result[0][0] == 1

    def test_gap_in_episodes(self, tmp_path, mkbrr_wizard: ModuleType) -> None:
        """Gaps are preserved (not filled in)."""
        for ep in [1, 2, 5, 10]:
            (tmp_path / f"Show.S01E{ep:02d}.mkv").write_text("x")
        result = mkbrr_wizard.scan_episodes(str(tmp_path))
        assert [ep for ep, _ in result] == [1, 2, 5, 10]

    def test_ts_and_m2ts_extensions(self, tmp_path, mkbrr_wizard: ModuleType) -> None:
        (tmp_path / "Show.S01E01.ts").write_text("x")
        (tmp_path / "Show.S01E02.m2ts").write_text("x")
        result = mkbrr_wizard.scan_episodes(str(tmp_path))
        assert len(result) == 2


# ---------------------------------------------------------------------------
# format_episode_ranges
# ---------------------------------------------------------------------------


class TestFormatEpisodeRanges:
    def test_contiguous(self, mkbrr_wizard: ModuleType) -> None:
        assert mkbrr_wizard.format_episode_ranges([1, 2, 3, 4, 5]) == "E01-E05"

    def test_single_episodes(self, mkbrr_wizard: ModuleType) -> None:
        assert mkbrr_wizard.format_episode_ranges([3, 7]) == "E03, E07"

    def test_mixed(self, mkbrr_wizard: ModuleType) -> None:
        assert mkbrr_wizard.format_episode_ranges([1, 2, 3, 5, 6, 8]) == "E01-E03, E05-E06, E08"

    def test_empty(self, mkbrr_wizard: ModuleType) -> None:
        assert mkbrr_wizard.format_episode_ranges([]) == ""

    def test_single(self, mkbrr_wizard: ModuleType) -> None:
        assert mkbrr_wizard.format_episode_ranges([1]) == "E01"

    def test_sakamoto_gaps(self, mkbrr_wizard: ModuleType) -> None:
        """Mirrors the user's example: E01-E14, E17-E18, E20-E21."""
        eps = list(range(1, 15)) + [17, 18, 20, 21]
        result = mkbrr_wizard.format_episode_ranges(eps)
        assert result == "E01-E14, E17-E18, E20-E21"


# ---------------------------------------------------------------------------
# parse_split_ranges
# ---------------------------------------------------------------------------


class TestParseSplitRanges:
    def test_basic_two_parts(self, mkbrr_wizard: ModuleType) -> None:
        available = list(range(1, 23))
        parts = mkbrr_wizard.parse_split_ranges("1-11, 12-22", available)
        assert len(parts) == 2
        assert parts[0] == list(range(1, 12))
        assert parts[1] == list(range(12, 23))

    def test_three_parts(self, mkbrr_wizard: ModuleType) -> None:
        available = list(range(1, 25))
        parts = mkbrr_wizard.parse_split_ranges("1-8, 9-16, 17-24", available)
        assert len(parts) == 3

    def test_semicolon_separator(self, mkbrr_wizard: ModuleType) -> None:
        available = list(range(1, 23))
        parts = mkbrr_wizard.parse_split_ranges("1-11; 12-22", available)
        assert len(parts) == 2

    def test_gaps_in_available(self, mkbrr_wizard: ModuleType) -> None:
        """Range 1-14 but episodes 15-16 missing, 17-22 present."""
        available = list(range(1, 15)) + [17, 18, 20, 21]
        parts = mkbrr_wizard.parse_split_ranges("1-11, 12-22", available)
        assert parts[0] == list(range(1, 12))
        # Part 2 only has available episodes within 12-22
        assert parts[1] == [12, 13, 14, 17, 18, 20, 21]

    def test_overlap_raises(self, mkbrr_wizard: ModuleType) -> None:
        available = list(range(1, 23))
        with pytest.raises(ValueError, match="Overlapping"):
            mkbrr_wizard.parse_split_ranges("1-12, 10-22", available)

    def test_empty_range_raises(self, mkbrr_wizard: ModuleType) -> None:
        available = list(range(1, 11))
        with pytest.raises(ValueError, match="no episodes found"):
            mkbrr_wizard.parse_split_ranges("1-5, 20-30", available)

    def test_single_part_allowed(self, mkbrr_wizard: ModuleType) -> None:
        available = list(range(1, 23))
        parts = mkbrr_wizard.parse_split_ranges("1-22", available)
        assert len(parts) == 1
        assert parts[0] == list(range(1, 23))

    def test_invalid_token_raises(self, mkbrr_wizard: ModuleType) -> None:
        with pytest.raises(ValueError, match="Invalid range token"):
            mkbrr_wizard.parse_split_ranges("abc, 12-22", [1, 2, 3])

    def test_reversed_range_raises(self, mkbrr_wizard: ModuleType) -> None:
        with pytest.raises(ValueError, match="start > end"):
            mkbrr_wizard.parse_split_ranges("11-1, 12-22", list(range(1, 23)))

    def test_no_ranges_raises(self, mkbrr_wizard: ModuleType) -> None:
        with pytest.raises(ValueError, match="No ranges"):
            mkbrr_wizard.parse_split_ranges("", [1, 2, 3])


# ---------------------------------------------------------------------------
# build_split_include_patterns
# ---------------------------------------------------------------------------


class TestBuildSplitIncludePatterns:
    def test_basic_patterns(self, mkbrr_wizard: ModuleType) -> None:
        episodes = [
            (1, "Show.S01E01.1080p.mkv"),
            (2, "Show.S01E02.1080p.mkv"),
            (3, "Show.S01E03.1080p.mkv"),
        ]
        patterns = mkbrr_wizard.build_split_include_patterns(episodes, [1, 3])
        assert patterns == ["*S01E01*", "*S01E03*"]

    def test_preserves_case_from_filename(self, mkbrr_wizard: ModuleType) -> None:
        episodes = [(1, "show.s01e01.mkv")]
        patterns = mkbrr_wizard.build_split_include_patterns(episodes, [1])
        # Pattern uses original case from filename
        assert patterns == ["*s01e01*"]

    def test_missing_episode_skipped(self, mkbrr_wizard: ModuleType) -> None:
        """If an episode number isn't in the scan results, it's simply skipped."""
        episodes = [(1, "Show.S01E01.mkv"), (3, "Show.S01E03.mkv")]
        patterns = mkbrr_wizard.build_split_include_patterns(episodes, [1, 2, 3])
        assert patterns == ["*S01E01*", "*S01E03*"]

    def test_sorted_output(self, mkbrr_wizard: ModuleType) -> None:
        episodes = [
            (5, "Show.S01E05.mkv"),
            (2, "Show.S01E02.mkv"),
            (8, "Show.S01E08.mkv"),
        ]
        patterns = mkbrr_wizard.build_split_include_patterns(episodes, [8, 2, 5])
        assert patterns == ["*S01E02*", "*S01E05*", "*S01E08*"]


# ---------------------------------------------------------------------------
# split_output_name
# ---------------------------------------------------------------------------


class TestSplitOutputName:
    def test_basic(self, mkbrr_wizard: ModuleType) -> None:
        result = mkbrr_wizard.split_output_name("Show.S01.1080p-GRP", 1)
        assert result == "Show.S01.1080p-GRP.Part1.torrent"

    def test_part2(self, mkbrr_wizard: ModuleType) -> None:
        result = mkbrr_wizard.split_output_name("Show.S01.1080p-GRP", 2)
        assert result == "Show.S01.1080p-GRP.Part2.torrent"

    def test_trailing_slash(self, mkbrr_wizard: ModuleType) -> None:
        result = mkbrr_wizard.split_output_name("Show.S01/", 1)
        assert result == "Show.S01.Part1.torrent"

    def test_full_path_extracts_folder_name(self, mkbrr_wizard: ModuleType) -> None:
        result = mkbrr_wizard.split_output_name("/mnt/data/Show.S01", 1)
        assert result == "Show.S01.Part1.torrent"

    def test_sakamoto_example(self, mkbrr_wizard: ModuleType) -> None:
        name = "SAKAMOTO.DAYS.S01.REPACK.1080p.BluRay.REMUX.AVC.FLAC.2.0-NAN0"
        result = mkbrr_wizard.split_output_name(name, 1)
        assert result == f"{name}.Part1.torrent"


# ---------------------------------------------------------------------------
# render_split_summary (smoke test — just ensure it doesn't crash)
# ---------------------------------------------------------------------------


class TestRenderSplitSummary:
    def test_renders_without_error(self, mkbrr_wizard: ModuleType) -> None:
        mkbrr_wizard.render_split_summary(
            "Show.S01",
            [[1, 2, 3], [4, 5, 6]],
            [["*S01E01*", "*S01E02*", "*S01E03*"], ["*S01E04*", "*S01E05*", "*S01E06*"]],
            "/output",
        )


# ---------------------------------------------------------------------------
# Integration: split-series flow through main()
# ---------------------------------------------------------------------------


def _mk_args(config_path: str) -> SimpleNamespace:
    return SimpleNamespace(config=config_path, docker=False, native=False)


class TestSplitSeriesMainFlow:
    def test_split_series_creates_two_parts(
        self, tmp_path, mkbrr_wizard: ModuleType, monkeypatch: Any
    ) -> None:
        # Prepare config directory and presets
        config_dir = tmp_path / "cfg"
        config_dir.mkdir()
        presets_yaml = config_dir / "presets.yaml"
        presets_yaml.write_text("presets:\n  btn:\n    announce: https://example.com/announce\n")

        # Prepare season folder with episodes
        season_dir = tmp_path / "data" / "Show.S01.1080p-GRP"
        season_dir.mkdir(parents=True)
        for ep in range(1, 13):
            (season_dir / f"Show.S01E{ep:02d}.1080p.mkv").write_text("x")

        config_yaml = tmp_path / "config.yaml"
        config_yaml.write_text(
            f"""
runtime: native
docker_support: false
chown: false
mkbrr:
  binary: mkbrr
paths:
  host_data_root: {tmp_path}/data
  container_data_root: /data
  host_output_dir: {tmp_path}/torrents
  container_output_dir: /torrentfiles
  host_config_dir: {config_dir}
  container_config_dir: /root/.config/mkbrr
presets_yaml: {presets_yaml}
"""
        )
        (tmp_path / "torrents").mkdir(exist_ok=True)

        monkeypatch.setattr(mkbrr_wizard, "parse_args", lambda: _mk_args(str(config_yaml)))
        monkeypatch.setattr(mkbrr_wizard, "pick_runtime", lambda cfg, forced: "native")
        monkeypatch.setattr(mkbrr_wizard, "_has_prompt_toolkit", False)

        # Prompt.ask sequence:
        # 1 -> choose_action create
        # 1 -> pick_preset btn
        # str(season_dir) -> content path
        # "1-6, 7-12" -> split ranges
        # "q" -> choose_action quit (split uses continue, skips do-another)
        seq = _Seq(
            [
                "1",  # create
                "1",  # preset
                str(season_dir),  # content path
                "1-6, 7-12",  # split ranges
            ]
        )
        monkeypatch.setattr(mkbrr_wizard.Prompt, "ask", seq)

        # Confirm.ask sequence:
        # True -> "Split this season into parts?"
        # True -> confirm command preview
        # False -> "Do another operation?" after split completes
        cseq = _Seq([True, True, False])
        monkeypatch.setattr(mkbrr_wizard.Confirm, "ask", cseq)

        # Track commands that were executed
        executed_cmds: list[list[str]] = []

        class Dummy:
            def __init__(self, returncode=0):
                self.returncode = returncode

        def fake_run(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("cmd", [])
            executed_cmds.append(list(cmd))
            return Dummy(0)

        monkeypatch.setattr(mkbrr_wizard.subprocess, "run", fake_run)

        # Run
        mkbrr_wizard.main()

        # Should have executed 2 commands (Part 1 and Part 2)
        assert len(executed_cmds) == 2

        # Both commands should have --include patterns
        for cmd in executed_cmds:
            assert "--include" in cmd
            # fail_on_season_warning=False means the flag is omitted (mkbrr default)
            assert "--fail-on-season-warning" not in cmd

        # Part 1 should include E01-E06
        part1_includes = []
        it = iter(executed_cmds[0])
        for token in it:
            if token == "--include":
                part1_includes.append(next(it))
        assert len(part1_includes) == 6
        assert "*S01E01*" in part1_includes
        assert "*S01E06*" in part1_includes

        # Part 2 should include E07-E12
        part2_includes = []
        it = iter(executed_cmds[1])
        for token in it:
            if token == "--include":
                part2_includes.append(next(it))
        assert len(part2_includes) == 6
        assert "*S01E07*" in part2_includes
        assert "*S01E12*" in part2_includes

        # Explicit unique output filenames are passed per split part
        assert "--output" in executed_cmds[0]
        assert "--output" in executed_cmds[1]
        out1 = executed_cmds[0][executed_cmds[0].index("--output") + 1]
        out2 = executed_cmds[1][executed_cmds[1].index("--output") + 1]
        assert out1.endswith("Part1.torrent")
        assert out2.endswith("Part2.torrent")

    def test_split_series_single_part_partial(
        self, tmp_path, mkbrr_wizard: ModuleType, monkeypatch: Any
    ) -> None:
        config_dir = tmp_path / "cfg"
        config_dir.mkdir()
        presets_yaml = config_dir / "presets.yaml"
        presets_yaml.write_text("presets:\n  btn:\n    announce: https://example.com/announce\n")

        season_dir = tmp_path / "data" / "Show.S01.1080p-GRP"
        season_dir.mkdir(parents=True)
        for ep in range(1, 13):
            (season_dir / f"Show.S01E{ep:02d}.1080p.mkv").write_text("x")

        config_yaml = tmp_path / "config.yaml"
        config_yaml.write_text(
            f"""
runtime: native
docker_support: false
chown: false
mkbrr:
  binary: mkbrr
paths:
  host_data_root: {tmp_path}/data
  container_data_root: /data
  host_output_dir: {tmp_path}/torrents
  container_output_dir: /torrentfiles
  host_config_dir: {config_dir}
  container_config_dir: /root/.config/mkbrr
presets_yaml: {presets_yaml}
"""
        )
        (tmp_path / "torrents").mkdir(exist_ok=True)

        monkeypatch.setattr(mkbrr_wizard, "parse_args", lambda: _mk_args(str(config_yaml)))
        monkeypatch.setattr(mkbrr_wizard, "pick_runtime", lambda cfg, forced: "native")
        monkeypatch.setattr(mkbrr_wizard, "_has_prompt_toolkit", False)

        seq = _Seq(
            [
                "1",  # create
                "1",  # preset
                str(season_dir),  # content path
                "1-11",  # single range / one partial torrent
            ]
        )
        monkeypatch.setattr(mkbrr_wizard.Prompt, "ask", seq)

        # split? yes, confirm preview yes, do another? no
        cseq = _Seq([True, True, False])
        monkeypatch.setattr(mkbrr_wizard.Confirm, "ask", cseq)

        executed_cmds: list[list[str]] = []

        class Dummy:
            returncode = 0

        def fake_run(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("cmd", [])
            executed_cmds.append(list(cmd))
            return Dummy()

        monkeypatch.setattr(mkbrr_wizard.subprocess, "run", fake_run)

        mkbrr_wizard.main()

        # one single partial command should run
        assert len(executed_cmds) == 1
        includes: list[str] = []
        it = iter(executed_cmds[0])
        for token in it:
            if token == "--include":
                includes.append(next(it))
        assert len(includes) == 11
        assert "*S01E01*" in includes
        assert "*S01E11*" in includes
        assert "*S01E12*" not in includes

    def test_user_declines_split(
        self, tmp_path, mkbrr_wizard: ModuleType, monkeypatch: Any
    ) -> None:
        """When user says No to split, normal create flow runs."""
        config_dir = tmp_path / "cfg"
        config_dir.mkdir()
        presets_yaml = config_dir / "presets.yaml"
        presets_yaml.write_text("presets:\n  btn:\n    announce: https://example.com/announce\n")

        season_dir = tmp_path / "data" / "Show.S01"
        season_dir.mkdir(parents=True)
        for ep in range(1, 7):
            (season_dir / f"Show.S01E{ep:02d}.mkv").write_text("x")

        config_yaml = tmp_path / "config.yaml"
        config_yaml.write_text(
            f"""
runtime: native
docker_support: false
chown: false
mkbrr:
  binary: mkbrr
paths:
  host_data_root: {tmp_path}/data
  container_data_root: /data
  host_output_dir: {tmp_path}/torrents
  container_output_dir: /torrentfiles
  host_config_dir: {config_dir}
  container_config_dir: /root/.config/mkbrr
presets_yaml: {presets_yaml}
"""
        )
        (tmp_path / "torrents").mkdir(exist_ok=True)

        monkeypatch.setattr(mkbrr_wizard, "parse_args", lambda: _mk_args(str(config_yaml)))
        monkeypatch.setattr(mkbrr_wizard, "pick_runtime", lambda cfg, forced: "native")
        monkeypatch.setattr(mkbrr_wizard, "_has_prompt_toolkit", False)

        seq = _Seq(
            [
                "1",  # create
                "1",  # preset
                str(season_dir),  # content path
            ]
        )
        monkeypatch.setattr(mkbrr_wizard.Prompt, "ask", seq)

        # Confirm.ask: decline split, confirm create, decline another
        cseq = _Seq([False, True, False])
        monkeypatch.setattr(mkbrr_wizard.Confirm, "ask", cseq)

        executed_cmds: list[list[str]] = []

        class Dummy:
            returncode = 0

        def fake_run(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("cmd", [])
            executed_cmds.append(list(cmd))
            return Dummy()

        monkeypatch.setattr(mkbrr_wizard.subprocess, "run", fake_run)

        mkbrr_wizard.main()

        # Normal create: 1 command, no --include
        assert len(executed_cmds) == 1
        assert "--include" not in executed_cmds[0]

    def test_no_episodes_goes_directly_to_create(
        self, tmp_path, mkbrr_wizard: ModuleType, monkeypatch: Any
    ) -> None:
        """If folder has no S##E## files, no split prompt appears."""
        config_dir = tmp_path / "cfg"
        config_dir.mkdir()
        presets_yaml = config_dir / "presets.yaml"
        presets_yaml.write_text("presets:\n  btn:\n    announce: https://example.com/announce\n")

        content_dir = tmp_path / "data" / "Movie.2024.1080p"
        content_dir.mkdir(parents=True)
        (content_dir / "movie.mkv").write_text("x")

        config_yaml = tmp_path / "config.yaml"
        config_yaml.write_text(
            f"""
runtime: native
docker_support: false
chown: false
mkbrr:
  binary: mkbrr
paths:
  host_data_root: {tmp_path}/data
  container_data_root: /data
  host_output_dir: {tmp_path}/torrents
  container_output_dir: /torrentfiles
  host_config_dir: {config_dir}
  container_config_dir: /root/.config/mkbrr
presets_yaml: {presets_yaml}
"""
        )
        (tmp_path / "torrents").mkdir(exist_ok=True)

        monkeypatch.setattr(mkbrr_wizard, "parse_args", lambda: _mk_args(str(config_yaml)))
        monkeypatch.setattr(mkbrr_wizard, "pick_runtime", lambda cfg, forced: "native")
        monkeypatch.setattr(mkbrr_wizard, "_has_prompt_toolkit", False)

        seq = _Seq(
            [
                "1",  # create
                "1",  # preset
                str(content_dir),  # content path
            ]
        )
        monkeypatch.setattr(mkbrr_wizard.Prompt, "ask", seq)

        # Confirm: create confirm, do-another -> no
        # (no split question since no episodes detected)
        cseq = _Seq([True, False])
        monkeypatch.setattr(mkbrr_wizard.Confirm, "ask", cseq)

        executed_cmds: list[list[str]] = []

        class Dummy:
            returncode = 0

        def fake_run(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("cmd", [])
            executed_cmds.append(list(cmd))
            return Dummy()

        monkeypatch.setattr(mkbrr_wizard.subprocess, "run", fake_run)

        mkbrr_wizard.main()

        assert len(executed_cmds) == 1
        assert "--include" not in executed_cmds[0]
