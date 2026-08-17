"""
Pytest configuration and fixtures for mkbrr-wizard tests.
"""

import importlib
import sys
from pathlib import Path
from types import ModuleType

import pytest

# Import the real src-layout package in source checkouts.
SOURCE_ROOT = (Path(__file__).parents[1] / "src").resolve()
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))


def _load_mkbrr_wizard() -> ModuleType:
    """Load the legacy compatibility module from the installable package."""
    return importlib.import_module("mkbrr_wizard.legacy_app")


# Load module once at import time and make it available globally
_module = _load_mkbrr_wizard()


@pytest.fixture
def mkbrr_wizard():
    """Fixture providing access to the mkbrr_wizard module."""
    return _module


@pytest.fixture
def base_app_cfg(mkbrr_wizard):
    """Factory fixture returning AppCfg with shared defaults and optional overrides."""

    def _factory(**overrides):
        cfg_values = {
            "runtime": "native",
            "docker_support": False,
            "chown": False,
            "docker_user": None,
            "mkbrr": mkbrr_wizard.MkbrrCfg(binary="mkbrr", image="ghcr.io/autobrr/mkbrr"),
            "paths": mkbrr_wizard.PathsCfg(
                host_data_root="/mnt/user/data",
                container_data_root="/data",
                host_output_dir="/mnt/user/data/downloads/torrents/torrentfiles",
                container_output_dir="/torrentfiles",
                host_config_dir="/mnt/cache/appdata/mkbrr",
                container_config_dir="/root/.config/mkbrr",
            ),
            "ownership": mkbrr_wizard.OwnershipCfg(uid=99, gid=100),
            "batch": mkbrr_wizard.BatchCfg(mode="simple"),
            "presets_yaml_host": "/mnt/cache/appdata/mkbrr/presets.yaml",
            "presets_yaml_container": "/root/.config/mkbrr/presets.yaml",
        }
        cfg_values.update(overrides)
        return mkbrr_wizard.AppCfg(**cfg_values)

    return _factory


class _Seq:
    """Callable that returns successive items from a list.

    Used in tests to script prompt answers.  The `__call__` method returns
    the next item each time it is invoked.  If invoked more times than there
    are available items it raises ``StopIteration``.
    """

    def __init__(self, items):
        self._it = iter(items)

    def __call__(self, *args, **kwargs):
        return next(self._it)


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
