"""Where config lives: the XDG layout, an environment variable, and ``--config-dir``."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.support.seam import run_cli_with_env
from tests.test_catalog_drift import notes

if TYPE_CHECKING:
    from pathlib import Path

    from tests.support.seam import ConfigDir


def test_env_var_overrides_where_config_lives(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"

    result = run_cli_with_env({"MCPSHAPE_CONFIG_DIR": str(target)}, "add", "g", "--stdio", "cmd")

    assert result.exit_code == 0, result.output
    assert (target / "upstreams" / "g" / "upstream.toml").is_file()


def test_xdg_config_home_is_honoured(tmp_path: Path) -> None:
    result = run_cli_with_env(
        {"XDG_CONFIG_HOME": str(tmp_path), "MCPSHAPE_CONFIG_DIR": ""}, "add", "g", "--stdio", "cmd"
    )

    assert result.exit_code == 0, result.output
    assert (tmp_path / "mcpshape" / "upstreams" / "g" / "upstream.toml").is_file()


def test_home_config_is_the_default(tmp_path: Path) -> None:
    env = {"HOME": str(tmp_path), "XDG_CONFIG_HOME": "", "MCPSHAPE_CONFIG_DIR": ""}

    result = run_cli_with_env(env, "add", "g", "--stdio", "cmd")

    assert result.exit_code == 0, result.output
    assert (tmp_path / ".config" / "mcpshape" / "upstreams" / "g" / "upstream.toml").is_file()


def test_state_dir_env_var_and_xdg_state_home(config_dir: ConfigDir, tmp_path: Path) -> None:
    config_dir.add_memory_upstream("notes", notes())
    config = {"MCPSHAPE_CONFIG_DIR": str(config_dir.path)}

    explicit = run_cli_with_env(
        {**config, "MCPSHAPE_STATE_DIR": str(tmp_path / "explicit")}, "upstream", "sync", "notes"
    )
    assert explicit.exit_code == 0, explicit.output
    assert (tmp_path / "explicit" / "upstreams" / "notes" / "catalog.json").is_file()

    xdg = run_cli_with_env(
        {**config, "MCPSHAPE_STATE_DIR": "", "XDG_STATE_HOME": str(tmp_path / "xdg")},
        "upstream",
        "sync",
        "notes",
    )
    assert xdg.exit_code == 0, xdg.output
    assert (tmp_path / "xdg" / "mcpshape" / "upstreams" / "notes" / "catalog.json").is_file()
