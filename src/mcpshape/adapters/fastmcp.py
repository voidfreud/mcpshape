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

import asyncio
import base64
import contextlib
import copy
import importlib
import json
import logging
import os
import threading
import time
import webbrowser
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, TextIO, cast

import httpx2
import jsonschema
import mcp_types
from fastmcp import Client, FastMCP
from fastmcp.client.oauth_callback import (
    OAuthCallbackResult,
    create_oauth_callback_server,
)
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
from fastmcp.utilities.http import find_available_port
from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.shared.auth import (
    AuthorizationCodeResult,
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthToken,
)
from mcp.shared.exceptions import MCPError
from pydantic import AnyUrl, PrivateAttr, TypeAdapter

from mcpshape import hooks
from mcpshape.calls import CallRecord, Outcome
from mcpshape.catalog import Catalog, Item
from mcpshape.connection import CONNECTED, Connection, UpstreamUnavailableError
from mcpshape.hooks import Call, UpstreamError, UserCode
from mcpshape.model import HttpTransport, MemoryTransport, SseTransport, StdioTransport
from mcpshape.proxy import ArgumentMap, Exposed, cut_output

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable, Generator, Sequence
    from contextlib import AbstractAsyncContextManager

    from fastmcp.client.transports import ClientTransport
    from fastmcp.prompts import Prompt
    from fastmcp.resources import Resource, ResourceTemplate
    from fastmcp.server.context import Context
    from fastmcp.tools import Tool
    from pydantic import BaseModel
    from starlette.types import ASGIApp

    from mcpshape.calls import CallLog
    from mcpshape.connection import Clock, Status
    from mcpshape.hooks import VirtualTool
    from mcpshape.model import Transport, Upstream
    from mcpshape.secrets import Secrets
    from mcpshape.tokens import Tokens

log = logging.getLogger("mcpshape.adapter")
child_log = logging.getLogger("mcpshape.upstream")
"""Where what an stdio Upstream's child writes to stderr goes, one line per line (#42)."""

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

    def __init__(self, upstream: Upstream, secrets: Secrets, tokens: Tokens | None) -> None:
        self.upstream = upstream
        self.transport = upstream.transport
        self.secrets = secrets
        self.tokens = tokens
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
            async with _concealing(self.transport, self.secrets, self.tokens):
                with _child_stderr(self.upstream, self.secrets) as errlog:
                    target = _target(self.transport, self.secrets, self.tokens, errlog)
                    client: ProxyClient[Any] = ProxyClient(target)
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

    def is_dead(self, exc: BaseException) -> bool:
        """Whether ``exc`` means this open connection is dead, not something the Upstream said.

        An error the Upstream itself answered leaves the connection standing: a JSON-RPC error
        such as method not found or invalid params, an ``isError`` result, and a ``ToolError``
        a Hook or a Virtual Tool raised. A dead transport is the MCP SDK's own
        ``CONNECTION_CLOSED``, which is what a call over a transport whose other end is gone
        raises, and, for whatever a call racing that one hits instead, a session FastMCP has
        already torn down (checked 2026-09-08, FastMCP 4.0.3; see docs/clients.md).
        """
        if isinstance(exc, FastMCPError):
            return False
        if isinstance(exc, MCPError):
            return exc.error.code == mcp_types.CONNECTION_CLOSED
        return self._client is not None and not self._client.is_connected()


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
        tokens: Tokens | None = None,
    ) -> None:
        self._link = _Link(upstream, secrets, tokens)
        self._on_catalog = on_catalog
        self._lifecycle = upstream.lifecycle
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
        await self._on_catalog(await self._over_open_client())

    async def _over_open_client(self) -> Catalog:
        """What the Upstream advertises, looked at over the connection already open."""
        client = self._link.client
        async with _concealing(self._link.transport, self._link.secrets, self._link.tokens), client:
            return await _catalog_of(client)

    def status(self) -> Status:
        return self._connection.status()

    async def observe(self) -> Catalog:
        """What the Upstream advertises now: ``/api`` sync (#16).

        Over the connection already open when there is one, acquired as a call acquires it so
        the idle timer starts over and an stdio Upstream is not spawned a second time;
        otherwise over a connection of its own, as the Daemon's own start-up scan does, which
        leaves the lifecycle where it was.
        """
        if self._connection.status().state in CONNECTED:
            await self._connection.acquire()
            return await self._over_open_client()
        return await scan(self._link.upstream, self._link.secrets, self._link.tokens)

    def retry(self) -> None:
        """Try to connect again now: a login just stored what the last attempt lacked (#16)."""
        self._connection.retry()

    def unscanned(self) -> None:
        """The start-up scan reached nothing, so the first connect rescans (#57)."""
        self._connection.unscanned()

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

    async def gone(self, exc: BaseException) -> str | None:
        """The Upstream's message when ``exc`` means the open connection is dead, else nothing.

        A call is what finds a lazy Upstream gone, since nothing pings one; telling the
        lifecycle here is what moves it to ``unavailable`` at once and starts the backoff,
        instead of leaving the transport's own words to reach the model. Only what the
        forward to the Upstream raised is judged here: a Hook's own exception never is.
        """
        if not self._link.is_dead(exc):
            return None
        await self._connection.lost(str(exc) or type(exc).__name__)
        return self._lifecycle.unavailable_message


def server_name(upstream: Upstream, proxy_name: str, exposed: Exposed) -> str:
    """The name the Proxy's server announces: the user's, else ``<upstream>/<proxy>``."""
    return exposed.name or f"{upstream.name}/{proxy_name}"


def proxy_app(
    upstream: Upstream,
    proxy_name: str,
    exposed: Exposed,
    connection: UpstreamConnection,
    calls: CallLog,
) -> ProxyApp:
    """The Proxy ``proxy_name`` of ``upstream``, exposing ``exposed`` over ``connection``.

    Every tool call it serves is recorded in ``calls``.
    """
    runtime = _Runtime(upstream.name, proxy_name, connection, calls)
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


async def scan(upstream: Upstream, secrets: Secrets, tokens: Tokens | None = None) -> Catalog:
    """Everything ``upstream`` advertises right now.

    This opens a connection of its own. An Upstream the Daemon is already connected to is
    looked at over that connection instead, by ``UpstreamConnection``.
    """
    transport = upstream.transport
    async with _concealing(transport, secrets, tokens):
        with _child_stderr(upstream, secrets) as errlog:
            async with Client(_target(transport, secrets, tokens, errlog)) as client:
                return await _catalog_of(client)


@asynccontextmanager
async def _concealing(
    transport: Transport, secrets: Secrets, tokens: Tokens | None = None
) -> AsyncGenerator[None]:
    """Let nothing fail with a resolved value or a stored token in its message.

    Whatever reaching the Upstream raises is re-raised as ``UpstreamTargetError`` with every
    resolved ``${VAR}`` written back as the reference and every stored OAuth token written as
    what it is, since the message goes on to the log, the status, and the terminal. The
    original is dropped, as its text is what leaks.
    """
    try:
        yield
    except Exception as exc:  # noqa: BLE001  # whatever it was, its text must not leak
        message = secrets.concealed(str(exc) or type(exc).__name__, transport)
        raise _refusal_kind(exc)(tokens.concealed(message) if tokens else message) from None


def _refusal_kind(exc: BaseException) -> type[UpstreamTargetError]:
    """The type ``_concealing`` re-raises as: the refusal's own where one caused the failure.

    A ``LoginNeededError`` is raised inside the SDK's auth flow and comes out wrapped in the
    client's own connect error, so it is looked for down the cause chain, not only on top.
    """
    seen: BaseException | None = exc
    while seen is not None:
        if isinstance(seen, LoginNeededError):
            return LoginNeededError
        seen = seen.__cause__ or seen.__context__
    return UpstreamTargetError


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


def run_shim(url: str, token: str | None = None) -> None:
    """Speak MCP over stdio and forward every request to the Proxy served at ``url``.

    The other half of the hidden ``serve`` command, for the Clients that accept stdio only.
    Runs until the Client closes the pipe. stdout carries the protocol, so the banner FastMCP
    would otherwise print is off. ``token`` is sent as a bearer token when the Daemon requires
    one; never logged.
    """
    headers = {"Authorization": f"Bearer {token}"} if token else None
    create_proxy(StreamableHttpTransport(url, headers=headers)).run(
        transport="stdio", show_banner=False
    )


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
    """``upstream`` for one Proxy: its own Upstream's shared client, under Catalog names.

    An error the Upstream answered is an ``UpstreamError`` user code may catch. A connection
    found dead instead is not the user's to handle: it moves the Upstream to ``unavailable``
    and fails this call with the Upstream's message, exactly as reaching for the client of an
    Upstream that is not connected already does.
    """

    def __init__(self, connection: UpstreamConnection) -> None:
        self._connection = connection

    async def call(self, name: str, args: dict[str, Any]) -> hooks.ToolResult:
        client = await self._connection.client()
        async with client:
            try:
                raw = await client.call_tool_mcp(name, args)
            except MCPError as exc:
                raise await self._failure(exc) from exc
        result = _tool_result_of(raw.content, raw.structured_content)
        if raw.is_error:
            raise UpstreamError(result.text or "the Upstream reported an error")
        return result

    async def read(self, uri: str) -> hooks.ResourceResult:
        client = await self._connection.client()
        async with client:
            try:
                contents = await client.read_resource(uri)
            except MCPError as exc:
                raise await self._failure(exc) from exc
        return hooks.ResourceResult(contents=[_content_of(item) for item in contents])

    async def get(self, name: str, args: dict[str, Any]) -> hooks.PromptResult:
        client = await self._connection.client()
        async with client:
            try:
                raw = await client.get_prompt(name, args)
            except MCPError as exc:
                raise await self._failure(exc) from exc
        return hooks.PromptResult(
            messages=[_message_of(message.role, message.content) for message in raw.messages],
            description=raw.description,
        )

    async def _failure(self, exc: MCPError) -> Exception:
        """What ``exc`` becomes: the Upstream's message when it is dead, else what it said."""
        message = await self._connection.gone(exc)
        if message is None:
            return UpstreamError(exc.error.message)
        return ToolError(message, log_level=logging.WARNING)


class _Runtime:
    """What every component of one Proxy runs its calls through: the Hooks, or the failure."""

    def __init__(
        self, upstream: str, proxy: str, connection: UpstreamConnection, calls: CallLog
    ) -> None:
        self.upstream, self.proxy = upstream, proxy
        self.label = f"{upstream}/{proxy}"
        self.connection = connection
        self.calls = calls
        self.handle = _Handle(connection)
        self.code = UserCode()
        self.failure: str | None = None

    async def run_tool(
        self,
        call: Call,
        exposed: str,
        forward: Callable[[Call], Awaitable[hooks.ToolResult]],
        cap: Callable[[hooks.ToolResult], hooks.ToolResult] | None,
        check: Callable[[hooks.ToolResult, str], None] | None,
    ) -> hooks.ToolResult:
        """``run`` for a tool call, recorded in the call log however it ends (#16).

        What is recorded is what the Client experienced: the arguments it sent, under Catalog
        names and before any Hook touched them, the time it waited, Hooks included, and the
        result or the error it was answered with.
        """
        at = datetime.now(UTC)
        arguments = copy.deepcopy(call.args)  # a Hook may change them in place, however deep
        started = time.perf_counter()
        try:
            result = await self.run(call, forward, hooks.ToolResult.of, ToolError, cap, check)
        except Exception as exc:
            self._record(at, call, exposed, arguments, started, "error", _message(exc))
            raise
        outcome: Outcome = "error" if result.is_error else "ok"
        self._record(at, call, exposed, arguments, started, outcome, result.text)
        return result

    def _record(  # noqa: PLR0913, PLR0917  # every one of these is what a record is
        self,
        at: datetime,
        call: Call,
        exposed: str,
        arguments: dict[str, Any],
        started: float,
        outcome: Outcome,
        result: str,
    ) -> None:
        self.calls.record(
            CallRecord.build(
                at,
                self.upstream,
                self.proxy,
                call.name,
                exposed,
                arguments,
                (time.perf_counter() - started) * 1000,
                outcome,
                result,
            )
        )

    def serve(self, code: UserCode) -> None:
        self.code, self.failure = code, None

    def fail(self, reason: str) -> None:
        self.failure = reason

    async def run[R](  # noqa: PLR0913, PLR0917  # every one of these is state the call needs
        self,
        call: Call,
        forward: Callable[[Call], Awaitable[R]],
        of: Callable[[object], R],
        error: type[FastMCPError],
        cap: Callable[[R], R] | None = None,
        check: Callable[[R, str], None] | None = None,
    ) -> R:
        """``call`` through the Hooks, with user failures turned into ``error`` for the Client.

        A forward that raises because the open connection is dead is the Upstream going away
        mid-call: the lifecycle hears of it and the Client is answered with the Upstream's
        ``unavailable_message``, the same words a call during the backoff gets.
        """
        if self.failure is not None:
            msg = f"Proxy {self.label} is unhealthy: {self.failure}"
            raise error(msg, log_level=logging.WARNING)

        async def forwarding(call: Call) -> R:
            try:
                return await forward(call)
            except FastMCPError:
                raise
            except Exception as exc:
                away = await self.connection.gone(exc)
                if away is None:
                    raise
                raise error(away, log_level=logging.WARNING) from exc

        with hooks.bound(self.handle):
            try:
                return await hooks.run_call(self.code, call, forwarding, of, cap, check)
            except FastMCPError:
                raise
            except Exception as exc:
                raise error(_message(exc), log_level=logging.WARNING) from exc


def _message(exc: BaseException) -> str:
    return str(exc) or type(exc).__name__


def _capped_output(
    ceiling: int | None, schema: dict[str, Any] | None
) -> Callable[[hooks.ToolResult], hooks.ToolResult] | None:
    """What ``_Runtime.run`` cuts a tool's answer with, or ``None`` before a Cap is known.

    The Cap ceilings the text the model reads. Structured content is the machine-readable
    answer a Client checks against the tool's output schema, so it stays whole where the
    schema is its own: cut JSON would fit no schema, and the Client would refuse the whole
    call. Only where the schema wraps one string is the structured content that text again,
    and then it follows the cut.
    """
    if ceiling is None:
        return None

    def apply(result: hooks.ToolResult) -> hooks.ToolResult:
        cut = cut_output(result.text, ceiling)
        if cut == result.text:
            return result
        structured = result.structured
        result.text = cut
        if not _wraps_string(schema):
            result.structured = structured
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
            result.is_error = raw.is_error
            return result

        call = Call("tool", self._origin, self._arguments.to_catalog(arguments))
        result = await self._runtime.run_tool(
            call,
            self.name,
            forward,
            _capped_output(self._output_cap, self.output_schema),
            _schema_check(self._origin, self.output_schema),
        )
        if result.is_error:
            raise ToolError(result.text or "the Upstream reported an error")
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
    a Catalog tool. A sync body runs in a worker thread, FastMCP's own default
    (``run_in_thread``), so it can block on the ``upstream`` handle and stalls only its own
    call. FastMCP dispatches it through ``anyio.to_thread.run_sync``, which copies the
    context, so the handle ``_Runtime.run`` bound is visible in that thread.
    """

    _runtime: _Runtime = PrivateAttr()
    _output_cap: int | None = PrivateAttr(default=None)

    @classmethod
    def build(
        cls, runtime: _Runtime, virtual: VirtualTool, output_cap: int | None = None
    ) -> _VirtualTool:
        built = cls.from_function(virtual.fn, name=virtual.name, description=virtual.description)
        tool = cast("_VirtualTool", built)
        tool._runtime = runtime  # noqa: SLF001  # our own private attribute
        tool._output_cap = output_cap  # noqa: SLF001  # our own private attribute
        return tool

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        run_body = super().run

        async def forward(call: Call) -> hooks.ToolResult:
            # A Virtual Tool's body raising is an exception here, which propagates and skips
            # the after Hooks like any other raise. A body that returns a ToolResult marked
            # is_error instead hands the after Hooks an error result, as an Upstream does.
            try:
                raw = await run_body(call.args)
            except FastMCPError:
                raise
            except Exception:
                log.warning("Virtual Tool %s raised", self.name, exc_info=True)
                raise
            result = _tool_result_of(raw.content, raw.structured_content)
            result.is_error = raw.is_error
            return result

        call = Call("tool", self.name, dict(arguments))
        result = await self._runtime.run_tool(
            call,
            self.name,
            forward,
            _capped_output(self._output_cap, self.output_schema),
            _schema_check(self.name, self.output_schema),
        )
        if result.is_error:
            raise ToolError(result.text or "the Virtual Tool reported an error")
        return _to_tool_result(result, self.output_schema)

    def convert_result(self, raw_value: Any) -> ToolResult:  # noqa: ANN401  # whatever the user returned
        """A ``ToolResult`` the body built keeps its ``is_error``: FastMCP's own carries one,
        so the mark survives the edge and the ``after`` Hooks see it, as for a Catalog tool."""
        if isinstance(raw_value, hooks.ToolResult):
            converted = _to_tool_result(raw_value, self.output_schema)
            converted.is_error = raw_value.is_error
            return converted
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


def _wrapped_property(schema: dict[str, Any] | None) -> dict[str, Any] | None:
    """The schema of the one value a wrapping output schema holds, or ``None`` for any other."""
    if not schema or WRAPPED not in schema:
        return None
    properties = cast("dict[str, dict[str, Any]]", schema.get("properties") or {})
    return properties.get("result") or {}


def _wraps_string(schema: dict[str, Any] | None) -> bool:
    wrapped = _wrapped_property(schema)
    return wrapped is not None and wrapped.get("type") == "string"


def _structured(result: hooks.ToolResult, schema: dict[str, Any] | None) -> dict[str, Any] | None:
    """The structured content to send: the user's, else what the output schema lets us derive."""
    if result.structured is not None or not schema:
        return result.structured
    text = result.text
    if (wrapped := _wrapped_property(schema)) is not None:
        return {"result": text if wrapped.get("type") == "string" else _loaded(text, text)}
    loaded = _loaded(text, None)
    return cast("dict[str, Any]", loaded) if isinstance(loaded, dict) else None


def _loaded(text: str, fallback: object) -> object:
    try:
        return json.loads(text)
    except ValueError:
        return fallback


def _schema_check(
    tool: str, schema: dict[str, Any] | None
) -> Callable[[hooks.ToolResult, str], None] | None:
    """What a Hook's result must satisfy: the tool's output schema, when it has one.

    ``doctor`` cannot know what a Hook returns, so this is the check: it runs on every result a
    ``before`` or ``after`` Hook hands back, computes the structured content the Client would be
    sent (``_structured``), and turns a mismatch into a tool error naming the Hook, the tool,
    and what the schema expects, instead of letting the Client's own validator refuse the call.
    """
    if not schema:
        return None

    def check(result: hooks.ToolResult, hook_name: str) -> None:
        if result.is_error:
            return
        structured = _structured(result, schema)
        try:
            jsonschema.validate(structured, schema)
        except jsonschema.ValidationError as exc:
            msg = (
                f"Hook {hook_name} on tool {tool} returned a result that does not fit its "
                f"output schema: {exc.message}; the schema expects {_schema_expectation(schema)}"
            )
            log.warning(msg)
            raise ToolError(msg, log_level=logging.WARNING) from exc

    return check


def _schema_expectation(schema: dict[str, Any]) -> str:
    """What the schema wants, in the glossary's words, for the tool error message."""
    if (wrapped := _wrapped_property(schema)) is not None:
        return f"a single {wrapped.get('type', 'value')} value"
    required = cast("list[str]", schema.get("required") or [])
    return f"a JSON object with {', '.join(required)}" if required else "a JSON object"


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


def _target(
    transport: Transport,
    secrets: Secrets,
    tokens: Tokens | None = None,
    errlog: TextIO | None = None,
) -> ClientTransport | FastMCP[Any]:
    """How FastMCP reaches this Upstream, with every ``${VAR}`` in it resolved.

    An stdio Upstream is a child process of the Daemon: ``keep_alive`` is off, because when
    the connection is let go the process goes with it (the Upstream's lifecycle decides that,
    not FastMCP), and ``errlog`` is where its stderr goes (``_child_stderr``).

    An Upstream reached by URL that says ``auth = "oauth"`` carries the stored login, and
    nothing here ever opens a browser: the Daemon refuses instead, naming the command that
    logs in (``login``, which the CLI calls).
    """
    match secrets.expanded(transport):
        case StdioTransport() as stdio:
            return StdioClientTransport(
                command=stdio.command,
                args=list(stdio.args),
                env=dict(stdio.env) or None,
                keep_alive=False,
                log_file=errlog,
            )
        case HttpTransport() | SseTransport() as remote:
            return _remote_target(remote, _stored_auth(remote, tokens))
        case MemoryTransport() as memory:
            return _import_server(memory.module, memory.attribute)


STDERR_LINE_BYTES = 8192
"""The most of one stderr line a relayed log line carries; a child that writes no newline is
logged in pieces of this size rather than buffered without end."""


@contextmanager
def _child_stderr(upstream: Upstream, secrets: Secrets) -> Generator[TextIO | None]:
    """A pipe for an stdio child's stderr, read into the app log under its name (#42).

    The SDK hands a child the stream it is given as its stderr, which needs a real file
    descriptor: this is the writing end of a pipe, and a thread reads the other end line by
    line into ``child_log``, every resolved ``${VAR}`` written back as the reference, until
    the end of the file. The block runs for the connect, after which the Daemon's own copy of
    the writing end is closed, so the end comes when the child, and anything the child
    started with its stderr, exits, and not before. Nothing but an stdio Upstream gets one.
    In the CLI's process nothing handles the app log, so what a scan's child says is dropped.
    """
    transport = upstream.transport
    if not isinstance(transport, StdioTransport):
        yield None
        return
    reading, writing = os.pipe()
    try:
        threading.Thread(
            target=_relay_stderr,
            args=(upstream.name, transport, secrets, reading),
            name=f"stderr:{upstream.name}",
            daemon=True,
        ).start()
    except BaseException:
        os.close(reading)
        os.close(writing)
        raise
    with os.fdopen(writing, "w", encoding="utf-8") as errlog:
        yield errlog


def _relay_stderr(name: str, transport: StdioTransport, secrets: Secrets, descriptor: int) -> None:
    with os.fdopen(descriptor, encoding="utf-8", errors="replace") as lines:
        while line := lines.readline(STDERR_LINE_BYTES):
            if text := line.rstrip():
                child_log.info("%s: %s", name, secrets.concealed(text, transport))


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


# --- OAuth: how an Upstream reached by URL is authorized ---------------------------------------

CLIENT_NAME = "mcpshape"
"""What mcpshape registers itself as with a provider, and what the user sees on the consent
screen."""

CALLBACK_HOST = "127.0.0.1"
CALLBACK_PATH = "/callback"
CALLBACK_TIMEOUT = 300.0
"""Seconds the loopback callback waits for the browser before the login is given up on."""

DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
"""RFC 8628's grant type, for the machines where no browser can open."""

DEVICE_PATIENCE = 300.0
"""Seconds device-code pairing waits for the user to approve at the provider."""

OK, CREATED = 200, 201
"""The two answers a provider gives to a request that worked."""

NO_REDIRECT = "http://localhost/callback"
"""The redirect the Daemon registers as, and never uses: nothing redirects to a Daemon."""


class OAuthError(Exception):
    """A login with the provider could not be completed. Names endpoints, never a token."""


class LoginNeededError(UpstreamTargetError):
    """An OAuth Upstream cannot be reached until the user logs in again, from the CLI.

    Its own type, so ``upstream sync`` can tell this failure, the one a new login fixes,
    from every other reason a scan can fail; ``_concealing`` keeps the type.
    """

    def __init__(self, upstream: str) -> None:
        super().__init__(
            f"the Upstream {upstream} has no usable OAuth token, so nothing was sent; "
            f"log in with: mcpshape upstream sync {upstream}"
        )


class _TokenStorage(TokenStorage):
    """The MCP SDK's token storage over ``mcpshape.tokens``: one encrypted file per Upstream.

    Everything the login produced lives in one document, so the token set and the dynamic
    client registration are written and read together and neither can outlive the other.

    ``expires_in`` is a duration the provider measured from the moment it answered, which says
    nothing after a Daemon restart, so the moment it runs out is what is stored and the
    duration is recomputed from it on every read.
    """

    TOKENS = "tokens"
    EXPIRES_AT = "expires_at"
    CLIENT = "client"

    def __init__(self, tokens: Tokens) -> None:
        self._tokens = tokens

    async def get_tokens(self) -> OAuthToken | None:
        document = self._tokens.read() or {}
        stored: Any = document.get(self.TOKENS)
        if stored is None:
            return None
        token = OAuthToken.model_validate(stored)
        expires_at: Any = document.get(self.EXPIRES_AT)
        if expires_at is not None:
            token.expires_in = max(int(float(expires_at) - time.time()), 0)
        return token

    async def set_tokens(self, tokens: OAuthToken) -> None:
        document = self._tokens.read() or {}
        document[self.TOKENS] = tokens.model_dump(mode="json", exclude_none=True)
        document[self.EXPIRES_AT] = (
            time.time() + tokens.expires_in if tokens.expires_in is not None else None
        )
        self._tokens.write(document)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        document = self._tokens.read() or {}
        stored: Any = document.get(self.CLIENT)
        return None if stored is None else OAuthClientInformationFull.model_validate(stored)

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        document = self._tokens.read() or {}
        document[self.CLIENT] = client_info.model_dump(mode="json", exclude_none=True)
        self._tokens.write(document)


def granted_scopes(tokens: Tokens) -> list[str] | None:
    """The scopes the stored token set was granted, or nothing when the provider did not say
    or no token set is stored.

    The provider's word (``scope`` in the token answer), not what was asked for, which the
    SDK's browser flow overwrites with what discovery found (#51). A provider that answers
    without ``scope`` granted what was asked (RFC 6749, 5.1). Never a token value.
    """
    document = tokens.read() or {}
    stored: Any = document.get(_TokenStorage.TOKENS)
    if stored is None:
        return None
    return str(OAuthToken.model_validate(stored).scope or "").split() or None


def logged_in(tokens: Tokens) -> bool:
    """Whether a token set is stored, as opposed to only the client registration.

    The SDK writes the registration first, so the file is there from the moment a login
    starts; the token set is what says the login finished (#56). A file that cannot be read
    raises as ``Tokens.read`` does, naming files and never a value: a key that no longer
    matches is for the user to hear about, not to log in over.
    """
    document = tokens.read() or {}
    return document.get(_TokenStorage.TOKENS) is not None


class _Provider(OAuthClientProvider):
    """The MCP SDK's OAuth client, told when the stored token runs out.

    The SDK loads a token without its expiry, so a token stored long enough ago to be dead
    would be sent once and rejected. The storage hands back the duration that is left, and
    this turns it into the moment the token stops being sent.
    """

    async def _initialize(self) -> None:
        await super()._initialize()
        if self.context.current_tokens is not None:
            self.context.update_token_expiry(self.context.current_tokens)


type _Redirect = Callable[[str], Awaitable[None]]
type _Callback = Callable[[], Awaitable[AuthorizationCodeResult]]


def _remote_target(
    remote: HttpTransport | SseTransport, auth: httpx2.Auth | None
) -> ClientTransport:
    if isinstance(remote, HttpTransport):
        return StreamableHttpTransport(remote.url, auth=auth)
    return SSETransport(remote.url, auth=auth)


def _stored_auth(remote: HttpTransport | SseTransport, tokens: Tokens | None) -> httpx2.Auth | None:
    """The Upstream's stored login, or nothing when it needs none.

    In the Daemon there is no browser and no loopback callback: an Upstream with no usable
    token fails to connect, naming the command that logs it in, which the state machine turns
    into ``unavailable`` with that reason.
    """
    if remote.auth is None:
        return None
    if tokens is None:
        msg = "an OAuth Upstream is reached from where no stored login can be read"
        raise UpstreamTargetError(msg)
    if not logged_in(tokens):
        raise LoginNeededError(tokens.upstream)
    redirect, callback = _refusing(tokens.upstream)
    return _provider(remote, tokens, NO_REDIRECT, redirect, callback)


def _refusing(upstream: str) -> tuple[_Redirect, _Callback]:
    """Handlers that say what to run instead of opening a browser nobody is sitting at."""

    async def redirect(_url: str) -> None:
        raise LoginNeededError(upstream)

    async def callback() -> AuthorizationCodeResult:
        raise LoginNeededError(upstream)

    return redirect, callback


def _provider(
    remote: HttpTransport | SseTransport,
    tokens: Tokens,
    redirect_uri: str,
    redirect: _Redirect,
    callback: _Callback,
) -> OAuthClientProvider:
    return _Provider(
        server_url=remote.url,
        client_metadata=OAuthClientMetadata(
            client_name=CLIENT_NAME,
            redirect_uris=[AnyUrl(redirect_uri)],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            scope=" ".join(remote.scopes) or None,
        ),
        storage=_TokenStorage(tokens),
        redirect_handler=redirect,
        callback_handler=callback,
    )


type Opener = Callable[[str], Awaitable[None]]
"""What is handed the provider's page to open, in the browser flow."""


async def login(  # noqa: PLR0913  # the three ways in are keywords, each with a default
    transport: Transport,
    secrets: Secrets,
    tokens: Tokens,
    announce: Callable[[str], None],
    *,
    device: bool = False,
    opener: Opener | None = None,
) -> None:
    """Log this Upstream in with its provider and keep what came back (stories 3 and 4).

    The browser flow opens the provider's page and receives the answer on a loopback port the
    provider redirects to. Device-code pairing instead prints a URI and a code to type there,
    for the machines where no browser can open, and needs a provider that offers it.

    Whatever comes back is written through the same token store the Daemon reads, so a Daemon
    started afterwards, or restarted later, uses it without asking again. ``announce`` is how
    the CLI is told what to do; nothing it is given is ever a token. ``opener`` is what the
    provider's page is handed to instead of this machine's browser: the ``/api`` flow (#16)
    hands it to the dashboard, which opens it wherever the user's browser is.
    """
    remote = _logging_in(secrets.expanded(transport))
    async with _concealing(transport, secrets, tokens):
        if device:
            await _device_login(remote, tokens, announce)
        else:
            await _browser_login(remote, tokens, announce, opener or _this_browser(announce))


def _this_browser(announce: Callable[[str], None]) -> Opener:
    async def open_here(authorization_url: str) -> None:
        announce("Opening your browser to finish the login.")
        webbrowser.open(authorization_url)

    return open_here


def _logging_in(transport: Transport) -> HttpTransport | SseTransport:
    if not isinstance(transport, HttpTransport | SseTransport) or transport.auth != "oauth":
        msg = 'only an Upstream reached by URL with auth = "oauth" has a login to perform'
        raise UpstreamTargetError(msg)
    return transport


async def _browser_login(
    remote: HttpTransport | SseTransport,
    tokens: Tokens,
    announce: Callable[[str], None],
    opener: Opener,
) -> None:
    """Open the provider's page and take the answer on a loopback port (story 3).

    The connect is what drives the flow: the Upstream answers the first request with a 401,
    and the SDK discovers the provider, registers, and exchanges the code from there.
    """
    port = find_available_port(host=CALLBACK_HOST)
    redirect_uri = f"http://{CALLBACK_HOST}:{port}{CALLBACK_PATH}"

    async def callback() -> AuthorizationCodeResult:
        return await _await_callback(port, remote.url)

    auth = _provider(remote, tokens, redirect_uri, opener, callback)
    async with Client(_remote_target(remote, auth)):
        announce("Logged in.")


async def _await_callback(port: int, mcp_url: str) -> AuthorizationCodeResult:
    """Serve the loopback callback until the browser reaches it, and say what it carried."""
    result = OAuthCallbackResult()
    ready = asyncio.Event()
    server = create_oauth_callback_server(
        port=port,
        host=CALLBACK_HOST,
        callback_path=CALLBACK_PATH,
        server_url=mcp_url,
        result_container=result,
        result_ready=cast("Any", ready),
    )
    serving = asyncio.create_task(server.serve())
    try:
        await asyncio.wait_for(ready.wait(), CALLBACK_TIMEOUT)
    except TimeoutError as exc:
        msg = f"the browser did not reach the callback within {CALLBACK_TIMEOUT:.0f} seconds"
        raise OAuthError(msg) from exc
    finally:
        server.should_exit = True
        with contextlib.suppress(asyncio.CancelledError):
            await serving
    if result.error is not None:
        raise OAuthError(str(result.error))
    return AuthorizationCodeResult(code=result.code or "", state=result.state, iss=result.iss)


async def _device_login(
    remote: HttpTransport | SseTransport, tokens: Tokens, announce: Callable[[str], None]
) -> None:
    """Pair by device code where the provider offers it (RFC 8628, story 4).

    The MCP SDK drives only the browser flow, so this speaks to the provider directly: it
    reads the endpoints out of the authorization server's metadata, registers the way the SDK
    would, asks for a device code, and polls the token endpoint until the user has approved.
    What it stores is what the browser flow stores, so nothing downstream can tell the two
    apart.
    """
    storage = _TokenStorage(tokens)
    async with httpx2.AsyncClient(follow_redirects=True) as http:
        metadata = await _authorization_server(http, remote.url)
        endpoint = metadata.get("device_authorization_endpoint")
        if not isinstance(endpoint, str):
            msg = (
                f"the provider behind {remote.url} offers no device-code pairing; "
                "log in from a machine with a browser instead"
            )
            raise OAuthError(msg)
        client = await _device_client(http, metadata, storage, remote.scopes)
        pairing = await _device_code(http, endpoint, client, remote.scopes)
        announce(f"Open {pairing['verification_uri']} and enter the code {pairing['user_code']}")
        answer = await _await_approval(http, str(metadata["token_endpoint"]), client, pairing)
        await storage.set_tokens(OAuthToken.model_validate(answer))
    announce("Logged in.")


async def _authorization_server(http: httpx2.AsyncClient, mcp_url: str) -> dict[str, Any]:
    """The provider's metadata for the Upstream at ``mcp_url``, found the way the SDK finds it.

    The protected resource's metadata names the authorization server; where there is none, the
    Upstream's own origin is the authorization server, as the 2025-03-26 spec had it.
    """
    origin = _origin(mcp_url)
    path = httpx2.URL(mcp_url).path.rstrip("/")
    resource = await _first_json(
        http,
        [
            f"{origin}/.well-known/oauth-protected-resource{path}",
            f"{origin}/.well-known/oauth-protected-resource",
        ],
    )
    servers: Any = (resource or {}).get("authorization_servers") or [origin]
    server = _origin(str(servers[0]))
    metadata = await _first_json(
        http,
        [
            f"{server}/.well-known/oauth-authorization-server",
            f"{server}/.well-known/openid-configuration",
        ],
    )
    if metadata is None or "token_endpoint" not in metadata:
        msg = f"{server} publishes no authorization server metadata"
        raise OAuthError(msg)
    return metadata


def _origin(url: str) -> str:
    parsed = httpx2.URL(url)
    return f"{parsed.scheme}://{parsed.netloc.decode()}"


async def _first_json(http: httpx2.AsyncClient, urls: Sequence[str]) -> dict[str, Any] | None:
    for url in urls:
        answer = await http.get(url)
        if answer.status_code == OK:
            document: dict[str, Any] = answer.json()
            return document
    return None


async def _device_client(
    http: httpx2.AsyncClient,
    metadata: dict[str, Any],
    storage: _TokenStorage,
    scopes: Sequence[str],
) -> OAuthClientInformationFull:
    """The registration to pair with: the stored one, or a fresh one asking for the grant."""
    stored = await storage.get_client_info()
    if stored is not None and DEVICE_GRANT in stored.grant_types:
        return stored
    endpoint = metadata.get("registration_endpoint")
    if not isinstance(endpoint, str):
        msg = "the provider registers no clients, so device-code pairing has nothing to pair"
        raise OAuthError(msg)
    answer = await http.post(
        endpoint,
        json={
            "client_name": CLIENT_NAME,
            "grant_types": [DEVICE_GRANT, "refresh_token"],
            "response_types": [],
            "token_endpoint_auth_method": "none",
            "scope": " ".join(scopes),
        },
    )
    if answer.status_code not in {OK, CREATED}:
        msg = f"{endpoint} refused to register mcpshape ({answer.status_code})"
        raise OAuthError(msg)
    client = OAuthClientInformationFull.model_validate(answer.json())
    await storage.set_client_info(client)
    return client


async def _device_code(
    http: httpx2.AsyncClient,
    endpoint: str,
    client: OAuthClientInformationFull,
    scopes: Sequence[str],
) -> dict[str, Any]:
    answer = await http.post(
        endpoint, data={"client_id": client.client_id, "scope": " ".join(scopes)}
    )
    if answer.status_code != OK:
        msg = f"{endpoint} refused to start device-code pairing ({answer.status_code})"
        raise OAuthError(msg)
    pairing: dict[str, Any] = answer.json()
    return pairing


async def _await_approval(
    http: httpx2.AsyncClient,
    token_endpoint: str,
    client: OAuthClientInformationFull,
    pairing: dict[str, Any],
) -> dict[str, Any]:
    """Ask the token endpoint for the pairing's token until the user has approved it.

    ``authorization_pending`` and ``slow_down`` are the provider saying "not yet" and "not so
    often"; anything else is the end of it.
    """
    interval = float(pairing.get("interval") or 5)
    deadline = time.monotonic() + DEVICE_PATIENCE
    while time.monotonic() < deadline:
        answer = await http.post(
            token_endpoint,
            data={
                "grant_type": DEVICE_GRANT,
                "device_code": pairing["device_code"],
                "client_id": client.client_id,
            },
        )
        body: dict[str, Any] = answer.json()
        if answer.status_code == OK:
            return body
        error = str(body.get("error"))
        if error == "slow_down":
            interval += 5
        elif error != "authorization_pending":
            msg = f"the provider refused the pairing: {error}"
            raise OAuthError(msg)
        await asyncio.sleep(interval)
    msg = f"nobody approved the pairing within {DEVICE_PATIENCE:.0f} seconds"
    raise OAuthError(msg)
