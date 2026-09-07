"""The FastMCP adapter: the only module that imports FastMCP (ADR 0001).

The rest of mcpshape sees three things: ``scan``, which turns an Upstream into a Catalog,
``proxy_app``, an ASGI app per Proxy that serves lists from what it is handed and forwards
calls to the Upstream under Catalog names through ``UpstreamConnection``, one Upstream's
shared client behind its lifecycle state machine, and ``run_shim``, the stdio shim's
other half.

FastMCP's proxy components keep the backend name when they are copied under a new one, which
is how a renamed tool, resource, or prompt still reaches its Catalog item. The server name is
fixed when the server is built (the MCP SDK caches its identity), so a Proxy whose exposed
server name changes is rebuilt by its owner; ``ProxyApp.name`` says what it was built with.

Every call, read, and get runs through the chain in ``mcpshape.hooks``: the Proxy's Hooks
around the forward to the Upstream, with mcpshape's own result types on the user's side and
FastMCP's on this side. Virtual Tools are FastMCP function tools built from the user's plain
functions, so the schema comes from the signature. The ``upstream`` handle user code reaches
is this module's ``_Handle`` over the Upstream's shared client.
"""

from __future__ import annotations

import base64
import contextlib
import importlib
import json
import logging
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, cast

import mcp_types
from fastmcp import Client, FastMCP
from fastmcp.client.transports import (
    SSETransport,
    StreamableHttpTransport,
)
from fastmcp.client.transports import StdioTransport as StdioClientTransport
from fastmcp.exceptions import FastMCPError, PromptError, ResourceError, ToolError
from fastmcp.prompts import Message, PromptResult
from fastmcp.resources import ResourceContent, ResourceResult
from fastmcp.server import create_proxy
from fastmcp.server.providers.base import Provider
from fastmcp.server.providers.proxy import (
    ProxyClient,
    ProxyPrompt,
    ProxyResource,
    ProxyTemplate,
    ProxyTool,
)
from fastmcp.tools import FunctionTool
from fastmcp.tools.base import ToolResult
from mcp.shared.exceptions import MCPError
from pydantic import AnyUrl, PrivateAttr, TypeAdapter

from mcpshape import hooks
from mcpshape.catalog import Catalog, Item
from mcpshape.connection import Connection, UpstreamUnavailableError
from mcpshape.hooks import Call, UpstreamError, UserCode
from mcpshape.model import HttpTransport, MemoryTransport, SseTransport, StdioTransport
from mcpshape.proxy import ArgumentMap, Exposed, cut_output

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
    from contextlib import AbstractAsyncContextManager

    from fastmcp.client.transports import ClientTransport
    from fastmcp.prompts import Prompt
    from fastmcp.resources import Resource, ResourceTemplate
    from fastmcp.server.context import Context
    from fastmcp.tools import Tool
    from pydantic import BaseModel
    from starlette.types import ASGIApp

    from mcpshape.connection import Clock, Status
    from mcpshape.hooks import VirtualTool
    from mcpshape.model import Transport, Upstream
    from mcpshape.secrets import Secrets

log = logging.getLogger("mcpshape.adapter")

MCP_PATH = "/mcp"

type ClientFactory = Callable[[], Awaitable[Client[Any]]]
"""What FastMCP calls to reach the Upstream for one call, read, or get."""


class UpstreamTargetError(Exception):
    """An Upstream's target cannot be reached the way its config describes."""


@dataclass(frozen=True)
class ProxyApp:
    """A Proxy as an ASGI app serving MCP at ``MCP_PATH``, plus the lifespan it needs.

    ``serve`` replaces what the Proxy exposes; Clients see the new set on their next request.
    ``fail`` keeps the last exposed set advertised and answers every call, read, and get with
    an error naming the Proxy and the reason, until the next ``serve``; nothing reaches the
    Upstream meanwhile. The server ``name`` cannot change: build a new app for a new name.
    """

    name: str
    asgi: ASGIApp
    lifespan: Callable[[], AbstractAsyncContextManager[None]]
    serve: Callable[[Exposed], None]
    fail: Callable[[str], None]


class _Link:
    """One Upstream's client, opened and closed on its ``Connection``'s word.

    The Upstream's target is resolved on every open, not once at build time: a Proxy is served
    whether or not its Upstream can be reached, and an Upstream that comes back does so without
    a Daemon restart.
    """

    def __init__(self, transport: Transport, secrets: Secrets) -> None:
        self.transport = transport
        self.secrets = secrets
        self._open: AsyncExitStack | None = None
        self._client: Client[Any] | None = None

    @property
    def client(self) -> Client[Any]:
        if self._client is None:
            msg = "the Upstream connection is not open"
            raise UpstreamTargetError(msg)
        return self._client

    async def open(self) -> None:
        """Connect, leaving nothing behind if the attempt is given up on halfway.

        The cleanup is registered before the connect starts, not after it returns: a connect
        cancelled by the connect timeout inside ``__aenter__`` would otherwise leave a
        half-spawned child process nobody owns.
        """
        stack = AsyncExitStack()
        self._open = stack
        try:
            async with _concealing(self.transport, self.secrets):
                client: ProxyClient[Any] = ProxyClient(_target(self.transport, self.secrets))
                stack.push_async_callback(client.close)
                self._client = await stack.enter_async_context(client)
        except BaseException:
            with contextlib.suppress(Exception):  # what a client that never connected raises
                await self.close()  # on closing is the connect failure again, already reported
            raise

    async def close(self) -> None:
        stack, self._open, self._client = self._open, None, None
        if stack is not None:
            await stack.aclose()

    async def ping(self) -> None:
        if not await self.client.ping():
            msg = "the Upstream did not answer a ping"
            raise UpstreamTargetError(msg)


class UpstreamConnection:
    """One Upstream's connection: the client every Proxy of it calls through (story 74).

    The Daemon builds one per Upstream and hands it to each of that Upstream's Proxies. What
    the Proxies get is ``client``: the factory FastMCP calls for every call, read, and get,
    which is what wakes a cold Upstream and what turns an outage into a readable tool error.
    """

    def __init__(
        self,
        upstream: Upstream,
        secrets: Secrets,
        clock: Clock | None = None,
        on_catalog: Callable[[Catalog], Awaitable[None]] | None = None,
    ) -> None:
        self._link = _Link(upstream.transport, secrets)
        self._on_catalog = on_catalog
        self._connection = Connection(
            upstream.name,
            self._link,
            upstream.lifecycle,
            clock,
            on_reconnect=None if on_catalog is None else self._rescan,
        )

    async def _rescan(self) -> None:
        """Look again over the connection that has just come back, opening no second one.

        Borrowing the shared client is what keeps a reconnect from spawning a second child
        process for an stdio Upstream.
        """
        if self._on_catalog is None:
            return
        client = self._link.client
        async with _concealing(self._link.transport, self._link.secrets), client:
            observed = await _catalog_of(client)
        await self._on_catalog(observed)

    def status(self) -> Status:
        return self._connection.status()

    def running(self) -> AbstractAsyncContextManager[None]:
        """Warm, time, and finally let the connection go, for the life of the Daemon."""
        return self._connection.running()

    async def client(self) -> Client[Any]:
        try:
            await self._connection.acquire()
        except UpstreamUnavailableError as exc:
            # A tool error, not a transport failure: the Client keeps its session and only
            # this call fails. An outage is a warning in the log, never an error.
            raise ToolError(str(exc), log_level=logging.WARNING) from exc
        return self._link.client


def server_name(upstream: Upstream, proxy_name: str, exposed: Exposed) -> str:
    """The name the Proxy's server announces: the user's, else ``<upstream>/<proxy>``."""
    return exposed.name or f"{upstream.name}/{proxy_name}"


def proxy_app(
    upstream: Upstream, proxy_name: str, exposed: Exposed, connection: UpstreamConnection
) -> ProxyApp:
    """The Proxy ``proxy_name`` of ``upstream``, exposing ``exposed`` over ``connection``."""
    runtime = _Runtime(f"{upstream.name}/{proxy_name}", _Handle(connection.client))
    provider = _CatalogProvider(connection.client, runtime)
    name = server_name(upstream, proxy_name, exposed)
    server = FastMCP(name=name)
    server.add_provider(provider)
    app = server.http_app(path=MCP_PATH)

    def serve(exposed: Exposed) -> None:
        provider.serve(exposed)
        server.instructions = exposed.catalog.instructions

    @asynccontextmanager
    async def lifespan() -> AsyncGenerator[None]:
        async with app.router.lifespan_context(app):
            yield

    serve(exposed)
    return ProxyApp(name=name, asgi=app, lifespan=lifespan, serve=serve, fail=runtime.fail)


async def scan(transport: Transport, secrets: Secrets) -> Catalog:
    """Everything the Upstream behind ``transport`` advertises right now.

    This opens a connection of its own. An Upstream the Daemon is already connected to is
    looked at over that connection instead, by ``UpstreamConnection``.
    """
    async with _concealing(transport, secrets), Client(_target(transport, secrets)) as client:
        return await _catalog_of(client)


@asynccontextmanager
async def _concealing(transport: Transport, secrets: Secrets) -> AsyncGenerator[None]:
    """Let nothing fail with a resolved value in its message.

    Whatever reaching the Upstream raises is re-raised as ``UpstreamTargetError`` with every
    resolved ``${VAR}`` written back as the reference, since the message goes on to the log,
    the status, and the terminal. The original is dropped, as its text is what leaks.
    """
    try:
        yield
    except Exception as exc:  # noqa: BLE001  # whatever it was, its text must not leak
        message = secrets.concealed(str(exc) or type(exc).__name__, transport)
        raise UpstreamTargetError(message) from None


async def _catalog_of(client: Client[Any]) -> Catalog:
    """What the Upstream ``client`` is connected to advertises right now."""
    tools = await _listed(client.list_tools)
    resources = await _listed(client.list_resources)
    templates = await _listed(client.list_resource_templates)
    prompts = await _listed(client.list_prompts)
    instructions = client.instructions
    return Catalog(
        scanned_at=datetime.now(UTC),
        instructions=instructions,
        tools={tool.name: _raw(tool) for tool in tools},
        resources={str(resource.uri): _raw(resource) for resource in resources},
        resource_templates={template.uri_template: _raw(template) for template in templates},
        prompts={prompt.name: _raw(prompt) for prompt in prompts},
    )


def run_shim(url: str) -> None:
    """Speak MCP over stdio and forward every request to the Proxy served at ``url``.

    The other half of the hidden ``serve`` command, for the Clients that accept stdio only.
    Runs until the Client closes the pipe. stdout carries the protocol, so the banner FastMCP
    would otherwise print is off.
    """
    create_proxy(StreamableHttpTransport(url)).run(transport="stdio", show_banner=False)


async def _listed[T](method: Callable[[], Awaitable[Sequence[T]]]) -> Sequence[T]:
    """The list ``method`` returns, or nothing when the Upstream lacks that capability."""
    try:
        return await method()
    except MCPError as exc:
        if exc.error.code == mcp_types.METHOD_NOT_FOUND:
            return []
        raise


def _raw(definition: BaseModel) -> dict[str, Any]:
    return definition.model_dump(mode="json", by_alias=True, exclude_none=True)


# --- the chain -------------------------------------------------------------------------------

_BLOCK: TypeAdapter[Any] = TypeAdapter(mcp_types.ContentBlock)
_MESSAGE_CONTENT: TypeAdapter[Any] = TypeAdapter(
    mcp_types.TextContent
    | mcp_types.ImageContent
    | mcp_types.AudioContent
    | mcp_types.EmbeddedResource
)


class _Handle:
    """``upstream`` for one Proxy: its own Upstream's shared client, under Catalog names."""

    def __init__(self, client_factory: ClientFactory) -> None:
        self._client_factory = client_factory

    async def call(self, name: str, args: dict[str, Any]) -> hooks.ToolResult:
        client = await self._client_factory()
        async with client:
            try:
                raw = await client.call_tool_mcp(name, args)
            except MCPError as exc:
                raise UpstreamError(exc.error.message) from exc
        result = _tool_result_of(raw.content, raw.structured_content)
        if raw.is_error:
            raise UpstreamError(result.text or "the Upstream reported an error")
        return result

    async def read(self, uri: str) -> hooks.ResourceResult:
        client = await self._client_factory()
        async with client:
            try:
                contents = await client.read_resource(uri)
            except MCPError as exc:
                raise UpstreamError(exc.error.message) from exc
        return hooks.ResourceResult(contents=[_content_of(item) for item in contents])

    async def get(self, name: str, args: dict[str, Any]) -> hooks.PromptResult:
        client = await self._client_factory()
        async with client:
            try:
                raw = await client.get_prompt(name, args)
            except MCPError as exc:
                raise UpstreamError(exc.error.message) from exc
        return hooks.PromptResult(
            messages=[_message_of(message.role, message.content) for message in raw.messages],
            description=raw.description,
        )


class _Runtime:
    """What every component of one Proxy runs its calls through: the Hooks, or the failure."""

    def __init__(self, label: str, handle: _Handle) -> None:
        self.label = label
        self.handle = handle
        self.code = UserCode()
        self.failure: str | None = None

    def serve(self, code: UserCode) -> None:
        self.code, self.failure = code, None

    def fail(self, reason: str) -> None:
        self.failure = reason

    async def run[R](
        self,
        call: Call,
        forward: Callable[[Call], Awaitable[R]],
        of: Callable[[object], R],
        error: type[FastMCPError],
        cap: Callable[[R], R] | None = None,
    ) -> R:
        """``call`` through the Hooks, with user failures turned into ``error`` for the Client."""
        if self.failure is not None:
            msg = f"Proxy {self.label} is unhealthy: {self.failure}"
            raise error(msg, log_level=logging.WARNING)
        with hooks.bound(self.handle):
            try:
                return await hooks.run_call(self.code, call, forward, of, cap)
            except FastMCPError:
                raise
            except Exception as exc:
                raise error(str(exc) or type(exc).__name__, log_level=logging.WARNING) from exc


def _capped_output(ceiling: int | None) -> Callable[[hooks.ToolResult], hooks.ToolResult] | None:
    """What ``_Runtime.run`` cuts a tool's answer with, or ``None`` before a Cap is known."""
    if ceiling is None:
        return None

    def apply(result: hooks.ToolResult) -> hooks.ToolResult:
        cut = cut_output(result.text, ceiling)
        if cut != result.text:
            result.text = cut
        return result

    return apply


class _CuratedTool(ProxyTool):
    """A ProxyTool whose call runs through the chain under its Catalog name and arguments."""

    _arguments: ArgumentMap = PrivateAttr(default_factory=ArgumentMap)
    _runtime: _Runtime = PrivateAttr()
    _origin: str = PrivateAttr(default="")
    _output_cap: int | None = PrivateAttr(default=None)

    def curate(
        self, runtime: _Runtime, origin: str, arguments: ArgumentMap, output_cap: int | None = None
    ) -> None:
        self._runtime, self._origin, self._arguments = runtime, origin, arguments
        self._output_cap = output_cap

    async def run(self, arguments: dict[str, Any], context: Context | None = None) -> ToolResult:
        run_upstream = super().run

        async def forward(call: Call) -> hooks.ToolResult:
            raw = await run_upstream(call.args, context)
            result = _tool_result_of(raw.content, raw.structured_content)
            if raw.is_error:
                raise ToolError(result.text or "the Upstream reported an error")
            return result

        call = Call("tool", self._origin, self._arguments.to_catalog(arguments))
        result = await self._runtime.run(
            call, forward, hooks.ToolResult.of, ToolError, _capped_output(self._output_cap)
        )
        return _to_tool_result(result, self.output_schema)


class _CuratedResource(ProxyResource):
    """A ProxyResource whose read runs through the chain under its Catalog URI."""

    _runtime: _Runtime = PrivateAttr()
    _origin: str = PrivateAttr(default="")

    def curate(self, runtime: _Runtime, origin: str) -> None:
        self._runtime, self._origin = runtime, origin

    async def read(self) -> ResourceResult:
        read_upstream = super().read

        async def forward(_call: Call) -> hooks.ResourceResult:
            return _resource_result_of(await read_upstream())

        call = Call("resource", self._origin, {})
        result = await self._runtime.run(call, forward, hooks.ResourceResult.of, ResourceError)
        return _to_resource_result(result)


class _CuratedTemplate(ProxyTemplate):
    """A ProxyTemplate whose reads run through the chain under its Catalog URI template.

    FastMCP reads a template by creating a resource for the URI and reading that; the proxy
    template reads the Upstream while creating it, so the chain runs there and the resource
    handed back carries the result the Hooks left.
    """

    _runtime: _Runtime = PrivateAttr()
    _origin: str = PrivateAttr(default="")

    def curate(self, runtime: _Runtime, origin: str) -> None:
        self._runtime, self._origin = runtime, origin

    async def create_resource(
        self, uri: str, params: dict[str, Any], context: Context | None = None
    ) -> ProxyResource:
        create_upstream = super().create_resource

        async def forward(call: Call) -> hooks.ResourceResult:
            resource = await create_upstream(uri, call.args, context)
            return _resource_result_of(await resource.read())

        call = Call("resource", self._origin, dict(params))
        result = await self._runtime.run(call, forward, hooks.ResourceResult.of, ResourceError)
        first = next((item.mime_type for item in result.contents if item.mime_type), None)
        return ProxyResource(
            client_factory=self._client_factory,  # pyright: ignore[reportUnknownMemberType]  # FastMCP's factory type is unparameterised
            uri=uri,
            name=self.name,
            title=self.title,
            description=self.description,
            mime_type=first or self.mime_type or "text/plain",
            icons=self.icons,
            meta=self.meta,
            tags=self.tags,
            _cached_content=_to_resource_result(result),
        )


class _CuratedPrompt(ProxyPrompt):
    """A ProxyPrompt whose get runs through the chain under its Catalog name."""

    _runtime: _Runtime = PrivateAttr()
    _origin: str = PrivateAttr(default="")

    def curate(self, runtime: _Runtime, origin: str) -> None:
        self._runtime, self._origin = runtime, origin

    async def render(self, arguments: dict[str, Any] | None = None) -> PromptResult:
        render_upstream = super().render

        async def forward(call: Call) -> hooks.PromptResult:
            return _prompt_result_of(await render_upstream(call.args))

        call = Call("prompt", self._origin, dict(arguments or {}))
        result = await self._runtime.run(call, forward, hooks.PromptResult.of, PromptError)
        return _to_prompt_result(result)


class _VirtualTool(FunctionTool):
    """A Virtual Tool: the user's function, its schema from its signature, run in the chain.

    Its exposed name is its identity, so Hooks keyed by that name run around it like around
    a Catalog tool. Sync functions run inline on the Daemon's loop like every Hook; a
    blocking one is the user's business, as the design brief says.
    """

    _runtime: _Runtime = PrivateAttr()
    _output_cap: int | None = PrivateAttr(default=None)

    @classmethod
    def build(
        cls, runtime: _Runtime, virtual: VirtualTool, output_cap: int | None = None
    ) -> _VirtualTool:
        built = cls.from_function(
            virtual.fn, name=virtual.name, description=virtual.description, run_in_thread=False
        )
        tool = cast("_VirtualTool", built)
        tool._runtime = runtime  # noqa: SLF001  # our own private attribute
        tool._output_cap = output_cap  # noqa: SLF001  # our own private attribute
        return tool

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        run_body = super().run

        async def forward(call: Call) -> hooks.ToolResult:
            try:
                raw = await run_body(call.args)
            except FastMCPError:
                raise
            except Exception:
                log.warning("Virtual Tool %s raised", self.name, exc_info=True)
                raise
            return _tool_result_of(raw.content, raw.structured_content)

        call = Call("tool", self.name, dict(arguments))
        result = await self._runtime.run(
            call, forward, hooks.ToolResult.of, ToolError, _capped_output(self._output_cap)
        )
        return _to_tool_result(result, self.output_schema)

    def convert_result(self, raw_value: Any) -> ToolResult:  # noqa: ANN401  # whatever the user returned
        if isinstance(raw_value, hooks.ToolResult):
            return _to_tool_result(raw_value, self.output_schema)
        return super().convert_result(raw_value)


# --- mcpshape's result types at the edge -----------------------------------------------------


def _tool_result_of(
    content: Sequence[mcp_types.ContentBlock], structured: dict[str, Any] | None
) -> hooks.ToolResult:
    return hooks.ToolResult(content=[_raw(block) for block in content], structured=structured)


def _to_tool_result(result: hooks.ToolResult, schema: dict[str, Any] | None) -> ToolResult:
    return ToolResult(
        content=[_BLOCK.validate_python(block) for block in result.content],
        structured_content=_structured(result, schema),
    )


WRAPPED = "x-fastmcp-wrap-result"
"""FastMCP's mark on the output schema of a tool whose one value it wraps as ``result``."""


def _structured(result: hooks.ToolResult, schema: dict[str, Any] | None) -> dict[str, Any] | None:
    """The structured content to send: the user's, else what the output schema lets us derive."""
    if result.structured is not None or not schema:
        return result.structured
    text = result.text
    if WRAPPED in schema:
        properties = cast("dict[str, dict[str, Any]]", schema.get("properties") or {})
        wrapped = properties.get("result") or {}
        return {"result": text if wrapped.get("type") == "string" else _loaded(text, text)}
    loaded = _loaded(text, None)
    return cast("dict[str, Any]", loaded) if isinstance(loaded, dict) else None


def _loaded(text: str, fallback: object) -> object:
    try:
        return json.loads(text)
    except ValueError:
        return fallback


def _resource_result_of(read: str | bytes | ResourceResult) -> hooks.ResourceResult:
    if not isinstance(read, ResourceResult):
        return hooks.ResourceResult(contents=[hooks.Content(read)])
    return hooks.ResourceResult(
        contents=[hooks.Content(item.content, item.mime_type) for item in read.contents]
    )


def _content_of(
    item: mcp_types.TextResourceContents | mcp_types.BlobResourceContents,
) -> hooks.Content:
    if isinstance(item, mcp_types.TextResourceContents):
        return hooks.Content(item.text, item.mime_type)
    return hooks.Content(base64.b64decode(item.blob), item.mime_type)


def _to_resource_result(result: hooks.ResourceResult) -> ResourceResult:
    return ResourceResult(
        contents=[ResourceContent(item.data, mime_type=item.mime_type) for item in result.contents]
    )


def _message_of(role: str, content: mcp_types.ContentBlock) -> hooks.Message:
    return hooks.Message(_raw(content), role)


def _prompt_result_of(rendered: str | list[Message | str] | PromptResult) -> hooks.PromptResult:
    if not isinstance(rendered, PromptResult):
        return hooks.PromptResult.of(rendered)
    return hooks.PromptResult(
        messages=[_message_of(message.role, message.content) for message in rendered.messages],
        description=rendered.description,
    )


def _to_prompt_result(result: hooks.PromptResult) -> PromptResult:
    messages = [
        Message(
            message.content
            if isinstance(message.content, str)
            else _MESSAGE_CONTENT.validate_python(message.content),
            role=_role(message.role),
        )
        for message in result.messages
    ]
    return PromptResult(messages, description=result.description)


def _role(role: str) -> Literal["user", "assistant"]:
    if role == "user" or role == "assistant":  # noqa: PLR1714  # narrows the literal
        return role
    msg = f"a prompt message's role is 'user' or 'assistant', not {role!r}"
    raise PromptError(msg)


class _CatalogProvider(Provider):
    """Serves lists from what it is handed and runs calls, reads, and gets through the chain.

    Each component is built under its Catalog name and then copied under its exposed name, so
    FastMCP keeps the Catalog name as the backend name to forward with.
    """

    def __init__(self, client_factory: ClientFactory, runtime: _Runtime) -> None:
        super().__init__()
        self._client_factory = client_factory
        self._runtime = runtime
        self._tools: list[Tool] = []
        self._resources: list[Resource] = []
        self._templates: list[ResourceTemplate] = []
        self._prompts: list[Prompt] = []

    def serve(self, exposed: Exposed) -> None:
        catalog = exposed.catalog
        self._tools = [self._tool(exposed, name, raw) for name, raw in catalog.tools.items()]
        self._tools += [
            _VirtualTool.build(self._runtime, virtual, exposed.output_caps.get(virtual.name))
            for virtual in exposed.code.tools.values()
        ]
        self._resources = [
            self._resource(exposed, uri, raw) for uri, raw in catalog.resources.items()
        ]
        self._templates = [
            self._template(exposed, uri, raw) for uri, raw in catalog.resource_templates.items()
        ]
        self._prompts = [self._prompt(exposed, name, raw) for name, raw in catalog.prompts.items()]
        self._runtime.serve(exposed.code)

    def _tool(self, exposed: Exposed, name: str, raw: dict[str, Any]) -> Tool:
        origin = exposed.origin(Item("tool", name))
        built = _CuratedTool.from_mcp_tool(  # pyright: ignore[reportUnknownMemberType]  # FastMCP's factory type is unparameterised
            self._client_factory, mcp_types.Tool.model_validate({**raw, "name": origin})
        )
        tool = _renamed(cast("_CuratedTool", built), origin, name, "name", name)
        tool.curate(
            self._runtime,
            origin,
            exposed.arguments.get(name, ArgumentMap()),
            exposed.output_caps.get(name),
        )
        return tool

    def _resource(self, exposed: Exposed, uri: str, raw: dict[str, Any]) -> Resource:
        origin = exposed.origin(Item("resource", uri))
        built = _CuratedResource.from_mcp_resource(  # pyright: ignore[reportUnknownMemberType]  # FastMCP's factory type is unparameterised
            self._client_factory, mcp_types.Resource.model_validate({**raw, "uri": origin})
        )
        resource = _renamed(cast("_CuratedResource", built), origin, uri, "uri", AnyUrl(uri))
        resource.curate(self._runtime, origin)
        return resource

    def _template(self, exposed: Exposed, uri: str, raw: dict[str, Any]) -> ResourceTemplate:
        origin = exposed.origin(Item("resource_template", uri))
        built = _CuratedTemplate.from_mcp_template(  # pyright: ignore[reportUnknownMemberType]  # FastMCP's factory type is unparameterised
            self._client_factory,
            mcp_types.ResourceTemplate.model_validate({**raw, "uriTemplate": origin}),
        )
        template = _renamed(cast("_CuratedTemplate", built), origin, uri, "uri_template", uri)
        template.curate(self._runtime, origin)
        return template

    def _prompt(self, exposed: Exposed, name: str, raw: dict[str, Any]) -> Prompt:
        origin = exposed.origin(Item("prompt", name))
        built = _CuratedPrompt.from_mcp_prompt(  # pyright: ignore[reportUnknownMemberType]  # FastMCP's factory type is unparameterised
            self._client_factory, mcp_types.Prompt.model_validate({**raw, "name": origin})
        )
        prompt = _renamed(cast("_CuratedPrompt", built), origin, name, "name", name)
        prompt.curate(self._runtime, origin)
        return prompt

    async def _list_tools(self) -> Sequence[Tool]:
        return self._tools

    async def _list_resources(self) -> Sequence[Resource]:
        return self._resources

    async def _list_resource_templates(self) -> Sequence[ResourceTemplate]:
        return self._templates

    async def _list_prompts(self) -> Sequence[Prompt]:
        return self._prompts


def _renamed[C: BaseModel](component: C, origin: str, exposed: str, key: str, value: object) -> C:
    """``component`` under its exposed identity, still forwarding to ``origin``."""
    if exposed == origin:
        return component
    return component.model_copy(update={key: value})


def _target(transport: Transport, secrets: Secrets) -> ClientTransport | FastMCP[Any]:
    """How FastMCP reaches this Upstream, with every ``${VAR}`` in it resolved.

    An stdio Upstream is a child process of the Daemon: ``keep_alive`` is off, because when
    the connection is let go the process goes with it (the Upstream's lifecycle decides that,
    not FastMCP).
    """
    match secrets.expanded(transport):
        case StdioTransport() as stdio:
            return StdioClientTransport(
                command=stdio.command,
                args=list(stdio.args),
                env=dict(stdio.env) or None,
                keep_alive=False,
            )
        case HttpTransport() as http:
            return StreamableHttpTransport(http.url)
        case SseTransport() as sse:
            return SSETransport(sse.url)
        case MemoryTransport() as memory:
            return _import_server(memory.module, memory.attribute)


def _import_server(module: str, attribute: str) -> FastMCP[Any]:
    try:
        server: object = getattr(importlib.import_module(module), attribute)
    except (ImportError, AttributeError) as exc:
        msg = f"cannot import {module}:{attribute}: {exc}"
        raise UpstreamTargetError(msg) from exc
    if not isinstance(server, FastMCP):
        msg = f"{module}:{attribute} is not an in-memory MCP server"
        raise UpstreamTargetError(msg)
    return cast("FastMCP[Any]", server)
