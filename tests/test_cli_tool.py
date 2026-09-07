"""``mcpshape tool``: hide, show, rename, describe, trim edit the Proxy file, Daemon down."""

from __future__ import annotations

import asyncio
import sys
from typing import TYPE_CHECKING

from tests.support.seam import run_cli, running_daemon
from tests.test_overrides import issues, synced

if TYPE_CHECKING:
    from pathlib import Path

    from tests.support.seam import ConfigDir


def proxy_file(config_dir: ConfigDir) -> Path:
    return config_dir.path / "upstreams" / "issues" / "default.toml"


def scanned(config_dir: ConfigDir) -> None:
    config_dir.add_memory_upstream("issues", issues())
    result = run_cli(config_dir, "upstream", "sync", "issues")
    assert result.exit_code == 0, result.output


def test_hide_and_show_toggle_hidden_and_keep_comments(config_dir: ConfigDir) -> None:
    scanned(config_dir)
    proxy_file(config_dir).write_text("version = 1  # keep\n\n# my curation\n[tools.close_issue]\n")

    hidden = run_cli(config_dir, "tool", "hide", "issues/default", "list_issues")
    assert hidden.exit_code == 0, hidden.output
    text = proxy_file(config_dir).read_text()
    assert text.startswith("version = 1  # keep\n")
    assert "# my curation\n[tools.close_issue]\n" in text
    assert "[tools.list_issues]\nhidden = true" in text
    assert "list_issues" in hidden.output

    shown = run_cli(config_dir, "tool", "show", "issues/default", "list_issues")
    assert shown.exit_code == 0, shown.output
    assert "[tools.list_issues]\nhidden = false" in proxy_file(config_dir).read_text()


async def test_hide_reaches_clients(config_dir: ConfigDir) -> None:
    await synced(config_dir, issues())

    result = await asyncio.to_thread(
        run_cli, config_dir, "tool", "hide", "issues/default", "close_issue"
    )

    assert result.exit_code == 0, result.output
    async with running_daemon(config_dir) as daemon, daemon.client("/issues/mcp") as client:
        assert sorted(t.name for t in await client.list_tools()) == ["create_issue", "list_issues"]


def test_rename_and_describe_write_overrides_by_catalog_name(config_dir: ConfigDir) -> None:
    scanned(config_dir)

    renamed = run_cli(config_dir, "tool", "rename", "issues/default", "create_issue", "new_issue")
    described = run_cli(
        config_dir, "tool", "describe", "issues/default", "create_issue", "Open an issue."
    )

    assert renamed.exit_code == 0, renamed.output
    assert described.exit_code == 0, described.output
    text = proxy_file(config_dir).read_text()
    assert "[tools.create_issue]\n" in text
    assert 'name = "new_issue"\n' in text
    assert 'description = "Open an issue."\n' in text
    assert "[tools.new_issue]" not in text


def test_argument_edits_go_under_the_tool_by_catalog_argument_name(config_dir: ConfigDir) -> None:
    scanned(config_dir)

    for args in (
        ("rename", "issues/default", "create_issue", "summary", "--arg", "title"),
        ("describe", "issues/default", "create_issue", "One line.", "--arg", "title"),
        ("hide", "issues/default", "create_issue", "--arg", "labels"),
        ("show", "issues/default", "create_issue", "--arg", "labels"),
    ):
        result = run_cli(config_dir, "tool", *args)
        assert result.exit_code == 0, result.output

    text = proxy_file(config_dir).read_text()
    assert "[tools.create_issue.args.title]\n" in text
    assert 'name = "summary"\n' in text
    assert 'description = "One line."\n' in text
    assert "[tools.create_issue.args.labels]\nhidden = false\n" in text


def test_edits_to_a_name_the_catalog_lacks_are_written_with_a_warning(
    config_dir: ConfigDir,
) -> None:
    scanned(config_dir)

    tool = run_cli(config_dir, "tool", "hide", "issues/default", "nope")
    argument = run_cli(
        config_dir, "tool", "hide", "issues/default", "create_issue", "--arg", "nope"
    )

    assert tool.exit_code == 0, tool.output
    assert "nope" in tool.output
    assert "Catalog" in tool.output
    assert argument.exit_code == 0, argument.output
    assert "nope" in argument.output
    text = proxy_file(config_dir).read_text()
    assert "[tools.nope]\nhidden = true" in text
    assert "[tools.create_issue.args.nope]\nhidden = true" in text


def test_edits_need_an_existing_proxy(config_dir: ConfigDir) -> None:
    scanned(config_dir)

    result = run_cli(config_dir, "tool", "hide", "issues/review", "close_issue")

    assert result.exit_code == 1
    assert "issues/review" in result.output


def test_trim_shows_the_original_and_replaces_it_with_the_edited_text(
    config_dir: ConfigDir, tmp_path: Path
) -> None:
    scanned(config_dir)
    editor = tmp_path / "editor.py"
    editor.write_text(
        "import pathlib, sys\n"
        "path = pathlib.Path(sys.argv[1])\n"
        "assert path.read_text().strip() == 'Create an issue in a repository.'\n"
        "path.write_text('Create an issue.\\n')\n"
    )

    result = run_cli(
        config_dir,
        "tool",
        "trim",
        "issues/default",
        "create_issue",
        env={"EDITOR": f"{sys.executable} {editor}"},
    )

    assert result.exit_code == 0, result.output
    assert "Create an issue in a repository." in result.output
    assert 'description = "Create an issue."' in proxy_file(config_dir).read_text()


def test_trim_leaves_the_file_alone_when_nothing_changed(
    config_dir: ConfigDir, tmp_path: Path
) -> None:
    scanned(config_dir)
    before = proxy_file(config_dir).read_text()
    editor = tmp_path / "editor.py"
    editor.write_text("pass\n")

    result = run_cli(
        config_dir,
        "tool",
        "trim",
        "issues/default",
        "create_issue",
        env={"EDITOR": f"{sys.executable} {editor}"},
    )

    assert result.exit_code == 0, result.output
    assert "unchanged" in result.output.lower()
    assert proxy_file(config_dir).read_text() == before


def test_trim_needs_the_tool_in_the_catalog(config_dir: ConfigDir) -> None:
    scanned(config_dir)

    result = run_cli(config_dir, "tool", "trim", "issues/default", "nope")

    assert result.exit_code == 1
    assert "nope" in result.output


def test_cap_writes_the_tool_cap_overrides_it_is_given(config_dir: ConfigDir) -> None:
    scanned(config_dir)

    result = run_cli(
        config_dir,
        "tool",
        "cap",
        "issues/default",
        "create_issue",
        "--description",
        "80",
        "--name",
        "20",
        "--argument-description",
        "30",
        "--output",
        "500",
    )

    assert result.exit_code == 0, result.output
    text = proxy_file(config_dir).read_text()
    assert "[tools.create_issue.caps]\n" in text
    assert "description = 80\n" in text
    assert "name = 20\n" in text
    assert "argument_description = 30\n" in text
    assert "output = 500\n" in text


def test_cap_needs_at_least_one_value(config_dir: ConfigDir) -> None:
    scanned(config_dir)

    result = run_cli(config_dir, "tool", "cap", "issues/default", "create_issue")

    assert result.exit_code == 1
    assert "at least one" in result.output
