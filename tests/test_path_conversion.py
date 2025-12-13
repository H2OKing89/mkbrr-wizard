"""Tests for mkbrr-wizard path conversion functions (new config-driven API)."""

from __future__ import annotations

from pathlib import Path
from types import ModuleType
from typing import Any

import pytest  # type: ignore[import-untyped]


class TestMapContentPath:
    """Tests for map_content_path function (config-driven)."""

    @pytest.fixture
    def sample_cfg(self, mkbrr_wizard: ModuleType) -> Any:
        """Create a sample config for testing."""
        return mkbrr_wizard.AppCfg(
            runtime="auto",
            docker_support=True,
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
            presets_yaml_host="/mnt/cache/appdata/mkbrr/presets.yaml",
            presets_yaml_container="/root/.config/mkbrr/presets.yaml",
        )

    def test_docker_already_container_path(self, mkbrr_wizard: ModuleType, sample_cfg: Any) -> None:
        """Container paths in docker mode should pass through unchanged."""
        assert (
            mkbrr_wizard.map_content_path(sample_cfg, "docker", "/data/downloads/file.mkv")
            == "/data/downloads/file.mkv"
        )
        assert mkbrr_wizard.map_content_path(sample_cfg, "docker", "/data") == "/data"

    def test_docker_host_to_container(self, mkbrr_wizard: ModuleType, sample_cfg: Any) -> None:
        """Host paths in docker mode should be converted to container paths."""
        assert (
            mkbrr_wizard.map_content_path(sample_cfg, "docker", "/mnt/user/data/downloads/file.mkv")
            == "/data/downloads/file.mkv"
        )
        assert mkbrr_wizard.map_content_path(sample_cfg, "docker", "/mnt/user/data") == "/data"

    def test_native_already_host_path(self, mkbrr_wizard: ModuleType, sample_cfg: Any) -> None:
        """Host paths in native mode should pass through unchanged."""
        assert (
            mkbrr_wizard.map_content_path(sample_cfg, "native", "/mnt/user/data/downloads/file.mkv")
            == "/mnt/user/data/downloads/file.mkv"
        )

    def test_native_container_to_host(self, mkbrr_wizard: ModuleType, sample_cfg: Any) -> None:
        """Container paths in native mode should be converted to host paths."""
        assert (
            mkbrr_wizard.map_content_path(sample_cfg, "native", "/data/downloads/file.mkv")
            == "/mnt/user/data/downloads/file.mkv"
        )

    def test_whitespace_trimmed(self, mkbrr_wizard: ModuleType, sample_cfg: Any) -> None:
        """Paths with leading/trailing whitespace should be trimmed."""
        assert (
            mkbrr_wizard.map_content_path(sample_cfg, "docker", "  /mnt/user/data/file  ")
            == "/data/file"
        )


class TestMapTorrentPath:
    """Tests for map_torrent_path function (config-driven)."""

    @pytest.fixture
    def sample_cfg(self, mkbrr_wizard: ModuleType) -> Any:
        """Create a sample config for testing."""
        return mkbrr_wizard.AppCfg(
            runtime="auto",
            docker_support=True,
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
            presets_yaml_host="/mnt/cache/appdata/mkbrr/presets.yaml",
            presets_yaml_container="/root/.config/mkbrr/presets.yaml",
        )

    def test_docker_already_container_path(self, mkbrr_wizard: ModuleType, sample_cfg: Any) -> None:
        """Container torrent paths in docker mode should pass through unchanged."""
        assert (
            mkbrr_wizard.map_torrent_path(sample_cfg, "docker", "/torrentfiles/test.torrent")
            == "/torrentfiles/test.torrent"
        )

    def test_docker_host_to_container(self, mkbrr_wizard: ModuleType, sample_cfg: Any) -> None:
        """Host torrent paths in docker mode should be converted."""
        host_path = "/mnt/user/data/downloads/torrents/torrentfiles/test.torrent"
        expected = "/torrentfiles/test.torrent"
        assert mkbrr_wizard.map_torrent_path(sample_cfg, "docker", host_path) == expected

    def test_native_already_host_path(self, mkbrr_wizard: ModuleType, sample_cfg: Any) -> None:
        """Host torrent paths in native mode should pass through unchanged."""
        host_path = "/mnt/user/data/downloads/torrents/torrentfiles/test.torrent"
        assert mkbrr_wizard.map_torrent_path(sample_cfg, "native", host_path) == host_path

    def test_native_container_to_host(self, mkbrr_wizard: ModuleType, sample_cfg: Any) -> None:
        """Container torrent paths in native mode should be converted to host."""
        assert (
            mkbrr_wizard.map_torrent_path(sample_cfg, "native", "/torrentfiles/test.torrent")
            == "/mnt/user/data/downloads/torrents/torrentfiles/test.torrent"
        )


class TestExpandPath:
    """Tests for _expand_path helper."""

    def test_tilde_expansion(self, mkbrr_wizard: ModuleType) -> None:
        """Tilde should expand to home directory."""
        result: str = mkbrr_wizard._expand_path("~/test")
        assert result == str(Path.home() / "test")

    def test_env_var_expansion(self, mkbrr_wizard: ModuleType, monkeypatch: Any) -> None:
        """Environment variables should be expanded."""
        monkeypatch.setenv("MY_TEST_VAR", "/custom/path")
        result: str = mkbrr_wizard._expand_path("$MY_TEST_VAR/subdir")
        assert result == "/custom/path/subdir"

    def test_empty_string(self, mkbrr_wizard: ModuleType) -> None:
        """Empty string should return empty string."""
        assert mkbrr_wizard._expand_path("") == ""
        assert mkbrr_wizard._expand_path("   ") == ""


class TestCleanUserPath:
    """Tests for _clean_user_path helper."""

    def test_strips_quotes(self, mkbrr_wizard: ModuleType) -> None:
        """Surrounding quotes should be stripped."""
        assert mkbrr_wizard._clean_user_path("'/path/to/file'") == "/path/to/file"
        assert mkbrr_wizard._clean_user_path('"/path/to/file"') == "/path/to/file"

    def test_strips_whitespace(self, mkbrr_wizard: ModuleType) -> None:
        """Leading/trailing whitespace should be stripped."""
        assert mkbrr_wizard._clean_user_path("  /path/to/file  ") == "/path/to/file"

    def test_handles_quotes_with_whitespace(self, mkbrr_wizard: ModuleType) -> None:
        """Quotes with whitespace should be handled."""
        assert mkbrr_wizard._clean_user_path("  '/path/to/file'  ") == "/path/to/file"

    def test_expands_tilde(self, mkbrr_wizard: ModuleType) -> None:
        """Tilde should be expanded after quote stripping."""
        result: str = mkbrr_wizard._clean_user_path("'~/test'")
        assert result == str(Path.home() / "test")
