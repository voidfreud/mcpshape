"""The management API under ``/api``: the live state the CLI's live commands and the dashboard
read, and the few things they trigger (#16).

Nothing here is configuration: config is read from files whether the Daemon runs or not, and
edited by the CLI alone. What lives here is what only a running Daemon knows or does: every
Upstream's connection and every Proxy's health, the stored Catalog and Drift, the call log
and the app log's tail, a reload, a rescan, and the OAuth flow for the dashboard. The CLI
reads the answers back through the same models.

Routes, all behind the bearer token when one is configured:

- ``GET /api/status``: every Upstream and Proxy, with lifecycle state and health; an Upstream
  or Proxy added while the Daemon runs is found and listed too (#67).
- ``POST /api/reload``: every Upstream file is re-read, changed or not, and so is every
  file of every Proxy left; an Upstream or Proxy added while the Daemon runs is found the same
  way; answers the live state after.
- ``POST /api/shutdown``: what ``daemon down`` posts to.
- ``GET /api/upstreams/<name>/catalog``: the accepted Catalog, or ``null`` before a scan.
- ``GET /api/upstreams/<name>/drift``: the unreviewed Drift, or ``null``.
- ``GET /api/upstreams/<name>/proxies/<proxy>/exposed``: the Proxy's exposed set as the
  Daemon derives it, the Catalog with its Overrides and Caps applied and its Virtual Tools,
  beside what it hides, and the Proxy's health, since an unhealthy Proxy keeps its last
  exposed set (#17).
- ``POST /api/upstreams/<name>/sync``: scan now and record the Drift. Accepting it edits
  Proxy files, so it stays the CLI's: ``upstream sync --accept``.
- ``GET`` and ``POST /api/upstreams/<name>/oauth``: the login's state, and starting one.
- ``POST /api/upstreams/<name>/connect``: connect now, from cold or from the backoff;
  answers the connection's state. What ``upstream connect`` posts to (#64), and what a login
  from the CLI is followed by (#58).
- ``GET /api/calls?upstream=&proxy=&limit=``: the latest calls, oldest first.
- ``GET /api/logs?lines=``: the app log's tail.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import os
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import BaseModel, Field
from starlette.responses import JSONResponse
from starlette.routing import Route

from mcpshape import catalog as catalogs
from mcpshape.adapters.fastmcp import CALLBACK_TIMEOUT, logged_in, login, server_name
from mcpshape.calls import CallRecord
from mcpshape.commands import command_missing
from mcpshape.connection import TimedOutError, bounded
from mcpshape.logs import tail
from mcpshape.model import HttpTransport, SseTransport, StdioTransport
from mcpshape.paths import daemon_log_file
from mcpshape.secrets import SecretError
from mcpshape.tokens import Tokens

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping
    from pathlib import Path

    from starlette.requests import Request
    from starlette.routing import BaseRoute

    from mcpshape.adapters.fastmcp import UpstreamConnection
    from mcpshape.calls import CallLog
    from mcpshape.connection import Clock
    from mcpshape.model import Upstream
    from mcpshape.proxy import Exposed
    from mcpshape.secrets import Secrets

log = logging.getLogger("mcpshape.api")

STATUS_PATH = "/api/status"
RELOAD_PATH = "/api/reload"
SHUTDOWN_PATH = "/api/shutdown"
CALLS_PATH = "/api/calls"
LOGS_PATH = "/api/logs"
UPSTREAMS_PATH = "/api/upstreams"

DEFAULT_LIMIT = 100
MOST_ENTRIES = 10_000
"""The most lines or calls one ``/api/logs`` or ``/api/calls`` answer carries, whatever was
asked."""

PAGE_WAIT = 10.0
"""Seconds a ``POST`` to the OAuth flow waits for the provider's page before answering
``pending`` with no page yet; ``GET`` picks it up later."""

NOT_FOUND, BAD_REQUEST, UPSTREAM_FAILED = 404, 400, 502


class _ScanFailedError(Exception):
    """An Upstream could not be scanned; the message says why, with nothing concealed lost."""


# --- what the API answers ------------------------------------------------------------------


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
    supervised: bool = True
    """False once the keeper gave up on this Upstream, until a reload starts one (#50)."""
    warm: bool = False
    retry_in: float | None = None
    """Seconds until a warm Upstream's keeper tries to connect again, while it is
    ``unavailable``; nothing for a lazy one, whose next call is what tries again (#64)."""
    missing_command: str | None = None
    """The stdio command nothing on the Daemon's PATH is, when there is one (#48)."""
    proxies: list[ProxyState] = Field(default_factory=list[ProxyState])


class LiveState(BaseModel):
    """What ``/api/status`` answers. The CLI reads it back through the same model."""

    upstreams: list[UpstreamState] = Field(default_factory=list[UpstreamState])
    path: str | None = None
    """The PATH the Daemon runs under: what ``missing_command`` was looked for on, which the
    CLI's own PATH need not be, since autostart gives the Daemon the one install captured."""


class ItemRef(BaseModel):
    """One Catalog item, by kind and Catalog name."""

    kind: str
    name: str


class DriftState(BaseModel):
    """Unreviewed Drift of one Upstream, as ``/api/upstreams/<name>/drift`` answers it."""

    added: list[ItemRef] = Field(default_factory=list[ItemRef])
    removed: list[ItemRef] = Field(default_factory=list[ItemRef])
    changed: list[ItemRef] = Field(default_factory=list[ItemRef])
    instructions_changed: bool = False
    summary: str = ""

    @classmethod
    def of(cls, drift: catalogs.Drift) -> DriftState:
        def refs(items: tuple[catalogs.Item, ...]) -> list[ItemRef]:
            return [ItemRef(kind=item.kind, name=item.name) for item in items]

        return cls(
            added=refs(drift.added),
            removed=refs(drift.removed),
            changed=refs(drift.changed),
            instructions_changed=drift.instructions_changed,
            summary=drift.summary(),
        )


class CatalogAnswer(BaseModel):
    """``/api/upstreams/<name>/catalog``: the accepted Catalog, or nothing before a scan."""

    catalog: catalogs.Catalog | None = None


class ExposedItem(BaseModel):
    """One item as a Proxy exposes it, or one it hides (#17)."""

    kind: str
    name: str
    """The exposed name; for a hidden item, its Catalog name."""
    origin: str | None = None
    """The Catalog name it stands for; nothing for a Virtual Tool."""
    description: str | None = None
    hidden: bool = False
    virtual: bool = False


class ExposedAnswer(BaseModel):
    """``/api/upstreams/<name>/proxies/<proxy>/exposed``: the exposed set, and what is hidden."""

    name: str
    """The server name a Client sees."""
    health: str
    detail: str | None = None
    """The Proxy's health as ``/api/status`` reports it: an unhealthy Proxy keeps advertising
    its last exposed set, which is what ``items`` then is."""
    scanned: bool = True
    """Whether the Upstream has a Catalog at all; before its first scan there is nothing to
    expose and nothing to hide."""
    instructions: str | None = None
    items: list[ExposedItem] = Field(default_factory=list[ExposedItem])


class DriftAnswer(BaseModel):
    """``/api/upstreams/<name>/drift``: the unreviewed Drift, or nothing."""

    drift: DriftState | None = None


class SyncState(BaseModel):
    """What a ``/api`` sync found: a first Catalog, or the Drift since the stored one."""

    first: bool
    drift: DriftState | None = None


class LoginState(BaseModel):
    """Where an Upstream's OAuth login stands. Never carries a token."""

    stored: bool
    """Whether a token set is on disk. Says nothing about whether it still works."""
    pending: bool
    """Whether a login started here is waiting for the browser."""
    url: str | None = None
    """The provider's page the user must open, while a login is pending."""
    error: str | None = None
    """Why the last login started here failed, until the next one starts."""


class ConnectAnswer(BaseModel):
    """``POST /api/upstreams/<name>/connect``: where the connection stands right after."""

    state: str


class CallsAnswer(BaseModel):
    calls: list[CallRecord] = Field(default_factory=list[CallRecord])


class LogsAnswer(BaseModel):
    lines: list[str] = Field(default_factory=list[str])


# --- what the API is built over --------------------------------------------------------------


class ProxyLike(Protocol):
    """One Proxy as the API needs it: its live state, its exposed set, and a reload."""

    async def state(self) -> ProxyState: ...
    async def exposed_now(self) -> Exposed: ...
    async def reload(self) -> None: ...


class ServedLike(Protocol):
    """One Upstream as the API needs it: what the Daemon holds for it, read at call time.

    An Upstream file the Daemon re-reads can change the Upstream under the API between one
    request and the next, and a removed one retires it, so nothing here is copied at build
    (#46, #62).
    """

    @property
    def upstream(self) -> Upstream: ...
    @property
    def connection(self) -> UpstreamConnection: ...
    @property
    def proxies(self) -> Mapping[str, ProxyLike]: ...
    @property
    def retired(self) -> bool: ...
    async def proxy(self, name: str) -> ProxyLike | None: ...


class UpstreamsLike(Protocol):
    """Every Upstream the Daemon holds, read at call time, and found on demand (#67)."""

    def held(self) -> Mapping[str, ServedLike]: ...
    async def lookup(self, name: str) -> ServedLike | None: ...
    async def refresh(self) -> None: ...
    async def reload(self) -> None: ...


class _Login:
    """One Upstream's OAuth flow as the dashboard drives it (#16).

    The Daemon opens no browser: it starts the same login the CLI runs, hands the provider's
    page back to whoever asked, and receives the callback on loopback as the CLI would. What
    it stores is what the CLI stores. A login that succeeds is followed by ``on_success``: the
    Upstream is scanned, since the Daemon's start-up scan had nothing to log in with, and
    then made to connect at once instead of waiting out its backoff. A login nobody finishes
    is given up after ``CALLBACK_TIMEOUT``, the browser flow's own patience, so a fresh one
    can start; a start while one is pending joins it. The Upstream is read from its owner when
    a login starts, so an edited file is what the login runs against (#46).
    """

    def __init__(
        self,
        served: ServedLike,
        secrets: Secrets,
        tokens: Tokens,
        on_success: Callable[[], Awaitable[None]],
    ) -> None:
        self._served = served
        self._secrets = secrets
        self._tokens = tokens
        self._on_success = on_success
        self._task: asyncio.Task[None] | None = None
        self._opened = asyncio.Event()
        self.url: str | None = None
        self.error: str | None = None

    @property
    def _upstream(self) -> Upstream:
        return self._served.upstream

    def state(self) -> LoginState:
        pending = self._task is not None and not self._task.done()
        return LoginState(
            stored=logged_in(self._tokens),
            pending=pending,
            url=self.url if pending else None,
            error=self.error,
        )

    async def start(self) -> LoginState:
        """Start a login unless one is pending, and answer once its page is known, it failed,
        or ``PAGE_WAIT`` has passed."""
        if self._task is None or self._task.done():
            self.url, self.error = None, None
            self._opened = asyncio.Event()
            self._task = asyncio.create_task(self._run())
        opened = asyncio.create_task(self._opened.wait())
        await asyncio.wait(
            {opened, self._task}, return_when=asyncio.FIRST_COMPLETED, timeout=PAGE_WAIT
        )
        opened.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await opened
        return self.state()

    async def _run(self) -> None:
        try:
            async with asyncio.timeout(CALLBACK_TIMEOUT):
                await login(
                    self._upstream.transport,
                    self._secrets,
                    self._tokens,
                    lambda text: log.info("Upstream %s login: %s", self._upstream.name, text),
                    opener=self._open,
                )
        except TimeoutError:
            self.error = f"nobody finished the login within {CALLBACK_TIMEOUT:.0f} seconds"
            log.warning("Upstream %s: %s", self._upstream.name, self.error)
            return
        except Exception as exc:  # noqa: BLE001  # however the provider refused, the state says why
            self.error = str(exc) or type(exc).__name__
            log.warning("Upstream %s could not log in: %s", self._upstream.name, self.error)
            return
        await self._on_success()

    async def _open(self, url: str) -> None:
        self.url = url
        self._opened.set()

    async def close(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task


@dataclass
class Management:
    """Everything the API answers from: built by the Daemon, one per Daemon."""

    upstreams: UpstreamsLike
    calls: CallLog
    state_dir: Path
    secrets: Secrets
    clock: Clock
    stop: asyncio.Event
    _logins: dict[str, tuple[ServedLike, _Login]] = field(
        default_factory=dict[str, tuple[ServedLike, _Login]]
    )

    def routes(self) -> list[BaseRoute]:
        return [
            Route(STATUS_PATH, self._status),
            Route(RELOAD_PATH, self._reload, methods=["POST"]),
            Route(SHUTDOWN_PATH, self._shutdown, methods=["POST"]),
            Route(f"{UPSTREAMS_PATH}/{{upstream}}/catalog", self._catalog),
            Route(f"{UPSTREAMS_PATH}/{{upstream}}/drift", self._drift),
            Route(f"{UPSTREAMS_PATH}/{{upstream}}/proxies/{{proxy}}/exposed", self._exposed),
            Route(f"{UPSTREAMS_PATH}/{{upstream}}/sync", self._sync, methods=["POST"]),
            Route(f"{UPSTREAMS_PATH}/{{upstream}}/oauth", self._login_state),
            Route(f"{UPSTREAMS_PATH}/{{upstream}}/oauth", self._login_start, methods=["POST"]),
            Route(f"{UPSTREAMS_PATH}/{{upstream}}/connect", self._connect, methods=["POST"]),
            Route(CALLS_PATH, self._calls),
            Route(LOGS_PATH, self._logs),
        ]

    async def close(self) -> None:
        """Let go of whatever the API started and is still waiting on: pending logins."""
        for _served, pending in self._logins.values():
            await pending.close()

    async def live(self) -> LiveState:
        """What every Upstream and Proxy is doing right now, each refreshed on the way.

        The registry is refreshed first, so an Upstream or Proxy added since is found (#67),
        one that was edited is in force, and one that is gone is retired, before anything is
        reported; a retired Upstream is not listed at all (#46, #62). A launching Upstream is
        listed as it is, its connection ``cold`` until the launch starts it: nothing here
        awaits ``ready()``, since the CLI reads live state on a short timeout and a launch can
        take a full ``connect_timeout``.
        """
        await self.upstreams.refresh()
        return LiveState(
            upstreams=[
                await self._state_of(served)
                for served in self.upstreams.held().values()
                if not served.retired
            ],
            path=os.environ.get("PATH"),
        )

    async def _state_of(self, served: ServedLike) -> UpstreamState:
        status = served.connection.status()
        return UpstreamState(
            name=served.upstream.name,
            state=status.state,
            seconds=round(status.seconds, 3),
            error=status.error,
            supervised=status.supervised,
            warm=status.warm,
            retry_in=None if status.retry_in is None else round(status.retry_in, 3),
            missing_command=self._missing_command(served.upstream),
            proxies=[await proxy.state() for proxy in list(served.proxies.values())],
        )

    def _missing_command(self, upstream: Upstream) -> str | None:
        """The command an stdio Upstream would spawn that this Daemon's PATH does not answer.

        Looked for on the Daemon's own PATH, which under autostart is the one ``daemon
        install`` captured; a command that is a ``${VAR}`` reference nothing resolves is left
        to ``doctor``, which names the unset variable instead.
        """
        transport = upstream.transport
        if not isinstance(transport, StdioTransport):
            return None
        try:
            resolved = self.secrets.expanded(transport)
        except SecretError:
            return None
        return command_missing(resolved, os.environ.get("PATH"))

    # --- the endpoints ---------------------------------------------------------------------

    async def _status(self, _request: Request) -> JSONResponse:
        return _answer(await self.live())

    async def _reload(self, _request: Request) -> JSONResponse:
        """Every file is re-read now, changed or not (#10, #46), and it says how it went.

        Every Upstream file first, so an Upstream or Proxy added since is found (#67), an edit
        to it is in force, and a removed Upstream is retired, then every Proxy of what is
        left, and every connection is supervised again, which is what brings back a keeper
        that gave up (#50) and nothing at all for an Upstream whose keeper is still running.
        """
        await self.upstreams.reload()
        return _answer(await self.live())

    async def _shutdown(self, _request: Request) -> JSONResponse:
        self.stop.set()
        return JSONResponse({"stopping": True})

    async def _catalog(self, request: Request) -> JSONResponse:
        return await self._for_upstream(request, self._catalog_of)

    async def _catalog_of(self, served: ServedLike) -> JSONResponse:
        name = served.upstream.name
        stored = await asyncio.to_thread(catalogs.load_catalog, self.state_dir, name)
        return _answer(CatalogAnswer(catalog=stored))

    async def _drift(self, request: Request) -> JSONResponse:
        return await self._for_upstream(request, self._drift_of)

    async def _drift_of(self, served: ServedLike) -> JSONResponse:
        name = served.upstream.name
        drift = await asyncio.to_thread(catalogs.load_drift, self.state_dir, name)
        return _answer(DriftAnswer(drift=DriftState.of(drift) if drift else None))

    async def _exposed(self, request: Request) -> JSONResponse:
        proxy_name: str = request.path_params["proxy"]
        return await self._for_upstream(request, partial(self._exposed_of, proxy_name))

    async def _exposed_of(self, proxy_name: str, served: ServedLike) -> JSONResponse:
        """The Proxy's exposed set beside what the Catalog has that it hides (#17)."""
        upstream = served.upstream.name
        proxy = await served.proxy(proxy_name)
        if proxy is None:
            return _refusal(f"no Proxy {upstream}/{proxy_name}", NOT_FOUND)
        health = await proxy.state()
        exposed = await proxy.exposed_now()
        stored = await asyncio.to_thread(catalogs.load_catalog, self.state_dir, upstream)
        return _answer(
            ExposedAnswer(
                name=server_name(served.upstream, proxy_name, exposed),
                health=health.health,
                detail=health.detail,
                scanned=stored is not None,
                instructions=exposed.catalog.instructions,
                items=_exposed_items(exposed, stored),
            )
        )

    async def _sync(self, request: Request) -> JSONResponse:
        return await self._for_upstream(request, self._sync_of)

    async def _sync_of(self, served: ServedLike) -> JSONResponse:
        try:
            return _answer(await self._scanned(served))
        except _ScanFailedError as exc:
            return _refusal(str(exc), UPSTREAM_FAILED)

    async def _scanned(self, served: ServedLike) -> SyncState:
        """Scan the Upstream now, over its open connection when it has one, and record it.

        Bounded by the Upstream's own ``connect_timeout``, as the start-up scan is (#20).
        Recording holds the Upstream's lock (#21), off the event loop. Raises
        ``_ScanFailedError`` with the reason when the Upstream could not be scanned, or when
        its state was removed while the scan waited for the lock (#49).
        """
        upstream = served.upstream
        try:
            observed = await bounded(
                served.connection.observe(), upstream.lifecycle.connect_timeout, self.clock
            )
        except TimedOutError:
            seconds = upstream.lifecycle.connect_timeout
            msg = (
                f"{upstream.name} could not be scanned within its connect_timeout of {seconds:.0f}s"
            )
            raise _ScanFailedError(msg) from None
        except Exception as exc:  # however the Upstream failed, the caller gets the why
            msg = f"{upstream.name} could not be scanned: {exc}"
            raise _ScanFailedError(msg) from exc
        try:
            scan = await asyncio.to_thread(
                catalogs.record_scan, self.state_dir, upstream.name, observed
            )
        except catalogs.ForgottenError as exc:  # an upstream rm won the lock first (#49)
            raise _ScanFailedError(str(exc)) from None
        return SyncState(first=scan.first, drift=DriftState.of(scan.drift) if scan.drift else None)

    async def _login_state(self, request: Request) -> JSONResponse:
        return await self._for_upstream(request, self._login_state_of)

    async def _login_state_of(self, served: ServedLike) -> JSONResponse:
        found = await self._login_of(served)
        if found is None:
            return _refusal(_not_oauth(served.upstream), BAD_REQUEST)
        return _answer(found.state())

    async def _login_start(self, request: Request) -> JSONResponse:
        return await self._for_upstream(request, self._login_start_of)

    async def _login_start_of(self, served: ServedLike) -> JSONResponse:
        found = await self._login_of(served)
        if found is None:
            return _refusal(_not_oauth(served.upstream), BAD_REQUEST)
        return _answer(await found.start())

    async def _login_of(self, served: ServedLike) -> _Login | None:
        """The cached login for ``served``, closing and replacing a stale one first.

        A re-added Upstream is a new owner (#67): ``served`` is compared by identity to what
        was cached, so a new owner gets a new ``_Login`` bound to it, and the old one, bound
        to an owner nothing reaches any more, is closed.
        """
        upstream = served.upstream
        transport = upstream.transport
        if not isinstance(transport, HttpTransport | SseTransport) or transport.auth != "oauth":
            return None
        cached = self._logins.get(upstream.name)
        if cached is not None and cached[0] is served:
            return cached[1]
        if cached is not None:
            await cached[1].close()
        login = _Login(
            served,
            self.secrets,
            Tokens(self.state_dir, upstream.name),
            partial(self._logged_in, served),
        )
        self._logins[upstream.name] = (served, login)
        return login

    async def _logged_in(self, served: ServedLike) -> None:
        """Scan the Upstream a login just made reachable, and have it connect now."""
        try:
            await self._scanned(served)
        except _ScanFailedError as exc:
            log.warning("after logging in, %s", exc)
        served.connection.retry()

    async def _connect(self, request: Request) -> JSONResponse:
        return await self._for_upstream(request, self._connect_of)

    async def _connect_of(self, served: ServedLike) -> JSONResponse:
        """Have the Upstream connect now, cold or ``unavailable``, and say where it stands."""
        served.connection.connect_now()
        return _answer(ConnectAnswer(state=served.connection.status().state))

    async def _calls(self, request: Request) -> JSONResponse:
        """The latest calls of every Proxy, or of the Upstream or Proxy named, oldest first."""
        limit = _count(request, "limit")
        if limit is None:
            return _refusal("limit must be a positive integer", BAD_REQUEST)
        recent = self.calls.recent(
            request.query_params.get("upstream"), request.query_params.get("proxy"), limit
        )
        return _answer(CallsAnswer(calls=recent))

    async def _logs(self, request: Request) -> JSONResponse:
        lines = _count(request, "lines")
        if lines is None:
            return _refusal("lines must be a positive integer", BAD_REQUEST)
        found = await asyncio.to_thread(tail, daemon_log_file(self.state_dir), lines)
        return _answer(LogsAnswer(lines=found))

    async def _for_upstream(
        self, request: Request, answer: Callable[[ServedLike], Awaitable[JSONResponse]]
    ) -> JSONResponse:
        """Answer for the Upstream the path names, found and re-read on the way (#46, #67).

        ``lookup`` finds one added since, refreshes one already held, and answers ``None`` for
        one whose file is gone or never existed, which is as unknown here as a name nothing
        was ever registered under (#62).
        """
        name: str = request.path_params["upstream"]
        found = await self.upstreams.lookup(name)
        if found is None:
            return _refusal(f"no Upstream named {name!r}", NOT_FOUND)
        return await answer(found)


def _exposed_items(exposed: Exposed, stored: catalogs.Catalog | None) -> list[ExposedItem]:
    """Every exposed item under its exposed name, every hidden Catalog item, and every
    Virtual Tool, in that order within each kind."""
    items: list[ExposedItem] = []
    for kind in catalogs.KINDS:
        field_name = f"{kind}s"
        covered: set[str] = set()
        for name, definition in getattr(exposed.catalog, field_name).items():
            origin = exposed.origin(catalogs.Item(kind=kind, name=name))
            covered.add(origin)
            items.append(
                ExposedItem(
                    kind=kind, name=name, origin=origin, description=definition.get("description")
                )
            )
        if stored is not None:
            for name, definition in getattr(stored, field_name).items():
                if name not in covered:
                    items.append(
                        ExposedItem(
                            kind=kind,
                            name=name,
                            origin=name,
                            description=definition.get("description"),
                            hidden=True,
                        )
                    )
        if kind == "tool":
            items += [
                ExposedItem(
                    kind=kind,
                    name=name,
                    description=virtual.description or inspect.getdoc(virtual.fn),
                    virtual=True,
                )
                for name, virtual in exposed.code.tools.items()
            ]
    return items


def _answer(model: BaseModel) -> JSONResponse:
    return JSONResponse(model.model_dump(mode="json"))


def _refusal(message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _not_oauth(upstream: Upstream) -> str:
    return f'{upstream.name} is not an Upstream with auth = "oauth", so there is no login'


def _count(request: Request, key: str) -> int | None:
    """``?key=<n>`` as a count between one and ``MOST_ENTRIES``, the default when absent."""
    given: Any = request.query_params.get(key)
    if given is None:
        return DEFAULT_LIMIT
    try:
        count = int(given)
    except ValueError:
        return None
    return min(count, MOST_ENTRIES) if count > 0 else None
