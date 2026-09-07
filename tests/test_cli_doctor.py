"""``doctor`` validates every config file against the shipped schema, Daemon down."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.support.seam import run_cli

if TYPE_CHECKING:
    from tests.support.seam import ConfigDir


def test_doctor_passes_on_files_the_cli_wrote(config_dir: ConfigDir) -> None:
    run_cli(config_dir, "add", "github", "--stdio", "cmd")
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
