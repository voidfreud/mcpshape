"""The Daemon's ASGI app: each Upstream's Proxies at their own paths, never merged (ADR 0002)."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from starlette.applications import Starlette
from starlette.routing import Mount

from mcpshape.adapters.fastmcp import proxy_app
from mcpshape.config import load_upstreams
from mcpshape.model import DEFAULT_PROXY_NAME

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path


def build_app(config_dir: Path) -> Starlette:
    """The Daemon app for the Upstreams registered under ``config_dir``.

    Every Proxy is served at ``/<upstream>/<proxy>/mcp``; the ``default`` Proxy also at
    ``/<upstream>/mcp``.
    """
    proxies = {
        (upstream.name, proxy_name): proxy_app(upstream, proxy_name)
        for upstream in load_upstreams(config_dir)
        for proxy_name in upstream.proxies
    }
    routes = [
        Mount(f"/{name}/{proxy_name}", app=proxy.asgi)
        for (name, proxy_name), proxy in proxies.items()
    ]
    routes += [
        Mount(f"/{name}", app=proxy.asgi)
        for (name, proxy_name), proxy in proxies.items()
        if proxy_name == DEFAULT_PROXY_NAME
    ]

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncGenerator[None]:
        async with contextlib.AsyncExitStack() as stack:
            for proxy in proxies.values():
                await stack.enter_async_context(proxy.lifespan())
            yield

    return Starlette(routes=routes, lifespan=lifespan)
