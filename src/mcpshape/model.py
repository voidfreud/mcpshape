"""The domain model. Vocabulary follows CONTEXT.md."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

DEFAULT_PROXY_NAME = "default"


class _Transport(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StdioTransport(_Transport):
    """A child process the Daemon spawns, spoken to over stdio."""

    transport: Literal["stdio"]
    command: str
    args: list[str] = Field(default_factory=list[str])


class HttpTransport(_Transport):
    """A Streamable HTTP server reached by URL."""

    transport: Literal["http"]
    url: str


class SseTransport(_Transport):
    """A legacy SSE server reached by URL."""

    transport: Literal["sse"]
    url: str


class MemoryTransport(_Transport):
    """An MCP server living in the Daemon process, as ``module:attribute``.

    Test-only: it lets an in-memory server act as an Upstream with no subprocess or network.
    """

    transport: Literal["memory"]
    target: str = Field(pattern=r"^[A-Za-z_][\w.]*:[A-Za-z_]\w*$")

    @property
    def module(self) -> str:
        return self.target.partition(":")[0]

    @property
    def attribute(self) -> str:
        return self.target.partition(":")[2]


Transport = Annotated[
    StdioTransport | HttpTransport | SseTransport | MemoryTransport,
    Field(discriminator="transport"),
]


@dataclass(frozen=True)
class Upstream:
    """An existing MCP server the user added once. Reachable through its Proxies."""

    name: str
    transport: Transport
    proxies: tuple[str, ...]
