"""What mcpshape relies on from FastMCP, pinned so an upgrade fails here first (ADR 0001).

These tests talk to FastMCP directly, on purpose. Everything else goes through the seam.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import fastmcp
import mcp_types
import pytest
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import ToolError
from fastmcp.server import create_proxy
from fastmcp.server.providers.proxy import (
    ProxyClient,
    ProxyPrompt,
    ProxyResource,
    ProxyTemplate,
    ProxyTool,
)
from mcp_types import TextContent, TextResourceContents
from pydantic import AnyUrl, PrivateAttr
from starlette.applications import Starlette
from starlette.routing import Mount

from tests.support.asgi import asgi_client_factory

if TYPE_CHECKING:
    from fastmcp.server.context import Context
    from fastmcp.tools.base import ToolResult

UNAVAILABLE = "the Upstream is not reachable right now"


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


async def test_create_proxy_forwards_to_a_streamable_http_backend() -> None:
    """What the stdio shim is: a proxy whose backend is a Proxy reached over Streamable HTTP."""
    served = echo_server().http_app(path="/mcp")
    bridge = create_proxy(
        StreamableHttpTransport(
            "http://contract/mcp",
            httpx_client_factory=asgi_client_factory(served, "http://contract"),
        ),
        name="bridge",
    )

    async with served.router.lifespan_context(served), Client(bridge) as client:
        assert [tool.name for tool in await client.list_tools()] == ["echo"]
        assert (await client.call_tool("echo", {"text": "hi"})).data == "hi"


async def test_http_app_serves_mcp_under_a_starlette_mount_with_its_own_lifespan() -> None:
    proxy_app = create_proxy(echo_server(), name="proxy").http_app(path="/mcp")
    root = Starlette(routes=[Mount("/nested", app=proxy_app)])
    transport = StreamableHttpTransport(
        "http://contract/nested/mcp",
        httpx_client_factory=asgi_client_factory(root, "http://contract"),
    )

    async with proxy_app.router.lifespan_context(proxy_app), Client(transport) as client:
        assert [tool.name for tool in await client.list_tools()] == ["echo"]


# --- what Overrides rely on ------------------------------------------------------------------


def notes_server() -> FastMCP[Any]:
    server = FastMCP("notes", instructions="Keep notes short.")

    def add(a: int, b: int = 1) -> int:
        return a + b

    def note(id: str) -> str:  # noqa: A002
        return f"note {id}"

    def greeting(name: str) -> str:
        return f"Hello {name}"

    server.tool(add)
    server.resource("notes://all")(lambda: "all notes")
    server.resource("notes://{id}")(note)
    server.prompt(greeting)
    return server


class Marked(ProxyTool):
    """A ProxyTool subclass with a private attribute, as the adapter's curated tool has."""

    _mark: str = PrivateAttr(default="")

    def set_mark(self, mark: str) -> None:
        self._mark = mark

    @property
    def mark(self) -> str:
        return self._mark

    async def run(self, arguments: dict[str, Any], context: Context | None = None) -> ToolResult:
        return await super().run({"a": arguments["x"], "b": 10}, context)


async def test_proxy_components_copied_under_a_new_name_still_reach_the_backend() -> None:
    upstream = notes_server()
    base: ProxyClient[Any] = ProxyClient(upstream)
    async with Client(upstream) as client:
        tool = (await client.list_tools())[0]
        resource = (await client.list_resources())[0]
        template = (await client.list_resource_templates())[0]
        prompt = (await client.list_prompts())[0]
    marked = cast("Marked", Marked.from_mcp_tool(base.new, tool))  # pyright: ignore[reportUnknownMemberType]
    marked.set_mark("kept")
    renamed = marked.model_copy(update={"name": "plus"})
    assert isinstance(renamed, Marked)
    assert renamed.mark == "kept"

    proxy = FastMCP("proxy")
    proxy.add_tool(renamed)
    proxy.add_resource(
        ProxyResource.from_mcp_resource(base.new, resource).model_copy(  # pyright: ignore[reportUnknownMemberType]
            update={"uri": AnyUrl("notes://everything")}
        )
    )
    proxy.add_template(
        ProxyTemplate.from_mcp_template(base.new, template).model_copy(  # pyright: ignore[reportUnknownMemberType]
            update={"uri_template": "note://{id}"}
        )
    )
    proxy.add_prompt(
        ProxyPrompt.from_mcp_prompt(base.new, prompt).model_copy(update={"name": "hello"})  # pyright: ignore[reportUnknownMemberType]
    )

    async with Client(proxy) as client:
        assert [t.name for t in await client.list_tools()] == ["plus"]
        assert (await client.call_tool("plus", {"x": 5})).data == 15
        assert [str(r.uri) for r in await client.list_resources()] == ["notes://everything"]
        contents = (await client.read_resource("notes://everything"))[0]
        assert isinstance(contents, TextResourceContents)
        assert contents.text == "all notes"
        contents = (await client.read_resource("note://7"))[0]
        assert isinstance(contents, TextResourceContents)
        assert contents.text == "note 7"
        assert [p.name for p in await client.list_prompts()] == ["hello"]
        content = (await client.get_prompt("hello", {"name": "Ann"})).messages[0].content
        assert isinstance(content, TextContent)
        assert content.text == "Hello Ann"


async def test_a_server_announces_the_name_it_was_built_with_and_live_instructions() -> None:
    server = FastMCP("built-name", instructions="first")

    async with Client(server) as client:
        assert client.server_info is not None
        assert client.server_info.name == "built-name"
        assert client.instructions == "first"
    server.instructions = "second"
    async with Client(server) as client:
        assert client.instructions == "second"


async def test_a_proxy_client_serves_many_calls_on_one_session_and_answers_a_ping() -> None:
    """One Upstream connection is shared: mcpshape holds a client open and calls borrow it.

    ``ping`` is what a warm Upstream is checked with. A ``ProxyClient`` negotiates the legacy
    protocol era, which is the era that has ``ping``; a plain modern-era ``Client`` answers
    ``Method not found`` (see docs/clients.md).
    """
    client: ProxyClient[Any] = ProxyClient(echo_server())

    async with client:
        assert await client.ping()
        async with client:
            assert (await client.call_tool("echo", {"text": "hi"})).data == "hi"
        assert client.is_connected(), "leaving a borrowed context must keep the session"
    assert not client.is_connected()


async def test_a_client_factory_may_be_async_and_fail_a_call_with_a_readable_tool_error() -> None:
    """How a connect failure reaches the caller: an error result, not a broken session."""

    async def factory() -> Client[Any]:
        raise ToolError(UNAVAILABLE)

    server = FastMCP("proxy")
    server.add_tool(
        ProxyTool.from_mcp_tool(  # pyright: ignore[reportUnknownMemberType]  # FastMCP's factory type is unparameterised
            factory, mcp_types.Tool(name="echo", input_schema={"type": "object"})
        )
    )

    async with Client(server) as client:
        result = await client.call_tool("echo", {}, raise_on_error=False)
        assert result.is_error
        content = result.content[0]
        assert isinstance(content, TextContent)
        assert UNAVAILABLE in content.text

        with pytest.raises(ToolError, match=UNAVAILABLE):
            await client.call_tool("echo", {})
        assert [tool.name for tool in await client.list_tools()] == ["echo"]
