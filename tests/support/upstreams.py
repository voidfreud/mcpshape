"""In-memory Upstreams for tests.

An Upstream configured with ``transport = "memory"`` names a module attribute holding a
FastMCP server. Tests park their servers here so a temp config directory can point at them
with ``target = "tests.support.upstreams:<name>"``, with no subprocess and no network.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastmcp import FastMCP

MODULE_PATH = "tests.support.upstreams"


def register(name: str, server: FastMCP[Any]) -> str:
    """Expose ``server`` as a module attribute and return its import target."""
    globals()[name] = server
    return f"{MODULE_PATH}:{name}"


def unregister(name: str) -> None:
    globals().pop(name, None)
