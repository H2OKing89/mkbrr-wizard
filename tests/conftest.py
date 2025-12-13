"""
Pytest configuration and fixtures for mkbrr-wizard tests.
"""

import importlib.util
import os
import sys
from types import ModuleType

import pytest

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_mkbrr_wizard() -> ModuleType:
    """Load the mkbrr-wizard module dynamically (handles hyphen in filename)."""
    module_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mkbrr-wizard.py"
    )
    spec = importlib.util.spec_from_file_location("mkbrr_wizard", module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load module from {module_path}")
    module = importlib.util.module_from_spec(spec)
    # Register in sys.modules BEFORE exec_module to fix dataclass resolution
    sys.modules["mkbrr_wizard"] = module
    spec.loader.exec_module(module)
    return module


# Load module once at import time and make it available globally
_module = _load_mkbrr_wizard()


@pytest.fixture
def mkbrr_wizard():
    """Fixture providing access to the mkbrr_wizard module."""
    return _module


# Expose key functions for convenience (updated to new API)
map_content_path = _module.map_content_path
map_torrent_path = _module.map_torrent_path
load_config = _module.load_config
load_presets = _module.load_presets
docker_run_base = _module.docker_run_base

# Dataclasses
AppCfg = _module.AppCfg
PathsCfg = _module.PathsCfg
OwnershipCfg = _module.OwnershipCfg
MkbrrCfg = _module.MkbrrCfg
