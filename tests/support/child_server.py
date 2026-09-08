"""A FastMCP server in a process of its own, built from an import path, for tests (#76).

An in-process FastMCP HTTP server reached by a legacy-era client, which mcpshape's Upstream
client is, can leave the test process unable to serve that era for the rest of the run
(``docs/clients.md``). So every loopback server a test puts behind the Daemon runs in a child
process: ``python -m tests.support.child_server --server tests.test_proxy_seam:calculator
--transport http``. The child imports the factory named, builds the server, binds a port of
its own choosing, says which on its first line of stdout, and serves until it is killed. The
OAuth provider's child (``tests.support.oauth_provider``) is served the same way, through
``serve``, and spawned the same way, through ``spawned``.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib
import os
import socket
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import uvicorn

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable

    from fastmcp import FastMCP
    from starlette.types import ASGIApp

REPO = Path(__file__).resolve().parent.parent.parent
PATIENCE = 30.0
"""Seconds a child may take to say its port: a cold runner importing FastMCP and a test
module. A child that dies is reported at once, whatever the patience."""
PORT_LINE = "port="


def factory_path(factory: Callable[[], FastMCP[Any]]) -> str:
    """``module:function`` for a server factory, what a child process is told to import."""
    return f"{factory.__module__}:{factory.__qualname__}"


def build(path: str) -> FastMCP[Any]:
    """The server the factory at ``path`` builds."""
    module_name, _, attribute = path.partition(":")
    factory = cast(
        "Callable[[], FastMCP[Any]]", getattr(importlib.import_module(module_name), attribute)
    )
    return factory()


def mcp_path(transport: str) -> str:
    """Where a server speaks MCP for ``transport``: the one place that knows."""
    return "/mcp" if transport == "http" else "/sse"


def command(path: str, transport: str) -> list[str]:
    """The command line that serves the factory at ``path`` over ``transport``."""
    return [
        sys.executable,
        "-m",
        "tests.support.child_server",
        "--server",
        path,
        "--transport",
        transport,
    ]


def env() -> dict[str, str]:
    """The child's environment: this checkout on the path, so the test modules import."""
    return {**os.environ, "PYTHONPATH": str(REPO)}


async def serve(app: ASGIApp, lifespan: Any = None) -> None:  # noqa: ANN401  # a context manager of any type
    """What a child runs: bind a loopback port of the system's choosing, say which on stdout,
    and serve ``app`` on it until killed, under ``lifespan`` when one is given."""
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    print(f"{PORT_LINE}{sock.getsockname()[1]}", flush=True)  # noqa: T201  # the parent reads this line
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="off")
    )
    async with lifespan or contextlib.nullcontext():
        await server.serve(sockets=[sock])


@contextlib.asynccontextmanager
async def spawned(args: list[str], patience: float = PATIENCE) -> AsyncGenerator[int]:
    """Run ``args`` as a child that serves through ``serve``, and yield the port it said.

    Waits for the port line, then for the port to answer; a child that ends first is reported
    at once with its exit code, since the message must name the child, not the port. The
    child is killed and reaped whatever happens after it started.
    """
    child = await asyncio.create_subprocess_exec(*args, env=env(), stdout=asyncio.subprocess.PIPE)
    try:
        port = await _port_said(child, patience)
        await _answering(child, port, patience)
        yield port
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()


async def _port_said(child: asyncio.subprocess.Process, patience: float) -> int:
    assert child.stdout is not None
    try:
        line = await asyncio.wait_for(child.stdout.readline(), patience)
    except TimeoutError:
        msg = f"the child said no port within {patience:.0f}s"
        raise TimeoutError(msg) from None
    text = line.decode().strip()
    if not text.startswith(PORT_LINE):
        code = await child.wait() if not text else child.returncode
        msg = f"the child ended before saying its port (exit code {code}): {text!r}"
        raise RuntimeError(msg)
    return int(text.removeprefix(PORT_LINE))


async def _answering(child: asyncio.subprocess.Process, port: int, patience: float) -> None:
    deadline = time.monotonic() + patience
    while True:
        if child.returncode is not None:
            msg = f"the child on port {port} ended with exit code {child.returncode}"
            raise RuntimeError(msg)
        try:
            _, writer = await asyncio.open_connection("127.0.0.1", port)
        except OSError:
            if time.monotonic() > deadline:
                msg = f"nothing answered on the child's port {port} within {patience:.0f}s"
                raise TimeoutError(msg) from None
            await asyncio.sleep(0.05)
            continue
        writer.close()
        await writer.wait_closed()
        return


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", required=True)
    parser.add_argument("--transport", choices=("http", "sse"), default="http")
    given = parser.parse_args()
    server = build(given.server)
    app = server.http_app(path=mcp_path(given.transport), transport=given.transport)  # pyright: ignore[reportArgumentType]  # the literal FastMCP takes
    asyncio.run(serve(app, app.router.lifespan_context(app)))


if __name__ == "__main__":
    main()
