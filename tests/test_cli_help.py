"""The CLI is learnable from ``--help`` alone."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest
import typer.main

from mcpshape.cli import app
from tests.support.seam import run_cli

if TYPE_CHECKING:
    from tests.support.seam import ConfigDir

TOP_LEVEL = ["add", "ls", "upstream", "proxy", "tool", "daemon", "ui", "doctor"]


def test_top_level_help_shows_exactly_the_public_commands(config_dir: ConfigDir) -> None:
    result = run_cli(config_dir, "--help")

    assert result.exit_code == 0
    shown = [name for name in [*TOP_LEVEL, "serve"] if f" {name} " in result.output]
    assert shown == TOP_LEVEL


def test_serve_is_registered_but_hidden(config_dir: ConfigDir) -> None:
    result = run_cli(config_dir, "serve", "--help")

    assert result.exit_code == 0
    assert "stdio" in result.output


def test_serve_documents_both_ways_of_naming_a_proxy(config_dir: ConfigDir) -> None:
    result = run_cli(config_dir, "serve", "--help")

    assert result.exit_code == 0
    assert "UPSTREAM[/PROXY]" in result.output, "the form proxy install writes"
    assert "[PROXY]" in result.output, "and the Proxy given separately"


def test_the_help_of_env_names_the_env_block(config_dir: ConfigDir) -> None:
    """The block's name is square-bracketed as the file shows it, which the help's own markup
    would otherwise read as a tag and drop."""
    for command in (["add"], ["upstream", "env"]):
        result = run_cli(config_dir, *command, "--help")

        assert result.exit_code == 0
        assert "[env] block" in result.output


def test_upstream_help_lists_scan(config_dir: ConfigDir) -> None:
    result = run_cli(config_dir, "upstream", "--help")

    assert result.exit_code == 0
    assert " scan " in result.output


def command_paths() -> list[list[str]]:
    def walk(command: object, path: list[str]) -> list[list[str]]:
        paths = [path]
        subcommands = cast("dict[str, object]", getattr(command, "commands", {}))
        for name, sub in subcommands.items():
            paths += walk(sub, [*path, name])
        return paths

    return walk(typer.main.get_command(app), [])


@pytest.mark.parametrize("path", command_paths(), ids=lambda path: " ".join(path) or "(root)")
@pytest.mark.parametrize("flag", ["-h", "--help"])
def test_every_command_answers_help_with_one_example(
    config_dir: ConfigDir, path: list[str], flag: str
) -> None:
    result = run_cli(config_dir, *path, flag)

    assert result.exit_code == 0, result.output
    assert result.output.count("Example: mcpshape") == 1
