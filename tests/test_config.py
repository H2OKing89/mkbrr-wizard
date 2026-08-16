"""Tests for mkbrr-wizard configuration loading."""

from __future__ import annotations

from pathlib import Path
from types import ModuleType
from typing import Any

import pytest  # type: ignore[import-untyped]


def _write_config(tmp_path: Path, yaml_content: str) -> Path:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml_content)
    return config_path


class TestLoadConfig:
    """Tests for load_config function."""

    def test_public_config_models_are_pydantic_and_normalize_paths(
        self, mkbrr_wizard: ModuleType
    ) -> None:
        paths = mkbrr_wizard.PathsCfg(
            host_config_dir="~/mkbrr/",
            container_data_root="/data/",
        )

        assert isinstance(paths, mkbrr_wizard.BaseModel)
        assert paths.host_data_root == "/mnt/user/data"
        assert paths.host_config_dir == str(Path.home() / "mkbrr")
        assert paths.container_data_root == "/data"

    def test_missing_config_raises(self, mkbrr_wizard: ModuleType) -> None:
        """Missing config file should raise FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            mkbrr_wizard.load_config(Path("/nonexistent/config.yaml"))

    def test_minimal_config(self, tmp_path: Path, mkbrr_wizard: ModuleType) -> None:
        """Minimal config should use defaults."""
        yaml_content = """
runtime: native
docker_support: false
chown: false
"""
        cfg = mkbrr_wizard.load_config(_write_config(tmp_path, yaml_content))

        assert cfg.runtime == "native"
        assert cfg.docker_support is False
        assert cfg.chown is False
        # Defaults
        assert cfg.mkbrr.binary == "mkbrr"
        assert cfg.mkbrr.image == mkbrr_wizard.DEFAULT_MKBRR_IMAGE
        assert cfg.ownership.uid == 99
        assert cfg.ownership.gid == 100
        assert cfg.unraid.enabled is False
        assert cfg.unraid.fuse_root == "/mnt/user"
        assert cfg.unraid.mount_priority == "disk_first"
        assert cfg.unraid.split_share_preflight == "fail"
        assert cfg.unraid.split_share_unmapped_docker_path == "warn"
        assert cfg.unraid.split_share_max_entries == 20000
        assert cfg.unraid.split_share_follow_symlinks is False

    def test_full_config(self, tmp_path: Path, mkbrr_wizard: ModuleType) -> None:
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
unraid:
    enabled: true
    fuse_root: /mnt/user
    mount_priority: cache_first
    split_share_preflight: warn
    split_share_unmapped_docker_path: fail
    split_share_max_entries: 123
    split_share_follow_symlinks: true
"""
        cfg = mkbrr_wizard.load_config(_write_config(tmp_path, yaml_content))

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
        assert cfg.unraid.enabled is True
        assert cfg.unraid.fuse_root == "/mnt/user"
        assert cfg.unraid.mount_priority == "cache_first"
        assert cfg.unraid.split_share_preflight == "warn"
        assert cfg.unraid.split_share_unmapped_docker_path == "fail"
        assert cfg.unraid.split_share_max_entries == 123
        assert cfg.unraid.split_share_follow_symlinks is True

    def test_invalid_unraid_mount_priority_raises(
        self, tmp_path: Path, mkbrr_wizard: ModuleType
    ) -> None:
        yaml_content = """
runtime: native
unraid:
  enabled: true
  mount_priority: maybe
"""
        with pytest.raises(ValueError, match=r"unraid\.mount_priority must be one of"):
            mkbrr_wizard.load_config(_write_config(tmp_path, yaml_content))

    def test_invalid_unmapped_docker_path_mode_raises(
        self, tmp_path: Path, mkbrr_wizard: ModuleType
    ) -> None:
        yaml_content = """
runtime: native
unraid:
  enabled: true
  split_share_unmapped_docker_path: maybe
"""
        with pytest.raises(
            ValueError, match=r"unraid\.split_share_unmapped_docker_path must be one of"
        ):
            mkbrr_wizard.load_config(_write_config(tmp_path, yaml_content))

    def test_invalid_unraid_preflight_mode_raises(
        self, tmp_path: Path, mkbrr_wizard: ModuleType
    ) -> None:
        yaml_content = """
runtime: native
unraid:
  enabled: true
  split_share_preflight: maybe
"""
        with pytest.raises(ValueError, match=r"unraid\.split_share_preflight must be one of"):
            mkbrr_wizard.load_config(_write_config(tmp_path, yaml_content))

    def test_invalid_unraid_max_entries_raises(
        self, tmp_path: Path, mkbrr_wizard: ModuleType
    ) -> None:
        yaml_content = """
runtime: native
unraid:
  enabled: true
  split_share_max_entries: 0
"""
        with pytest.raises(
            ValueError, match=r"unraid\.split_share_max_entries must be a positive integer"
        ):
            mkbrr_wizard.load_config(_write_config(tmp_path, yaml_content))

    def test_legacy_ture_is_migrated_with_one_warning(
        self, tmp_path: Path, mkbrr_wizard: ModuleType
    ) -> None:
        yaml_content = """
runtime: auto
docker_support: ture
chown: ture
"""
        with pytest.warns(UserWarning, match="legacy 'ture'") as warning_records:
            cfg = mkbrr_wizard.load_config(_write_config(tmp_path, yaml_content))

        assert len(warning_records) == 1
        assert cfg.docker_support is True
        assert cfg.chown is True

    @pytest.mark.parametrize(
        "yaml_content",
        [
            pytest.param("runtime: native\nrunttime: native\n", id="top_level"),
            pytest.param(
                "runtime: native\npaths:\n  host_data_rooot: /data\n",
                id="nested",
            ),
        ],
    )
    def test_unknown_config_fields_raise(
        self, tmp_path: Path, mkbrr_wizard: ModuleType, yaml_content: str
    ) -> None:
        with pytest.raises(ValueError, match="Invalid configuration"):
            mkbrr_wizard.load_config(_write_config(tmp_path, yaml_content))

    def test_invalid_runtime_type_is_rejected(
        self, tmp_path: Path, mkbrr_wizard: ModuleType
    ) -> None:
        yaml_content = "runtime: []\n"
        with pytest.raises(ValueError, match="Invalid configuration"):
            mkbrr_wizard.load_config(_write_config(tmp_path, yaml_content))

    def test_validation_error_excludes_input_and_url(
        self, tmp_path: Path, mkbrr_wizard: ModuleType
    ) -> None:
        yaml_content = "unexpected_option: secret-value\n"
        with pytest.raises(ValueError, match="Invalid configuration") as error:
            mkbrr_wizard.load_config(_write_config(tmp_path, yaml_content))

        message = str(error.value)
        assert "secret-value" not in message
        assert "https://errors.pydantic.dev" not in message

    def test_legacy_scalar_values_are_normalized_before_strict_validation(
        self, tmp_path: Path, mkbrr_wizard: ModuleType
    ) -> None:
        yaml_content = """
runtime: native
docker_support: "yes"
chown: 0
ownership:
  uid: "1000"
  gid: "1001"
unraid:
  enabled: 1
"""
        cfg = mkbrr_wizard.load_config(_write_config(tmp_path, yaml_content))

        assert cfg.docker_support is True
        assert cfg.chown is False
        assert cfg.ownership.uid == 1000
        assert cfg.ownership.gid == 1001
        assert cfg.unraid.enabled is True

    @pytest.mark.parametrize("field_name", ["docker_support", "chown"])
    def test_invalid_legacy_boolean_values_are_rejected(
        self, tmp_path: Path, mkbrr_wizard: ModuleType, field_name: str
    ) -> None:
        config_path = _write_config(tmp_path, f"{field_name}: definitely-not-valid\n")

        with pytest.raises(ValueError, match="Invalid configuration"):
            mkbrr_wizard.load_config(config_path)

    def test_null_model_sections_use_defaults(
        self, tmp_path: Path, mkbrr_wizard: ModuleType
    ) -> None:
        top_level_path = tmp_path / "null-top-level.yaml"
        top_level_path.write_text("unraid:\nnotifications:\n")
        nested_path = tmp_path / "null-notification-providers.yaml"
        nested_path.write_text("notifications:\n  pushover:\n  discord:\n")

        top_level_cfg = mkbrr_wizard.load_config(top_level_path)
        nested_cfg = mkbrr_wizard.load_config(nested_path)

        assert top_level_cfg.unraid.enabled is False
        assert top_level_cfg.notifications.enabled is False
        assert nested_cfg.notifications.pushover.enabled is False
        assert nested_cfg.notifications.discord.enabled is False

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            pytest.param("3.0", 3, id="integral_float"),
            pytest.param("3.9", None, id="fractional_float"),
            pytest.param("yes", None, id="boolean"),
        ],
    )
    def test_legacy_integer_normalization_accepts_only_integral_values(
        self, tmp_path: Path, mkbrr_wizard: ModuleType, value: str, expected: int | None
    ) -> None:
        config_path = tmp_path / "integer-normalization.yaml"
        config_path.write_text(f"ownership:\n  uid: {value}\n")

        if expected is None:
            with pytest.raises(ValueError, match="Invalid configuration"):
                mkbrr_wizard.load_config(config_path)
        else:
            assert mkbrr_wizard.load_config(config_path).ownership.uid == expected

    def test_sample_config_is_valid(self, mkbrr_wizard: ModuleType) -> None:
        sample_path = Path(__file__).parents[1] / "config.yaml.sample"

        cfg = mkbrr_wizard.load_config(sample_path)

        assert cfg.mkbrr.image == mkbrr_wizard.DEFAULT_MKBRR_IMAGE

    def test_invalid_runtime_raises(self, tmp_path: Path, mkbrr_wizard: ModuleType) -> None:
        """Invalid runtime value should raise ValueError."""
        yaml_content = """
runtime: invalid
"""
        with pytest.raises(ValueError, match="runtime must be one of"):
            mkbrr_wizard.load_config(_write_config(tmp_path, yaml_content))

    def test_tilde_expansion_in_paths(self, tmp_path: Path, mkbrr_wizard: ModuleType) -> None:
        """Paths with ~ should be expanded."""
        yaml_content = """
runtime: native
paths:
  host_config_dir: ~/.config/mkbrr
presets_yaml: ~/.config/mkbrr/presets.yaml
"""
        cfg = mkbrr_wizard.load_config(_write_config(tmp_path, yaml_content))

        # Should not contain ~
        assert "~" not in cfg.paths.host_config_dir
        assert "~" not in cfg.presets_yaml_host
        # Should contain actual home directory
        assert str(Path.home()) in cfg.paths.host_config_dir

    def test_batch_mode_defaults_to_simple(self, tmp_path: Path, mkbrr_wizard: ModuleType) -> None:
        yaml_content = """
runtime: native
"""
        cfg = mkbrr_wizard.load_config(_write_config(tmp_path, yaml_content))
        assert cfg.batch.mode == "simple"

    def test_batch_mode_advanced_is_loaded(self, tmp_path: Path, mkbrr_wizard: ModuleType) -> None:
        yaml_content = """
runtime: native
batch:
  mode: advanced
"""
        cfg = mkbrr_wizard.load_config(_write_config(tmp_path, yaml_content))
        assert cfg.batch.mode == "advanced"

    def test_invalid_batch_mode_raises(self, tmp_path: Path, mkbrr_wizard: ModuleType) -> None:
        yaml_content = """
runtime: native
batch:
  mode: fast
"""
        with pytest.raises(ValueError, match=r"batch\.mode must be one of"):
            mkbrr_wizard.load_config(_write_config(tmp_path, yaml_content))

    @pytest.mark.parametrize("priority_field", ["priority", "failure_priority"])
    def test_emergency_pushover_priority_is_rejected(
        self, tmp_path: Path, mkbrr_wizard: ModuleType, priority_field: str
    ) -> None:
        config_path = _write_config(
            tmp_path,
            f"notifications:\n  pushover:\n    {priority_field}: 2\n",
        )

        with pytest.raises(ValueError, match=f"pushover.{priority_field}"):
            mkbrr_wizard.load_config(config_path)


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
            batch=mkbrr_wizard.BatchCfg(mode="simple"),
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

    def test_includes_unique_container_name(
        self, mkbrr_wizard: ModuleType, sample_cfg: Any
    ) -> None:
        first = mkbrr_wizard.docker_run_base(sample_cfg, "/data")
        second = mkbrr_wizard.docker_run_base(sample_cfg, "/data")

        first_name = first[first.index("--name") + 1]
        second_name = second[second.index("--name") + 1]
        assert first_name.startswith("mkbrr-wizard-")
        assert first_name != second_name

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
