"""The Upstream connection: one per Upstream, shared by its Proxies and every Client.

A Proxy answers ``initialize`` and every list from the stored Catalog, so it stays up and
reachable through an Upstream outage; only a call, a read, or a get wakes the Upstream, and
only those fail while it is away. That is what this state machine is for.

States:

``cold``
    Nothing is connected and no timer is running. The Daemon starts here unless ``warm``.
``connecting``
    A connect attempt is in flight, bounded by ``connect_timeout``.
``ready``
    Connected, with no idle timer running: a ``warm`` Upstream, an ``idle_timeout`` of zero,
    or a connection that has just been used.
``idle-pending``
    Connected, with the idle timer running towards ``idle_timeout``.
``unavailable``
    The last connect or ping failed, or a call found the open connection dead. A backoff timer
    is running; calls fail at once with the Upstream's ``unavailable_message`` rather than
    waiting for it.
``stopping``
    The Daemon is shutting the connection down.

Time is injected: everything the keeper task waits on goes through a ``Clock``, so tests
advance time by hand instead of waiting for it. The idle timer measures the time since the
last call *started*; a call still in flight keeps its own hold on the connection, so letting
go here never cuts one short.

This module knows nothing of FastMCP. What it drives is a ``Link``, which the FastMCP adapter
implements over one shared client.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable

    from mcpshape.model import LifecycleSettings

log = logging.getLogger("mcpshape.connection")

State = Literal["cold", "connecting", "ready", "idle-pending", "unavailable", "stopping"]
CONNECTED: tuple[State, ...] = ("ready", "idle-pending")

BACKOFF_BASE = 1.0
"""Seconds before the first retry of a failed connect."""

BACKOFF_CAP = 60.0
"""The longest the exponential backoff between retries ever grows to."""

KEEPER_RESTART_CAP = 5
"""How many times the keeper restarts itself after an unexpected exception before giving up.

Past the cap the Upstream is moved to ``unavailable`` with the reason visible in the
management API, rather than left connected with no idle disconnect, ping, or retry running."""

_Due = Literal["nothing", "ping", "sleep", "retry"]
"""What the keeper does when the timer it armed runs out."""


class UpstreamUnavailableError(Exception):
    """A call reached an Upstream that is not connected. Carries the message for the Client."""


class Clock(Protocol):
    """The passage of time, so tests can drive it."""

    def now(self) -> float:
        """Seconds on a monotonic scale; only differences mean anything."""
        ...

    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    """Real time, as the Daemon runs on."""

    def now(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class Link(Protocol):
    """The connection itself, as the state machine needs it. Raising means it failed."""

    async def open(self) -> None: ...
    async def close(self) -> None: ...
    async def ping(self) -> None: ...


@dataclass(frozen=True)
class Status:
    """What an Upstream's connection looks like from the outside, for the management API."""

    state: State
    seconds: float
    """How long it has been in this state."""
    error: str | None
    """Why the last connect or ping failed, while that is what put it here."""


class Connection:
    """One Upstream's connection and the lifecycle it moves through.

    Every Proxy of the Upstream and every Client session share one of these (story 74).
    """

    def __init__(
        self,
        name: str,
        link: Link,
        settings: LifecycleSettings,
        clock: Clock | None = None,
        on_reconnect: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._name = name
        self._link = link
        self._settings = settings
        self._clock = clock or SystemClock()
        self._on_reconnect = on_reconnect
        self._state: State = "cold"
        self._since = self._clock.now()
        self._used_at = self._since
        self._checked_at = self._since
        self._error: str | None = None
        self._failures = 0
        self._attempted = False
        self._wake = asyncio.Event()
        self._keeper: asyncio.Task[None] | None = None
        self._connecting: asyncio.Task[None] | None = None
        self._rescanning: asyncio.Task[None] | None = None

    # --- what the Daemon drives ------------------------------------------------------------

    def status(self) -> Status:
        return Status(state=self._state, seconds=self._clock.now() - self._since, error=self._error)

    @asynccontextmanager
    async def running(self) -> AsyncGenerator[None]:
        """Keep the connection for the life of the Daemon: warm it, time it, and let it go."""
        await self.start()
        try:
            yield
        finally:
            await self.stop()

    async def start(self) -> None:
        """Start the keeper, and connect right away when the Upstream is ``warm``."""
        self._keeper = asyncio.create_task(self._keep())
        if self._settings.warm:
            self._begin_connect()

    async def stop(self) -> None:
        self._enter("stopping")
        self._nudge()
        await _finish(self._keeper, self._connecting, self._rescanning)
        self._keeper = self._connecting = self._rescanning = None
        await self._shut()
        self._enter("cold")

    # --- what a call drives ----------------------------------------------------------------

    async def acquire(self) -> None:
        """Wait until the Upstream is connected, waking it if it is asleep.

        Raises ``UpstreamUnavailableError`` with the configured message when it is not
        reachable: while a connect attempt is failing, and at once during the backoff after
        one did, so that no call waits longer than the connect timeout.
        """
        if self._state in CONNECTED:
            self._touch()
            return
        if self._state in ("unavailable", "stopping"):
            raise UpstreamUnavailableError(self._settings.unavailable_message)
        connecting = self._begin_connect()
        self._nudge()
        await asyncio.wait({connecting})
        if self._state in CONNECTED:
            self._touch()
            return
        raise UpstreamUnavailableError(self._settings.unavailable_message)

    async def lost(self, reason: str) -> None:
        """A call found the connection it used dead, which fails it as a failed ping does.

        Only a connected Upstream is failed this way: several calls dying together on one dead
        connection are the one failure, since the first of them moves the state before anything
        is awaited and the rest find the Upstream already ``unavailable``, where every call is
        answered with its message anyway.
        """
        if self._state not in CONNECTED:
            return
        await self._fail(f"a call found the connection dead: {reason}")

    def unscanned(self) -> None:
        """The Daemon's start-up scan reached nothing, so the first connect looks (#57).

        Every connect after the first is a reconnect, which rescans; the first is not, since
        the start-up scan covers it. When that scan failed, the first connect is the first
        look the Daemon gets, so it is made to count as a reconnect.
        """
        self._attempted = True

    def retry(self) -> None:
        """Try again now instead of waiting out the backoff, when that is where it is.

        For a login the ``/api`` flow has just stored (#16): the reason the last attempt
        failed is gone, so the next one need not wait. Anywhere else this is nothing.
        """
        if self._state == "unavailable":
            self._begin_connect()
            self._nudge()

    # --- the keeper ------------------------------------------------------------------------

    async def _keep(self) -> None:
        """Run every time-based transition: the idle timer, the pings, and the backoff.

        An unexpected exception restarts this loop rather than leaving the Upstream connected
        with nothing supervising it; past ``KEEPER_RESTART_CAP`` restarts the Upstream is
        moved to ``unavailable`` instead, with the reason visible in the management API.
        """
        failures = 0
        while not self._stopping():
            try:
                await self._keep_loop()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # an Upstream must never take the Daemon down
                failures += 1
                log.exception(
                    "Upstream %s: the keeper failed (%d/%d)",
                    self._name,
                    failures,
                    KEEPER_RESTART_CAP,
                )
                if failures >= KEEPER_RESTART_CAP:
                    await self._fail(
                        f"the keeper stopped supervising after {failures} failures: {exc}"
                    )
                    return
            else:
                return

    async def _keep_loop(self) -> None:
        while not self._stopping():
            self._wake.clear()
            due, delay = self._plan()
            if await self._wait(delay) and not self._stopping():
                await self._fire(due)

    def _plan(self) -> tuple[_Due, float | None]:
        """Arm the timer this state calls for: what runs out, and in how long."""
        now = self._clock.now()
        if self._state in CONNECTED:
            if self._settings.warm:
                self._enter("ready")
                return "ping", _left(self._settings.ping_interval, now - self._checked_at)
            if self._settings.idle_timeout > 0:
                self._enter("idle-pending")
                return "sleep", _left(self._settings.idle_timeout, now - self._used_at)
            self._enter("ready")
            return "nothing", None
        if self._state == "unavailable":
            return "retry", _left(self._backoff(), now - self._since)
        return "nothing", None

    async def _wait(self, delay: float | None) -> bool:
        """Wait for ``delay`` or for the state to change. True when the timer ran out."""
        waking = asyncio.create_task(self._woken())
        if delay is None:
            await waking
            return False
        timing = asyncio.create_task(self._clock.sleep(delay))
        done = await _first_of(waking, timing)
        return timing in done

    async def _fire(self, due: _Due) -> None:
        """Do what the timer that just ran out asked for, unless the state moved on under it."""
        connected = self._state in CONNECTED
        match due:
            case "ping" if connected:
                await self._ping()
            case "sleep" if connected and self._idle_for() >= self._settings.idle_timeout:
                log.info("Upstream %s has been idle; letting the connection go", self._name)
                await self._shut()
                self._enter("cold")
            case "retry" if self._state == "unavailable":
                self._begin_connect()
            case _:
                pass

    def _idle_for(self) -> float:
        return self._clock.now() - self._used_at

    async def _ping(self) -> None:
        try:
            await self._link.ping()
        except Exception as exc:  # noqa: BLE001  # any failed ping means the Upstream is gone
            await self._fail(f"ping failed: {exc}")
            return
        self._checked_at = self._clock.now()

    # --- connecting ------------------------------------------------------------------------

    def _begin_connect(self) -> asyncio.Task[None]:
        """The connect attempt in flight, starting one if there is none.

        Everything after the first attempt of the Daemon's life is a reconnect, whether the
        Upstream was let go, went away, or never came up: the Daemon's own start-up scan
        covers the first, and a rescan covers every one after it (story 11).
        """
        if self._connecting is None or self._connecting.done():
            self._enter("connecting")
            reconnect, self._attempted = self._attempted, True
            self._connecting = asyncio.create_task(self._connect(reconnect=reconnect))
        return self._connecting

    async def _connect(self, *, reconnect: bool) -> None:
        opening = asyncio.create_task(self._link.open())
        timing = asyncio.create_task(self._clock.sleep(self._settings.connect_timeout))
        done = await _first_of(opening, timing)
        if opening not in done:
            await self._fail(f"connect timed out after {self._settings.connect_timeout}s")
            return
        if (failure := opening.exception()) is not None:
            await self._fail(str(failure) or type(failure).__name__)
            return
        self._ready(reconnect=reconnect)

    def _ready(self, *, reconnect: bool) -> None:
        self._failures = 0
        self._error = None
        self._used_at = self._checked_at = self._clock.now()
        self._enter("ready")
        self._nudge()
        log.info("Upstream %s is connected", self._name)
        if reconnect and self._on_reconnect is not None:
            self._rescanning = asyncio.create_task(self._rescan())

    async def _rescan(self) -> None:
        """A reconnected Upstream may advertise something else, so look again (story 11)."""
        if self._on_reconnect is None:
            return
        try:
            await self._on_reconnect()
        except Exception:  # a rescan must never take the Daemon down
            log.exception("Upstream %s could not be rescanned after reconnecting", self._name)

    async def _fail(self, reason: str) -> None:
        """Record why, enter the backoff, and only then let the dead link go.

        Nothing is awaited before the state moves, so a second call failing on the same dead
        connection cannot count a second failure and restart the backoff under the first.
        """
        self._error = reason
        self._failures += 1
        self._enter("unavailable")
        log.warning(
            "Upstream %s is unavailable (%s); retrying in %.0fs",
            self._name,
            reason,
            self._backoff(),
        )
        await self._shut()
        self._nudge()

    async def _shut(self) -> None:
        try:
            await self._link.close()
        except Exception:  # a dead connection is what we are closing
            log.warning("Upstream %s did not close cleanly", self._name, exc_info=True)

    def _backoff(self) -> float:
        return min(BACKOFF_CAP, BACKOFF_BASE * 2 ** max(0, self._failures - 1))

    # --- state -----------------------------------------------------------------------------

    def _enter(self, state: State) -> None:
        if state == self._state:
            return
        log.debug("Upstream %s: %s -> %s", self._name, self._state, state)
        self._state = state
        self._since = self._clock.now()

    def _touch(self) -> None:
        """A call just went through, so the idle timer starts over."""
        self._used_at = self._clock.now()
        self._enter("ready")
        self._nudge()

    def _stopping(self) -> bool:
        """Read through a call, since the Daemon may stop the connection while we wait."""
        return self._state == "stopping"

    def _nudge(self) -> None:
        """Tell the keeper the state changed under it, so it re-arms its timer."""
        self._wake.set()

    async def _woken(self) -> None:
        await self._wake.wait()


class TimedOutError(Exception):
    """A ``bounded`` call did not finish within its timeout, on the clock it ran on."""


async def bounded[T](coro: Awaitable[T], seconds: float, clock: Clock) -> T:
    """Run ``coro``, raising ``TimedOutError`` if it outlasts ``seconds`` of ``clock`` time.

    Used for the start-up rescan (#20): a hung Upstream must not hold up the Daemon's other
    Upstreams, so each is bounded by its own ``connect_timeout`` on the same clock the
    lifecycle runs on, not on wall-clock time a fake clock cannot see.
    """
    task: asyncio.Task[T] = asyncio.ensure_future(coro)
    timing = asyncio.create_task(clock.sleep(seconds))
    try:
        done, pending = await asyncio.wait({task, timing}, return_when=asyncio.FIRST_COMPLETED)
    except asyncio.CancelledError:
        await _finish(task, timing)
        raise
    await _finish(*pending)
    if task not in done:
        raise TimedOutError
    return task.result()


def _left(timeout: float, elapsed: float) -> float:
    return max(0.0, timeout - elapsed)


async def _first_of(*tasks: asyncio.Task[None]) -> set[asyncio.Task[None]]:
    """Wait for the first of ``tasks``; the rest are cancelled, also when the waiter is.

    ``asyncio.wait`` leaves its members running when the waiting task is cancelled, which
    would orphan a connect attempt or a timer past ``stop()``.
    """
    try:
        done, pending = await asyncio.wait(set(tasks), return_when=asyncio.FIRST_COMPLETED)
    except asyncio.CancelledError:
        await _finish(*tasks)
        raise
    await _finish(*pending)
    return done


async def _finish(*tasks: asyncio.Task[Any] | None) -> None:
    """Cancel whatever is still running and wait for it, swallowing what it raises."""
    running = [task for task in tasks if task is not None]
    for task in running:
        task.cancel()
    for task in running:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
