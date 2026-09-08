"""``doctor`` validates every config file against the shipped schema, Daemon down."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from tests.support.seam import run_cli, run_cli_with_env

if TYPE_CHECKING:
    from pathlib import Path

    from tests.support.seam import ConfigDir


def test_doctor_passes_on_files_the_cli_wrote(config_dir: ConfigDir) -> None:
    run_cli(config_dir, "add", "github", "--stdio", "sh")
    run_cli(config_dir, "proxy", "new", "github/review")
    (config_dir.path / "config.toml").write_text("version = 1\n[daemon]\nport = 9000\n")

    result = run_cli(config_dir, "doctor")

    assert result.exit_code == 0, result.output
    assert "4 file(s)" in result.output


def test_doctor_reports_file_and_key_of_every_problem(config_dir: ConfigDir) -> None:
    run_cli(config_dir, "add", "github", "--stdio", "cmd")
    upstream_dir = config_dir.path / "upstreams" / "github"
    (upstream_dir / "upstream.toml").write_text('version = 2\ntransport = "stdio"\ncommand = "x"\n')
    (upstream_dir / "default.toml").write_text("version = 1\nbogus = true\n")
    (config_dir.path / "config.toml").write_text("version = 1\n[daemon]\nport = 70000\n")

    result = run_cli(config_dir, "doctor")

    assert result.exit_code == 1
    assert "upstream.toml: version" in result.output
    assert "default.toml: bogus" in result.output
    assert "config.toml: daemon.port" in result.output


def test_doctor_reports_unparseable_toml_and_bad_names(config_dir: ConfigDir) -> None:
    run_cli(config_dir, "add", "github", "--stdio", "cmd")
    upstream_dir = config_dir.path / "upstreams" / "github"
    (upstream_dir / "Review.toml").write_text("version = 1\n")
    (upstream_dir / "default.toml").write_text("version = [\n")

    result = run_cli(config_dir, "doctor")

    assert result.exit_code == 1
    assert "Review.toml" in result.output
    assert "default.toml" in result.output


def test_doctor_reports_a_stdio_command_that_is_not_on_this_shells_path(
    config_dir: ConfigDir,
) -> None:
    """#48: a command nothing on the PATH has cannot start, said before anything is called."""
    run_cli(config_dir, "add", "github", "--stdio", "mcpshape-no-such-command")

    result = run_cli(config_dir, "doctor", env={"PATH": "/nowhere/mcpshape-bin"})

    assert result.exit_code == 1
    assert "upstream.toml" in result.output
    assert "command 'mcpshape-no-such-command' is not found on this shell's PATH" in result.output
    assert "/nowhere/mcpshape-bin" in result.output
    assert "under autostart the Daemon uses the PATH written into its unit" in result.output


def test_doctor_passes_a_stdio_command_it_can_find(config_dir: ConfigDir) -> None:
    """A bare name the PATH answers, and an absolute path, are both found."""
    run_cli(config_dir, "add", "shell", "--stdio", "sh")
    run_cli(config_dir, "add", "python", "--stdio", sys.executable)

    result = run_cli(config_dir, "doctor")

    assert result.exit_code == 0, result.output


def test_a_users_file_cannot_name_a_memory_transport(tmp_path: Path) -> None:
    """The ``memory`` transport is the test seam's (#18): outside it, a file saying so is
    refused with a clear reason, and the seam's own fixture is not used here on purpose."""
    upstream_dir = tmp_path / "config" / "upstreams" / "notes"
    upstream_dir.mkdir(parents=True)
    (upstream_dir / "upstream.toml").write_text(
        'version = 1\ntransport = "memory"\ntarget = "tests.support.upstreams:notes"\n'
    )
    (upstream_dir / "default.toml").write_text("version = 1\n")
    env = {
        "MCPSHAPE_CONFIG_DIR": str(tmp_path / "config"),
        "MCPSHAPE_STATE_DIR": str(tmp_path / "state"),
    }

    result = run_cli_with_env(env, "doctor")

    assert result.exit_code == 1, result.output
    assert "upstream.toml: transport" in result.output
    assert "'memory' is the test seam's transport" in result.output
    assert "stdio, http, or sse" in result.output
