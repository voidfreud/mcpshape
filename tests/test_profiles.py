"""Client Profiles drive install, export, and doctor. The core still knows no Client."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from fastmcp import FastMCP

from tests.support.seam import run_cli

if TYPE_CHECKING:
    from pathlib import Path

    from tests.support.seam import ConfigDir

LONG_TOOL = "search_the_whole_repository_for_something_that_matches_a_pattern"
"""Over Claude Code's budget once ``mcp__notes__`` is in front of it."""


def notes() -> FastMCP[Any]:
    """An Upstream whose tool name and argument name both strain a Client's rules."""
    server = FastMCP("notes", instructions="Keep notes short.")

    def add_note(text: str) -> str:
        """Add a note."""
        return text

    server.tool(add_note)
    return server


LONG_PROPERTY = "the_text_of_the_note_to_add_together_with_every_tag_it_should_carry"
"""Over the 64 characters Claude Code accepts in an input-schema property name."""


def long_property() -> FastMCP[Any]:
    """An Upstream whose tool takes an argument no prefixing Client would accept."""
    server = FastMCP("notes")

    def add_note(the_text_of_the_note_to_add_together_with_every_tag_it_should_carry: str) -> str:
        """Add a note."""
        return the_text_of_the_note_to_add_together_with_every_tag_it_should_carry

    server.tool(add_note)
    return server


def wordy() -> FastMCP[Any]:
    """An Upstream with a tool name no prefixing Client has room for."""
    server = FastMCP("wordy")

    def tool(query: str) -> str:
        """Search."""
        return query

    server.tool(tool, name=LONG_TOOL)
    return server


def synced(config_dir: ConfigDir, name: str, server: FastMCP[Any]) -> None:
    """Register ``server`` as an Upstream and store its Catalog, as a user would."""
    config_dir.add_memory_upstream(name, server)
    result = run_cli(config_dir, "upstream", "sync", name)
    assert result.exit_code == 0, result.output


def entries(path: Path, container: str = "mcpServers") -> dict[str, Any]:
    document: dict[str, Any] = json.loads(path.read_text())
    return document[container]


# --- install ------------------------------------------------------------------------------------


def test_install_writes_the_http_entry_the_client_expects(
    config_dir: ConfigDir, tmp_path: Path
) -> None:
    run_cli(config_dir, "add", "github", "--stdio", "cmd")
    target = tmp_path / "client" / ".mcp.json"

    result = run_cli(
        config_dir,
        "proxy",
        "install",
        "github/default",
        "--to",
        "claude-code",
        "--config",
        str(target),
    )

    assert result.exit_code == 0, result.output
    assert entries(target) == {
        "github": {"type": "http", "url": "http://127.0.0.1:8321/github/mcp"}
    }


def test_install_merges_into_a_client_config_without_touching_other_entries(
    config_dir: ConfigDir, tmp_path: Path
) -> None:
    run_cli(config_dir, "add", "github", "--stdio", "cmd")
    target = tmp_path / "mcp.json"
    target.write_text(json.dumps({"mcpServers": {"linear": {"command": "linear-mcp"}}}))

    result = run_cli(
        config_dir, "proxy", "install", "github/default", "--to", "cursor", "--config", str(target)
    )

    assert result.exit_code == 0, result.output
    assert entries(target)["linear"] == {"command": "linear-mcp"}
    assert entries(target)["github"]["url"] == "http://127.0.0.1:8321/github/mcp"
    assert "reconnect" in result.output, "only Claude Code refreshes on tools/list_changed"


def test_install_writes_the_shim_entry_for_a_stdio_only_client(
    config_dir: ConfigDir, tmp_path: Path
) -> None:
    run_cli(config_dir, "add", "github", "--stdio", "cmd")
    target = tmp_path / "settings.json"

    result = run_cli(
        config_dir, "proxy", "install", "github/default", "--to", "zed", "--config", str(target)
    )

    assert result.exit_code == 0, result.output
    assert entries(target, "context_servers") == {
        "github": {"command": "mcpshape", "args": ["serve", "github/default"]}
    }


def test_install_writes_a_toml_client_config_under_its_own_key(
    config_dir: ConfigDir, tmp_path: Path
) -> None:
    run_cli(config_dir, "add", "github", "--stdio", "cmd")
    target = tmp_path / "config.toml"
    target.write_text('model = "gpt-5"\n\n[mcp_servers.linear]\ncommand = "linear-mcp"\n')

    result = run_cli(
        config_dir,
        "proxy",
        "install",
        "github/default",
        "--to",
        "codex-cli",
        "--config",
        str(target),
    )

    assert result.exit_code == 0, result.output
    written = target.read_text()
    assert 'model = "gpt-5"' in written
    assert "[mcp_servers.linear]" in written
    assert "[mcp_servers.github]" in written
    assert 'url = "http://127.0.0.1:8321/github/mcp"' in written


def test_install_names_a_non_default_proxy_after_its_upstream_and_proxy(
    config_dir: ConfigDir, tmp_path: Path
) -> None:
    run_cli(config_dir, "add", "github", "--stdio", "cmd")
    run_cli(config_dir, "proxy", "new", "github/review")
    target = tmp_path / ".mcp.json"

    run_cli(
        config_dir,
        "proxy",
        "install",
        "github/review",
        "--to",
        "claude-code",
        "--config",
        str(target),
    )

    assert list(entries(target)) == ["github-review"]
    assert entries(target)["github-review"]["url"] == "http://127.0.0.1:8321/github/review/mcp"


def test_install_warns_when_an_exposed_tool_name_exceeds_the_clients_budget(
    config_dir: ConfigDir, tmp_path: Path
) -> None:
    synced(config_dir, "wordy", wordy())
    target = tmp_path / ".mcp.json"

    result = run_cli(
        config_dir,
        "proxy",
        "install",
        "wordy/default",
        "--to",
        "claude-code",
        "--config",
        str(target),
    )

    assert result.exit_code == 0, result.output
    assert f"mcp__wordy__{LONG_TOOL}" in result.output
    assert "64" in result.output
    assert entries(target)["wordy"], "a warning does not stop the install"


def test_install_says_a_sync_is_needed_before_names_can_be_checked(
    config_dir: ConfigDir, tmp_path: Path
) -> None:
    run_cli(config_dir, "add", "github", "--stdio", "cmd")

    result = run_cli(
        config_dir,
        "proxy",
        "install",
        "github/default",
        "--to",
        "claude-code",
        "--config",
        str(tmp_path / ".mcp.json"),
    )

    assert result.exit_code == 0, result.output
    assert "No stored Catalog for github" in result.output
    assert "mcpshape upstream sync github" in result.output


def test_install_takes_out_the_clients_own_entry_and_prints_it_back(
    config_dir: ConfigDir, tmp_path: Path
) -> None:
    run_cli(config_dir, "add", "github", "--stdio", "cmd")
    target = tmp_path / ".mcp.json"
    target.write_text(json.dumps({"mcpServers": {"github-raw": {"command": "github-mcp"}}}))

    result = run_cli(
        config_dir,
        "proxy",
        "install",
        "github/default",
        "--to",
        "claude-code",
        "--config",
        str(target),
        "--disable",
        "github-raw",
    )

    assert result.exit_code == 0, result.output
    assert list(entries(target)) == ["github"]
    assert "github-mcp" in result.output, "the removed entry is printed so it can be pasted back"


def test_install_sets_the_off_switch_a_client_documents_instead_of_removing(
    config_dir: ConfigDir, tmp_path: Path
) -> None:
    run_cli(config_dir, "add", "github", "--stdio", "cmd")
    target = tmp_path / "cline_mcp_settings.json"
    target.write_text(json.dumps({"mcpServers": {"github-raw": {"command": "github-mcp"}}}))

    result = run_cli(
        config_dir,
        "proxy",
        "install",
        "github/default",
        "--to",
        "cline",
        "--config",
        str(target),
        "--disable",
        "github-raw",
    )

    assert result.exit_code == 0, result.output
    assert entries(target)["github-raw"] == {"command": "github-mcp", "disabled": True}


def test_install_prints_a_snippet_rather_than_rewriting_a_yaml_client_config(
    config_dir: ConfigDir,
) -> None:
    run_cli(config_dir, "add", "github", "--stdio", "cmd")

    result = run_cli(config_dir, "proxy", "install", "github/default", "--to", "goose")

    assert result.exit_code == 0, result.output
    assert "extensions:" in result.output
    assert '"http://127.0.0.1:8321/github/mcp"' in result.output


def test_install_refuses_a_client_that_cannot_reach_a_local_proxy(config_dir: ConfigDir) -> None:
    run_cli(config_dir, "add", "github", "--stdio", "cmd")

    result = run_cli(config_dir, "proxy", "install", "github/default", "--to", "chatgpt")

    assert result.exit_code == 1
    assert "remote HTTPS" in result.output


def test_install_names_the_clients_it_knows_when_asked_for_one_it_does_not(
    config_dir: ConfigDir,
) -> None:
    run_cli(config_dir, "add", "github", "--stdio", "cmd")

    result = run_cli(config_dir, "proxy", "install", "github/default", "--to", "emacs")

    assert result.exit_code == 1
    assert "claude-code" in result.output


# --- export -------------------------------------------------------------------------------------


def test_export_prints_parseable_mcp_servers_json_on_stdout(config_dir: ConfigDir) -> None:
    run_cli(config_dir, "add", "github", "--stdio", "cmd")

    result = run_cli(config_dir, "proxy", "export", "github/default")

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "mcpServers": {"github": {"type": "http", "url": "http://127.0.0.1:8321/github/mcp"}}
    }


def test_export_for_a_stdio_only_client_prints_the_shim_entry(config_dir: ConfigDir) -> None:
    run_cli(config_dir, "add", "github", "--stdio", "cmd")

    result = run_cli(config_dir, "proxy", "export", "github/default", "--for", "claude-desktop")

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["mcpServers"]["github"] == {
        "command": "mcpshape",
        "args": ["serve", "github/default"],
    }


def test_export_for_a_client_uses_that_clients_own_section(config_dir: ConfigDir) -> None:
    run_cli(config_dir, "add", "github", "--stdio", "cmd")

    result = run_cli(config_dir, "proxy", "export", "github/default", "--for", "vscode")

    assert result.exit_code == 0, result.output
    exported = json.loads(result.stdout)
    assert list(exported) == ["servers"]
    assert exported["servers"]["github"]["url"] == "http://127.0.0.1:8321/github/mcp"


# --- doctor -------------------------------------------------------------------------------------


def test_doctor_for_a_client_reports_a_property_name_that_client_would_refuse(
    config_dir: ConfigDir,
) -> None:
    synced(config_dir, "notes", long_property())

    result = run_cli(config_dir, "doctor", "--for", "claude-code")

    assert result.exit_code == 1, result.output
    assert LONG_PROPERTY in result.output
    assert "code.claude.com" in result.output, "the rule is reported with its source"


def test_doctor_for_claude_code_reminds_that_critical_text_goes_first(
    config_dir: ConfigDir,
) -> None:
    synced(config_dir, "notes", notes())

    result = run_cli(config_dir, "doctor", "--for", "claude-code")

    assert result.exit_code == 0, result.output
    assert "routing hint" in result.output
    assert "2KB" in result.output
    assert "1800" in result.output, "the Caps the Profile recommends are shown with their source"
    assert "checked 2026-09-07" in result.output


def test_doctor_for_a_client_reports_an_exposed_tool_name_over_its_budget(
    config_dir: ConfigDir,
) -> None:
    synced(config_dir, "wordy", wordy())

    result = run_cli(config_dir, "doctor", "--for", "claude-code")

    assert result.exit_code == 1, result.output
    assert f"mcp__wordy__{LONG_TOOL}" in result.output


def test_doctor_ignores_a_tool_the_proxy_hides(config_dir: ConfigDir) -> None:
    synced(config_dir, "wordy", wordy())
    proxy_file = config_dir.path / "upstreams" / "wordy" / "default.toml"
    proxy_file.write_text(f'version = 1\n\n[tools."{LONG_TOOL}"]\nhidden = true\n')

    result = run_cli(config_dir, "doctor", "--for", "claude-code")

    assert result.exit_code == 0, result.output
    assert LONG_TOOL not in result.output


def test_doctor_for_a_client_says_which_upstream_needs_a_sync_first(config_dir: ConfigDir) -> None:
    run_cli(config_dir, "add", "github", "--stdio", "sh")

    result = run_cli(config_dir, "doctor", "--for", "claude-code")

    assert result.exit_code == 0, result.output
    assert "mcpshape upstream sync github" in result.output


def test_doctor_without_a_client_says_nothing_about_client_rules(config_dir: ConfigDir) -> None:
    synced(config_dir, "wordy", wordy())

    result = run_cli(config_dir, "doctor")

    assert result.exit_code == 0, result.output
    assert "mcp__" not in result.output


def test_doctor_names_the_clients_it_knows_when_asked_for_one_it_does_not(
    config_dir: ConfigDir,
) -> None:
    result = run_cli(config_dir, "doctor", "--for", "emacs")

    assert result.exit_code == 1
    assert "claude-code" in result.output
