"""In-memory Upstreams for tests.

An Upstream configured with ``transport = "memory"`` names a module attribute holding a
FastMCP server. Tests park their servers here so a temp config directory can point at them
with ``target = "tests.support.upstreams:<name>"``, with no subprocess and no network.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

from fastmcp import FastMCP

if TYPE_CHECKING:
    import asyncio
    from collections.abc import AsyncGenerator

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
