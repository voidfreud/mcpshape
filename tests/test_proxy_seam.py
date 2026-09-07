"""One Upstream tool reaches a Client through a Proxy served by the Daemon."""

from __future__ import annotations

from typing import Any

import pytest
from fastmcp import FastMCP

from tests.support.seam import ConfigDir, running_daemon


def calculator() -> FastMCP[Any]:
    server = FastMCP("calculator")

    def add(a: int, b: int) -> int:
        """Add two integers."""
        return a + b

    server.tool(add)
    return server


@pytest.mark.parametrize("path", ["/calc/mcp", "/calc/default/mcp"])
async def test_client_lists_and_calls_upstream_tool_through_proxy(
    config_dir: ConfigDir, path: str
) -> None:
    config_dir.add_memory_upstream("calc", calculator())

    async with running_daemon(config_dir) as daemon, daemon.client(path) as client:
        tools = await client.list_tools()
        assert [tool.name for tool in tools] == ["add"]

        result = await client.call_tool("add", {"a": 2, "b": 3})
        assert result.data == 5
