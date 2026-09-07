"""The Daemon's ASGI app: each Upstream's Proxies at their own paths, never merged (ADR 0002).

Every Proxy serves from its Upstream's accepted Catalog, curated by its Proxy file. Both are
files the CLI edits, so each Proxy re-reads them when they change, checked on every request.
Starting the Daemon rescans every Upstream, recording Drift rather than serving it.
"""

from __future__ import annotations

import contextlib
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from starlette.applications import Starlette
from starlette.routing import Mount

from mcpshape import catalog as catalogs
from mcpshape.adapters.fastmcp import UpstreamTargetError, proxy_app, scan
from mcpshape.config import ConfigError, load_proxy, load_upstreams, proxy_file
from mcpshape.model import DEFAULT_PROXY_NAME
from mcpshape.proxy import exposed_catalog

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path

    from starlette.types import Receive, Scope, Send

    from mcpshape.adapters.fastmcp import ProxyApp
    from mcpshape.model import Upstream

log = logging.getLogger("mcpshape.daemon")


class _Proxy:
    """One Proxy, re-reading its Catalog and Proxy file whenever either changes on disk."""

    def __init__(self, config_dir: Path, state_dir: Path, upstream: Upstream, name: str) -> None:
        self._sources = (
            catalogs.catalog_path(state_dir, upstream.name),
            proxy_file(config_dir, upstream.name, name),
        )
        self._label = f"{upstream.name}/{name}"
        self._config_dir, self._state_dir, self._upstream, self._name = (
            config_dir,
            state_dir,
            upstream,
            name,
        )
        self._stamp: tuple[tuple[int, int] | None, ...] | None = None
        self.app: ProxyApp = proxy_app(upstream, name, _empty())

    def refresh(self) -> None:
        stamp = tuple(_stamp(path) for path in self._sources)
        if stamp == self._stamp:
            return
        try:
            stored = catalogs.load_catalog(self._state_dir, self._upstream.name) or _empty()
            proxy = load_proxy(self._config_dir, self._upstream.name, self._name)
        except (catalogs.CatalogError, ConfigError):
            log.warning("Proxy %s keeps its last exposed set", self._label, exc_info=True)
            return
        self._stamp = stamp
        self.app.serve(exposed_catalog(stored, proxy))

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            self.refresh()
        await self.app.asgi(scope, receive, send)


def _stamp(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


def _empty() -> catalogs.Catalog:
    return catalogs.Catalog(scanned_at=datetime.now(UTC))


async def rescan(state_dir: Path, upstream: Upstream) -> None:
    """Scan ``upstream`` into its Catalog on a first scan, else record Drift. Never raises."""
    try:
        catalogs.record_scan(state_dir, upstream.name, await scan(upstream.transport))
    except (UpstreamTargetError, catalogs.CatalogError):
        log.warning("Upstream %s could not be scanned", upstream.name, exc_info=True)


def build_app(config_dir: Path, state_dir: Path) -> Starlette:
    """The Daemon app for the Upstreams registered under ``config_dir``.

    Every Proxy is served at ``/<upstream>/<proxy>/mcp``; the ``default`` Proxy also at
    ``/<upstream>/mcp``.
    """
    upstreams = load_upstreams(config_dir)
    proxies = {
        (upstream.name, proxy_name): _Proxy(config_dir, state_dir, upstream, proxy_name)
        for upstream in upstreams
        for proxy_name in upstream.proxies
    }
    routes = [
        Mount(f"/{name}/{proxy_name}", app=proxy) for (name, proxy_name), proxy in proxies.items()
    ]
    routes += [
        Mount(f"/{name}", app=proxy)
        for (name, proxy_name), proxy in proxies.items()
        if proxy_name == DEFAULT_PROXY_NAME
    ]

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncGenerator[None]:
        for upstream in upstreams:
            await rescan(state_dir, upstream)
        async with contextlib.AsyncExitStack() as stack:
            for proxy in proxies.values():
                await stack.enter_async_context(proxy.app.lifespan())
            yield

    return Starlette(routes=routes, lifespan=lifespan)
