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
    The last connect or ping failed, or a call found the open connection dead. A backoff is
    in force, doubling from a second to the Upstream's ``backoff_cap``; a call within it fails
    at once with the Upstream's ``unavailable_message`` rather than waiting, and one after it
    tries again. A warm Upstream is also retried by the keeper when the backoff runs out; a
    lazy one never on its own, since lazy means connect when asked, at failure as at start
    (#64). Nothing is spent on an Upstream nobody is calling.
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
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mcpshape.model import LifecycleSettings

log = logging.getLogger("mcpshape.connection")

State = Literal["cold", "connecting", "ready", "idle-pending", "unavailable", "stopping"]
CONNECTED: tuple[State, ...] = ("ready", "idle-pending")

BACKOFF_BASE = 1.0
"""Seconds before the first retry of a failed connect; the Upstream's ``backoff_cap`` is the
longest the doubling ever grows to."""

KEEPER_RESTART_CAP = 5
"""How many failures within ``KEEPER_RESTART_WINDOW`` the keeper restarts itself through.

The cap is a rate, not a running total (#50): reaching it means the keeper is failing now, not
that it has failed this often over a long life. Past it the Upstream is moved to
``unavailable`` with the reason visible in the management API, rather than left connected with
no idle disconnect, ping, or retry running, and only ``reload()`` starts a keeper again."""

KEEPER_RESTART_WINDOW = 600.0
"""How long one keeper failure counts against ``KEEPER_RESTART_CAP``, on the injected clock."""

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
    supervised: bool = True
    """Whether a keeper is running. False once one gave up, until ``reload()`` (#50)."""
    warm: bool = False
    """Whether the keeper connects and retries on its own: what the dashboard and the CLI read
    to say how an Upstream comes back."""
    retry_in: float | None = None
    """Seconds until the keeper tries again, while ``unavailable``, warm, and supervised;
    nothing otherwise, since the next call is then what tries again (#64)."""


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
        self._supervised = True
        self._attempted = False
        self._announced: tuple[str, float] | None = None
        self._wake = asyncio.Event()
        self._keeper: asyncio.Task[None] | None = None
        self._connecting: asyncio.Task[None] | None = None
        self._rescanning: asyncio.Task[None] | None = None

    # --- what the Daemon drives ------------------------------------------------------------

    def status(self) -> Status:
        now = self._clock.now()
        retry_in = None
        if self._state == "unavailable" and self._settings.warm and self._supervised:
            retry_in = _left(self._backoff(), now - self._since)
        return Status(
            state=self._state,
            seconds=now - self._since,
            error=self._error,
            supervised=self._supervised,
            warm=self._settings.warm,
            retry_in=retry_in,
        )

    async def start(self) -> None:
        """Start the keeper, and connect right away when the Upstream is ``warm``."""
        self._keeper = asyncio.create_task(self._keep())
        if self._settings.warm:
            self._begin_connect()

    async def reload(self) -> None:
        """Supervise this Upstream again when the keeper gave up, and try to connect (#50).

        What ``daemon reload`` reaches every connection with. A keeper still running is left
        exactly as it is, so a reload of a healthy Upstream is nothing; only one that gave up
        past ``KEEPER_RESTART_CAP`` is started again, with an empty record of failures, and
        the Upstream it left ``unavailable`` is connected as ``retry()`` connects it.
        """
        if self._stopping() or (self._keeper is not None and not self._keeper.done()):
            return
        log.info("Upstream %s: supervising again", self._name)
        self._supervised = True
        self._keeper = asyncio.create_task(self._keep())
        self.retry()

    async def stop(self) -> None:
        """Let the connection go and stop supervising it. Safe before ``start`` and twice."""
        self._enter("stopping")
        self._nudge()
        await _finish(self._keeper, self._connecting, self._rescanning)
        self._keeper = self._connecting = self._rescanning = None
        await self._shut()
        self._enter("cold")

    async def reconfigure(self, settings: LifecycleSettings) -> None:
        """Connect again under ``settings``: the Upstream file changed (#46).

        The keeper, any connect in flight, and any rescan are stopped and the link is let go,
        exactly as ``stop()`` does; then the record of failures is emptied, the Upstream is
        supervised again, and the connection starts as at Daemon start: connecting at once when
        ``warm``, else on the next call. The next connect counts as a reconnect, so it rescans:
        what a changed transport reaches may advertise something else. A call waiting in
        ``acquire`` on the cancelled connect finds the state not connected and is answered with
        the Upstream's message, and one in flight over the old link fails as a dead connection
        that ``lost()`` ignores, since the state is no longer connected.
        """
        await self.stop()
        self._settings = settings
        self._failures = 0
        self._error = None
        self._announced = None
        self._supervised = True
        self._attempted = True
        await self.start()

    # --- what a call drives ----------------------------------------------------------------

    async def acquire(self) -> None:
        """Wait until the Upstream is connected, waking it if it is asleep.

        Raises ``UpstreamUnavailableError`` with the configured message when it is not
        reachable: while a connect attempt is failing, and at once during the backoff after
        one did, so that no call waits longer than the connect timeout. A call after the
        backoff is what tries again, the only thing that does for a lazy Upstream (#64).
        """
        if self._state in CONNECTED:
            self._touch()
            return
        if self._state == "stopping" or (
            self._state == "unavailable" and self._clock.now() - self._since < self._backoff()
        ):
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

    def connect_now(self) -> None:
        """Connect now from wherever it is, whatever the backoff says: ``upstream connect``.

        A cold Upstream is connected as a call would connect it; an ``unavailable`` one tries
        again at once; one already connecting or connected is left alone (#64).
        """
        if self._state in CONNECTED or self._state in ("connecting", "stopping"):
            return
        self._begin_connect()
        self._nudge()

    # --- the keeper ------------------------------------------------------------------------

    async def _keep(self) -> None:
        """Run every time-based transition: the idle timer, the pings, and the backoff.

        An unexpected exception restarts this loop rather than leaving the Upstream connected
        with nothing supervising it. Only the failures within ``KEEPER_RESTART_WINDOW`` count
        against ``KEEPER_RESTART_CAP``, so failures spread over a long life never end
        supervision and a quiet window empties the record (#50); reaching the cap moves the
        Upstream to ``unavailable`` with the reason visible in the management API, and nothing
        but ``reload()`` supervises it again.
        """
        failures: list[float] = []
        while not self._stopping():
            try:
                await self._keep_loop()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # an Upstream must never take the Daemon down
                now = self._clock.now()
                failures = [at for at in failures if now - at < KEEPER_RESTART_WINDOW]
                failures.append(now)
                log.exception(
                    "Upstream %s: the keeper failed (%d/%d within %.0fs)",
                    self._name,
                    len(failures),
                    KEEPER_RESTART_CAP,
                    KEEPER_RESTART_WINDOW,
                )
                if len(failures) >= KEEPER_RESTART_CAP:
                    self._supervised = False
                    await self._fail(
                        f"the keeper stopped supervising after {len(failures)} failures "
                        f"within {KEEPER_RESTART_WINDOW:.0f}s: {exc}"
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
        if self._state == "unavailable" and self._settings.warm:
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
        self._announced = None
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
        connection cannot count a second failure and restart the backoff under the first. The
        log says it once per reason and once per doubling of the backoff, not once per
        attempt (#64): a dead Upstream is one line, then one each time the wait grows.
        """
        self._error = reason
        self._failures += 1
        self._enter("unavailable")
        delay = self._backoff()
        if self._announced != (reason, delay):
            self._announced = (reason, delay)
            if self._settings.warm and self._supervised:
                log.warning(
                    "Upstream %s is unavailable (%s); retrying in %.0fs", self._name, reason, delay
                )
            else:
                log.warning(
                    "Upstream %s is unavailable (%s); the next call after %.0fs tries again",
                    self._name,
                    reason,
                    delay,
                )
        await self._shut(dead=True)
        self._nudge()

    async def _shut(self, *, dead: bool = False) -> None:
        """Let the link go. Closing one found ``dead`` raises the failure that killed it again,
        which is one readable line here, not a traceback (#52): the warning that names the
        Upstream and the reason is already written. Any other close that fails is news."""
        try:
            await self._link.close()
        except Exception as exc:
            if dead:
                log.info("Upstream %s let its dead connection go: %s", self._name, exc)
                return
            log.warning("Upstream %s did not close cleanly", self._name, exc_info=True)

    def _backoff(self) -> float:
        return min(self._settings.backoff_cap, BACKOFF_BASE * 2 ** max(0, self._failures - 1))

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
