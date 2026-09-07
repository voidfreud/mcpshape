"""A real MCP server for the stdio Upstream tests, run as a child process of the Daemon.

A subprocess is an allowed exception to the seam (the design brief lists it): nothing else
proves that one child process is spawned, that it is given the environment the Upstream file
asks for, and that it is gone when the connection is.

What it does is all reported through MCP or through files the test reads: every start appends
its pid to the file ``MCPSHAPE_TEST_SPAWNS`` names, so a test counts the children; the server
advertises one more tool while the file ``MCPSHAPE_TEST_GROWN`` names exists, so a rescan has
something to find; and it never finishes starting while the file ``MCPSHAPE_TEST_HANG`` names
exists, which is the connect a connect timeout exists for.
"""

from __future__ import annotations

import os
import subprocess  # a child process is what this module is about
import sys
import time
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

MODULE = "tests.support.child_upstream"
SPAWNS = "MCPSHAPE_TEST_SPAWNS"
GROWN = "MCPSHAPE_TEST_GROWN"
HANG = "MCPSHAPE_TEST_HANG"

FOREVER = 3600.0
REPO = Path(__file__).parent.parent.parent


def args() -> list[str]:
    """What to run this module with, next to ``sys.executable`` as the command."""
    return ["-m", MODULE]


def env(**extra: str) -> dict[str, str]:
    """What the child needs to be this server, plus ``extra``.

    ``PYTHONPATH`` has to be spelled out: the MCP SDK passes on only a small safe set of the
    parent's environment, which is exactly why an Upstream file carries an ``env`` block.
    """
    return {"PYTHONPATH": str(REPO), **extra}


def spawned(spawns: Path) -> list[int]:
    """Every child that started, in order, read from the file they append their pid to."""
    if not spawns.is_file():
        return []
    return [int(line) for line in spawns.read_text().split()]


def alive(pid: int) -> bool:
    """Whether ``pid`` is still a process. A reaped or finished child is neither."""
    state = subprocess.run(  # noqa: S603  # /bin/ps, with one argument of our own making
        ["/bin/ps", "-o", "state=", "-p", str(pid)], check=False, capture_output=True, text=True
    )
    return state.returncode == 0 and state.stdout.strip() not in ("", "Z", "Z+")


def command() -> str:
    """The interpreter running the tests, which is the one that can import this module."""
    return sys.executable


child: FastMCP[Any] = FastMCP("child", instructions="A server in a process of its own.")


@child.tool
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


@child.tool
def pid() -> int:
    """The process this server runs in."""
    return os.getpid()


@child.tool
def env_value(name: str) -> str:
    """What this process's environment holds under ``name``, or an empty string."""
    return os.environ.get(name, "")


def marked(variable: str) -> bool:
    """Whether the file ``variable`` names is there, which is how a test steers a fresh start."""
    marker = os.environ.get(variable)
    return bool(marker) and Path(marker).exists()


if marked(GROWN):

    @child.tool
    def subtract(a: int, b: int) -> int:
        """Subtract one number from another."""
        return a - b


def main() -> None:
    if spawns := os.environ.get(SPAWNS):
        with Path(spawns).open("a") as log:
            log.write(f"{os.getpid()}\n")
    if marked(HANG):
        time.sleep(FOREVER)
        return
    child.run(transport="stdio", show_banner=False)


if __name__ == "__main__":
    main()
