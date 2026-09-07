"""``upstream scan`` reads the Client configs on this machine and offers what it finds.

Every location a Profile records is anchored at ``HOME`` or the working directory, so these
tests point both at a temp directory and never read the real home directory.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from tests.support.seam import CliResult, run_cli, run_cli_with_env

if TYPE_CHECKING:
    from pathlib import Path

    from tests.support.seam import ConfigDir


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def servers(**entries: dict[str, Any]) -> str:
    return json.dumps({"mcpServers": entries})


def claude_code(home: Path, **entries: dict[str, Any]) -> None:
    """The user-scope location the Claude Code Profile records."""
    write(home / ".claude.json", servers(**entries))


def cursor(home: Path, **entries: dict[str, Any]) -> None:
    write(home / ".cursor" / "mcp.json", servers(**entries))


def codex(home: Path, name: str, url: str) -> None:
    write(
        home / ".codex" / "config.toml", f'model = "gpt-5"\n\n[mcp_servers.{name}]\nurl = "{url}"\n'
    )


def goose(home: Path) -> Path:
    """A YAML Profile location. mcpshape has no YAML dependency and must say so."""
    path = home / ".config" / "goose" / "config.yaml"
    write(path, "extensions:\n  memory:\n    uri: http://localhost:9000/mcp\n")
    return path


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A home directory and a working directory of our own, so no real config is read."""
    where = tmp_path / "home"
    project = tmp_path / "project"
    where.mkdir()
    project.mkdir()
    monkeypatch.chdir(project)
    return where


def scan(config_dir: ConfigDir, home: Path, *args: str, answers: str | None = None) -> CliResult:
    return run_cli_with_env(
        {
            "HOME": str(home),
            "MCPSHAPE_CONFIG_DIR": str(config_dir.path),
            "MCPSHAPE_STATE_DIR": str(config_dir.state),
        },
        "upstream",
        "scan",
        *args,
        answers=answers,
    )


def upstream_file(config_dir: ConfigDir, name: str) -> Path:
    return config_dir.path / "upstreams" / name / "upstream.toml"


# --- what it finds --------------------------------------------------------------------------------


def test_scan_lists_servers_from_every_client_config_location(
    config_dir: ConfigDir, home: Path
) -> None:
    claude_code(home, github={"command": "npx", "args": ["-y", "server-github"]})
    cursor(home, linear={"type": "http", "url": "https://mcp.linear.app/mcp"})
    codex(home, "docs", "https://docs.example/mcp")

    result = scan(config_dir, home, "--list")

    assert result.exit_code == 0, result.output
    assert "npx -y server-github" in result.output
    assert "https://mcp.linear.app/mcp" in result.output
    assert "https://docs.example/mcp" in result.output
    assert "Claude Code" in result.output
    assert "Cursor" in result.output
    assert "Codex CLI" in result.output
    assert not (config_dir.path / "upstreams").exists(), "--list adds nothing"


def test_scan_finds_a_project_config_in_the_working_directory(
    config_dir: ConfigDir, home: Path, tmp_path: Path
) -> None:
    write(tmp_path / "project" / ".mcp.json", servers(local={"command": "./serve.sh"}))

    result = scan(config_dir, home, "--list")

    assert result.exit_code == 0, result.output
    assert "./serve.sh" in result.output


def test_scan_leaves_a_project_file_in_the_home_directory_alone(
    config_dir: ConfigDir, home: Path
) -> None:
    """``.mcp.json`` is a per-project file: one in the home directory is not a project's."""
    write(home / ".mcp.json", servers(stray={"command": "stray-mcp"}))

    result = scan(config_dir, home, "--list")

    assert result.exit_code == 0, result.output
    assert "stray-mcp" not in result.output


def test_scan_skips_a_server_its_client_switched_off(
    config_dir: ConfigDir, home: Path, tmp_path: Path
) -> None:
    elsewhere = tmp_path / "elsewhere"
    write(
        elsewhere / "mcp.json",
        servers(off={"command": "off-mcp", "disabled": True}, on={"command": "on-mcp"}),
    )

    result = scan(config_dir, home, str(elsewhere), "--list")

    assert result.exit_code == 0, result.output
    assert "on-mcp" in result.output
    assert "off-mcp" not in result.output
    assert "switched off" in result.output


def test_scan_says_when_an_entry_sets_environment_variables(
    config_dir: ConfigDir, home: Path
) -> None:
    claude_code(
        home,
        github={
            "command": "npx",
            "args": ["server-github"],
            "env": {"GITHUB_TOKEN": "ghp-not-for-an-upstream-file"},
        },
    )

    result = scan(config_dir, home, "--list")

    assert result.exit_code == 0, result.output
    assert "GITHUB_TOKEN" in result.output
    assert "secrets.toml" in result.output
    assert "ghp-not-for-an-upstream-file" not in result.output


def test_scan_carries_over_the_names_of_an_env_block_as_references(
    config_dir: ConfigDir, home: Path
) -> None:
    """A secret stays in the Client's file: what is carried over is the name to resolve."""
    claude_code(
        home,
        github={
            "command": "npx",
            "args": ["server-github"],
            "env": {"GITHUB_TOKEN": "ghp-not-for-an-upstream-file"},
        },
    )

    result = scan(config_dir, home, "--yes")

    assert result.exit_code == 0, result.output
    written = upstream_file(config_dir, "github").read_text()
    assert 'GITHUB_TOKEN = "${GITHUB_TOKEN}"' in written
    assert "ghp-not-for-an-upstream-file" not in written
    checked = run_cli(config_dir, "doctor")
    assert checked.exit_code == 1, checked.output
    assert "GITHUB_TOKEN" in checked.output


def test_scan_reads_a_directory_named_on_the_command_line(
    config_dir: ConfigDir, home: Path, tmp_path: Path
) -> None:
    elsewhere = tmp_path / "elsewhere"
    write(elsewhere / "mcp.json", servers(notes={"command": "notes-mcp"}))

    result = scan(config_dir, home, str(elsewhere), "--list")

    assert result.exit_code == 0, result.output
    assert "notes-mcp" in result.output
    assert str(elsewhere / "mcp.json") in result.output


def test_scan_reads_an_sse_entry_as_an_sse_upstream(config_dir: ConfigDir, home: Path) -> None:
    claude_code(home, legacy={"type": "sse", "url": "https://old.example/sse"})

    result = scan(config_dir, home, "--yes")

    assert result.exit_code == 0, result.output
    assert 'transport = "sse"' in upstream_file(config_dir, "legacy").read_text()


def test_scan_names_a_yaml_config_it_cannot_read(config_dir: ConfigDir, home: Path) -> None:
    path = goose(home)

    result = scan(config_dir, home, "--list")

    assert result.exit_code == 0, result.output
    assert str(path) in result.output
    assert "YAML" in result.output


def test_scan_with_nothing_configured_says_there_is_nothing_to_add(
    config_dir: ConfigDir, home: Path
) -> None:
    result = scan(config_dir, home, "--list")

    assert result.exit_code == 0, result.output
    assert "No MCP server to add" in result.output


# --- what it adds ---------------------------------------------------------------------------------


def test_scan_with_yes_adds_every_server_it_found(config_dir: ConfigDir, home: Path) -> None:
    claude_code(home, github={"command": "npx", "args": ["-y", "server-github"]})
    cursor(home, linear={"type": "http", "url": "https://mcp.linear.app/mcp"})

    result = scan(config_dir, home, "--yes")

    assert result.exit_code == 0, result.output
    assert 'command = "npx"' in upstream_file(config_dir, "github").read_text()
    assert 'url = "https://mcp.linear.app/mcp"' in upstream_file(config_dir, "linear").read_text()
    listed = run_cli(config_dir, "ls")
    assert "github" in listed.output
    assert "linear" in listed.output
    assert "http://127.0.0.1:8321/github/mcp" in listed.output, "each got its default Proxy"


def test_scan_asks_about_each_server_and_adds_only_the_ones_confirmed(
    config_dir: ConfigDir, home: Path
) -> None:
    claude_code(
        home,
        github={"command": "npx", "args": ["server-github"]},
        linear={"command": "linear-mcp"},
    )

    result = scan(config_dir, home, answers="n\ny\n")

    assert result.exit_code == 0, result.output
    assert not upstream_file(config_dir, "github").exists()
    assert upstream_file(config_dir, "linear").exists()
    assert "Added 1 Upstream." in result.output


def test_scan_derives_an_upstream_name_and_says_what_it_did(
    config_dir: ConfigDir, home: Path
) -> None:
    claude_code(home, **{"GitHub MCP!": {"command": "npx"}})

    result = scan(config_dir, home, "--yes")

    assert result.exit_code == 0, result.output
    assert upstream_file(config_dir, "github-mcp").exists()
    assert "named after GitHub MCP!" in result.output


def test_scan_skips_a_server_that_is_already_an_upstream(config_dir: ConfigDir, home: Path) -> None:
    run_cli(config_dir, "add", "github", "--stdio", "cmd")
    claude_code(home, github={"command": "npx", "args": ["server-github"]})

    result = scan(config_dir, home, "--yes")

    assert result.exit_code == 0, result.output
    assert "already the Upstream github" in result.output
    assert 'command = "cmd"' in upstream_file(config_dir, "github").read_text(), "left alone"


def test_scan_skips_entries_that_are_already_mcpshape_proxies(
    config_dir: ConfigDir, home: Path
) -> None:
    claude_code(
        home,
        shimmed={"command": "mcpshape", "args": ["serve", "notes/default"]},
        pointed={"type": "http", "url": "http://localhost:8321/notes/mcp"},
    )

    result = scan(config_dir, home, "--yes")

    assert result.exit_code == 0, result.output
    assert result.output.count("is an mcpshape Proxy already") == 2
    assert not (config_dir.path / "upstreams").exists()


def test_scan_offers_a_server_found_in_two_client_configs_once(
    config_dir: ConfigDir, home: Path
) -> None:
    claude_code(home, notes={"command": "notes-mcp"})
    cursor(home, notes={"command": "notes-mcp"})

    result = scan(config_dir, home, "--yes")

    assert result.exit_code == 0, result.output
    assert "Added 1 Upstream." in result.output


def test_scan_has_help_with_an_example(config_dir: ConfigDir) -> None:
    result = run_cli(config_dir, "upstream", "scan", "--help")

    assert result.exit_code == 0, result.output
    assert "Example: mcpshape upstream scan" in result.output
    assert "--yes" in result.output
    assert "--list" in result.output
