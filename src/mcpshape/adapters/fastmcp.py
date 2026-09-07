"""The FastMCP adapter: the only module that imports FastMCP (ADR 0001).

The rest of mcpshape sees an ASGI app per Proxy and the lifespan that must run around it.
"""

from __future__ import annotations

import importlib
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from fastmcp import FastMCP
from fastmcp.server import create_proxy

from mcpshape.model import MemoryTransport

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable
    from contextlib import AbstractAsyncContextManager

    from starlette.types import ASGIApp

    from mcpshape.model import Transport, Upstream

MCP_PATH = "/mcp"


class UpstreamTargetError(Exception):
    """An Upstream's target cannot be reached the way its config describes."""


@dataclass(frozen=True)
class ProxyApp:
    """A Proxy as an ASGI app serving MCP at ``MCP_PATH``, plus the lifespan it needs."""

    asgi: ASGIApp
    lifespan: Callable[[], AbstractAsyncContextManager[None]]


def proxy_app(upstream: Upstream, proxy_name: str) -> ProxyApp:
    """The Proxy ``proxy_name`` of ``upstream``, forwarding everything the Upstream advertises."""
    server = _resolve(upstream.transport)
    proxy = create_proxy(server, name=f"{upstream.name}/{proxy_name}")
    app = proxy.http_app(path=MCP_PATH)

    @asynccontextmanager
    async def lifespan() -> AsyncGenerator[None]:
        async with app.router.lifespan_context(app):
            yield

    return ProxyApp(asgi=app, lifespan=lifespan)


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
