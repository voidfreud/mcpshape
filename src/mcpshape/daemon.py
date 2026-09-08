"""The Daemon's ASGI app: each Upstream's Proxies at their own paths, never merged (ADR 0002).

Every Proxy serves from its Upstream's accepted Catalog, curated by its Proxy file. Both are
files the CLI edits, so each Proxy re-reads them when they change, checked on every request.
Starting the Daemon rescans every Upstream, recording Drift rather than serving it.

One connection per Upstream is built here and shared by all of that Upstream's Proxies and
every Client (story 74); one owner per Upstream holds it, warms it, times it, and lets it go,
and re-reads the Upstream file the same way a Proxy re-reads its own, so an edit is in force
on the next request and a removed Upstream is retired (#46, #62). The management API
under ``/api`` (``mcpshape.api``) is the live state the CLI and the dashboard read: nothing
there is configuration, which is read from files whether the Daemon runs or not. Every tool
call through a Proxy goes to the call log, and the app log goes to the state directory, both
rotated under the one size cap ``config.toml`` sets (#16).
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Mount

from mcpshape import catalog as catalogs
from mcpshape.adapters.fastmcp import UpstreamConnection, proxy_app, scan, server_name
from mcpshape.api import Management, ProxyState
from mcpshape.calls import CallLog
from mcpshape.config import (
    SETTINGS_FILE,
    UPSTREAM_FILE,
    ConfigError,
    DaemonSettings,
    load_proxy,
    load_settings,
    load_upstream,
    load_upstreams,
    proxy_code_file,
    proxy_file,
    secrets_for,
    upstream_dir,
)
from mcpshape.connection import SystemClock, TimedOutError, bounded
from mcpshape.hooks import UserCodeError, load_user_code
from mcpshape.logs import RotatingFile, configure_app_log
from mcpshape.model import DEFAULT_PROXY_NAME, CapError, CapSettings
from mcpshape.paths import call_log_file
from mcpshape.proxy import Exposed, OverrideError, cap, expose
from mcpshape.tokens import Tokens

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path

    from starlette.routing import BaseRoute
    from starlette.types import ASGIApp, Receive, Scope, Send

    from mcpshape.adapters.fastmcp import ProxyApp
    from mcpshape.catalog import Catalog
    from mcpshape.connection import Clock
    from mcpshape.model import Upstream
    from mcpshape.secrets import Secrets

log = logging.getLogger("mcpshape.daemon")


def is_loopback(host: str) -> bool:
    """Whether ``host`` is reachable only from this machine."""
    return host in {"127.0.0.1", "::1", "localhost"} or host.startswith("127.")


def check_bind(settings: DaemonSettings) -> None:
    """Refuse a bind anything but this machine could reach unless a bearer token guards it.

    Raises ``ConfigError``; the Daemon checks this itself, however it was started.
    """
    if not is_loopback(settings.host) and not settings.token:
        msg = (
            f"binding to {settings.host!r}, which is not loopback, needs a bearer token: "
            'set [daemon] token = "..." in config.toml, or bind to 127.0.0.1'
        )
        raise ConfigError(msg)


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
        return hmac.compare_digest(given, f"Bearer {self._token}".encode("latin-1"))


def _authed(app: ASGIApp, token: str | None) -> ASGIApp:
    return _BearerAuth(app, token) if token else app


def _middleware(token: str | None) -> list[Middleware]:
    return [Middleware(_BearerAuth, token=token)] if token else []


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

    The Upstream file is its owner's (``_Served``), not one of these sources: it belongs to
    the shared connection as much as to any one Proxy, so the owner watches it and this Proxy
    reads the Upstream and the global Caps from the owner every time it derives (#46).
    """

    def __init__(
        self,
        config_dir: Path,
        state_dir: Path,
        owner: _Served,
        name: str,
        calls: CallLog,
    ) -> None:
        upstream = owner.upstream
        self._owner = owner
        self._code_path = proxy_code_file(config_dir, upstream.name, name)
        self._sources = (
            catalogs.catalog_path(state_dir, upstream.name),
            proxy_file(config_dir, upstream.name, name),
            self._code_path,
        )
        self._label = f"{upstream.name}/{name}"
        self._config_dir, self._state_dir, self._name = config_dir, state_dir, name
        self._stamp: tuple[_StampEntry, ...] | None = None
        self._connection = owner.connection
        self._calls = calls
        self._lock = asyncio.Lock()
        self._held: _Held | None = None
        self.health = "ok"
        self.detail: str | None = None
        self.app: ProxyApp = proxy_app(upstream, name, _nothing(), self._connection, calls)

    @property
    def _upstream(self) -> Upstream:
        """The Upstream as its owner holds it now, which an edit may have changed (#46)."""
        return self._owner.upstream

    async def start(self) -> None:
        self._held = _Held(self.app)
        await self._held.ready()

    async def stop(self) -> None:
        if self._held is not None:
            await self._held.close()
            self._held = None

    async def reload(self) -> None:
        """Re-read every source now, whether or not it changed: ``daemon reload``."""
        self.derive_again()
        await self.refresh()

    def derive_again(self) -> None:
        """Derive the exposed set again on the next refresh, whatever the sources say.

        What the owner asks for when the Upstream file changed the Caps this Proxy inherits,
        or when a file that could not be read can be read again (#46).
        """
        self._stamp = None

    def fail_upstream(self, reason: str) -> None:
        """Mark this Proxy unhealthy because its Upstream file cannot be read (#46).

        The Proxy pattern for a file that cannot be read: it keeps advertising its last
        exposed set and every call errors with the reason. Undone by the next refresh that
        finds the Upstream file readable again.
        """
        self.health, self.detail = "unhealthy", reason
        self.app.fail(reason)

    async def refresh(self) -> None:
        """Re-read the sources that changed since the last look, on every request (#10).

        This is the file watching: the stamps are checked when a request comes in, so a
        change is served on the next request after it, the affected Proxy alone, and no
        watcher runs between requests. The fixed sources come first, then every ``*.py``
        file in the Upstream's directory, sorted by name, so a helper appearing, changing,
        or vanishing refreshes every Proxy of that Upstream too (#27). An Upstream file the
        owner could not read is the same kind of failure, and is reported before anything
        here is applied over the settings that file no longer supplies (#46).
        """
        async with self._lock:
            problem = self._owner.upstream_problem
            if problem is not None:
                self.fail_upstream(problem)
                return
            upstream, global_caps = self._owner.upstream, self._owner.global_caps
            stamp = (*(_stamp(path) for path in self._sources), *self._helper_stamps())
            if stamp == self._stamp:
                return
            self._stamp = stamp
            try:
                stored = catalogs.load_catalog(self._state_dir, upstream.name) or _empty()
                proxy = load_proxy(self._config_dir, upstream.name, self._name)
                code = load_user_code(self._code_path, self._label)
                upstream_caps = upstream.caps.over(global_caps, f"Upstream {upstream.name}")
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
            if server_name(upstream, self._name, exposed) == self.app.name:
                self.app.serve(exposed)
                return
            await self._rebuild(exposed)

    def _helper_stamps(self) -> tuple[tuple[str, _Stat], ...]:
        """Every ``*.py`` file in the Upstream's directory, by name and stat, sorted by name.

        The Upstream's directory is what ``import helpers`` resolves against (#27); a helper
        appearing, changing, or vanishing has to be seen here too, not only the Proxy's own
        fixed files.
        """
        try:
            names = sorted(entry.name for entry in self._code_path.parent.glob("*.py"))
        except OSError:
            return ()
        return tuple((name, _stamp(self._code_path.parent / name)) for name in names)

    async def _rebuild(self, exposed: Exposed) -> None:
        previous = self._held
        self.app = proxy_app(self._upstream, self._name, exposed, self._connection, self._calls)
        await self.start()
        if previous is not None:
            await previous.close()

    async def state(self) -> ProxyState:
        await self._owner.refresh()
        await self.refresh()
        return ProxyState(name=self._name, health=self.health, detail=self.detail)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            await self._owner.refresh()
            if self._owner.retired:
                answer = JSONResponse(
                    {"error": f"no Upstream named {self._owner.upstream.name!r}"}, status_code=404
                )
                await answer(scope, receive, send)
                return
            await self.refresh()
        await self.app.asgi(scope, receive, send)


_Stat = tuple[int, int] | None
_StampEntry = _Stat | tuple[str, _Stat]


def _stamp(path: Path) -> _Stat:
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


def _reach(upstream: Upstream) -> tuple[type[object], dict[str, object]]:
    """How the Upstream is reached: the transport's kind and settings, and nothing else.

    The transport an Upstream carries is its whole file as loaded, Caps and lifecycle beside
    the transport keys, so comparing it whole would have a Cap edit connect again (#46).
    """
    transport = upstream.transport
    return type(transport), transport.model_dump(exclude={"version", "lifecycle", "caps"})


async def rescan(state_dir: Path, secrets: Secrets, upstream: Upstream) -> bool:
    """Scan ``upstream`` into its Catalog on a first scan, else record Drift. Never raises;
    says whether the Upstream was reached.

    This is the Daemon's own start-up scan, which reaches an Upstream nothing is connected to
    yet. A reconnect looks again over the connection it already has (story 11). Recording
    holds the Upstream's lock across its read-then-write (#21), which can wait behind a
    concurrent ``upstream sync``; a thread keeps that wait off the Daemon's own event loop.
    """
    try:
        observed = await scan(upstream, secrets, Tokens(state_dir, upstream.name))
        await asyncio.to_thread(catalogs.record_scan, state_dir, upstream.name, observed)
    except Exception:  # an Upstream that cannot be reached must not keep the Daemon from starting
        log.warning("Upstream %s could not be scanned", upstream.name, exc_info=True)
        return False
    return True


async def record_observation(state_dir: Path, name: str, observed: Catalog) -> None:
    """Keep what a reconnected Upstream advertises: its first Catalog, or the Drift since.

    An ``upstream rm`` can remove the Upstream's state while this scan waits for its lock
    (#49). There is then nothing left to keep, which is ordinary: one line says so, rather
    than the traceback the rescan would otherwise log.
    """
    try:
        await asyncio.to_thread(catalogs.record_scan, state_dir, name, observed)
    except catalogs.ForgottenError:
        log.info("Upstream %s was removed while its scan waited; dropping what it saw", name)


class _Served:
    """One Upstream as the Daemon holds it: its file, its shared connection, and its Proxies.

    The Upstream file is watched here the way a Proxy watches its own (#10): its stamp, and
    the global settings file's, are checked when a request comes in, so an edit is in force on
    the next request and no watcher runs between them. A Cap edit has every Proxy derive
    again; a transport or lifecycle edit connects again under the new settings; a file that
    cannot be read leaves the connection where it is and marks every Proxy of the Upstream
    unhealthy with the reason. A file that is gone retires the Upstream: its Proxies answer
    not found, its connection is let go, and nothing is written to its state directory again,
    since only the Daemon, which knows the file, can tell a removed Upstream from a new one
    (#46, #62).
    """

    def __init__(  # noqa: PLR0913, PLR0917  # every one of these is state the Upstream needs
        self,
        config_dir: Path,
        state_dir: Path,
        upstream: Upstream,
        global_caps: CapSettings,
        secrets: Secrets,
        clock: Clock | None,
        calls: CallLog,
    ) -> None:
        self._config_dir, self._state_dir = config_dir, state_dir
        self._name = upstream.name
        self._file = upstream_dir(config_dir, upstream.name) / UPSTREAM_FILE
        self._sources = (self._file, config_dir / SETTINGS_FILE)
        self._stamp: tuple[_Stat, ...] | None = self._stamps()
        self._lock = asyncio.Lock()
        self._retiring: asyncio.Task[None] | None = None
        self.upstream: Upstream = upstream
        self.global_caps: CapSettings = global_caps
        self.upstream_problem: str | None = None
        self.retired = False
        self.connection = UpstreamConnection(
            upstream,
            secrets,
            clock,
            on_catalog=self.on_catalog,
            tokens=Tokens(state_dir, upstream.name),
        )
        self.proxies: dict[str, _Proxy] = {
            proxy_name: _Proxy(config_dir, state_dir, self, proxy_name, calls)
            for proxy_name in upstream.proxies
        }

    def _stamps(self) -> tuple[_Stat, ...]:
        return tuple(_stamp(path) for path in self._sources)

    async def start(self) -> None:
        """Start every Proxy, then supervise the connection: the Daemon's lifespan."""
        for proxy in self.proxies.values():
            await proxy.start()
        await self.connection.start()

    async def stop(self) -> None:
        """Let the connection go, then stop every Proxy: the Daemon's lifespan again."""
        await self._settle_retirement()
        await self.connection.stop()
        for proxy in self.proxies.values():
            await proxy.stop()

    async def reload(self) -> None:
        """Re-read the Upstream file now, changed or not, and reload what is left of it."""
        self._stamp = None
        await self.refresh()
        if self.retired:
            return
        for proxy in self.proxies.values():
            await proxy.reload()
        await self.connection.reload()

    async def refresh(self) -> None:
        """Re-read the Upstream file when it changed since the last look, on every request."""
        async with self._lock:
            if self.retired:
                return
            stamp = self._stamps()
            if stamp == self._stamp:
                return
            self._stamp = stamp
            if not self._file.is_file():
                await self._retire()
                return
            try:
                settings = load_settings(self._config_dir)
                upstream = load_upstream(self._config_dir, self._name, settings.lifecycle)
            except ConfigError as exc:
                log.warning(
                    "Upstream %s could not be re-read and keeps its last settings: %s",
                    self._name,
                    exc,
                )
                self.upstream_problem = str(exc)
                for proxy in self.proxies.values():
                    proxy.fail_upstream(str(exc))
                return
            previous = self.upstream
            self._apply(upstream, settings.caps)
            if _reach(upstream) != _reach(previous) or upstream.lifecycle != previous.lifecycle:
                await self.connection.reconfigure(upstream)

    def _apply(self, upstream: Upstream, global_caps: CapSettings) -> None:
        """Take the Upstream the file now describes, and say who has to derive again."""
        recovered, self.upstream_problem = self.upstream_problem is not None, None
        capped = upstream.caps != self.upstream.caps or global_caps != self.global_caps
        self.upstream, self.global_caps = upstream, global_caps
        if recovered or capped:
            for proxy in self.proxies.values():
                proxy.derive_again()

    async def on_catalog(self, observed: Catalog) -> None:
        """Keep what a reconnected Upstream advertises, unless the Upstream is no longer there.

        ``catalog.py`` cannot tell a removed Upstream from a brand-new one, so a rescan that
        started before an ``upstream rm`` would write the state directory back (#62); only the
        Daemon, which knows the file, can tell them apart, so the file is read here, before
        the write and once more after it, since the removal may land in between. Retirement is
        scheduled rather than awaited: this runs in the connection's own rescan task, which
        stopping that connection cancels and waits for.
        """
        if self.retired or not self._file.is_file():
            log.info("Upstream %s was removed; dropping what its reconnect saw", self._name)
            self._retire_soon()
            return
        await record_observation(self._state_dir, self._name, observed)
        if not self._file.is_file():
            await asyncio.to_thread(catalogs.forget, self._state_dir, self._name)
            self._retire_soon()

    def _retire_soon(self) -> None:
        if self.retired or (self._retiring is not None and not self._retiring.done()):
            return
        self._retiring = asyncio.create_task(self._retire())

    async def _retire(self) -> None:
        """Stop holding an Upstream whose file is gone. Idempotent (#62)."""
        if self.retired:
            return
        self.retired = True
        log.info("Upstream %s was removed; letting its connection go", self._name)
        await self.connection.stop()
        for proxy in self.proxies.values():
            await proxy.stop()

    async def _settle_retirement(self) -> None:
        retiring, self._retiring = self._retiring, None
        if retiring is None:
            return
        retiring.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await retiring


async def _bounded_rescan(
    state_dir: Path, secrets: Secrets, upstream: Upstream, clock: Clock
) -> bool:
    """``rescan``, bounded by ``upstream``'s own ``connect_timeout`` on ``clock`` (#20).

    An Upstream whose connect hangs must not hold up the Daemon's start, nor any other
    Upstream's: this is awaited concurrently with every other Upstream's bounded rescan, and a
    scan that times out is logged and skipped, exactly like one that fails outright. Says
    whether the Upstream was reached.
    """
    try:
        return await bounded(
            rescan(state_dir, secrets, upstream), upstream.lifecycle.connect_timeout, clock
        )
    except TimedOutError:
        log.warning(
            "Upstream %s could not be scanned within its connect_timeout of %.0fs",
            upstream.name,
            upstream.lifecycle.connect_timeout,
        )
        return False


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
    settings = load_settings(config_dir)
    configure_app_log(state_dir, settings.log.level, settings.log.max_bytes)
    calls = CallLog(RotatingFile(call_log_file(state_dir), settings.log.max_bytes))
    secrets = secrets_for(config_dir)
    served = {
        upstream.name: _Served(
            config_dir, state_dir, upstream, settings.caps, secrets, clock, calls
        )
        for upstream in upstreams
    }
    proxies = {
        (name, proxy_name): proxy
        for name, owner in served.items()
        for proxy_name, proxy in owner.proxies.items()
    }
    stop = asyncio.Event()
    management = Management(
        served=served,
        calls=calls,
        state_dir=state_dir,
        secrets=secrets,
        clock=running_clock,
        stop=stop,
    )
    routes: list[BaseRoute] = management.routes()
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
        reached = await asyncio.gather(
            *(
                _bounded_rescan(state_dir, secrets, upstream, running_clock)
                for upstream in upstreams
            )
        )
        for upstream, scanned in zip(upstreams, reached, strict=True):
            if not scanned:
                # its first connect looks instead (#57)
                served[upstream.name].connection.unscanned()
        async with contextlib.AsyncExitStack() as stack:
            stack.push_async_callback(management.close)
            for owner in served.values():
                await owner.start()
                stack.push_async_callback(owner.stop)
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
    ``daemon down`` or a signal stops it. Refuses an unguarded non-loopback bind itself."""
    settings = load_settings(config_dir).daemon
    check_bind(settings)
    app = build_app(config_dir, state_dir, token=settings.token)
    log.info("Daemon starting on %s:%d", settings.host, settings.port)
    asyncio.run(serve_all(app, settings.host, settings.port))
    log.info("Daemon stopped")
