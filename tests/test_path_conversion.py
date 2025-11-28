"""Tests for mkbrr-wizard path conversion functions.
"""

from tests.conftest import host_to_container_path, host_to_container_torrent_path


class TestHostToContainerPath:
    """Tests for host_to_container_path function."""

    def test_already_container_path(self):
        """Container paths should pass through unchanged."""
        assert host_to_container_path("/data/downloads/file.mkv") == "/data/downloads/file.mkv"
        assert host_to_container_path("/data") == "/data"
        assert host_to_container_path("/data/") == "/data/"

    def test_host_path_conversion(self):
        """Host paths under HOST_DATA_ROOT should be converted."""
        assert (
            host_to_container_path("/mnt/user/data/downloads/file.mkv")
            == "/data/downloads/file.mkv"
        )
        assert host_to_container_path("/mnt/user/data") == "/data"
        assert host_to_container_path("/mnt/user/data/movies/test") == "/data/movies/test"

    def test_host_path_with_whitespace(self):
        """Paths with leading/trailing whitespace should be trimmed."""
        assert host_to_container_path("  /mnt/user/data/file  ") == "/data/file"
        assert host_to_container_path("  /data/file  ") == "/data/file"

    def test_non_matching_path_passthrough(self):
        """Paths not under HOST_DATA_ROOT should pass through."""
        assert host_to_container_path("/some/other/path") == "/some/other/path"
        assert host_to_container_path("/tmp/test") == "/tmp/test"

    def test_nested_paths(self):
        """Deeply nested paths should be handled correctly."""
        path = "/mnt/user/data/downloads/movies/2024/action/movie.mkv"
        expected = "/data/downloads/movies/2024/action/movie.mkv"
        assert host_to_container_path(path) == expected


class TestHostToContainerTorrentPath:
    """Tests for host_to_container_torrent_path function."""

    def test_already_container_path(self):
        """Container torrent paths should pass through unchanged."""
        assert (
            host_to_container_torrent_path("/torrentfiles/test.torrent")
            == "/torrentfiles/test.torrent"
        )
        assert host_to_container_torrent_path("/torrentfiles") == "/torrentfiles"

    def test_host_torrent_path_conversion(self):
        """Host torrent paths should be converted."""
        host_path = "/mnt/user/data/downloads/torrents/torrentfiles/test.torrent"
        expected = "/torrentfiles/test.torrent"
        assert host_to_container_torrent_path(host_path) == expected

    def test_host_torrent_path_with_whitespace(self):
        """Paths with leading/trailing whitespace should be trimmed."""
        path = "  /mnt/user/data/downloads/torrents/torrentfiles/test.torrent  "
        expected = "/torrentfiles/test.torrent"
        assert host_to_container_torrent_path(path) == expected

    def test_non_matching_path_passthrough(self):
        """Paths not under HOST_OUTPUT_DIR should pass through."""
        assert (
            host_to_container_torrent_path("/some/other/path.torrent") == "/some/other/path.torrent"
        )

    def test_nested_torrent_paths(self):
        """Nested paths under torrentfiles should be handled correctly."""
        host_path = "/mnt/user/data/downloads/torrents/torrentfiles/subdir/test.torrent"
        expected = "/torrentfiles/subdir/test.torrent"
        assert host_to_container_torrent_path(host_path) == expected
