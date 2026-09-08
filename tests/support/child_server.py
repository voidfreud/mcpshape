"""A FastMCP server in a process of its own, built from an import path, for tests (#76).

An in-process FastMCP HTTP server reached by a legacy-era client, which mcpshape's Upstream
client is, can leave the test process unable to serve that era for the rest of the run
(``docs/clients.md``). So every loopback server a test puts behind the Daemon runs here, in a
child process: ``python -m tests.support.child_server --server tests.test_proxy_seam:calculator
--transport http --port 12345``. The child imports the factory named, builds the server, and
serves it on loopback until it is killed.
"""

from __future__ import annotations

import argparse
import importlib
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastmcp import FastMCP

REPO = Path(__file__).resolve().parent.parent.parent


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


def command(path: str, transport: str, port: int) -> list[str]:
    """The command line that serves ``path`` over ``transport`` on ``port``."""
    return [
        sys.executable,
        "-m",
        "tests.support.child_server",
        "--server",
        path,
        "--transport",
        transport,
        "--port",
        str(port),
    ]


def env() -> dict[str, str]:
    """The child's environment: this checkout on the path, so the test modules import."""
    return {**os.environ, "PYTHONPATH": str(REPO)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", required=True)
    parser.add_argument("--transport", choices=("http", "sse"), default="http")
    parser.add_argument("--port", type=int, required=True)
    given = parser.parse_args()
    server = build(given.server)
    path = "/mcp" if given.transport == "http" else "/sse"
    server.run(
        transport=given.transport, host="127.0.0.1", port=given.port, path=path, show_banner=False
    )


if __name__ == "__main__":
    main()
