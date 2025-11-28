"""Tests for mkbrr-wizard command building functions.
"""

from tests.conftest import (
    CONTAINER_DATA_ROOT,
    CONTAINER_OUTPUT_DIR,
    HOST_DATA_ROOT,
    HOST_OUTPUT_DIR,
    IMAGE,
    _docker_base,
    build_check_command,
    build_command,
    build_inspect_command,
)


class TestDockerBase:
    """Tests for _docker_base function."""

    def test_docker_base_structure(self):
        """Docker base command should have correct structure."""
        cmd = _docker_base()

        assert cmd[0] == "docker"
        assert cmd[1] == "run"
        assert "--rm" in cmd
        assert "-it" in cmd
        assert IMAGE in cmd

    def test_docker_base_volume_mounts(self):
        """Docker base should include all required volume mounts."""
        cmd = _docker_base()
        cmd_str = " ".join(cmd)

        # Check data mount
        assert f"{HOST_DATA_ROOT}:{CONTAINER_DATA_ROOT}" in cmd_str
        # Check torrentfiles mount
        assert f"{HOST_OUTPUT_DIR}:{CONTAINER_OUTPUT_DIR}" in cmd_str


class TestBuildCommand:
    """Tests for build_command function (create torrent)."""

    def test_build_command_structure(self):
        """Build command should have correct structure."""
        cmd = build_command("/data/test", "btn")

        assert "mkbrr" in cmd
        assert "create" in cmd
        assert "/data/test" in cmd
        assert "-P" in cmd
        assert "btn" in cmd
        assert "--output-dir" in cmd
        assert CONTAINER_OUTPUT_DIR in cmd

    def test_build_command_preset_placement(self):
        """Preset should come after -P flag."""
        cmd = build_command("/data/test", "custom_preset")

        p_index = cmd.index("-P")
        assert cmd[p_index + 1] == "custom_preset"

    def test_build_command_different_paths(self):
        """Build command should work with various paths."""
        cmd = build_command("/data/downloads/movies/test.mkv", "mam")

        assert "/data/downloads/movies/test.mkv" in cmd
        assert "mam" in cmd


class TestBuildInspectCommand:
    """Tests for build_inspect_command function."""

    def test_inspect_command_structure(self):
        """Inspect command should have correct structure."""
        cmd = build_inspect_command("/torrentfiles/test.torrent", verbose=False)

        assert "mkbrr" in cmd
        assert "inspect" in cmd
        assert "/torrentfiles/test.torrent" in cmd

    def test_inspect_command_verbose(self):
        """Verbose flag should be added when requested."""
        cmd = build_inspect_command("/torrentfiles/test.torrent", verbose=True)

        assert "-v" in cmd

    def test_inspect_command_no_verbose(self):
        """Verbose flag should not be present when not requested."""
        cmd = build_inspect_command("/torrentfiles/test.torrent", verbose=False)

        # Check that -v doesn't appear after "inspect" (ignore docker volume flags)
        inspect_idx = cmd.index("inspect")
        cmd_after_inspect = cmd[inspect_idx:]
        assert "-v" not in cmd_after_inspect


class TestBuildCheckCommand:
    """Tests for build_check_command function."""

    def test_check_command_structure(self):
        """Check command should have correct structure."""
        cmd = build_check_command(
            torrent_container_path="/torrentfiles/test.torrent",
            content_container_path="/data/downloads/test",
            verbose=False,
            quiet=False,
            workers=None,
        )

        assert "mkbrr" in cmd
        assert "check" in cmd
        assert "/torrentfiles/test.torrent" in cmd
        assert "/data/downloads/test" in cmd

    def test_check_command_verbose(self):
        """Verbose flag should be added when requested."""
        cmd = build_check_command(
            torrent_container_path="/torrentfiles/test.torrent",
            content_container_path="/data/test",
            verbose=True,
            quiet=False,
            workers=None,
        )

        assert "-v" in cmd

    def test_check_command_quiet(self):
        """Quiet flag should be added when requested."""
        cmd = build_check_command(
            torrent_container_path="/torrentfiles/test.torrent",
            content_container_path="/data/test",
            verbose=False,
            quiet=True,
            workers=None,
        )

        assert "--quiet" in cmd

    def test_check_command_workers(self):
        """Workers should be added when specified."""
        cmd = build_check_command(
            torrent_container_path="/torrentfiles/test.torrent",
            content_container_path="/data/test",
            verbose=False,
            quiet=False,
            workers=4,
        )

        assert "--workers" in cmd
        workers_index = cmd.index("--workers")
        assert cmd[workers_index + 1] == "4"

    def test_check_command_all_options(self):
        """All options should be included when specified."""
        cmd = build_check_command(
            torrent_container_path="/torrentfiles/test.torrent",
            content_container_path="/data/test",
            verbose=True,
            quiet=True,
            workers=8,
        )

        assert "-v" in cmd
        assert "--quiet" in cmd
        assert "--workers" in cmd
        assert "8" in cmd
