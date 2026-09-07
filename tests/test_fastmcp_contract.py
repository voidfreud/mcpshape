"""What mcpshape relies on from FastMCP, pinned so an upgrade fails here first (ADR 0001).

These tests talk to FastMCP directly, on purpose. Everything else goes through the seam.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import fastmcp
import httpx2
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server import create_proxy
from fastmcp.utilities.asgi_transport import StreamingASGITransport
from starlette.applications import Starlette
from starlette.routing import Mount

if TYPE_CHECKING:
    from mcp.shared._httpx_utils import McpHttpClientFactory


def echo_server() -> FastMCP[Any]:
    server = FastMCP("echo")

    def echo(text: str) -> str:
        return text

    server.tool(echo)
    return server


def test_pinned_to_fastmcp_major_4() -> None:
    assert fastmcp.__version__.split(".")[0] == "4"


async def test_create_proxy_forwards_tools_of_an_in_memory_server() -> None:
    proxy = create_proxy(echo_server(), name="proxy")

    async with Client(proxy) as client:
        assert [tool.name for tool in await client.list_tools()] == ["echo"]
        assert (await client.call_tool("echo", {"text": "hi"})).data == "hi"


def asgi_client_factory(app: Starlette) -> McpHttpClientFactory:
    def factory(
        headers: dict[str, str] | None = None,
        timeout: httpx2.Timeout | None = None,
        auth: httpx2.Auth | None = None,
        **kwargs: Any,  # noqa: ANN401  # FastMCP passes more than its factory type declares
    ) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(
            transport=StreamingASGITransport(app),
            base_url="http://contract",
            headers=headers,
            timeout=timeout,
            auth=auth,
            **kwargs,
        )

    return factory


async def test_http_app_serves_mcp_under_a_starlette_mount_with_its_own_lifespan() -> None:
    proxy_app = create_proxy(echo_server(), name="proxy").http_app(path="/mcp")
    root = Starlette(routes=[Mount("/nested", app=proxy_app)])
    transport = StreamableHttpTransport(
        "http://contract/nested/mcp", httpx_client_factory=asgi_client_factory(root)
    )

    async with proxy_app.router.lifespan_context(proxy_app), Client(transport) as client:
        assert [tool.name for tool in await client.list_tools()] == ["echo"]
