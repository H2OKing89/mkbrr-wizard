"""
Pytest configuration and fixtures for mkbrr-wizard tests.
"""

import importlib.util
import os
import sys

import pytest

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_mkbrr_wizard():
    """Load the mkbrr-wizard module dynamically (handles hyphen in filename)."""
    module_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mkbrr-wizard.py"
    )
    spec = importlib.util.spec_from_file_location("mkbrr_wizard", module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load module from {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Load module once at import time and make it available globally
_module = _load_mkbrr_wizard()


@pytest.fixture
def mkbrr_wizard():
    """Fixture providing access to the mkbrr_wizard module."""
    return _module


# Also expose key functions directly for convenience
host_to_container_path = _module.host_to_container_path
host_to_container_torrent_path = _module.host_to_container_torrent_path
load_presets_from_yaml = _module.load_presets_from_yaml
build_command = _module.build_command
build_inspect_command = _module.build_inspect_command
build_check_command = _module.build_check_command
_docker_base = _module._docker_base

# Constants
HOST_DATA_ROOT = _module.HOST_DATA_ROOT
CONTAINER_DATA_ROOT = _module.CONTAINER_DATA_ROOT
HOST_OUTPUT_DIR = _module.HOST_OUTPUT_DIR
CONTAINER_OUTPUT_DIR = _module.CONTAINER_OUTPUT_DIR
IMAGE = _module.IMAGE
