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

from mcpshape.model import MemoryTarget

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable
    from contextlib import AbstractAsyncContextManager

    from starlette.types import ASGIApp

    from mcpshape.model import Upstream, UpstreamTarget

MCP_PATH = "/mcp"


class UpstreamTargetError(Exception):
    """An Upstream's target cannot be reached the way its config describes."""


@dataclass(frozen=True)
class ProxyApp:
    """A Proxy as an ASGI app serving MCP at ``MCP_PATH``, plus the lifespan it needs."""

    asgi: ASGIApp
    lifespan: Callable[[], AbstractAsyncContextManager[None]]


def proxy_app(upstream: Upstream) -> ProxyApp:
    """A Proxy of ``upstream`` that forwards everything the Upstream advertises."""
    server = _resolve(upstream.target)
    proxy = create_proxy(server, name=upstream.name)
    app = proxy.http_app(path=MCP_PATH)

    @asynccontextmanager
    async def lifespan() -> AsyncGenerator[None]:
        async with app.router.lifespan_context(app):
            yield

    return ProxyApp(asgi=app, lifespan=lifespan)


def _resolve(target: UpstreamTarget) -> FastMCP[Any]:
    match target:
        case MemoryTarget(import_path=import_path):
            return _import_server(import_path)


def _import_server(import_path: str) -> FastMCP[Any]:
    module_name, _, attribute = import_path.partition(":")
    try:
        server: object = getattr(importlib.import_module(module_name), attribute)
    except (ImportError, AttributeError) as exc:
        msg = f"cannot import {import_path}: {exc}"
        raise UpstreamTargetError(msg) from exc
    if not isinstance(server, FastMCP):
        msg = f"{import_path} is not an in-memory MCP server"
        raise UpstreamTargetError(msg)
    return cast("FastMCP[Any]", server)
