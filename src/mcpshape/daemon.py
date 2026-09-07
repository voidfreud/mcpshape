"""The Daemon's ASGI app: each Upstream's Proxies at their own paths, never merged (ADR 0002).

Every Proxy serves from its Upstream's accepted Catalog, curated by its Proxy file. Both are
files the CLI edits, so each Proxy re-reads them when they change, checked on every request.
Starting the Daemon rescans every Upstream, recording Drift rather than serving it.

One connection per Upstream is built here and shared by all of that Upstream's Proxies and
every Client (story 74); the lifespan warms it, times it, and lets it go. ``/api/status`` is
the live state the CLI and, later, the dashboard read: nothing here is configuration, which is
read from files whether the Daemon runs or not.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from mcpshape import catalog as catalogs
from mcpshape.adapters.fastmcp import UpstreamConnection, proxy_app, scan, server_name
from mcpshape.config import (
    ConfigError,
    load_proxy,
    load_settings,
    load_upstreams,
    proxy_code_file,
    proxy_file,
    secrets_for,
)
from mcpshape.connection import SystemClock, TimedOutError, bounded
from mcpshape.hooks import UserCodeError, load_user_code
from mcpshape.model import DEFAULT_PROXY_NAME, CapError, CapSettings
from mcpshape.proxy import Exposed, OverrideError, cap, expose

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable
    from pathlib import Path

    from starlette.requests import Request
    from starlette.routing import BaseRoute
    from starlette.types import ASGIApp, Receive, Scope, Send

    from mcpshape.adapters.fastmcp import ProxyApp
    from mcpshape.catalog import Catalog
    from mcpshape.connection import Clock
    from mcpshape.model import Upstream
    from mcpshape.secrets import Secrets

log = logging.getLogger("mcpshape.daemon")

STATUS_PATH = "/api/status"
"""One of two management routes so far: live state, which #16 grows into the management API."""

SHUTDOWN_PATH = "/api/shutdown"
"""What ``daemon down`` posts to: sets the stop event ``serve`` is watching."""


def is_loopback(host: str) -> bool:
    """Whether ``host`` is reachable only from this machine."""
    return host in {"127.0.0.1", "::1", "localhost"} or host.startswith("127.")


class _BearerAuth:
    """Requires ``Authorization: Bearer <token>`` on every HTTP request when a token is set.

    Wraps every route: Proxies, the management API, and, later, the dashboard. Never logs the
    token itself, given or expected.
    """

    def __init__(self, app: ASGIApp, token: str) -> None:
        self._app = app
        self._token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self._authorized(scope):
            await self._app(scope, receive, send)
            return
        response = JSONResponse({"error": "a bearer token is required"}, status_code=401)
        await response(scope, receive, send)

    def _authorized(self, scope: Scope) -> bool:
        headers: dict[bytes, bytes] = dict(scope.get("headers") or ())
        given: bytes = headers.get(b"authorization", b"")
        return given == f"Bearer {self._token}".encode("latin-1")


def _authed(app: ASGIApp, token: str | None) -> ASGIApp:
    return _BearerAuth(app, token) if token else app


def _middleware(token: str | None) -> list[Middleware]:
    return [Middleware(_BearerAuth, token=token)] if token else []


class ProxyState(BaseModel):
    """One Proxy as the Daemon sees it right now."""

    name: str
    health: str
    detail: str | None = None


class UpstreamState(BaseModel):
    """One Upstream's connection as the Daemon sees it right now."""

    name: str
    state: str
    seconds: float
    error: str | None = None
    proxies: list[ProxyState] = Field(default_factory=list[ProxyState])


class LiveState(BaseModel):
    """What ``/api/status`` answers. The CLI reads it back through the same model."""

    upstreams: list[UpstreamState] = Field(default_factory=list[UpstreamState])


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
    """One Proxy, re-reading its Catalog, Proxy file, and Python file whenever one changes.

    A change to what is exposed is served in place. A change to the exposed server name needs
    a new app, since a server's identity is fixed when it is built: the old app is closed after
    the new one is up, and Clients with an old session reconnect. A file that cannot be read,
    applied, or loaded marks the Proxy unhealthy: it keeps advertising its last exposed set
    and every call errors naming the Proxy and the reason, and nothing reaches the Upstream.
    """

    def __init__(  # noqa: PLR0913, PLR0917  # every one of these is state the Proxy needs
        self,
        config_dir: Path,
        state_dir: Path,
        upstream: Upstream,
        name: str,
        connection: UpstreamConnection,
        global_caps: CapSettings,
    ) -> None:
        self._code_path = proxy_code_file(config_dir, upstream.name, name)
        self._sources = (
            catalogs.catalog_path(state_dir, upstream.name),
            proxy_file(config_dir, upstream.name, name),
            self._code_path,
        )
        self._label = f"{upstream.name}/{name}"
        self._config_dir, self._state_dir, self._upstream, self._name = (
            config_dir,
            state_dir,
            upstream,
            name,
        )
        self._global_caps = global_caps
        self._stamp: tuple[tuple[int, int] | None, ...] | None = None
        self._connection = connection
        self._lock = asyncio.Lock()
        self._held: _Held | None = None
        self.health = "ok"
        self.detail: str | None = None
        self.app: ProxyApp = proxy_app(upstream, name, _nothing(), connection)

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
            self._stamp = stamp
            try:
                stored = catalogs.load_catalog(self._state_dir, self._upstream.name) or _empty()
                proxy = load_proxy(self._config_dir, self._upstream.name, self._name)
                code = load_user_code(self._code_path, self._label)
                upstream_caps = self._upstream.caps.over(
                    self._global_caps, f"Upstream {self._upstream.name}"
                )
                proxy_caps = proxy.caps.over(upstream_caps, f"Proxy {self._label}")
                exposed = cap(expose(stored, proxy, code), proxy_caps, proxy)
            except (
                catalogs.CatalogError,
                ConfigError,
                OverrideError,
                UserCodeError,
                CapError,
            ) as exc:
                log.warning(
                    "Proxy %s is unhealthy and keeps its last exposed set: %s",
                    self._label,
                    exc,
                    exc_info=True,
                )
                self.health, self.detail = "unhealthy", str(exc)
                self.app.fail(str(exc))
                return
            self.health, self.detail = "ok", None
            if server_name(self._upstream, self._name, exposed) == self.app.name:
                self.app.serve(exposed)
                return
            await self._rebuild(exposed)

    async def _rebuild(self, exposed: Exposed) -> None:
        previous = self._held
        self.app = proxy_app(self._upstream, self._name, exposed, self._connection)
        await self.start()
        if previous is not None:
            await previous.close()

    async def state(self) -> ProxyState:
        await self.refresh()
        return ProxyState(name=self._name, health=self.health, detail=self.detail)

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


async def rescan(state_dir: Path, secrets: Secrets, upstream: Upstream) -> None:
    """Scan ``upstream`` into its Catalog on a first scan, else record Drift. Never raises.

    This is the Daemon's own start-up scan, which reaches an Upstream nothing is connected to
    yet. A reconnect looks again over the connection it already has (story 11). Recording
    holds the Upstream's lock across its read-then-write (#21), which can wait behind a
    concurrent ``upstream sync``; a thread keeps that wait off the Daemon's own event loop.
    """
    try:
        observed = await scan(upstream.transport, secrets)
        await asyncio.to_thread(catalogs.record_scan, state_dir, upstream.name, observed)
    except Exception:  # an Upstream that cannot be reached must not keep the Daemon from starting
        log.warning("Upstream %s could not be scanned", upstream.name, exc_info=True)


async def record_observation(state_dir: Path, name: str, observed: Catalog) -> None:
    """Keep what a reconnected Upstream advertises: its first Catalog, or the Drift since."""
    await asyncio.to_thread(catalogs.record_scan, state_dir, name, observed)


async def _bounded_rescan(
    state_dir: Path, secrets: Secrets, upstream: Upstream, clock: Clock
) -> None:
    """``rescan``, bounded by ``upstream``'s own ``connect_timeout`` on ``clock`` (#20).

    An Upstream whose connect hangs must not hold up the Daemon's start, nor any other
    Upstream's: this is awaited concurrently with every other Upstream's bounded rescan, and a
    scan that times out is logged and skipped, exactly like one that fails outright.
    """
    try:
        await bounded(
            rescan(state_dir, secrets, upstream), upstream.lifecycle.connect_timeout, clock
        )
    except TimedOutError:
        log.warning(
            "Upstream %s could not be scanned within its connect_timeout of %.0fs",
            upstream.name,
            upstream.lifecycle.connect_timeout,
        )


@dataclass(frozen=True)
class DaemonApp:
    """Every ASGI app the Daemon serves: the main one, and one per Proxy port override.

    ``stop`` is what ``/api/shutdown`` sets and what ``serve`` watches to close its sockets.
    """

    main: Starlette
    extra: dict[int, ASGIApp]
    stop: asyncio.Event


def build_app(
    config_dir: Path,
    state_dir: Path,
    clock: Clock | None = None,
    token: str | None = None,
) -> DaemonApp:
    """The Daemon apps for the Upstreams registered under ``config_dir``.

    Every Proxy is served at ``/<upstream>/<proxy>/mcp``; the ``default`` Proxy also at
    ``/<upstream>/mcp``; live state at ``/api/status``; ``/api/shutdown`` stops it. ``clock``
    is what every lifecycle timer runs on, so tests advance time instead of waiting for it. A
    Proxy whose file sets ``port`` is also mounted alone on that additional listener, in
    ``.extra``. ``token``, when given, requires ``Authorization: Bearer <token>`` on every
    request to any of them.
    """
    running_clock = clock or SystemClock()
    upstreams = load_upstreams(config_dir)
    global_caps = load_settings(config_dir).caps
    secrets = secrets_for(config_dir)
    connections = {
        upstream.name: UpstreamConnection(
            upstream,
            secrets,
            clock,
            on_catalog=partial(record_observation, state_dir, upstream.name),
        )
        for upstream in upstreams
    }
    proxies = {
        (upstream.name, proxy_name): _Proxy(
            config_dir, state_dir, upstream, proxy_name, connections[upstream.name], global_caps
        )
        for upstream in upstreams
        for proxy_name in upstream.proxies
    }
    stop = asyncio.Event()
    routes: list[BaseRoute] = [
        Route(STATUS_PATH, _status(upstreams, connections, proxies)),
        Route(SHUTDOWN_PATH, _shutdown(stop), methods=["POST"]),
    ]
    routes += [
        Mount(f"/{name}/{proxy_name}", app=proxy) for (name, proxy_name), proxy in proxies.items()
    ]
    routes += [
        Mount(f"/{name}", app=proxy)
        for (name, proxy_name), proxy in proxies.items()
        if proxy_name == DEFAULT_PROXY_NAME
    ]

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncGenerator[None]:
        await asyncio.gather(
            *(
                _bounded_rescan(state_dir, secrets, upstream, running_clock)
                for upstream in upstreams
            )
        )
        async with contextlib.AsyncExitStack() as stack:
            for proxy in proxies.values():
                await proxy.start()
                stack.push_async_callback(proxy.stop)
            for connection in connections.values():
                await stack.enter_async_context(connection.running())
            yield

    main = Starlette(routes=routes, lifespan=lifespan, middleware=_middleware(token))
    ports = _proxy_ports(config_dir, upstreams)
    extra = {port: _authed(proxies[key], token) for key, port in ports.items()}
    return DaemonApp(main=main, extra=extra, stop=stop)


def _proxy_ports(config_dir: Path, upstreams: list[Upstream]) -> dict[tuple[str, str], int]:
    """The additional port every Proxy that sets one asks to be served on besides its path."""
    ports: dict[tuple[str, str], int] = {}
    for upstream in upstreams:
        for proxy_name in upstream.proxies:
            try:
                proxy = load_proxy(config_dir, upstream.name, proxy_name)
            except ConfigError:
                continue
            if proxy.port is not None:
                ports[upstream.name, proxy_name] = proxy.port
    return ports


def _status(
    upstreams: list[Upstream],
    connections: dict[str, UpstreamConnection],
    proxies: dict[tuple[str, str], _Proxy],
) -> Callable[[Request], Awaitable[JSONResponse]]:
    """The ``/api/status`` endpoint: what every Upstream and Proxy is doing right now."""

    async def state_of(upstream: Upstream) -> UpstreamState:
        status = connections[upstream.name].status()
        return UpstreamState(
            name=upstream.name,
            state=status.state,
            seconds=round(status.seconds, 3),
            error=status.error,
            proxies=[await proxies[upstream.name, name].state() for name in upstream.proxies],
        )

    async def endpoint(_request: Request) -> JSONResponse:
        live = LiveState(upstreams=[await state_of(upstream) for upstream in upstreams])
        return JSONResponse(live.model_dump(mode="json"))

    return endpoint


def _shutdown(stop: asyncio.Event) -> Callable[[Request], Awaitable[JSONResponse]]:
    """``/api/shutdown``: what ``daemon down`` posts to. Sets ``stop`` and answers at once."""

    async def endpoint(_request: Request) -> JSONResponse:
        stop.set()
        return JSONResponse({"stopping": True})

    return endpoint


async def serve(
    app: ASGIApp,
    host: str,
    port: int,
    stop: asyncio.Event | None = None,
    *,
    lifespan: Literal["on", "off"] = "on",
) -> None:
    """Serve ``app`` on ``host``:``port`` until ``stop`` is set or the process is signalled.

    The Daemon process's main loop. A cooperative stop lets the server close its socket;
    cancelling the task would leave it open. ``lifespan="off"`` is for a Proxy's port
    override: the Proxy's own lifespan is already run once, by the main app.
    """
    import uvicorn  # noqa: PLC0415  # only the running Daemon needs a server

    config = uvicorn.Config(app, host=host, port=port, log_level="warning", lifespan=lifespan)
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


async def serve_all(daemon: DaemonApp, host: str, port: int) -> None:
    """Serve the main app on ``host``:``port`` and every Proxy port override alongside it."""
    await asyncio.gather(
        serve(daemon.main, host, port, daemon.stop),
        *(
            serve(app, host, extra_port, daemon.stop, lifespan="off")
            for extra_port, app in daemon.extra.items()
        ),
    )


def run(config_dir: Path, state_dir: Path) -> None:
    """Build the Daemon app from ``config_dir`` and serve it, and every port override, until
    ``daemon down`` or a signal stops it."""
    settings = load_settings(config_dir).daemon
    app = build_app(config_dir, state_dir, token=settings.token)
    asyncio.run(serve_all(app, settings.host, settings.port))
