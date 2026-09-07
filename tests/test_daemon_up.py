"""The Daemon serves its Proxies on a real loopback port, as ``daemon up`` runs it."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastmcp import Client

from tests.support.seam import run_cli, serving_daemon
from tests.test_proxy_seam import calculator

if TYPE_CHECKING:
    from tests.support.seam import ConfigDir


async def test_a_client_reaches_a_proxy_over_tcp(config_dir: ConfigDir) -> None:
    config_dir.add_memory_upstream("calc", calculator())

    async with serving_daemon(config_dir) as url, Client(f"{url}/calc/mcp") as client:
        assert [tool.name for tool in await client.list_tools()] == ["add"]
        assert (await client.call_tool("add", {"a": 2, "b": 3})).data == 5


def test_daemon_up_has_help_with_an_example(config_dir: ConfigDir) -> None:
    result = run_cli(config_dir, "daemon", "up", "--help")

    assert result.exit_code == 0
    assert "Example: mcpshape daemon up" in result.output
