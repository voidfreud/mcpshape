"""An in-memory Upstream a separate process can import, for the stdio shim tests.

``tests.support.upstreams`` parks servers in module globals, which only the process that put
them there can see. The shim tests let the shim start a real Daemon of its own, and that
Daemon has to resolve the Upstream after importing this module fresh, so the server is here
at module scope.
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

TARGET = "tests.support.shim_upstream:calculator"

calculator: FastMCP[Any] = FastMCP("calculator")


@calculator.tool
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b
