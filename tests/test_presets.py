"""Tests for mkbrr-wizard preset loading functionality (new config-driven API)."""

from __future__ import annotations

import os
import tempfile
from types import ModuleType


class TestLoadPresets:
    """Tests for load_presets function."""

    def test_missing_file_returns_fallback(self, mkbrr_wizard: ModuleType) -> None:
        """Missing file should return fallback presets."""
        result: list[str] = mkbrr_wizard.load_presets("/nonexistent/path/presets.yaml")

        assert result == ["btn", "custom"]

    def test_valid_presets_file(self, mkbrr_wizard: ModuleType) -> None:
        """Valid presets file should return preset names."""
        yaml_content = """
presets:
  btn:
    announce: https://example.com/announce
    source: BTN
  mam:
    announce: https://example2.com/announce
    source: MAM
  red:
    announce: https://example3.com/announce
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            temp_path = f.name

        try:
            result = mkbrr_wizard.load_presets(temp_path)

            # btn should be first
            assert result[0] == "btn"
            assert "mam" in result
            assert "red" in result
            assert len(result) == 3
        finally:
            os.unlink(temp_path)

    def test_btn_is_prioritized(self, mkbrr_wizard: ModuleType) -> None:
        """btn preset should be moved to first position."""
        yaml_content = """
presets:
  mam:
    announce: https://example.com/announce
  btn:
    announce: https://example2.com/announce
  red:
    announce: https://example3.com/announce
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            temp_path = f.name

        try:
            result = mkbrr_wizard.load_presets(temp_path)

            # btn should be moved to first position
            assert result[0] == "btn"
            assert result == ["btn", "mam", "red"]
        finally:
            os.unlink(temp_path)

    def test_empty_presets_returns_fallback(self, mkbrr_wizard: ModuleType) -> None:
        """Empty presets section should return fallback."""
        yaml_content = """
presets:
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            temp_path = f.name

        try:
            result = mkbrr_wizard.load_presets(temp_path)
            assert result == ["btn", "custom"]
        finally:
            os.unlink(temp_path)

    def test_no_presets_key_returns_fallback(self, mkbrr_wizard: ModuleType) -> None:
        """YAML without presets key should return fallback."""
        yaml_content = """
other_stuff:
  key: value
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            temp_path = f.name

        try:
            result = mkbrr_wizard.load_presets(temp_path)
            assert result == ["btn", "custom"]
        finally:
            os.unlink(temp_path)
