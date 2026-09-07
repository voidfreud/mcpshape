"""The domain model. Vocabulary follows CONTEXT.md."""

from __future__ import annotations

from dataclasses import dataclass, field
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

DEFAULT_UNAVAILABLE_MESSAGE = (
    "The Upstream is not reachable right now, so this call did not run. "
    "Nothing changed; try again in a moment."
)

LIFECYCLE_HELP = {
    "warm": (
        "Connect at Daemon start and ping on an interval, instead of waiting for the first "
        "call. Warm Upstreams never go idle."
    ),
    "idle_timeout": ("Seconds without a call before the connection is let go. 0 keeps it forever."),
    "connect_timeout": "Seconds a connect attempt may take before it counts as a failure.",
    "ping_interval": (
        "Seconds between pings of a warm Upstream, so it is never falsely reported up."
    ),
    "unavailable_message": (
        "The tool error a call is answered with while the Upstream is not connected."
    ),
}
"""One wording per lifecycle setting, so the global default and the per-Upstream override
never describe the same key differently."""


class LifecycleSettings(BaseModel):
    """When an Upstream's connection is opened, checked, and let go.

    These belong to the Upstream, since the connection is the Upstream's and is shared by its
    Proxies. ``config.toml`` sets the defaults; an Upstream file overrides what it cares about.
    """

    model_config = ConfigDict(extra="forbid")

    warm: bool = Field(default=False, description=LIFECYCLE_HELP["warm"])
    idle_timeout: float = Field(default=600.0, ge=0, description=LIFECYCLE_HELP["idle_timeout"])
    connect_timeout: float = Field(
        default=10.0, gt=0, description=LIFECYCLE_HELP["connect_timeout"]
    )
    ping_interval: float = Field(default=30.0, gt=0, description=LIFECYCLE_HELP["ping_interval"])
    unavailable_message: str = Field(
        default=DEFAULT_UNAVAILABLE_MESSAGE,
        min_length=1,
        description=LIFECYCLE_HELP["unavailable_message"],
    )


class LifecycleOverrides(BaseModel):
    """One Upstream's lifecycle settings: whatever it sets wins over the global defaults."""

    model_config = ConfigDict(extra="forbid")

    warm: bool | None = Field(default=None, description=LIFECYCLE_HELP["warm"])
    idle_timeout: float | None = Field(
        default=None, ge=0, description=LIFECYCLE_HELP["idle_timeout"]
    )
    connect_timeout: float | None = Field(
        default=None, gt=0, description=LIFECYCLE_HELP["connect_timeout"]
    )
    ping_interval: float | None = Field(
        default=None, gt=0, description=LIFECYCLE_HELP["ping_interval"]
    )
    unavailable_message: str | None = Field(
        default=None, min_length=1, description=LIFECYCLE_HELP["unavailable_message"]
    )

    def over(self, defaults: LifecycleSettings) -> LifecycleSettings:
        return defaults.model_copy(update=self.model_dump(exclude_none=True))


@dataclass(frozen=True)
class Upstream:
    """An existing MCP server the user added once. Reachable through its Proxies."""

    name: str
    transport: Transport
    proxies: tuple[str, ...]
    lifecycle: LifecycleSettings = field(default_factory=LifecycleSettings)
