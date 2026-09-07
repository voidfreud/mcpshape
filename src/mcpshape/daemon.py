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

    Each Upstream's ``default`` Proxy is served at ``/<upstream>/mcp`` and at
    ``/<upstream>/default/mcp``.
    """
    proxies = {upstream.name: proxy_app(upstream) for upstream in load_upstreams(config_dir)}
    routes = [
        route
        for name, proxy in proxies.items()
        for route in (
            Mount(f"/{name}/{DEFAULT_PROXY_NAME}", app=proxy.asgi),
            Mount(f"/{name}", app=proxy.asgi),
        )
    ]

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncGenerator[None]:
        async with contextlib.AsyncExitStack() as stack:
            for proxy in proxies.values():
                await stack.enter_async_context(proxy.lifespan())
            yield

    return Starlette(routes=routes, lifespan=lifespan)
