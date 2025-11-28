"""Tests for mkbrr-wizard preset loading functionality.
"""

import os
import tempfile

from tests.conftest import load_presets_from_yaml


class TestLoadPresetsFromYaml:
    """Tests for load_presets_from_yaml function."""

    def test_missing_file_returns_fallback(self):
        """Missing file should return fallback presets."""
        result = load_presets_from_yaml("/nonexistent/path/presets.yaml")

        assert result == ["btn", "custom"]

    def test_valid_presets_file(self):
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
            result = load_presets_from_yaml(temp_path)

            # btn should be first
            assert result[0] == "btn"
            assert "mam" in result
            assert "red" in result
            assert len(result) == 3
        finally:
            os.unlink(temp_path)

    def test_btn_is_prioritized(self):
        """btn preset should be moved to first position."""
        yaml_content = """
presets:
  mam:
    source: MAM
  red:
    source: RED
  btn:
    source: BTN
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            temp_path = f.name

        try:
            result = load_presets_from_yaml(temp_path)

            assert result[0] == "btn"
        finally:
            os.unlink(temp_path)

    def test_empty_presets_returns_fallback(self):
        """Empty presets section should return fallback."""
        yaml_content = """
presets: {}
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            temp_path = f.name

        try:
            result = load_presets_from_yaml(temp_path)

            assert result == ["btn", "custom"]
        finally:
            os.unlink(temp_path)

    def test_missing_presets_key_returns_fallback(self):
        """Missing presets key should return fallback."""
        yaml_content = """
other_key: value
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            temp_path = f.name

        try:
            result = load_presets_from_yaml(temp_path)

            assert result == ["btn", "custom"]
        finally:
            os.unlink(temp_path)

    def test_invalid_yaml_returns_fallback(self):
        """Invalid YAML should return fallback."""
        yaml_content = """
presets:
  - invalid: yaml: syntax
  broken
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            temp_path = f.name

        try:
            result = load_presets_from_yaml(temp_path)

            assert result == ["btn", "custom"]
        finally:
            os.unlink(temp_path)

    def test_presets_without_btn(self):
        """Presets without btn should return in original order."""
        yaml_content = """
presets:
  mam:
    source: MAM
  red:
    source: RED
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            temp_path = f.name

        try:
            result = load_presets_from_yaml(temp_path)

            assert "mam" in result
            assert "red" in result
            assert "btn" not in result
        finally:
            os.unlink(temp_path)
