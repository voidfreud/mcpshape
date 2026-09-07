"""The FastMCP adapter: the only module that imports FastMCP (ADR 0001).

The rest of mcpshape sees three things: ``scan``, which turns an Upstream into a Catalog,
``proxy_app``, an ASGI app per Proxy that serves lists from what it is handed and forwards
calls to the Upstream under Catalog names through ``UpstreamConnection``, one Upstream's
shared client behind its lifecycle state machine, and ``run_stdio_bridge``, the stdio shim's
other half.

FastMCP's proxy components keep the backend name when they are copied under a new one, which
is how a renamed tool, resource, or prompt still reaches its Catalog item. The server name is
fixed when the server is built (the MCP SDK caches its identity), so a Proxy whose exposed
server name changes is rebuilt by its owner; ``ProxyApp.name`` says what it was built with.
"""

from __future__ import annotations

import importlib
import logging
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import mcp_types
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import ToolError
from fastmcp.server import create_proxy
from fastmcp.server.providers.base import Provider
from fastmcp.server.providers.proxy import (
    ProxyClient,
    ProxyPrompt,
    ProxyResource,
    ProxyTemplate,
    ProxyTool,
)
from mcp.shared.exceptions import MCPError
from pydantic import AnyUrl, PrivateAttr

from mcpshape.catalog import Catalog, Item
from mcpshape.model import MemoryTransport
from mcpshape.proxy import ArgumentMap, Exposed
from mcpshape.upstream import Connection, UpstreamUnavailableError

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
    from contextlib import AbstractAsyncContextManager

    from fastmcp.prompts import Prompt
    from fastmcp.resources import Resource, ResourceTemplate
    from fastmcp.server.context import Context
    from fastmcp.tools import Tool
    from fastmcp.tools.base import ToolResult
    from pydantic import BaseModel
    from starlette.types import ASGIApp

    from mcpshape.model import Transport, Upstream
    from mcpshape.upstream import Clock, Status

MCP_PATH = "/mcp"

type ClientFactory = Callable[[], Awaitable[Client[Any]]]
"""What FastMCP calls to reach the Upstream for one call, read, or get."""


class UpstreamTargetError(Exception):
    """An Upstream's target cannot be reached the way its config describes."""


@dataclass(frozen=True)
class ProxyApp:
    """A Proxy as an ASGI app serving MCP at ``MCP_PATH``, plus the lifespan it needs.

    ``serve`` replaces what the Proxy exposes; Clients see the new set on their next request.
    The server ``name`` cannot change: build a new app for a new name.
    """

    name: str
    asgi: ASGIApp
    lifespan: Callable[[], AbstractAsyncContextManager[None]]
    serve: Callable[[Exposed], None]


class _Link:
    """One Upstream's client, opened and closed on its ``Connection``'s word.

    The Upstream's target is resolved on every open, not once at build time: a Proxy is served
    whether or not its Upstream can be reached, and an Upstream that comes back does so without
    a Daemon restart.
    """

    def __init__(self, transport: Transport) -> None:
        self._transport = transport
        self._open: AsyncExitStack | None = None
        self._client: Client[Any] | None = None

    @property
    def client(self) -> Client[Any]:
        if self._client is None:
            msg = "the Upstream connection is not open"
            raise UpstreamTargetError(msg)
        return self._client

    async def open(self) -> None:
        target = _resolve(self._transport)
        stack = AsyncExitStack()
        client: ProxyClient[Any] = ProxyClient(target)
        self._client = await stack.enter_async_context(client)
        self._open = stack

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
        clock: Clock | None = None,
        on_reconnect: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._link = _Link(upstream.transport)
        self._connection = Connection(
            upstream.name, self._link, upstream.lifecycle, clock, on_reconnect=on_reconnect
        )

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
    provider = _CatalogProvider(connection.client)
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
    return ProxyApp(name=name, asgi=app, lifespan=lifespan, serve=serve)


async def scan(transport: Transport) -> Catalog:
    """Everything the Upstream behind ``transport`` advertises right now."""
    target = _resolve(transport)
    async with Client(target) as client:
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


def run_stdio_bridge(url: str) -> None:
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


class _CuratedTool(ProxyTool):
    """A ProxyTool whose exposed arguments are mapped back onto the Catalog tool's on each call."""

    _arguments: ArgumentMap = PrivateAttr(default_factory=ArgumentMap)

    def map_arguments(self, arguments: ArgumentMap) -> None:
        self._arguments = arguments

    async def run(self, arguments: dict[str, Any], context: Context | None = None) -> ToolResult:
        return await super().run(self._arguments.to_catalog(arguments), context)


class _CatalogProvider(Provider):
    """Serves lists from what it is handed and forwards calls, reads, and gets to the Upstream.

    Each component is built under its Catalog name and then copied under its exposed name, so
    FastMCP keeps the Catalog name as the backend name to forward with.
    """

    def __init__(self, client_factory: ClientFactory) -> None:
        super().__init__()
        self._client_factory = client_factory
        self._tools: list[Tool] = []
        self._resources: list[Resource] = []
        self._templates: list[ResourceTemplate] = []
        self._prompts: list[Prompt] = []

    def serve(self, exposed: Exposed) -> None:
        factory = self._client_factory
        catalog = exposed.catalog
        self._tools = [
            self._tool(factory, exposed, name, raw) for name, raw in catalog.tools.items()
        ]
        self._resources = [
            _renamed(
                ProxyResource.from_mcp_resource(  # pyright: ignore[reportUnknownMemberType]  # FastMCP's factory type is unparameterised
                    factory, mcp_types.Resource.model_validate(raw)
                ),
                exposed.origin(Item("resource", uri)),
                uri,
                "uri",
                AnyUrl(uri),
            )
            for uri, raw in catalog.resources.items()
        ]
        self._templates = [
            _renamed(
                ProxyTemplate.from_mcp_template(  # pyright: ignore[reportUnknownMemberType]  # FastMCP's factory type is unparameterised
                    factory, mcp_types.ResourceTemplate.model_validate(raw)
                ),
                exposed.origin(Item("resource_template", uri)),
                uri,
                "uri_template",
                uri,
            )
            for uri, raw in catalog.resource_templates.items()
        ]
        self._prompts = [
            _renamed(
                ProxyPrompt.from_mcp_prompt(  # pyright: ignore[reportUnknownMemberType]  # FastMCP's factory type is unparameterised
                    factory, mcp_types.Prompt.model_validate(raw)
                ),
                exposed.origin(Item("prompt", name)),
                name,
                "name",
                name,
            )
            for name, raw in catalog.prompts.items()
        ]

    @staticmethod
    def _tool(factory: ClientFactory, exposed: Exposed, name: str, raw: dict[str, Any]) -> Tool:
        origin = exposed.origin(Item("tool", name))
        built = _CuratedTool.from_mcp_tool(  # pyright: ignore[reportUnknownMemberType]  # FastMCP's factory type is unparameterised
            factory, mcp_types.Tool.model_validate({**raw, "name": origin})
        )
        tool = _renamed(cast("_CuratedTool", built), origin, name, "name", name)
        tool.map_arguments(exposed.arguments.get(name, ArgumentMap()))
        return tool

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


def _resolve(transport: Transport) -> FastMCP[Any]:
    match transport:
        case MemoryTransport():
            return _import_server(transport.module, transport.attribute)
        case _:
            msg = f"{transport.transport} Upstreams are not supported yet"
            raise UpstreamTargetError(msg)


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
