"""Tests for the workers auto-tune feature (HDD vs SSD detection)."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import MagicMock, patch

import pytest  # type: ignore[import-untyped]

# ---------------------------------------------------------------------------
# WorkersCfg parsing tests
# ---------------------------------------------------------------------------


class TestWorkersCfgParsing:
    """Test that workers config is parsed from YAML correctly."""

    def test_missing_workers_section_uses_defaults(self, mkbrr_wizard: ModuleType) -> None:
        yaml_content = "runtime: native\ndocker_support: false\nchown: false\n"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            temp_path = f.name
        try:
            cfg = mkbrr_wizard.load_config(Path(temp_path))
            assert cfg.workers.hdd == 1
            assert cfg.workers.ssd is None
            assert cfg.workers.default is None
        finally:
            os.unlink(temp_path)

    def test_explicit_workers_values(self, mkbrr_wizard: ModuleType) -> None:
        yaml_content = """\
runtime: native
docker_support: false
chown: false
workers:
  hdd: 2
  ssd: 4
  default: 8
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            temp_path = f.name
        try:
            cfg = mkbrr_wizard.load_config(Path(temp_path))
            assert cfg.workers.hdd == 2
            assert cfg.workers.ssd == 4
            assert cfg.workers.default == 8
        finally:
            os.unlink(temp_path)

    def test_auto_string_becomes_none(self, mkbrr_wizard: ModuleType) -> None:
        yaml_content = """\
runtime: native
docker_support: false
chown: false
workers:
  hdd: auto
  ssd: auto
  default: auto
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            temp_path = f.name
        try:
            cfg = mkbrr_wizard.load_config(Path(temp_path))
            assert cfg.workers.hdd is None
            assert cfg.workers.ssd is None
            assert cfg.workers.default is None
        finally:
            os.unlink(temp_path)

    def test_mixed_auto_and_int(self, mkbrr_wizard: ModuleType) -> None:
        yaml_content = """\
runtime: native
docker_support: false
chown: false
workers:
  hdd: 1
  ssd: auto
  default: 4
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            temp_path = f.name
        try:
            cfg = mkbrr_wizard.load_config(Path(temp_path))
            assert cfg.workers.hdd == 1
            assert cfg.workers.ssd is None
            assert cfg.workers.default == 4
        finally:
            os.unlink(temp_path)

    def test_invalid_workers_value_raises(self, mkbrr_wizard: ModuleType) -> None:
        yaml_content = """\
runtime: native
docker_support: false
chown: false
workers:
  hdd: -1
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            temp_path = f.name
        try:
            with pytest.raises(ValueError, match="workers.hdd"):
                mkbrr_wizard.load_config(Path(temp_path))
        finally:
            os.unlink(temp_path)

    def test_zero_workers_raises(self, mkbrr_wizard: ModuleType) -> None:
        yaml_content = """\
runtime: native
docker_support: false
chown: false
workers:
  ssd: 0
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            temp_path = f.name
        try:
            with pytest.raises(ValueError, match="workers.ssd"):
                mkbrr_wizard.load_config(Path(temp_path))
        finally:
            os.unlink(temp_path)

    def test_non_numeric_string_raises(self, mkbrr_wizard: ModuleType) -> None:
        yaml_content = """\
runtime: native
docker_support: false
chown: false
workers:
  hdd: banana
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            temp_path = f.name
        try:
            with pytest.raises(ValueError, match="workers.hdd"):
                mkbrr_wizard.load_config(Path(temp_path))
        finally:
            os.unlink(temp_path)


# ---------------------------------------------------------------------------
# detect_storage_type tests
# ---------------------------------------------------------------------------


class TestDetectStorageType:
    """Test storage type detection via Unraid path patterns and /sys/block fallback."""

    def test_unraid_disk_path_is_hdd(self, mkbrr_wizard: ModuleType) -> None:
        assert mkbrr_wizard.detect_storage_type("/mnt/disk1/data/test") == "hdd"
        assert mkbrr_wizard.detect_storage_type("/mnt/disk5/data") == "hdd"
        assert mkbrr_wizard.detect_storage_type("/mnt/disk12") == "hdd"
        assert mkbrr_wizard.detect_storage_type("/mnt/disk99/some/deep/path") == "hdd"

    def test_unraid_cache_path_is_ssd(self, mkbrr_wizard: ModuleType) -> None:
        assert mkbrr_wizard.detect_storage_type("/mnt/cache/data/test") == "ssd"
        assert mkbrr_wizard.detect_storage_type("/mnt/cache-nvme/data") == "ssd"
        assert mkbrr_wizard.detect_storage_type("/mnt/cache-ssd/stuff") == "ssd"
        assert mkbrr_wizard.detect_storage_type("/mnt/cache") == "ssd"

    def test_non_unraid_path_no_unraid_match(self, mkbrr_wizard: ModuleType) -> None:
        """Non-Unraid paths should fall through to /sys/block detection."""
        with patch.object(mkbrr_wizard, "_detect_storage_type_sysblock", return_value="unknown"):
            assert mkbrr_wizard.detect_storage_type("/home/user/data") == "unknown"
            assert mkbrr_wizard.detect_storage_type("/tmp/test") == "unknown"

    def test_unraid_disk_prefix_only_not_substring(self, mkbrr_wizard: ModuleType) -> None:
        """Paths like /mnt/diskfoo should NOT match the disk pattern."""
        with patch.object(mkbrr_wizard, "_detect_storage_type_sysblock", return_value="unknown"):
            assert mkbrr_wizard.detect_storage_type("/mnt/diskfoo/test") == "unknown"

    def test_sysblock_fallback_hdd(self, mkbrr_wizard: ModuleType) -> None:
        """Test /sys/block rotational detection returning HDD."""
        mock_stat = MagicMock()
        mock_stat.st_dev = os.makedev(8, 1)  # /dev/sda1

        with (
            patch("os.stat", return_value=mock_stat),
            patch(
                "os.path.realpath",
                return_value="/sys/devices/pci/ata1/host0/target0/0:0:0:0/block/sda/sda1",
            ),
            patch("os.path.isfile") as mock_isfile,
            patch.object(Path, "read_text", return_value="1\n"),
        ):
            mock_isfile.return_value = True
            result = mkbrr_wizard._detect_storage_type_sysblock("/data/test")
            assert result == "hdd"

    def test_sysblock_fallback_ssd(self, mkbrr_wizard: ModuleType) -> None:
        """Test /sys/block rotational detection returning SSD."""
        mock_stat = MagicMock()
        mock_stat.st_dev = os.makedev(259, 0)  # /dev/nvme0n1

        with (
            patch("os.stat", return_value=mock_stat),
            patch("os.path.realpath", return_value="/sys/devices/pci/nvme/nvme0/nvme0n1/nvme0n1p1"),
            patch("os.path.isfile") as mock_isfile,
            patch.object(Path, "read_text", return_value="0\n"),
        ):
            mock_isfile.return_value = True
            result = mkbrr_wizard._detect_storage_type_sysblock("/data/test")
            assert result == "ssd"

    def test_sysblock_fallback_os_error(self, mkbrr_wizard: ModuleType) -> None:
        """When os.stat fails, return unknown."""
        with patch("os.stat", side_effect=OSError("no such file")):
            result = mkbrr_wizard._detect_storage_type_sysblock("/nonexistent/path")
            assert result == "unknown"


# ---------------------------------------------------------------------------
# resolve_workers tests
# ---------------------------------------------------------------------------


class TestResolveWorkers:
    """Test mapping storage type to worker count."""

    def test_hdd_returns_hdd_value(self, mkbrr_wizard: ModuleType) -> None:
        cfg = mkbrr_wizard.WorkersCfg(hdd=1, ssd=None, default=None)
        assert mkbrr_wizard.resolve_workers("hdd", cfg) == 1

    def test_ssd_returns_ssd_value(self, mkbrr_wizard: ModuleType) -> None:
        cfg = mkbrr_wizard.WorkersCfg(hdd=1, ssd=4, default=None)
        assert mkbrr_wizard.resolve_workers("ssd", cfg) == 4

    def test_ssd_auto_returns_none(self, mkbrr_wizard: ModuleType) -> None:
        cfg = mkbrr_wizard.WorkersCfg(hdd=1, ssd=None, default=None)
        assert mkbrr_wizard.resolve_workers("ssd", cfg) is None

    def test_unknown_returns_default(self, mkbrr_wizard: ModuleType) -> None:
        cfg = mkbrr_wizard.WorkersCfg(hdd=1, ssd=None, default=2)
        assert mkbrr_wizard.resolve_workers("unknown", cfg) == 2

    def test_unknown_auto_default_returns_none(self, mkbrr_wizard: ModuleType) -> None:
        cfg = mkbrr_wizard.WorkersCfg(hdd=1, ssd=None, default=None)
        assert mkbrr_wizard.resolve_workers("unknown", cfg) is None

    def test_all_auto_returns_none(self, mkbrr_wizard: ModuleType) -> None:
        cfg = mkbrr_wizard.WorkersCfg(hdd=None, ssd=None, default=None)
        assert mkbrr_wizard.resolve_workers("hdd", cfg) is None
        assert mkbrr_wizard.resolve_workers("ssd", cfg) is None
        assert mkbrr_wizard.resolve_workers("unknown", cfg) is None

    def test_all_explicit(self, mkbrr_wizard: ModuleType) -> None:
        cfg = mkbrr_wizard.WorkersCfg(hdd=1, ssd=8, default=4)
        assert mkbrr_wizard.resolve_workers("hdd", cfg) == 1
        assert mkbrr_wizard.resolve_workers("ssd", cfg) == 8
        assert mkbrr_wizard.resolve_workers("unknown", cfg) == 4


# ---------------------------------------------------------------------------
# _resolve_host_path_for_detection tests
# ---------------------------------------------------------------------------


class TestResolveHostPathForDetection:
    """Test host path resolution for storage detection."""

    def _make_cfg(self, mkbrr_wizard: ModuleType) -> Any:
        cfg = mkbrr_wizard.AppCfg(
            runtime="auto",
            docker_support=True,
            chown=True,
            docker_user=None,
            mkbrr=mkbrr_wizard.MkbrrCfg(binary="mkbrr", image="ghcr.io/autobrr/mkbrr"),
            paths=mkbrr_wizard.PathsCfg(
                host_data_root="/mnt/user/data",
                container_data_root="/data",
                host_output_dir="/mnt/user/data/output",
                container_output_dir="/output",
                host_config_dir="/mnt/cache/appdata/mkbrr",
                container_config_dir="/root/.config/mkbrr",
            ),
            ownership=mkbrr_wizard.OwnershipCfg(uid=99, gid=100),
            batch=mkbrr_wizard.BatchCfg(mode="simple"),
            presets_yaml_host="/mnt/cache/appdata/mkbrr/presets.yaml",
            presets_yaml_container="/root/.config/mkbrr/presets.yaml",
        )
        return cfg

    def test_native_returns_mapped_path(self, mkbrr_wizard: ModuleType) -> None:
        cfg = self._make_cfg(mkbrr_wizard)
        result = mkbrr_wizard._resolve_host_path_for_detection(
            cfg, "native", "/mnt/disk5/data/test", None
        )
        assert result == "/mnt/disk5/data/test"

    def test_docker_with_override(self, mkbrr_wizard: ModuleType) -> None:
        cfg = self._make_cfg(mkbrr_wizard)
        result = mkbrr_wizard._resolve_host_path_for_detection(
            cfg, "docker", "/data/test", "/mnt/disk5/data"
        )
        assert result == "/mnt/disk5/data"

    def test_docker_without_override_maps_to_host(self, mkbrr_wizard: ModuleType) -> None:
        cfg = self._make_cfg(mkbrr_wizard)
        result = mkbrr_wizard._resolve_host_path_for_detection(
            cfg, "docker", "/data/downloads/test", None
        )
        # Should map container /data/ → host /mnt/user/data/
        assert result.startswith("/mnt/user/data/")
