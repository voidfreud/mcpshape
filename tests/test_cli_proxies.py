"""Creating, listing, showing, and removing Proxies with the Daemon down."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.support.seam import run_cli, running_daemon
from tests.test_proxy_seam import calculator

if TYPE_CHECKING:
    from tests.support.seam import ConfigDir


def test_proxy_new_writes_a_versioned_proxy_file(config_dir: ConfigDir) -> None:
    run_cli(config_dir, "add", "github", "--stdio", "cmd")

    result = run_cli(config_dir, "proxy", "new", "github/review")

    assert result.exit_code == 0, result.output
    proxy = (config_dir.path / "upstreams" / "github" / "review.toml").read_text()
    assert proxy.startswith("#:schema ")
    assert "version = 1" in proxy
    assert "http://127.0.0.1:8321/github/review/mcp" in result.output


@pytest.mark.parametrize("ref", ["github", "github/", "/review", "github/Review", "github/mcp"])
def test_proxy_new_refuses_bad_refs_and_names(config_dir: ConfigDir, ref: str) -> None:
    run_cli(config_dir, "add", "github", "--stdio", "cmd")

    result = run_cli(config_dir, "proxy", "new", ref)

    assert result.exit_code == 1
    assert sorted(p.name for p in (config_dir.path / "upstreams" / "github").iterdir()) == [
        "default.toml",
        "upstream.toml",
    ]


def test_proxy_new_needs_an_existing_upstream(config_dir: ConfigDir) -> None:
    result = run_cli(config_dir, "proxy", "new", "nope/review")

    assert result.exit_code == 1
    assert "nope" in result.output


def test_proxy_ls_and_show(config_dir: ConfigDir) -> None:
    run_cli(config_dir, "add", "github", "--stdio", "cmd")
    run_cli(config_dir, "proxy", "new", "github/review")

    listed = run_cli(config_dir, "proxy", "ls", "github")
    assert listed.exit_code == 0, listed.output
    assert "github/default" in listed.output
    assert "github/review" in listed.output

    shown = run_cli(config_dir, "proxy", "show", "github/review")
    assert shown.exit_code == 0, shown.output
    assert "review.toml" in shown.output
    assert "version = 1" in shown.output


def test_proxy_rm_removes_the_file_but_never_default(config_dir: ConfigDir) -> None:
    run_cli(config_dir, "add", "github", "--stdio", "cmd")
    run_cli(config_dir, "proxy", "new", "github/review")

    removed = run_cli(config_dir, "proxy", "rm", "github/review", "--yes")
    assert removed.exit_code == 0, removed.output
    assert not (config_dir.path / "upstreams" / "github" / "review.toml").exists()

    refused = run_cli(config_dir, "proxy", "rm", "github/default", "--yes")
    assert refused.exit_code == 1
    assert (config_dir.path / "upstreams" / "github" / "default.toml").exists()


async def test_daemon_serves_a_proxy_created_by_the_cli(config_dir: ConfigDir) -> None:
    config_dir.add_memory_upstream("calc", calculator())
    assert run_cli(config_dir, "proxy", "new", "calc/short").exit_code == 0

    async with running_daemon(config_dir) as daemon, daemon.client("/calc/short/mcp") as client:
        assert [tool.name for tool in await client.list_tools()] == ["add"]
