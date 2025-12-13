"""Tests for mkbrr-wizard configuration loading."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest  # type: ignore[import-untyped]


class TestLoadConfig:
    """Tests for load_config function."""

    def test_missing_config_raises(self, mkbrr_wizard: ModuleType) -> None:
        """Missing config file should raise FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            mkbrr_wizard.load_config(Path("/nonexistent/config.yaml"))

    def test_minimal_config(self, mkbrr_wizard: ModuleType) -> None:
        """Minimal config should use defaults."""
        yaml_content = """
runtime: native
docker_support: false
chown: false
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            temp_path = f.name

        try:
            cfg = mkbrr_wizard.load_config(Path(temp_path))

            assert cfg.runtime == "native"
            assert cfg.docker_support is False
            assert cfg.chown is False
            # Defaults
            assert cfg.mkbrr.binary == "mkbrr"
            assert cfg.mkbrr.image == "ghcr.io/autobrr/mkbrr"
            assert cfg.ownership.uid == 99
            assert cfg.ownership.gid == 100
        finally:
            os.unlink(temp_path)

    def test_full_config(self, mkbrr_wizard: ModuleType) -> None:
        """Full config should load all values."""
        yaml_content = """
runtime: docker
docker_support: true
docker_user: "1000:1000"
chown: true

mkbrr:
  binary: /usr/local/bin/mkbrr
  image: custom/mkbrr:latest

paths:
  host_data_root: /custom/data
  container_data_root: /mnt
  host_output_dir: /custom/torrents
  container_output_dir: /output
  host_config_dir: /custom/config
  container_config_dir: /config

ownership:
  uid: 1000
  gid: 1000

presets_yaml: /custom/presets.yaml
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            temp_path = f.name

        try:
            cfg = mkbrr_wizard.load_config(Path(temp_path))

            assert cfg.runtime == "docker"
            assert cfg.docker_support is True
            assert cfg.docker_user == "1000:1000"
            assert cfg.chown is True

            assert cfg.mkbrr.binary == "/usr/local/bin/mkbrr"
            assert cfg.mkbrr.image == "custom/mkbrr:latest"

            assert cfg.paths.host_data_root == "/custom/data"
            assert cfg.paths.container_data_root == "/mnt"
            assert cfg.paths.host_output_dir == "/custom/torrents"
            assert cfg.paths.container_output_dir == "/output"

            assert cfg.ownership.uid == 1000
            assert cfg.ownership.gid == 1000

            assert cfg.presets_yaml_host == "/custom/presets.yaml"
        finally:
            os.unlink(temp_path)

    def test_bool_coercion_handles_typo(self, mkbrr_wizard: ModuleType) -> None:
        """Common typo 'ture' should be coerced to True."""
        yaml_content = """
runtime: auto
docker_support: ture
chown: ture
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            temp_path = f.name

        try:
            cfg = mkbrr_wizard.load_config(Path(temp_path))

            assert cfg.docker_support is True
            assert cfg.chown is True
        finally:
            os.unlink(temp_path)

    def test_invalid_runtime_raises(self, mkbrr_wizard: ModuleType) -> None:
        """Invalid runtime value should raise ValueError."""
        yaml_content = """
runtime: invalid
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            temp_path = f.name

        try:
            with pytest.raises(ValueError, match="runtime must be one of"):
                mkbrr_wizard.load_config(Path(temp_path))
        finally:
            os.unlink(temp_path)

    def test_tilde_expansion_in_paths(self, mkbrr_wizard: ModuleType) -> None:
        """Paths with ~ should be expanded."""
        yaml_content = """
runtime: native
paths:
  host_config_dir: ~/.config/mkbrr
presets_yaml: ~/.config/mkbrr/presets.yaml
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            temp_path = f.name

        try:
            cfg = mkbrr_wizard.load_config(Path(temp_path))

            # Should not contain ~
            assert "~" not in cfg.paths.host_config_dir
            assert "~" not in cfg.presets_yaml_host
            # Should contain actual home directory
            assert str(Path.home()) in cfg.paths.host_config_dir
        finally:
            os.unlink(temp_path)


class TestDockerRunBase:
    """Tests for docker_run_base command builder."""

    @pytest.fixture
    def sample_cfg(self, mkbrr_wizard: ModuleType) -> Any:
        """Create a sample config for testing."""
        return mkbrr_wizard.AppCfg(
            runtime="docker",
            docker_support=True,
            chown=False,
            docker_user="1000:1000",
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

    def test_includes_volume_mounts(self, mkbrr_wizard: ModuleType, sample_cfg: Any) -> None:
        """Docker command should include volume mounts."""
        cmd: list[str] = mkbrr_wizard.docker_run_base(sample_cfg, "/data")

        assert "-v" in cmd
        assert f"{sample_cfg.paths.host_data_root}:{sample_cfg.paths.container_data_root}" in cmd
        assert f"{sample_cfg.paths.host_output_dir}:{sample_cfg.paths.container_output_dir}" in cmd

    def test_includes_user_flag(self, mkbrr_wizard: ModuleType, sample_cfg: Any) -> None:
        """Docker command should include --user when docker_user is set."""
        cmd: list[str] = mkbrr_wizard.docker_run_base(sample_cfg, "/data")

        assert "--user" in cmd
        assert "1000:1000" in cmd

    def test_includes_workdir(self, mkbrr_wizard: ModuleType, sample_cfg: Any) -> None:
        """Docker command should include -w workdir."""
        cmd: list[str] = mkbrr_wizard.docker_run_base(sample_cfg, "/output")

        assert "-w" in cmd
        idx = cmd.index("-w")
        assert cmd[idx + 1] == "/output"

    def test_includes_image_and_mkbrr(self, mkbrr_wizard: ModuleType, sample_cfg: Any) -> None:
        """Docker command should end with image and mkbrr."""
        cmd = mkbrr_wizard.docker_run_base(sample_cfg, "/data")

        assert "ghcr.io/autobrr/mkbrr" in cmd
        assert cmd[-1] == "mkbrr"
