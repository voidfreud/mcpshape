"""The Daemon's ASGI app: each Upstream's Proxies at their own paths, never merged (ADR 0002).

Every Proxy serves from its Upstream's accepted Catalog, curated by its Proxy file. Both are
files the CLI edits, so each Proxy re-reads them when they change, checked on every request.
Starting the Daemon rescans every Upstream, recording Drift rather than serving it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from starlette.applications import Starlette
from starlette.routing import Mount

from mcpshape import catalog as catalogs
from mcpshape.adapters.fastmcp import UpstreamTargetError, proxy_app, scan, server_name
from mcpshape.config import ConfigError, load_proxy, load_settings, load_upstreams, proxy_file
from mcpshape.model import DEFAULT_PROXY_NAME
from mcpshape.proxy import Exposed, OverrideError, expose

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path

    from starlette.types import Receive, Scope, Send

    from mcpshape.adapters.fastmcp import ProxyApp
    from mcpshape.model import Upstream

log = logging.getLogger("mcpshape.daemon")


class _Held:
    """A Proxy app's lifespan, held open by one task so it is entered and left in one context.

    FastMCP's lifespan sets context variables it must reset where it set them; entering it in a
    request task and leaving it in another would fail.
    """

    def __init__(self, app: ProxyApp) -> None:
        self._stop = asyncio.Event()
        self._ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._task = asyncio.create_task(self._hold(app))

    async def _hold(self, app: ProxyApp) -> None:
        try:
            async with app.lifespan():
                self._ready.set_result(None)
                await self._stop.wait()
        except Exception as exc:
            if not self._ready.done():
                self._ready.set_exception(exc)
            raise

    async def ready(self) -> None:
        await self._ready

    async def close(self) -> None:
        self._stop.set()
        try:
            await self._task
        except Exception:
            log.warning("a Proxy app did not shut down cleanly", exc_info=True)


class _Proxy:
    """One Proxy, re-reading its Catalog and Proxy file whenever either changes on disk.

    A change to what is exposed is served in place. A change to the exposed server name needs
    a new app, since a server's identity is fixed when it is built: the old app is closed after
    the new one is up, and Clients with an old session reconnect.
    """

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
        self._lock = asyncio.Lock()
        self._held: _Held | None = None
        self.app: ProxyApp = proxy_app(upstream, name, _nothing())

    async def start(self) -> None:
        self._held = _Held(self.app)
        await self._held.ready()

    async def stop(self) -> None:
        if self._held is not None:
            await self._held.close()
            self._held = None

    async def refresh(self) -> None:
        async with self._lock:
            stamp = tuple(_stamp(path) for path in self._sources)
            if stamp == self._stamp:
                return
            try:
                stored = catalogs.load_catalog(self._state_dir, self._upstream.name) or _empty()
                proxy = load_proxy(self._config_dir, self._upstream.name, self._name)
                exposed = expose(stored, proxy)
            except (catalogs.CatalogError, ConfigError, OverrideError):
                log.warning("Proxy %s keeps its last exposed set", self._label, exc_info=True)
                return
            self._stamp = stamp
            if server_name(self._upstream, self._name, exposed) == self.app.name:
                self.app.serve(exposed)
                return
            await self._rebuild(exposed)

    async def _rebuild(self, exposed: Exposed) -> None:
        previous = self._held
        self.app = proxy_app(self._upstream, self._name, exposed)
        await self.start()
        if previous is not None:
            await previous.close()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            await self.refresh()
        await self.app.asgi(scope, receive, send)


def _stamp(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


def _empty() -> catalogs.Catalog:
    return catalogs.Catalog(scanned_at=datetime.now(UTC))


def _nothing() -> Exposed:
    """What a Proxy exposes before its first refresh."""
    return Exposed(catalog=_empty(), name=None)


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
        for proxy in proxies.values():
            await proxy.start()
        try:
            yield
        finally:
            for proxy in reversed(proxies.values()):
                await proxy.stop()

    return Starlette(routes=routes, lifespan=lifespan)


async def serve(app: Starlette, host: str, port: int, stop: asyncio.Event | None = None) -> None:
    """Serve ``app`` on ``host``:``port`` until ``stop`` is set or the process is signalled.

    The Daemon process's main loop. A cooperative stop lets the server close its socket;
    cancelling the task would leave it open.
    """
    import uvicorn  # noqa: PLC0415  # only the running Daemon needs a server

    config = uvicorn.Config(app, host=host, port=port, log_level="warning", lifespan="on")
    server = uvicorn.Server(config)

    async def stop_when_asked() -> None:
        if stop is not None:
            await stop.wait()
            server.should_exit = True

    stopper = asyncio.create_task(stop_when_asked())
    try:
        await server.serve()
    finally:
        stopper.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await stopper


def run(config_dir: Path, state_dir: Path) -> None:
    """Build the Daemon app from ``config_dir`` and serve it on the configured address."""
    daemon = load_settings(config_dir).daemon
    asyncio.run(serve(build_app(config_dir, state_dir), daemon.host, daemon.port))
