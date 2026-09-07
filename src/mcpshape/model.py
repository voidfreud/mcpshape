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


# --- Caps: ceilings on how long a kind of text a Proxy exposes may be --------------------------

CAP_KINDS = ("tool_name", "tool_description", "argument_description", "instructions", "tool_output")
"""Every kind of text a Cap ceilings: a tool's exposed name, its description, one of its
argument's descriptions, the Proxy's instructions, and a tool call's answer."""

CAP_HELP = {
    "tool_name": "Ceiling on an exposed tool name, in characters.",
    "tool_description": "Ceiling on an exposed tool description, in characters.",
    "argument_description": "Ceiling on an exposed argument description, in characters.",
    "instructions": "Ceiling on the Proxy's exposed instructions, in characters.",
    "tool_output": "Ceiling on a tool call's answer, in characters.",
}
"""One wording per Cap kind, so the global master and every level that may lower it never
describe the same kind differently."""


class CapError(ValueError):
    """A Cap tried to raise what it inherits; a Cap may only be lowered."""


class CapSettings(BaseModel):
    """The global master Cap per kind, in ``config.toml``. Every Upstream, Proxy, and tool
    inherits these, and may only lower what it inherits (global, then Upstream, then Proxy,
    then tool)."""

    model_config = ConfigDict(extra="forbid")

    # The defaults for descriptions and instructions sit under every documented Client
    # cutoff (docs/clients.md), so a Proxy nobody tuned survives intact everywhere. The
    # Profiles carry the numbers with their sources; doctor --for reports them.
    tool_name: int = Field(default=64, gt=0, description=CAP_HELP["tool_name"])
    tool_description: int = Field(default=1800, gt=0, description=CAP_HELP["tool_description"])
    argument_description: int = Field(
        default=400, gt=0, description=CAP_HELP["argument_description"]
    )
    instructions: int = Field(default=1800, gt=0, description=CAP_HELP["instructions"])
    tool_output: int = Field(default=20_000, gt=0, description=CAP_HELP["tool_output"])


def _lowered(inherited: CapSettings, level: str, given: dict[str, int]) -> CapSettings:
    """``inherited`` with ``given`` applied over it, each one only ever lowering it."""
    for kind, value in given.items():
        ceiling = getattr(inherited, kind)
        if value > ceiling:
            label = kind.replace("_", " ")
            msg = (
                f"{level}: the {label} Cap is {value}, higher than the {ceiling} it inherits; "
                "a Cap may only be lowered"
            )
            raise CapError(msg)
    return inherited.model_copy(update=given)


class CapOverrides(BaseModel):
    """One Upstream's or one Proxy's Caps: unset keeps what it inherits, set only ever lowers
    it."""

    model_config = ConfigDict(extra="forbid")

    tool_name: int | None = Field(default=None, gt=0, description=CAP_HELP["tool_name"])
    tool_description: int | None = Field(
        default=None, gt=0, description=CAP_HELP["tool_description"]
    )
    argument_description: int | None = Field(
        default=None, gt=0, description=CAP_HELP["argument_description"]
    )
    instructions: int | None = Field(default=None, gt=0, description=CAP_HELP["instructions"])
    tool_output: int | None = Field(default=None, gt=0, description=CAP_HELP["tool_output"])

    def over(self, inherited: CapSettings, level: str) -> CapSettings:
        """The Caps this level exposes: ``inherited``, lowered by whatever it sets.

        Raises ``CapError`` naming ``level`` when a set value would raise what it inherits.
        """
        return _lowered(inherited, level, self.model_dump(exclude_none=True))


class ToolCapOverrides(BaseModel):
    """One tool's Caps: name, description, argument description, and output. A tool has no
    instructions of its own, so there is no Cap for that kind at this level."""

    model_config = ConfigDict(extra="forbid")

    name: int | None = Field(default=None, gt=0, description=CAP_HELP["tool_name"])
    description: int | None = Field(default=None, gt=0, description=CAP_HELP["tool_description"])
    argument_description: int | None = Field(
        default=None, gt=0, description=CAP_HELP["argument_description"]
    )
    output: int | None = Field(default=None, gt=0, description=CAP_HELP["tool_output"])

    def over(self, inherited: CapSettings, level: str) -> CapSettings:
        """The Caps this tool exposes: ``inherited``, lowered by whatever it sets."""
        mapped = {
            "tool_name": self.name,
            "tool_description": self.description,
            "argument_description": self.argument_description,
            "tool_output": self.output,
        }
        given = {kind: value for kind, value in mapped.items() if value is not None}
        return _lowered(inherited, level, given)


@dataclass(frozen=True)
class Upstream:
    """An existing MCP server the user added once. Reachable through its Proxies."""

    name: str
    transport: Transport
    proxies: tuple[str, ...]
    lifecycle: LifecycleSettings = field(default_factory=LifecycleSettings)
    caps: CapOverrides = field(default_factory=CapOverrides)
