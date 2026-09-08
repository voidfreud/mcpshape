"""Adding, listing, showing, and removing Upstreams and Proxies with the Daemon down."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.support.seam import run_cli

if TYPE_CHECKING:
    from tests.support.seam import ConfigDir


def test_add_stdio_upstream_writes_upstream_file_and_default_proxy(config_dir: ConfigDir) -> None:
    result = run_cli(
        config_dir, "add", "github", "--stdio", "npx -y @modelcontextprotocol/server-github"
    )

    assert result.exit_code == 0, result.output
    upstream_dir = config_dir.path / "upstreams" / "github"
    upstream = (upstream_dir / "upstream.toml").read_text()
    assert upstream.startswith("#:schema ")
    assert "version = 1" in upstream
    assert 'transport = "stdio"' in upstream
    assert 'command = "npx"' in upstream
    assert 'args = ["-y", "@modelcontextprotocol/server-github"]' in upstream
    proxy = (upstream_dir / "default.toml").read_text()
    assert proxy.startswith("#:schema ")
    assert "version = 1" in proxy


def test_add_url_upstream_writes_http_transport(config_dir: ConfigDir) -> None:
    result = run_cli(config_dir, "add", "docs", "--url", "https://docs.example/mcp")

    assert result.exit_code == 0, result.output
    upstream = (config_dir.path / "upstreams" / "docs" / "upstream.toml").read_text()
    assert 'transport = "http"' in upstream
    assert 'url = "https://docs.example/mcp"' in upstream


def test_add_url_with_sse_writes_sse_transport(config_dir: ConfigDir) -> None:
    result = run_cli(config_dir, "add", "legacy", "--url", "https://old.example/sse", "--sse")

    assert result.exit_code == 0, result.output
    upstream = (config_dir.path / "upstreams" / "legacy" / "upstream.toml").read_text()
    assert 'transport = "sse"' in upstream


@pytest.mark.parametrize(
    "args",
    [
        ("add", "x"),
        ("add", "x", "--stdio", "cmd", "--url", "https://x"),
        ("add", "x", "--stdio", "cmd", "--sse"),
    ],
)
def test_add_needs_exactly_one_transport(config_dir: ConfigDir, args: tuple[str, ...]) -> None:
    result = run_cli(config_dir, *args)

    assert result.exit_code == 1
    assert not (config_dir.path / "upstreams").exists()


@pytest.mark.parametrize("name", ["mcp", "api", "Github", "git hub", "-git", "git_hub", ""])
def test_add_refuses_names_that_are_not_slugs_or_are_reserved(
    config_dir: ConfigDir, name: str
) -> None:
    result = run_cli(config_dir, "add", name, "--stdio", "cmd")

    assert result.exit_code != 0
    assert not (config_dir.path / "upstreams").exists()


def test_add_refuses_an_existing_upstream(config_dir: ConfigDir) -> None:
    run_cli(config_dir, "add", "github", "--stdio", "cmd")
    result = run_cli(config_dir, "add", "github", "--url", "https://x")

    assert result.exit_code == 1
    assert "already exists" in result.output


def test_add_stdio_with_env_writes_env_block_and_notes_the_literal(
    config_dir: ConfigDir,
) -> None:
    result = run_cli(
        config_dir,
        "add",
        "github",
        "--stdio",
        "cmd",
        "--env",
        "A=1",
        "--env",
        "TOKEN=${GH}",
    )

    assert result.exit_code == 0, result.output
    upstream = (config_dir.path / "upstreams" / "github" / "upstream.toml").read_text()
    assert "[env]" in upstream
    assert 'A = "1"' in upstream
    assert 'TOKEN = "${GH}"' in upstream
    assert "secrets.toml" in result.output
    assert "1" in upstream  # sanity: the literal really was written


def test_add_stdio_with_only_reference_env_prints_no_secret_note(
    config_dir: ConfigDir,
) -> None:
    result = run_cli(config_dir, "add", "github", "--stdio", "cmd", "--env", "TOKEN=${GH}")

    assert result.exit_code == 0, result.output
    assert "secrets.toml" not in result.output


def test_env_with_url_fails(config_dir: ConfigDir) -> None:
    result = run_cli(config_dir, "add", "docs", "--url", "https://docs.example/mcp", "--env", "A=1")

    assert result.exit_code == 1
    assert "--env" in result.output
    assert not (config_dir.path / "upstreams").exists()


@pytest.mark.parametrize("item", ["NOEQUALS", "=value"])
def test_env_malformed_item_fails_naming_it(config_dir: ConfigDir, item: str) -> None:
    result = run_cli(config_dir, "add", "github", "--stdio", "cmd", "--env", item)

    assert result.exit_code == 1
    assert item in result.output


def test_upstream_env_sets_a_key_on_an_existing_upstream_preserving_comments(
    config_dir: ConfigDir,
) -> None:
    run_cli(config_dir, "add", "github", "--stdio", "cmd")
    path = config_dir.path / "upstreams" / "github" / "upstream.toml"
    path.write_text(path.read_text().replace("command = ", "# keep me\ncommand = "))

    result = run_cli(config_dir, "upstream", "env", "github", "B=2")

    assert result.exit_code == 0, result.output
    written = path.read_text()
    assert "# keep me" in written
    assert "[env]" in written
    assert 'B = "2"' in written

    result = run_cli(config_dir, "upstream", "env", "github", "B=3")

    assert result.exit_code == 0, result.output
    written = path.read_text()
    assert "# keep me" in written
    assert 'B = "3"' in written
    assert 'B = "2"' not in written


def test_upstream_env_refuses_a_non_stdio_upstream(config_dir: ConfigDir) -> None:
    run_cli(config_dir, "add", "docs", "--url", "https://docs.example/mcp")

    result = run_cli(config_dir, "upstream", "env", "docs", "A=1")

    assert result.exit_code == 1
    assert "docs" in result.output


def test_upstream_add_env_behaves_like_root_add(config_dir: ConfigDir) -> None:
    result = run_cli(config_dir, "upstream", "add", "github", "--stdio", "cmd", "--env", "A=1")

    assert result.exit_code == 0, result.output
    upstream = (config_dir.path / "upstreams" / "github" / "upstream.toml").read_text()
    assert 'A = "1"' in upstream


def test_ls_lists_upstreams_with_proxies_and_urls(config_dir: ConfigDir) -> None:
    run_cli(config_dir, "add", "github", "--stdio", "npx -y server-github")
    run_cli(config_dir, "proxy", "new", "github/review")

    result = run_cli(config_dir, "ls")

    assert result.exit_code == 0, result.output
    assert "github" in result.output
    assert "npx -y server-github" in result.output
    assert "http://127.0.0.1:8321/github/mcp" in result.output
    assert "http://127.0.0.1:8321/github/review/mcp" in result.output


def test_ls_with_nothing_added_points_at_add(config_dir: ConfigDir) -> None:
    result = run_cli(config_dir, "ls")

    assert result.exit_code == 0
    assert "mcpshape add" in result.output


def test_upstream_show_prints_the_file(config_dir: ConfigDir) -> None:
    run_cli(config_dir, "add", "github", "--stdio", "cmd")

    result = run_cli(config_dir, "upstream", "show", "github")

    assert result.exit_code == 0, result.output
    assert "upstream.toml" in result.output
    assert 'transport = "stdio"' in result.output
    assert "default" in result.output


def test_upstream_rm_removes_the_folder(config_dir: ConfigDir) -> None:
    run_cli(config_dir, "add", "github", "--stdio", "cmd")

    result = run_cli(config_dir, "upstream", "rm", "github", "--yes")

    assert result.exit_code == 0, result.output
    assert not (config_dir.path / "upstreams" / "github").exists()


def test_upstream_rm_unknown_fails(config_dir: ConfigDir) -> None:
    result = run_cli(config_dir, "upstream", "rm", "nope", "--yes")

    assert result.exit_code == 1
    assert "nope" in result.output


def test_ls_reports_a_broken_upstream_file(config_dir: ConfigDir) -> None:
    run_cli(config_dir, "add", "github", "--stdio", "cmd")
    (config_dir.path / "upstreams" / "github" / "upstream.toml").write_text("version = 1\n")

    result = run_cli(config_dir, "ls")

    assert result.exit_code == 1
    assert "upstream.toml" in result.output
    assert "transport" in result.output
