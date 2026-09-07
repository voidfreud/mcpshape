"""The FastMCP adapter: the only module that imports FastMCP (ADR 0001).

The rest of mcpshape sees two things: ``scan``, which turns an Upstream into a Catalog, and
``proxy_app``, an ASGI app per Proxy that serves lists from a Catalog it is handed and
forwards calls to the Upstream.
"""

from __future__ import annotations

import importlib
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import mcp_types
from fastmcp import Client, FastMCP
from fastmcp.server.providers.base import Provider
from fastmcp.server.providers.proxy import (
    ProxyClient,
    ProxyPrompt,
    ProxyResource,
    ProxyTemplate,
    ProxyTool,
)
from mcp.shared.exceptions import MCPError

from mcpshape.catalog import Catalog
from mcpshape.model import MemoryTransport

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
    from contextlib import AbstractAsyncContextManager

    from fastmcp.prompts import Prompt
    from fastmcp.resources import Resource, ResourceTemplate
    from fastmcp.tools import Tool
    from pydantic import BaseModel
    from starlette.types import ASGIApp

    from mcpshape.model import Transport, Upstream

MCP_PATH = "/mcp"


class UpstreamTargetError(Exception):
    """An Upstream's target cannot be reached the way its config describes."""


@dataclass(frozen=True)
class ProxyApp:
    """A Proxy as an ASGI app serving MCP at ``MCP_PATH``, plus the lifespan it needs.

    ``serve`` replaces what the Proxy exposes; Clients see the new Catalog on their next
    request.
    """

    asgi: ASGIApp
    lifespan: Callable[[], AbstractAsyncContextManager[None]]
    serve: Callable[[Catalog], None]


def proxy_app(upstream: Upstream, proxy_name: str, catalog: Catalog) -> ProxyApp:
    """The Proxy ``proxy_name`` of ``upstream``, exposing ``catalog`` and forwarding calls."""
    target = _resolve(upstream.transport)
    base: ProxyClient[Any] = ProxyClient(target)
    provider = _CatalogProvider(base.new)
    server = FastMCP(name=f"{upstream.name}/{proxy_name}")
    server.add_provider(provider)
    app = server.http_app(path=MCP_PATH)

    def serve(catalog: Catalog) -> None:
        provider.serve(catalog)
        server.instructions = catalog.instructions

    @asynccontextmanager
    async def lifespan() -> AsyncGenerator[None]:
        async with app.router.lifespan_context(app):
            yield

    serve(catalog)
    return ProxyApp(asgi=app, lifespan=lifespan, serve=serve)


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


class _CatalogProvider(Provider):
    """Serves lists from a Catalog and forwards calls, reads, and gets to the Upstream."""

    def __init__(self, client_factory: Callable[[], Client[Any]]) -> None:
        super().__init__()
        self._client_factory = client_factory
        self._tools: list[Tool] = []
        self._resources: list[Resource] = []
        self._templates: list[ResourceTemplate] = []
        self._prompts: list[Prompt] = []

    def serve(self, catalog: Catalog) -> None:
        factory = self._client_factory
        self._tools = [
            ProxyTool.from_mcp_tool(  # pyright: ignore[reportUnknownMemberType]  # FastMCP's factory type is unparameterised
                factory, mcp_types.Tool.model_validate(raw)
            )
            for raw in catalog.tools.values()
        ]
        self._resources = [
            ProxyResource.from_mcp_resource(  # pyright: ignore[reportUnknownMemberType]  # FastMCP's factory type is unparameterised
                factory, mcp_types.Resource.model_validate(raw)
            )
            for raw in catalog.resources.values()
        ]
        self._templates = [
            ProxyTemplate.from_mcp_template(  # pyright: ignore[reportUnknownMemberType]  # FastMCP's factory type is unparameterised
                factory, mcp_types.ResourceTemplate.model_validate(raw)
            )
            for raw in catalog.resource_templates.values()
        ]
        self._prompts = [
            ProxyPrompt.from_mcp_prompt(  # pyright: ignore[reportUnknownMemberType]  # FastMCP's factory type is unparameterised
                factory, mcp_types.Prompt.model_validate(raw)
            )
            for raw in catalog.prompts.values()
        ]

    async def _list_tools(self) -> Sequence[Tool]:
        return self._tools

    async def _list_resources(self) -> Sequence[Resource]:
        return self._resources

    async def _list_resource_templates(self) -> Sequence[ResourceTemplate]:
        return self._templates

    async def _list_prompts(self) -> Sequence[Prompt]:
        return self._prompts


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
