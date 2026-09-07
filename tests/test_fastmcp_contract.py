"""What mcpshape relies on from FastMCP, pinned so an upgrade fails here first (ADR 0001).

These tests talk to FastMCP directly, on purpose. Everything else goes through the seam.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any, cast

import fastmcp
import mcp_types
import pytest
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StdioTransport, StreamableHttpTransport
from fastmcp.exceptions import ToolError
from fastmcp.prompts import Message, PromptResult
from fastmcp.resources import ResourceContent, ResourceResult
from fastmcp.server import create_proxy
from fastmcp.server.providers.proxy import (
    ProxyClient,
    ProxyPrompt,
    ProxyResource,
    ProxyTemplate,
    ProxyTool,
)
from fastmcp.tools import FunctionTool, Tool
from fastmcp.tools.base import ToolResult
from mcp_types import TextContent, TextResourceContents
from pydantic import AnyUrl, PrivateAttr
from starlette.applications import Starlette
from starlette.routing import Mount

from tests.support import child_upstream
from tests.support.asgi import asgi_client_factory
from tests.support.seam import free_port, until

if TYPE_CHECKING:
    from fastmcp.server.context import Context

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


# --- what Hooks and Virtual Tools rely on ----------------------------------------------------


class Counted(FunctionTool):
    """A FunctionTool subclass with a private attribute and a ``run`` that wraps the body."""

    _seen: list[dict[str, Any]] = PrivateAttr(default_factory=list[dict[str, Any]])

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        self._seen.append(dict(arguments))
        return await super().run(arguments)

    def convert_result(self, raw_value: Any) -> ToolResult:  # noqa: ANN401  # whatever the body returned
        if isinstance(raw_value, tuple):
            return ToolResult(content=[TextContent(type="text", text="converted")])
        return super().convert_result(raw_value)

    @property
    def seen(self) -> list[dict[str, Any]]:
        return self._seen


async def test_a_function_tool_subclass_takes_its_schema_from_a_plain_function() -> None:
    """How a Virtual Tool is built: the user's function, its docstring, and its signature."""

    def close_all(ids: list[int], note: str = ""):  # noqa: ANN202  # no output schema
        """Close several issues."""
        return note, len(ids)

    built = Counted.from_function(close_all, run_in_thread=False)
    assert isinstance(built, Counted)
    assert built.name == "close_all"
    assert built.description == "Close several issues."
    assert built.parameters["properties"]["ids"] == {"type": "array", "items": {"type": "integer"}}
    assert built.parameters["required"] == ["ids"]

    server = FastMCP("virtual")
    server.add_tool(built)
    async with Client(server) as client:
        listed = (await client.list_tools())[0]
        assert listed.input_schema == built.parameters
        result = await client.call_tool("close_all", {"ids": [1, 2]})
    content = result.content[0]
    assert isinstance(content, TextContent)
    assert content.text == "converted", "run() routes the body's return through convert_result()"
    assert built.seen == [{"ids": [1, 2]}]


async def test_a_function_tool_advertises_a_wrapped_output_schema_the_client_checks() -> None:
    """A FastMCP tool returning one value wraps it as ``result``, and the Client insists on it.

    That mark is what the adapter reads to rebuild structured content after a Hook replaced a
    result with plain text.
    """
    server = FastMCP("wrapped")

    def greet(name: str) -> str:
        return f"hi {name}"

    server.tool(greet)
    async with Client(server) as client:
        schema = (await client.list_tools())[0].output_schema
    assert schema is not None
    assert schema["x-fastmcp-wrap-result"] is True
    assert schema["properties"]["result"]["type"] == "string"

    plain = FastMCP("plain")
    plain.add_tool(
        ProxyTool.from_mcp_tool(  # pyright: ignore[reportUnknownMemberType]  # FastMCP's factory type is unparameterised
            lambda: Client(server),
            mcp_types.Tool(name="greet", input_schema={"type": "object"}, output_schema=schema),
        )
    )
    async with Client(plain) as client:
        assert (await client.call_tool("greet", {"name": "Ann"})).data == "hi Ann"

    class Unstructured(Tool):
        async def run(self, arguments: dict[str, Any]) -> ToolResult:  # noqa: ARG002
            return ToolResult(content=[TextContent(type="text", text="only text")])

    bare = FastMCP("bare")
    bare.add_tool(Unstructured(name="greet", parameters={"type": "object"}, output_schema=schema))
    async with Client(bare) as client:
        with pytest.raises(RuntimeError, match="did not return structured content"):
            await client.call_tool("greet", {"name": "Ann"})


async def test_a_proxy_template_reads_while_creating_and_a_cached_resource_serves_that() -> None:
    """The chain for a template read runs in ``create_resource``, and hands back a resource
    carrying the result the Hooks left as its cached content."""
    upstream = notes_server()
    base: ProxyClient[Any] = ProxyClient(upstream)
    async with Client(upstream) as client:
        template = (await client.list_resource_templates())[0]

    class Rewritten(ProxyTemplate):
        async def create_resource(
            self, uri: str, params: dict[str, Any], context: Context | None = None
        ) -> ProxyResource:
            created = await super().create_resource(uri, params, context)
            read = await created.read()
            assert isinstance(read, ResourceResult)
            assert [item.content for item in read.contents] == [f"note {params['id']}"]
            return ProxyResource(
                client_factory=self._client_factory,  # pyright: ignore[reportUnknownMemberType]
                uri=uri,
                name=self.name,
                mime_type="text/plain",
                _cached_content=ResourceResult(contents=[ResourceContent("rewritten")]),
            )

    server = FastMCP("proxy")
    server.add_template(Rewritten.from_mcp_template(base.new, template))  # pyright: ignore[reportUnknownMemberType]
    async with Client(server) as client:
        contents = (await client.read_resource("notes://7"))[0]
    assert isinstance(contents, TextResourceContents)
    assert contents.text == "rewritten"


async def test_a_proxy_prompt_renders_to_messages_that_can_be_rebuilt() -> None:
    upstream = notes_server()
    base: ProxyClient[Any] = ProxyClient(upstream)
    async with Client(upstream) as client:
        prompt = (await client.list_prompts())[0]

    class Rewritten(ProxyPrompt):
        async def render(self, arguments: dict[str, Any] | None = None) -> PromptResult:
            rendered = await super().render(arguments or {})
            assert isinstance(rendered, PromptResult)
            first = rendered.messages[0]
            assert isinstance(first.content, TextContent)
            return PromptResult(
                [
                    Message(first.content.text + "!", role=first.role),
                    Message("Sure.", role="assistant"),
                ]
            )

    server = FastMCP("proxy")
    server.add_prompt(Rewritten.from_mcp_prompt(base.new, prompt))  # pyright: ignore[reportUnknownMemberType]
    async with Client(server) as client:
        messages = (await client.get_prompt("greeting", {"name": "Ann"})).messages
    texts = [(m.role, m.content.text) for m in messages if isinstance(m.content, TextContent)]
    assert texts == [("user", "Hello Ann!"), ("assistant", "Sure.")]


async def test_a_borrowed_client_reports_a_failed_or_unknown_call_as_an_error_result() -> None:
    """What the ``upstream`` handle sees: an error result carrying the message, either way."""
    server = FastMCP("failing")

    def explode() -> str:
        msg = "boom"
        raise ValueError(msg)

    server.tool(explode)
    client: ProxyClient[Any] = ProxyClient(server)
    async with client:
        async with client:
            failed = await client.call_tool_mcp("explode", {})
            assert failed.is_error
            content = failed.content[0]
            assert isinstance(content, TextContent)
            assert "boom" in content.text
            missing = await client.call_tool_mcp("no_such_tool", {})
            assert missing.is_error
            content = missing.content[0]
            assert isinstance(content, TextContent)
            assert "no_such_tool" in content.text
        assert client.is_connected()


# --- what real Upstream transports rely on ---------------------------------------------------


async def test_a_stdio_transport_ends_its_child_process_only_when_keep_alive_is_off() -> None:
    """Why mcpshape spawns every stdio Upstream with ``keep_alive=False``.

    The Upstream's own lifecycle decides when the connection goes; a kept-alive child would
    outlive the connection that was let go, and nothing would ever come back for it.
    """
    transport = StdioTransport(
        command=child_upstream.command(),
        args=child_upstream.args(),
        env=child_upstream.env(),
        keep_alive=False,
    )
    async with Client(transport) as client:
        gone = (await client.call_tool("pid", {})).data
    assert isinstance(gone, int)
    await until(lambda: not child_upstream.alive(gone), "the child going with the connection")

    kept = StdioTransport(
        command=child_upstream.command(),
        args=child_upstream.args(),
        env=child_upstream.env(),
        keep_alive=True,
    )
    async with Client(kept) as client:
        staying = (await client.call_tool("pid", {})).data
    assert isinstance(staying, int)
    assert child_upstream.alive(staying), "keep_alive leaves the child running"
    await kept.disconnect()
    await until(lambda: not child_upstream.alive(staying), "the child a disconnect ends")


async def test_a_stdio_child_gets_a_safe_slice_of_the_environment_plus_what_it_is_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What an Upstream's ``env`` block has to carry, and what it does not.

    The MCP SDK passes on HOME, LOGNAME, PATH, SHELL, TERM and USER, and merges the transport's
    ``env`` on top. Nothing else of the Daemon's environment reaches the child.
    """
    monkeypatch.setenv("MCPSHAPE_CONTRACT_UNPASSED", "not for the child")
    transport = StdioTransport(
        command=child_upstream.command(),
        args=child_upstream.args(),
        env=child_upstream.env(CARRIED="given to the child"),
        keep_alive=False,
    )

    async with Client(transport) as client:
        assert (
            await client.call_tool("env_value", {"name": "CARRIED"})
        ).data == "given to the child"
        assert (await client.call_tool("env_value", {"name": "PATH"})).data == os.environ["PATH"]
        assert (
            await client.call_tool("env_value", {"name": "MCPSHAPE_CONTRACT_UNPASSED"})
        ).data == ""


async def test_a_client_raises_when_nothing_serves_the_url() -> None:
    """A connect that finds nothing raises, which is what a scan and a connect turn into a
    warning and an unavailable Upstream."""
    with pytest.raises(RuntimeError, match="failed to connect"):
        async with Client(StreamableHttpTransport(f"http://127.0.0.1:{free_port()}/mcp")):
            pass


async def test_closing_a_client_that_never_connected_is_harmless() -> None:
    """The cleanup ``_Link.open`` registers before it connects, so a cancelled connect leaves
    nothing behind."""
    await Client(StreamableHttpTransport("http://127.0.0.1:1/mcp")).close()
