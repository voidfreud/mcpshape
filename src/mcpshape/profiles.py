"""Client Profiles: what mcpshape knows about one kind of Client.

The core knows no Client (ADR 0003). Everything Client-specific lives here as plain data:
where the Client keeps its config and in what format, which transports it accepts, whether it
needs the stdio shim, the naming scheme it applies to a Proxy's tools before a model sees
them, the limits it documents, and the conveniences it offers.

The seed data and its sources are ``docs/clients.md``; update that file, and these Profiles,
when the facts change. Limits are dated facts that go stale, so nothing here is enforced
silently: ``proxy install`` warns, ``doctor`` reports, and the user decides.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from mcpshape.model import DEFAULT_PROXY_NAME

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

CLIENTS_DOC = "docs/clients.md"
CHECKED = "2026-09-07"

Format = Literal["json", "toml", "yaml"]
"""The syntax of the Client's config file. Only JSON and TOML are written for the user."""

Reach = Literal["http", "stdio", "remote-https"]
"""How a Client can reach an MCP server. ``remote-https`` cannot reach a local Proxy at all."""

Overflow = Literal["reject", "truncate"]
"""What the Client does with a tool name longer than its budget."""


@dataclass(frozen=True)
class Source:
    """Where a fact came from and when it was last checked."""

    where: str
    checked: str = CHECKED

    def __str__(self) -> str:
        return f"{self.where}, checked {self.checked}"


DOC = Source(CLIENTS_DOC)
CLAUDE_CODE_MCP_DOC = Source("https://code.claude.com/docs/en/mcp")
ANTHROPIC_TOOL_NAMES = Source("Anthropic API tool-name pattern, recorded in " + CLIENTS_DOC)


@dataclass(frozen=True)
class Location:
    """One place a Client reads its MCP servers from."""

    path: str
    """``~/...`` for a user file, a relative path for a per-project file."""
    scope: Literal["user", "project"]

    def __str__(self) -> str:
        return f"{self.path} ({self.scope})"


@dataclass(frozen=True)
class NameScheme:
    """How the Client renames a Proxy's tools before a model sees them.

    ``template`` takes ``{server}`` (the Client's entry name for the Proxy) and ``{tool}``
    (the Proxy's exposed tool name) and yields the name the model and the Client's API see.
    """

    template: str = "{tool}"
    max_length: int | None = None
    pattern: re.Pattern[str] | None = None
    overflow: Overflow = "reject"
    source: Source = DOC

    def exposed(self, server: str, tool: str) -> str:
        """The name the Client presents for ``tool`` on the Proxy installed as ``server``."""
        return self.template.format(server=server, tool=tool)

    def rule(self) -> str:
        """The rule this scheme enforces, as one sentence for a report."""
        parts = [f"names it {self.template}"]
        if self.max_length is not None:
            parts.append(f"at most {self.max_length} characters")
        if self.pattern is not None:
            parts.append(f"matching {self.pattern.pattern}")
        return ", ".join(parts)

    def server_violation(self, server: str) -> str | None:
        """Why the prefix this server name produces leaves no room for a tool name."""
        prefix = self.exposed(server, "")
        if self.max_length is not None and len(prefix) >= self.max_length:
            return (
                f"the server name {server!r} alone makes a {len(prefix)}-character prefix, "
                f"leaving nothing under the Client's {self.max_length} ({self.source})"
            )
        return None

    def violation(self, server: str, tool: str) -> str | None:
        """Why the Client would not accept this tool name, or ``None`` when it would."""
        exposed = self.exposed(server, tool)
        if self.max_length is not None and len(exposed) > self.max_length:
            verb = "rejects" if self.overflow == "reject" else "truncates"
            return (
                f"{exposed!r} is {len(exposed)} characters; the Client {verb} names over "
                f"{self.max_length} ({self.rule()}; {self.source})"
            )
        if self.pattern is not None and not self.pattern.fullmatch(exposed):
            return f"{exposed!r} does not match {self.pattern.pattern} ({self.source})"
        return None


@dataclass(frozen=True)
class PropertyRule:
    """What the Client accepts as an input-schema property name."""

    pattern: re.Pattern[str]
    max_length: int
    source: Source = DOC

    def rule(self) -> str:
        return f"property names are 1-{self.max_length} characters matching {self.pattern.pattern}"

    def violation(self, name: str) -> str | None:
        if len(name) > self.max_length or not self.pattern.fullmatch(name):
            return f"property {name!r} breaks the rule that {self.rule()} ({self.source})"
        return None


@dataclass(frozen=True)
class Caps:
    """Cap values this Profile recommends, comfortably under what the Client truncates.

    Data only. Caps are applied by the Proxy, which is ticket #7; nothing here truncates
    anything yet. ``doctor`` and ``proxy install`` report these numbers, they do not enforce.
    """

    tool_description: int | None = None
    instructions: int | None = None
    source: Source | None = None


@dataclass(frozen=True)
class Limits:
    """Numbers the Client documents. ``None`` wherever no primary source gives one."""

    output_tokens: int | None = None
    visible_tools: int | None = None
    startup_timeout_seconds: int | None = None
    tool_timeout_seconds: int | None = None
    source: Source | None = None


@dataclass(frozen=True)
class Profile:
    """One Client, as data. Never a special case in the core."""

    slug: str
    """What ``--to`` and ``--for`` take, for example ``claude-code``."""
    name: str
    reach: tuple[Reach, ...]
    locations: tuple[Location, ...] = ()
    file_format: Format = "json"
    container: tuple[str, ...] = ("mcpServers",)
    """Key path from the top of the config file down to the map of servers."""
    url_key: str = "url"
    """The key an HTTP entry puts the Proxy URL under."""
    http_fields: Mapping[str, Any] = field(default_factory=dict[str, Any])
    """Fields an HTTP entry carries besides the URL, in the order the Client documents."""
    shim_fields: Mapping[str, Any] = field(default_factory=dict[str, Any])
    """Fields a stdio entry carries besides ``command`` and ``args``."""
    name_field: str | None = None
    """The key an entry repeats its own name under, where the Client wants it inside."""
    entry_shape: Literal["map", "list"] = "map"
    """Whether the container is a map keyed by name or a list of named entries."""
    disable_flag: str | None = None
    """The documented per-server off switch, where the Client has one. Nowhere else."""
    scheme: NameScheme = field(default_factory=NameScheme)
    properties: PropertyRule | None = None
    caps: Caps = field(default_factory=Caps)
    limits: Limits = field(default_factory=Limits)
    extras: Mapping[str, str] = field(default_factory=dict[str, str])
    """Per-server conveniences the Client offers, by config key."""
    refreshes_on_list_changed: bool = False
    """Documented to pick up a changed tool set without a reconnect."""
    notes: tuple[str, ...] = ()

    @property
    def needs_shim(self) -> bool:
        """This Client cannot reach a Proxy over HTTP, so it gets the stdio shim entry."""
        return "http" not in self.reach and "stdio" in self.reach

    @property
    def installable(self) -> bool:
        """A local Proxy is reachable at all: ChatGPT takes remote HTTPS only."""
        return "http" in self.reach or "stdio" in self.reach

    @property
    def writable(self) -> bool:
        """``proxy install`` can merge into this Client's file without losing what is there."""
        return self.file_format in ("json", "toml") and self.entry_shape == "map"

    def default_location(self) -> Location | None:
        """Where ``proxy install`` writes without ``--config``, or ``None`` when unrecorded."""
        return self.locations[0] if self.locations else None

    def name_violations(self, server: str, tools: Iterable[str]) -> list[str]:
        """Every exposed name this Client would refuse or reshape, server name first."""
        found = [self.scheme.server_violation(server)]
        found += [self.scheme.violation(server, tool) for tool in tools]
        return [line for line in found if line]

    def entry(self, url: str, ref: str, name: str) -> dict[str, Any]:
        """The config entry that points this Client at the Proxy served at ``url``."""
        named = {self.name_field: name} if self.name_field else {}
        if self.needs_shim:
            return {**named, **self.shim_fields, "command": "mcpshape", "args": ["serve", ref]}
        return {**named, **self.http_fields, self.url_key: url}


# --- the Profiles ------------------------------------------------------------------------------
#
# Seed data: docs/clients.md, checked 2026-09-06/07. Real numbers only where a primary source
# documents one; every other limit is None rather than a guess.

CLAUDE_CODE = Profile(
    slug="claude-code",
    name="Claude Code",
    reach=("http", "stdio"),
    locations=(Location(".mcp.json", "project"), Location("~/.claude.json", "user")),
    http_fields={"type": "http"},
    scheme=NameScheme(
        template="mcp__{server}__{tool}",
        max_length=64,
        pattern=re.compile(r"[a-zA-Z0-9_-]+"),
        overflow="reject",
        source=ANTHROPIC_TOOL_NAMES,
    ),
    properties=PropertyRule(re.compile(r"[a-zA-Z0-9_.-]+"), 64, CLAUDE_CODE_MCP_DOC),
    # Comfortably under the documented 2KB truncation, whether that is bytes or characters.
    caps=Caps(tool_description=1800, instructions=1800, source=CLAUDE_CODE_MCP_DOC),
    limits=Limits(output_tokens=25_000, source=CLAUDE_CODE_MCP_DOC),
    extras={
        "alwaysLoad": "Load this server's tool definitions eagerly instead of on demand.",
        "disabledMcpjsonServers": (
            "A list in .claude/settings.json, not here, naming .mcp.json servers to keep off."
        ),
    },
    refreshes_on_list_changed=True,
    notes=(
        (
            "Truncates tool descriptions and server instructions at 2KB each: put critical text "
            "first, the first sentence carries the routing hint."
        ),
        (
            "Defers tool definitions and searches on demand; the model sees names plus server "
            "instructions until it loads a tool."
        ),
    ),
)

CLAUDE_DESKTOP = Profile(
    slug="claude-desktop",
    name="Claude Desktop",
    reach=("stdio",),
    locations=(
        Location("~/Library/Application Support/Claude/claude_desktop_config.json", "user"),
    ),
    notes=(
        (
            "Its config file takes stdio entries only, so it gets the shim entry; HTTP servers go "
            "through the Connectors UI, which no config file drives."
        ),
    ),
)

CURSOR = Profile(
    slug="cursor",
    name="Cursor",
    reach=("http",),
    locations=(Location(".cursor/mcp.json", "project"), Location("~/.cursor/mcp.json", "user")),
    http_fields={"type": "http"},
    notes=("Enable and disable is a UI toggle; its config file documents no off switch.",),
)

WINDSURF = Profile(
    slug="windsurf",
    name="Windsurf",
    reach=("http",),
    locations=(Location("~/.codeium/windsurf/mcp_config.json", "user"),),
    http_fields={"type": "http"},
    limits=Limits(visible_tools=100, source=DOC),
    notes=(
        "Shows at most 100 tools in total and allows 20 tool calls per prompt.",
        (
            "Its remote entry shape is not recorded in docs/clients.md, so the common "
            "mcpServers url form is written; check it before relying on it."
        ),
    ),
)

VS_CODE = Profile(
    slug="vscode",
    name="VS Code",
    reach=("http",),
    locations=(Location(".vscode/mcp.json", "project"),),
    container=("servers",),
    http_fields={"type": "http"},
)

ZED = Profile(
    slug="zed",
    name="Zed",
    reach=("stdio",),
    locations=(Location("~/.config/zed/settings.json", "user"),),
    container=("context_servers",),
    notes=(
        "Recorded as stdio only, so it gets the shim entry.",
        (
            "Its own docs now also show a remote url entry under context_servers; docs/clients.md "
            "records that, and this Profile stays on the shim until it is confirmed."
        ),
    ),
)

GEMINI_CLI = Profile(
    slug="gemini-cli",
    name="Gemini CLI",
    reach=("http",),
    locations=(Location("~/.gemini/settings.json", "user"),),
    container=("mcpServers",),
    http_fields={"type": "http"},
    scheme=NameScheme(
        template="mcp_{server}_{tool}",
        max_length=63,
        overflow="truncate",
        source=DOC,
    ),
    notes=(
        "Replaces invalid characters and middle-truncates names over 63 characters.",
        "Filters tools with includeTools and excludeTools; per-server timeout defaults to 10 min.",
        (
            "Its remote entry shape is not recorded in docs/clients.md, so the common "
            "mcpServers url form is written; check it before relying on it."
        ),
    ),
)

CODEX_CLI = Profile(
    slug="codex-cli",
    name="Codex CLI",
    reach=("http",),
    locations=(Location("~/.codex/config.toml", "user"),),
    file_format="toml",
    container=("mcp_servers",),
    limits=Limits(startup_timeout_seconds=10, tool_timeout_seconds=60, source=DOC),
    notes=("Recommends that the first 512 characters of instructions be self-contained.",),
)

GOOSE = Profile(
    slug="goose",
    name="Goose",
    reach=("http",),
    locations=(Location("~/.config/goose/config.yaml", "user"),),
    file_format="yaml",
    container=("extensions",),
    url_key="uri",
    http_fields={"type": "streamable_http", "enabled": True},
    name_field="name",
    limits=Limits(tool_timeout_seconds=300, source=DOC),
)

CLINE = Profile(
    slug="cline",
    name="Cline",
    reach=("http",),
    locations=(),
    http_fields={"type": "streamableHttp"},
    disable_flag="disabled",
    limits=Limits(tool_timeout_seconds=60, source=DOC),
    notes=("Its cline_mcp_settings.json lives inside the editor's storage; pass --config.",),
)

CONTINUE = Profile(
    slug="continue",
    name="Continue",
    reach=("http",),
    locations=(Location("~/.continue/config.yaml", "user"),),
    file_format="yaml",
    container=("mcpServers",),
    entry_shape="list",
    name_field="name",
    http_fields={"type": "streamable-http"},
)

OPENCODE = Profile(
    slug="opencode",
    name="OpenCode",
    reach=("http",),
    locations=(Location("opencode.json", "project"),),
    container=("mcp",),
    http_fields={"type": "remote", "enabled": True},
    limits=Limits(startup_timeout_seconds=5, source=DOC),
)

CHATGPT = Profile(
    slug="chatgpt",
    name="ChatGPT",
    reach=("remote-https",),
    notes=("it takes remote HTTPS servers only, and a Proxy is served over loopback HTTP.",),
)

PROFILES: Mapping[str, Profile] = {
    profile.slug: profile
    for profile in (
        CLAUDE_CODE,
        CLAUDE_DESKTOP,
        CURSOR,
        WINDSURF,
        VS_CODE,
        ZED,
        GEMINI_CLI,
        CODEX_CLI,
        GOOSE,
        CLINE,
        CONTINUE,
        OPENCODE,
        CHATGPT,
    )
}


def entry_name(upstream: str, proxy: str) -> str:
    """The name a Proxy is installed under: ``<upstream>`` for the default Proxy, else
    ``<upstream>-<proxy>``. The Client's naming scheme prefixes tool names with it."""
    return upstream if proxy == DEFAULT_PROXY_NAME else f"{upstream}-{proxy}"


class UnknownClientError(ValueError):
    """``--to`` or ``--for`` named a Client mcpshape has no Profile for."""


def slugs() -> list[str]:
    """Every Client ``--to`` and ``--for`` accept, in name order."""
    return sorted(PROFILES)


def profile(slug: str) -> Profile:
    """The Profile for ``slug``, or ``UnknownClientError`` naming the ones there are."""
    found = PROFILES.get(slug)
    if found is None:
        msg = f"no Client Profile for {slug!r}; known Clients: {', '.join(slugs())}"
        raise UnknownClientError(msg)
    return found


def clients_needing_reconnect() -> list[str]:
    """Clients that only see a changed tool set after a reconnect, by display name.

    Live refresh on ``tools/list_changed`` is documented for Claude Code alone; every other
    Client keeps the tool list it fetched. What ``upstream sync --accept`` tells the user.
    """
    return sorted(
        found.name
        for found in PROFILES.values()
        if found.installable and not found.refreshes_on_list_changed
    )
