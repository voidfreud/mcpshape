"""A clock the test moves by hand, so time-based transitions need no waiting.

The Daemon takes its clock as a parameter, so a test can put six hundred seconds behind an
Upstream in a single call and watch it go cold.
"""

from __future__ import annotations

import asyncio
import contextlib

ROUNDS = 20
"""Turns of the event loop that count as "everything that could run, ran"."""


class FakeClock:
    """Time that only moves when ``advance`` is called."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = start
        self._sleepers: list[tuple[float, asyncio.Event]] = []

    def now(self) -> float:
        return self._now

    async def sleep(self, seconds: float) -> None:
        target = self._now + seconds
        if target <= self._now:
            await asyncio.sleep(0)
            return
        sleeper = (target, asyncio.Event())
        self._sleepers.append(sleeper)
        try:
            await sleeper[1].wait()
        finally:
            with contextlib.suppress(ValueError):
                self._sleepers.remove(sleeper)

    async def advance(self, seconds: float) -> None:
        """Let everything runnable run, move time on, then let it run again."""
        await settle()
        self._now += seconds
        for target, waking in list(self._sleepers):
            if target <= self._now:
                waking.set()
        await settle()


async def settle(rounds: int = ROUNDS) -> None:
    """Give every task waiting on nothing but the loop a chance to get on with it."""
    for _ in range(rounds):
        await asyncio.sleep(0)
