"""Where config lives: the XDG layout, an environment variable, and ``--config-dir``."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.support.seam import run_cli_with_env

if TYPE_CHECKING:
    from pathlib import Path


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
