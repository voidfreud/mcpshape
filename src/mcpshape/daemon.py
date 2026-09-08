"""The Daemon's ASGI app: each Upstream's Proxies at their own paths, never merged (ADR 0002).

Every Proxy serves from its Upstream's accepted Catalog, curated by its Proxy file. Both are
files the CLI edits, so each Proxy re-reads them when they change, checked on every request.
Starting the Daemon rescans every Upstream, recording Drift rather than serving it.

One connection per Upstream is built here and shared by all of that Upstream's Proxies and
every Client (story 74); one owner per Upstream holds it, warms it, times it, and lets it go,
and re-reads the Upstream file the same way a Proxy re-reads its own, so an edit is in force
on the next request and a removed Upstream is retired (#46, #62). An Upstream or Proxy added
while the Daemon runs is found on its first request, on ``daemon status``, and on ``daemon
reload``, and launched the same way one present at Daemon start is; a removed and re-added
name is a new Upstream (#67). The management API
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
import socket
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Match, Mount
from starlette.staticfiles import StaticFiles

from mcpshape import catalog as catalogs
from mcpshape import dashboard
from mcpshape.adapters.fastmcp import (
    MCP_PATH,
    UpstreamConnection,
    proxy_app,
    scan,
    server_name,
)
from mcpshape.api import Management, ProxyState
from mcpshape.calls import CallLog
from mcpshape.config import (
    SETTINGS_FILE,
    UPSTREAM_FILE,
    UPSTREAMS_DIR,
    ConfigError,
    DaemonSettings,
    list_proxies,
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
from mcpshape.names import RESERVED
from mcpshape.paths import call_log_file
from mcpshape.proxy import Exposed, OverrideError, cap, expose
from mcpshape.tokens import Tokens

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Mapping
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

    Wraps every route: Proxies, the management API, and the dashboard. Never logs the token
    itself, given or expected.
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
        self._key = (upstream.name, name)
        self._listeners = owner.listeners
        self._config_dir, self._state_dir, self._name = config_dir, state_dir, name
        self._stamp: tuple[_StampEntry, ...] | None = None
        self._connection = owner.connection
        self._calls = calls
        self._lock = asyncio.Lock()
        self._held: _Held | None = None
        self.health = "ok"
        self.detail: str | None = None
        self.exposed: Exposed = _nothing()
        """What the Proxy exposes, as last derived: what the dashboard asks for (#17)."""
        self.listener_problem: str | None = None
        """Why the port this Proxy's file asks for is not listened on, while it is not (#70)."""
        self._port: int | None = None
        self._gone = False
        self.app: ProxyApp = proxy_app(upstream, name, _nothing(), self._connection, calls)

    @property
    def _upstream(self) -> Upstream:
        """The Upstream as its owner holds it now, which an edit may have changed (#46)."""
        return self._owner.upstream

    async def start(self) -> None:
        """Idempotent for the app currently held: a status look during the Upstream's launch
        refreshes this Proxy, and a changed server name then rebuilds and holds its app before
        the launch's own ``start()`` reaches it (#67). ``_rebuild`` clears ``self._held``
        first, so a rebuilt app still gets a fresh hold.
        """
        if self._held is not None:
            return
        self._held = _Held(self.app)
        await self._held.ready()
        self._want_port_now()

    def _want_port_now(self) -> None:
        """Ask for the port the Proxy file sets before any request derives it (#13, #70).

        A port set at Daemon start is listened on as the Daemon comes up, not on the first
        request; a file that cannot be read is left to ``refresh``, which reports it.
        """
        try:
            self._port = load_proxy(self._config_dir, self._upstream.name, self._name).port
        except ConfigError:
            return
        self._listeners.want(self._key, self._port, self)

    async def stop(self) -> None:
        """Let the app go. A stopped Proxy is gone: a request that still reaches it, on a
        port listener not yet closed, is answered not found naming it (#68)."""
        self._gone = True
        self._listeners.forget(self._key)
        self.listener_problem = None
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
                if self.listener_problem is not None:  # a look is a retry of the port (#70)
                    self._listeners.want(self._key, self._port, self)
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
            self.exposed = exposed
            self._port = proxy.port
            self._listeners.want(self._key, self._port, self)
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
        self._held = None  # a new app needs its own hold; start()'s idempotency is per app
        await self.start()
        if previous is not None:
            await previous.close()

    async def exposed_now(self) -> Exposed:
        """The exposed set after the files are looked at: ``/api/.../exposed`` (#17)."""
        await self._owner.refresh()
        await self.refresh()
        return self.exposed

    async def state(self) -> ProxyState:
        await self._owner.refresh()
        await self.refresh()
        health, detail = self.health, self.detail
        if health == "ok" and self.listener_problem is not None:
            health, detail = "unhealthy", self.listener_problem
        return ProxyState(name=self._name, health=health, detail=detail)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            await self._owner.ready()
            await self._owner.refresh()
            if self._owner.retired:
                answer = JSONResponse(
                    {"error": f"no Upstream named {self._owner.upstream.name!r}"}, status_code=404
                )
                await answer(scope, receive, send)
                return
            if self._gone:
                await _not_found(f"no Proxy {self._label}", scope, receive, send)
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


def _reached_by(upstream: Upstream) -> tuple[type[object], dict[str, object]]:
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
    (#46, #62). The Upstream's directory is watched too: a Proxy file that appears is adopted
    (#67), and one that is gone has its Proxy let go, its URL answering not found, while the
    connection and every other Proxy stay as they are (#68).
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
        listeners: Listeners,
    ) -> None:
        self._config_dir, self._state_dir = config_dir, state_dir
        self._name = upstream.name
        self._file = upstream_dir(config_dir, upstream.name) / UPSTREAM_FILE
        self.listeners = listeners
        self._sources = (
            self._file,
            config_dir / SETTINGS_FILE,
            upstream_dir(config_dir, upstream.name),
        )
        self._stamp: tuple[_Stat, ...] | None = self._stamps()
        self._lock = asyncio.Lock()
        self._retiring: asyncio.Task[None] | None = None
        self._secrets = secrets
        self._running_clock = clock or SystemClock()
        self._calls = calls
        self._launched = asyncio.Event()
        self._launch: asyncio.Task[None] | None = None
        self._failure: Exception | None = None
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

    def launch(self) -> None:
        """Start the launch task: the start-up scan, then ``start()`` (#67).

        Needs a running loop, so this is called from the lifespan or from a request that
        found the Upstream, never from ``build_app`` itself.
        """
        self._launch = asyncio.create_task(self._start_up())

    async def _start_up(self) -> None:
        """The Daemon's start-up scan for this one Upstream, then ``start()``.

        What every Upstream held at Daemon start goes through in the lifespan; an Upstream
        found later goes through the same thing, on its own task, so it is served the same
        way (#67). Never lets an unexpected exception escape unnoticed.
        """
        try:
            reached = await _bounded_rescan(
                self._state_dir, self._secrets, self.upstream, self._running_clock
            )
            if not reached:
                self.connection.unscanned()
            if self.retired:  # checked right before starting; a retirement raced the scan
                return
            await self.start()
        except Exception as exc:
            self._failure = exc
            log.exception("Upstream %s failed to launch", self._name)
        finally:
            self._launched.set()

    async def ready(self) -> None:
        """Wait until the launch has reached the point ``start()`` was called, or given up."""
        await self._launched.wait()

    async def launched(self) -> None:
        """Wait for the launch, and raise what it failed with: the Daemon's own start.

        A launch that fails at Daemon start fails the Daemon, as it did before #67; one that
        fails on a request is logged by the launch itself, and its Proxies answer as they can.
        """
        if self._launch is not None:
            await self._launch
        if self._failure is not None:
            raise self._failure

    async def start(self) -> None:
        """Start every Proxy, then supervise the connection: what the launch task calls."""
        for proxy in list(self.proxies.values()):
            await proxy.start()
        await self.connection.start()

    async def stop(self) -> None:
        """Let the connection go, then stop every Proxy: the Daemon's lifespan again."""
        launch, self._launch = self._launch, None
        if launch is not None and not launch.done():
            launch.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await launch
        await self._settle_retirement()
        await self.connection.stop()
        for proxy in list(self.proxies.values()):
            await proxy.stop()

    async def reload(self) -> None:
        """Re-read the Upstream file now, changed or not, and reload what is left of it."""
        if not self._launched.is_set():  # still launching: its own read covers this
            return
        self._stamp = None
        await self.refresh()
        if self.retired:
            return
        for proxy in list(self.proxies.values()):
            await proxy.reload()
        await self.connection.reload()

    async def refresh(self) -> None:
        """Re-read the Upstream file when it changed since the last look, on every request."""
        if not self._launched.is_set():  # still launching: its own read covers this
            return
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
            await self._adopt()
            await self._let_go()
            if (
                _reached_by(upstream) != _reached_by(previous)
                or upstream.lifecycle != previous.lifecycle
            ):
                await self.connection.reconfigure(upstream)

    def _apply(self, upstream: Upstream, global_caps: CapSettings) -> None:
        """Take the Upstream the file now describes, and say who has to derive again."""
        recovered, self.upstream_problem = self.upstream_problem is not None, None
        capped = upstream.caps != self.upstream.caps or global_caps != self.global_caps
        self.upstream, self.global_caps = upstream, global_caps
        if recovered or capped:
            for proxy in self.proxies.values():
                proxy.derive_again()

    async def _adopt(self) -> None:
        """Build and start a Proxy for every name the Upstream now lists that is not held.

        The Upstream's own directory is one of ``_sources`` (#67), so a Proxy file appearing
        changes the directory's stamp and is seen by the next ``refresh()``, right after the
        Upstream this re-read just derived is applied.
        """
        for name in self.upstream.proxies:
            if name not in self.proxies:
                await self._adopt_one(name)

    async def _adopt_one(self, name: str) -> _Proxy:
        proxy = _Proxy(self._config_dir, self._state_dir, self, name, self._calls)
        self.proxies[name] = proxy
        await proxy.start()
        log.info("Upstream %s: adopted new Proxy %s", self._name, name)
        return proxy

    async def _let_go(self) -> None:
        """Stop and drop every held Proxy the Upstream no longer lists: its file is gone (#68).

        Seen the way adoption is, by the directory's stamp. The Upstream's connection is not
        touched, since it is the Upstream's, and no other Proxy is.
        """
        for name in list(self.proxies):
            if name not in self.upstream.proxies:
                await self._let_go_of(name)

    async def _let_go_of(self, name: str) -> None:
        proxy = self.proxies.pop(name, None)
        if proxy is None:
            return
        await proxy.stop()
        log.info("Upstream %s: Proxy %s was removed; letting it go", self._name, name)

    async def proxy(self, name: str) -> _Proxy | None:
        """The held Proxy called ``name``, adopting it now if its file appeared since (#67),
        or letting it go now if its file is gone (#68).

        The route's fallback so a request never depends on the directory stamp alone: a Proxy
        added, or removed, and requested before any other look at the Upstream is still found,
        or refused. Only a name the directory lists as a Proxy counts: ``upstream.toml`` is a
        TOML file too, and is not one.
        """
        held = self.proxies.get(name)
        if held is not None and proxy_file(self._config_dir, self._name, name).is_file():
            return held
        if held is None and name not in list_proxies(self._config_dir, self._name):
            return None
        async with self._lock:
            if name in self.proxies:
                if proxy_file(self._config_dir, self._name, name).is_file():
                    return self.proxies[name]
                await self._let_go_of(name)
                return None
            return await self._adopt_one(name)

    async def on_catalog(self, observed: Catalog) -> None:
        """Keep what a reconnected Upstream advertises, unless the Upstream is no longer there.

        ``catalog.py`` cannot tell a removed Upstream from a brand-new one, so a rescan that
        started before an ``upstream rm`` would write the state directory back (#62); only the
        Daemon, which knows the Upstream file, can tell them apart, so it is asked about the
        file under the Catalog lock, right before the write, where an ``rm`` is either done
        already or still waiting behind the write it will remove. Retirement is scheduled
        rather than awaited: this runs in the connection's own rescan task, which stopping
        that connection cancels and waits for.
        """
        if self.retired:
            return
        try:
            await asyncio.to_thread(
                catalogs.record_scan, self._state_dir, self._name, observed, self._file.is_file
            )
        except catalogs.ForgottenError:
            log.info("Upstream %s was removed; dropping what its reconnect saw", self._name)
            self._retire_soon()

    def _retire_soon(self) -> None:
        if self.retired or (self._retiring is not None and not self._retiring.done()):
            return
        self._retiring = asyncio.create_task(self._retire())

    async def _retire(self) -> None:
        """Stop holding an Upstream whose file is gone. Idempotent (#62).

        Waits for the launch first, so nothing is started after retirement (#67): the launch
        checks ``retired`` right before its own ``start()`` and skips it, but only once it
        has reached that check.
        """
        await self._launched.wait()
        if self.retired:
            return
        self.retired = True
        log.info("Upstream %s was removed; letting its connection go", self._name)
        await self.connection.stop()
        for proxy in list(self.proxies.values()):
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


class _ProxyRoute:
    """Resolves ``/<upstream>/mcp`` and ``/<upstream>/<proxy>/mcp`` to their owner per request.

    Routes are fixed at build; the Upstreams and Proxies behind them are not. One Mount at
    ``/{upstream}`` instead of one per Proxy is what lets an Upstream or Proxy added while the
    Daemon runs be served without a restart (#67): the owner and the Proxy are found fresh on
    every request. What follows the Upstream's name is either ``/mcp...``, the default Proxy,
    or ``/<proxy>/mcp...``; ``mcp`` is a reserved name, so the two never collide, and a
    trailing slash reaches the Proxy app exactly as it did with a Mount of its own. A reserved
    Upstream name is not an Upstream path at all: ``/api/<unknown>`` stays a plain not found.
    """

    def __init__(self, upstreams: _Upstreams) -> None:
        self._upstreams = upstreams

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        params: dict[str, str] = scope["path_params"]
        name = params["upstream"]
        if name in RESERVED:
            await PlainTextResponse("Not Found", status_code=404)(scope, receive, send)
            return
        owner = await self._upstreams.lookup(name)
        if owner is None:
            await _not_found(f"no Upstream named {name!r}", scope, receive, send)
            return
        await owner.ready()
        if owner.retired:
            await _not_found(f"no Upstream named {name!r}", scope, receive, send)
            return
        first = _route_path(scope).split("/", 2)[1:2]
        proxy_name = first[0] if first and first[0] not in ("", MCP_SEGMENT) else None
        proxy = await owner.proxy(proxy_name or DEFAULT_PROXY_NAME)
        if proxy is None:
            wanted = proxy_name or DEFAULT_PROXY_NAME
            await _not_found(f"no Proxy {name}/{wanted}", scope, receive, send)
            return
        if proxy_name is None:
            await proxy(scope, receive, send)
            return
        mount = Mount(f"/{proxy_name}", app=proxy)
        match, child = mount.matches(scope)
        if match == Match.NONE:
            await PlainTextResponse("Not Found", status_code=404)(scope, receive, send)
            return
        await mount.handle({**scope, **child}, receive, send)


MCP_SEGMENT = MCP_PATH.strip("/")
"""The path segment every Proxy app serves under: ``/<upstream>[/<proxy>]/mcp``."""


def _route_path(scope: Scope) -> str:
    """The path left after the Mount that reached here, as Starlette hands it to a child."""
    path: str = scope["path"]
    root: str = scope.get("root_path", "")
    if root and path.startswith(root) and path[len(root) :].startswith("/"):
        return path[len(root) :]
    return path


async def _not_found(message: str, scope: Scope, receive: Receive, send: Send) -> None:
    response = JSONResponse({"error": message}, status_code=404)
    await response(scope, receive, send)


class _Upstreams:
    """Every Upstream the Daemon holds, by name, found at build and on demand (#67).

    Backs the parameterised routes ``_ProxyRoute`` resolves through, and what ``Management``
    reads the live state from. An Upstream added while the Daemon runs is found the same way
    one held already is re-read: its directory's ``upstream.toml`` appearing is what
    ``lookup``, ``refresh``, and ``reload`` all look for, under the lock, before adding it. A
    removed and re-added name replaces its retired owner with a fresh one: a fresh connection,
    a first scan, not Drift against what the old owner last saw.
    """

    def __init__(  # noqa: PLR0913, PLR0917  # every one of these is state an owner needs
        self,
        config_dir: Path,
        state_dir: Path,
        secrets: Secrets,
        clock: Clock | None,
        calls: CallLog,
        listeners: Listeners,
        held: dict[str, _Served],
    ) -> None:
        self._config_dir, self._state_dir = config_dir, state_dir
        self._secrets, self._clock, self._calls = secrets, clock, calls
        self._listeners = listeners
        self._lock = asyncio.Lock()
        self._held = held

    def held(self) -> Mapping[str, _Served]:
        """What is held now, retired owners included, as a snapshot: a lookup can add an
        owner while the caller is still awaiting something for another (#67)."""
        return dict(self._held)

    async def lookup(self, name: str) -> _Served | None:
        """The Upstream called ``name``, adopting it now if its directory appeared since.

        The route's fallback so a request never depends on a discovery pass having run: a
        held, live owner is refreshed and returned; otherwise the file is looked for and, if
        there, added, replacing a retired owner of the same name with a fresh one. A retired
        owner with no file for its name is left in place, unlisted (#67).
        """
        owner = self._held.get(name)
        if owner is not None and not owner.retired:
            await owner.refresh()
            if not owner.retired:
                return owner
        async with self._lock:
            owner = self._held.get(name)  # another request may have replaced it meanwhile
            if owner is None or owner.retired:
                if not (upstream_dir(self._config_dir, name) / UPSTREAM_FILE).is_file():
                    return None
                if owner is not None:
                    await owner.stop()  # idempotent: its connection and Proxies are stopped
                return await self._add(name)
            return owner

    async def _add(self, name: str) -> _Served | None:
        """Build, hold, and launch a fresh owner for ``name``, or say why it cannot be read."""
        settings = load_settings(self._config_dir)
        try:
            upstream = load_upstream(self._config_dir, name, settings.lifecycle)
        except ConfigError as exc:
            log.warning("Upstream %s was found but cannot be read: %s", name, exc)
            return None
        owner = _Served(
            self._config_dir,
            self._state_dir,
            upstream,
            settings.caps,
            self._secrets,
            self._clock,
            self._calls,
            self._listeners,
        )
        self._held[name] = owner
        owner.launch()
        return owner

    async def _discover(self) -> None:
        """Add an owner for every Upstream directory not already held live (#67)."""
        upstreams_dir = self._config_dir / UPSTREAMS_DIR
        if not upstreams_dir.is_dir():
            return
        for path in sorted(upstreams_dir.iterdir()):
            if not (path / UPSTREAM_FILE).is_file():
                continue
            name = path.name
            owner = self._held.get(name)
            if owner is not None and not owner.retired:
                continue
            async with self._lock:
                owner = self._held.get(name)
                if owner is not None and not owner.retired:
                    continue
                if owner is not None:
                    await owner.stop()
                await self._add(name)

    async def refresh(self) -> None:
        """Discover, then refresh every held owner: what ``Management.live()`` calls."""
        await self._discover()
        for owner in list(self._held.values()):
            await owner.refresh()

    async def reload(self) -> None:
        """Discover, then reload every held owner: what ``daemon reload`` calls."""
        await self._discover()
        for owner in list(self._held.values()):
            await owner.reload()

    async def start(self) -> None:
        """Launch every owner held at build, and wait for every launch: the Daemon's lifespan.

        Awaiting the launch task itself, not just ``ready()``, is what lets an exception at
        Daemon start still propagate as it did before #67.
        """
        owners = list(self._held.values())
        for owner in owners:
            owner.launch()
        await asyncio.gather(*(owner.launched() for owner in owners))

    async def stop(self) -> None:
        """Stop every held owner, in order: the Daemon's lifespan again."""
        for owner in list(self._held.values()):
            await owner.stop()


_Key = tuple[str, str]
"""A Proxy by Upstream name and Proxy name."""


@dataclass
class _Listener:
    """One listener on one port, serving one Proxy, until its own ``stop``."""

    key: _Key
    proxy: _Proxy
    task: asyncio.Task[None]
    stop: asyncio.Event

    async def close(self) -> None:
        self.stop.set()
        try:
            await self.task
        except Exception:
            log.warning("a Proxy port listener did not close cleanly", exc_info=True)


class Listeners:
    """The additional listeners the Proxies' port overrides ask for (#13), kept in step with
    the files while the Daemon runs (#70).

    A Proxy asks for its port whenever it derives, and gives it up when it stops; ``serve``
    is the loop that starts a listener on every port asked for, moves one whose port changed,
    closes one no longer asked for, and answers a bind that fails by telling the Proxy why, so
    it reports itself unhealthy with the reason while its path is served as before. The next
    look at the file, or ``daemon reload``, asks again, which is the retry. Two Proxies asking
    for one port get it in name order: the first listens, the second is told who has it. A
    listener is the Proxy's, not its name's: a Proxy let go and a new one under the same name
    and port is a listener closed and another opened. The loop wakes when what is asked for
    changes and when a listener ends on its own, which is logged and, while its port is still
    asked for, tried again.
    """

    def __init__(self, token: str | None) -> None:
        self._token = token
        self._wanted: dict[_Key, tuple[int, _Proxy]] = {}
        self._changed = asyncio.Event()

    def want(self, key: _Key, port: int | None, proxy: _Proxy) -> None:
        """What ``key``'s file asks for now: a port, or none."""
        if port is None:
            self._wanted.pop(key, None)
            proxy.listener_problem = None
        else:
            self._wanted[key] = (port, proxy)
        self._changed.set()

    def forget(self, key: _Key) -> None:
        """``key`` is stopping, so nothing is listened on for it any more."""
        if self._wanted.pop(key, None) is not None:
            self._changed.set()

    async def serve(self, host: str, stop: asyncio.Event) -> None:
        """Keep the listeners in step with what is asked for, until ``stop``."""
        running: dict[int, _Listener] = {}
        try:
            while not stop.is_set():
                await self._reconcile(host, running)
                ended = [listener.task for listener in running.values()]
                await _either((self._changed, stop), ended)
                self._changed.clear()
        finally:
            for listener in running.values():
                await listener.close()

    async def _reconcile(self, host: str, running: dict[int, _Listener]) -> None:
        for port, listener in list(running.items()):
            if listener.task.done():
                del running[port]
                _reap(listener, port)
        wanted = self._by_port()
        for port, listener in list(running.items()):
            if port not in wanted or wanted[port][1] is not listener.proxy:
                await listener.close()
                del running[port]
                log.info("Proxy %s/%s: no longer listening on port %d", *listener.key, port)
        wanted = self._by_port()  # asked again: every close above was awaited
        for port, (key, proxy) in wanted.items():
            if port in running:
                continue
            try:
                sock = _bound(host, port)
            except OSError as exc:
                problem = f"port {port} cannot be bound: {exc.strerror or exc}"
                if problem != proxy.listener_problem:
                    log.warning("Proxy %s/%s: %s", *key, problem)
                proxy.listener_problem = problem
                continue
            proxy.listener_problem = None
            own_stop = asyncio.Event()
            task = asyncio.create_task(
                serve(
                    _authed(proxy, self._token),
                    host,
                    port,
                    own_stop,
                    lifespan="off",
                    sockets=[sock],
                    signals=False,
                )
            )
            running[port] = _Listener(key, proxy, task, own_stop)
            log.info("Proxy %s/%s: listening on port %d", *key, port)

    def _by_port(self) -> dict[int, tuple[_Key, _Proxy]]:
        """Every port asked for, by the first Proxy in name order that asks for it."""
        by_port: dict[int, tuple[_Key, _Proxy]] = {}
        for key, (port, proxy) in sorted(self._wanted.items()):
            if port in by_port:
                holder = by_port[port][0]
                proxy.listener_problem = f"port {port} is taken by Proxy {holder[0]}/{holder[1]}"
                continue
            by_port[port] = (key, proxy)
        return by_port


def _bound(host: str, port: int) -> socket.socket:
    """A listening socket on ``host``:``port``, or the ``OSError`` that says why not."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        sock.listen(128)
    except OSError:
        sock.close()
        raise
    return sock


def _reap(listener: _Listener, port: int) -> None:
    """Say how a listener that ended on its own ended; nothing here cancels one."""
    if listener.task.cancelled():
        return
    if (failure := listener.task.exception()) is not None:
        log.warning("Proxy %s/%s: the listener on port %d died: %s", *listener.key, port, failure)
        return
    log.info("Proxy %s/%s: the listener on port %d ended", *listener.key, port)


async def _either(events: tuple[asyncio.Event, ...], tasks: list[asyncio.Task[None]]) -> None:
    """Wait until any one of ``events`` is set or any one of ``tasks`` is done.

    The tasks are watched, never cancelled: only the waits on the events are this function's.
    """
    waits = [asyncio.ensure_future(event.wait()) for event in events]
    try:
        await asyncio.wait([*waits, *tasks], return_when=asyncio.FIRST_COMPLETED)
    finally:
        for wait in waits:
            wait.cancel()
        await asyncio.gather(*waits, return_exceptions=True)


@dataclass(frozen=True)
class DaemonApp:
    """What the Daemon serves: the main ASGI app, and the listeners its Proxies' port
    overrides ask for, kept in step with the files while it runs (#70).

    ``stop`` is what ``/api/shutdown`` sets and what ``serve`` watches to close its sockets.
    """

    main: Starlette
    listeners: Listeners
    stop: asyncio.Event


def build_app(
    config_dir: Path,
    state_dir: Path,
    clock: Clock | None = None,
    token: str | None = None,
) -> DaemonApp:
    """The Daemon apps for the Upstreams registered under ``config_dir``.

    Every Proxy is served at ``/<upstream>/<proxy>/mcp``; the ``default`` Proxy also at
    ``/<upstream>/mcp``; live state at ``/api/status``; ``/api/shutdown`` stops it; the
    dashboard at ``/`` unless ``[daemon] dashboard = false`` (#17). ``clock``
    is what every lifecycle timer runs on, so tests advance time instead of waiting for it. A
    Proxy whose file sets ``port`` is also served alone on that additional listener, which
    ``.listeners`` keeps in step with the file while the Daemon runs (#70); ``serve_all`` is
    what runs them. ``token``, when given, requires ``Authorization: Bearer <token>`` on every
    request to any of them.
    """
    running_clock = clock or SystemClock()
    loaded = load_upstreams(config_dir)  # raises on a broken file, so the build still fails
    settings = load_settings(config_dir)
    configure_app_log(state_dir, settings.log.level, settings.log.max_bytes)
    calls = CallLog(RotatingFile(call_log_file(state_dir), settings.log.max_bytes))
    secrets = secrets_for(config_dir)
    listeners = Listeners(token)
    held = {
        upstream.name: _Served(
            config_dir, state_dir, upstream, settings.caps, secrets, clock, calls, listeners
        )
        for upstream in loaded
    }
    upstreams = _Upstreams(config_dir, state_dir, secrets, clock, calls, listeners, held)
    stop = asyncio.Event()
    management = Management(
        upstreams=upstreams,
        calls=calls,
        state_dir=state_dir,
        secrets=secrets,
        clock=running_clock,
        stop=stop,
    )
    routes: list[BaseRoute] = management.routes()
    routes += [
        Mount("/{upstream}", app=_ProxyRoute(upstreams)),
    ]
    if settings.daemon.dashboard:
        # last, since "/" matches everything a route above did not: the page and its files
        routes.append(Mount("/", app=StaticFiles(directory=dashboard.directory(), html=True)))

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncGenerator[None]:
        async with contextlib.AsyncExitStack() as stack:
            stack.push_async_callback(management.close)
            stack.push_async_callback(upstreams.stop)
            await upstreams.start()  # a launch that fails here unwinds what started already
            yield

    main = Starlette(routes=routes, lifespan=lifespan, middleware=_middleware(token))
    main.router.redirect_slashes = False  # a path nothing serves is not found, not redirected
    return DaemonApp(main=main, listeners=listeners, stop=stop)


async def serve(  # noqa: PLR0913  # a listener is what, where, for whom, until when, and how
    app: ASGIApp,
    host: str,
    port: int,
    stop: asyncio.Event | None = None,
    *,
    lifespan: Literal["on", "off"] = "on",
    sockets: list[socket.socket] | None = None,
    signals: bool = True,
) -> None:
    """Serve ``app`` on ``host``:``port`` until ``stop`` is set or the process is signalled.

    The Daemon process's main loop. A cooperative stop lets the server close its socket;
    cancelling the task would leave it open. ``lifespan="off"`` is for a Proxy's port
    override: the Proxy's own lifespan is already run once, by the main app. ``sockets``,
    when given, are already bound, so a port that cannot be had is known before anything is
    served (#70). ``signals=False`` is for a port override too: uvicorn takes the process's
    SIGINT and SIGTERM handlers for every server it serves and puts back, when that server
    ends, whatever it found, so a listener that comes and goes would leave the handlers on a
    server that is gone; the main server alone owns them, and its end is what stops the rest.
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
        if signals:
            await server.serve(sockets=sockets)
        else:
            await server._serve(sockets)  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]  # serve() without its signal handlers
    finally:
        stopper.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await stopper


async def serve_all(daemon: DaemonApp, host: str, port: int) -> None:
    """Serve the main app on ``host``:``port``, and every Proxy port override alongside it,
    started, moved, and closed as the Proxy files change (#70).

    The main server ends on ``daemon down`` or a signal; either way its end sets ``stop``,
    so the listeners close with it and this returns.
    """

    async def main() -> None:
        try:
            await serve(daemon.main, host, port, daemon.stop)
        finally:
            daemon.stop.set()

    await asyncio.gather(main(), daemon.listeners.serve(host, daemon.stop))


def run(config_dir: Path, state_dir: Path) -> None:
    """Build the Daemon app from ``config_dir`` and serve it, and every port override, until
    ``daemon down`` or a signal stops it. Refuses an unguarded non-loopback bind itself."""
    settings = load_settings(config_dir).daemon
    check_bind(settings)
    app = build_app(config_dir, state_dir, token=settings.token)
    log.info("Daemon starting on %s:%d", settings.host, settings.port)
    asyncio.run(serve_all(app, settings.host, settings.port))
    log.info("Daemon stopped")
