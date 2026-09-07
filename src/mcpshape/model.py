"""The domain model. Vocabulary follows CONTEXT.md."""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_PROXY_NAME = "default"


@dataclass(frozen=True)
class MemoryTarget:
    """An Upstream living in this process, named by an ``module:attribute`` import path.

    Test-only: it lets an in-memory server act as an Upstream with no subprocess or network.
    """

    import_path: str


UpstreamTarget = MemoryTarget


@dataclass(frozen=True)
class Upstream:
    """An existing MCP server the user added once. Reachable through its Proxies."""

    name: str
    target: UpstreamTarget
