"""In-memory Upstreams for tests.

An Upstream configured with ``transport = "memory"`` names a module attribute holding a
FastMCP server. Tests park their servers here so a temp config directory can point at them
with ``target = "tests.support.upstreams:<name>"``, with no subprocess and no network.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from typing import TYPE_CHECKING, Any

from fastmcp import FastMCP
from fastmcp.server.middleware import Middleware

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from fastmcp.server.middleware import CallNext, MiddlewareContext

MODULE_PATH = "tests.support.upstreams"


def register(name: str, server: FastMCP[Any]) -> str:
    """Expose ``server`` as a module attribute and return its import target."""
    globals()[name] = server
    return f"{MODULE_PATH}:{name}"


def unregister(name: str) -> None:
    globals().pop(name, None)


def slow_server(gate: asyncio.Event) -> FastMCP[Any]:
    """An Upstream that does not finish connecting until ``gate`` is set.

    A connect that hangs is the failure a connect timeout exists for; a Client cannot tell it
    apart from a server that never came up.
    """

    @contextlib.asynccontextmanager
    async def lifespan(_server: FastMCP[Any]) -> AsyncGenerator[None]:
        await gate.wait()
        yield

    server: FastMCP[Any] = FastMCP("slow", lifespan=lifespan)

    def echo(text: str) -> str:
        """Say it back."""
        return text

    server.tool(echo)
    return server


class Gate:
    """A pause a test can hold shut or let open, for every call that reaches it at once.

    Shutting it stalls every ``tools/list`` request against a gated Upstream (a scan started
    by the Daemon's reconnect, one started by ``upstream sync``, or both) right after each has
    already heard back what the Upstream has, so a parked caller holds a point-in-time
    snapshot rather than blocking before asking; the Upstream can then grow further while one
    or more scans sit on an older answer, and opening the gate lets every parked scan carry
    its own snapshot into ``record_scan``/``accept`` at once, for a real race between whatever
    they each saw. ``parked`` reports how many calls are currently held, so a test can wait
    until the ones it expects have actually arrived before growing the Upstream and opening
    the gate. ``threading.Event``, not ``asyncio.Event``: a scan started from a worker thread
    (the CLI, via ``asyncio.to_thread``) runs its own event loop, so a lock usable from either
    loop has to live below both of them. ``wait`` still gives its own loop back while blocked.
    """

    def __init__(self) -> None:
        self._event = threading.Event()
        self._event.set()
        self._parked = 0
        self._lock = threading.Lock()

    def shut(self) -> None:
        self._event.clear()

    def open(self) -> None:
        self._event.set()

    def parked(self) -> int:
        """How many calls are currently held at the gate."""
        with self._lock:
            return self._parked

    async def wait(self) -> None:
        with self._lock:
            self._parked += 1
        try:
            await asyncio.to_thread(self._event.wait)
        finally:
            with self._lock:
                self._parked -= 1


async def until_parked(gate: Gate, count: int, *, patience: float = 5.0) -> None:
    """Wait until ``count`` calls are held at ``gate``, or say it never happened."""
    deadline = time.monotonic() + patience
    while gate.parked() < count:
        if time.monotonic() > deadline:
            msg = f"only {gate.parked()} call(s) ever reached the gate, not {count}"
            raise AssertionError(msg)
        await asyncio.sleep(0.005)


def gate_tools_list(server: FastMCP[Any], gate: Gate) -> None:
    """Hold every ``tools/list`` request against ``server`` at ``gate`` once it has an answer.

    A scan asks for tools first, then resources, resource templates, and prompts before it
    turns what it saw into a Catalog and writes it; pausing right after the Upstream answers
    freezes that answer in the parked call, decoupled from anything the Upstream is told
    next, which is what lets a test make two scans see two different moments in time.
    """

    class _Gated(Middleware):
        async def on_list_tools(
            self, context: MiddlewareContext[Any], call_next: CallNext[Any, Any]
        ) -> Any:  # noqa: ANN401  # matches Middleware's own signature
            result = await call_next(context)
            await gate.wait()
            return result

    server.add_middleware(_Gated())
