"""Finding the MCP servers already configured on this machine, for ``upstream scan``.

Where to look is Client knowledge, so it comes from the Client Profiles (ADR 0003): every
location a Profile records, the same file names inside the typical directories below, and
inside any directory the user names. What a found entry means comes from the Profile's
container and entry shape, and from the entry shapes every Client shares: a command with
arguments, or a URL with a type.

JSON and TOML are read. YAML is not: mcpshape has no YAML dependency and does not grow one
for a listing, so a YAML file is reported as found and unread, by name.

Nothing here decides anything. It reports what is on disk; ``upstream scan`` asks the user.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Literal, cast
from urllib.parse import urlsplit

import tomlkit
from tomlkit.exceptions import TOMLKitError

from mcpshape.model import HttpTransport, SseTransport, StdioTransport, Transport
from mcpshape.names import RESERVED, SLUG
from mcpshape.profiles import PROFILES, Format, Profile

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

TYPICAL_DIRECTORIES: tuple[str, ...] = (
    ".",  # the current directory: every project-scoped Client file in docs/clients.md sits in one
    "~",  # the home directory: Claude Code keeps ~/.claude.json straight in it
)
"""Directories searched besides the Profiles' own locations and the ones the user names.

Both come from the Profiles' location scopes in ``docs/clients.md``: a Client file is either
per-project, and then relative to the directory the user works in, or per-user, and then
under the home directory.
"""

CONTAINERS: tuple[tuple[str, ...], ...] = tuple(
    dict.fromkeys(found.container for found in PROFILES.values())
)
"""Every key path a Client keeps its map of servers under, from the Profiles."""

HTTP_TYPES: frozenset[str] = frozenset(
    {str(found.http_fields["type"]) for found in PROFILES.values() if "type" in found.http_fields}
    | {"streamable-http", "http"}
)
"""What a Client writes in an entry's ``type`` for a Streamable HTTP server, from the Profiles."""

URL_KEYS: tuple[str, ...] = tuple(dict.fromkeys(found.url_key for found in PROFILES.values()))
"""The keys a Client puts a server's URL under, from the Profiles."""

DISABLE_FLAGS: frozenset[str] = frozenset(
    found.disable_flag for found in PROFILES.values() if found.disable_flag
)
"""Every per-server off switch a Client documents, for files attributed to no Client."""

SSE_TYPE = "sse"
LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})  # noqa: S104  # compared, never bound
FORMATS: Mapping[str, Format] = {".json": "json", ".toml": "toml", ".yaml": "yaml", ".yml": "yaml"}


@dataclass(frozen=True)
class Found:
    """One MCP server read out of a Client's config file."""

    name: str
    """What the Client's file calls it, which is rarely a valid Upstream name."""
    transport: Transport
    path: Path
    client: str | None
    """The Client whose file this is, when the path is one that Client uses."""
    disabled: bool = False
    """The Client's file switches this server off, by the flag that Client documents."""
    env: tuple[str, ...] = ()
    """Environment variables the entry sets. Their names are carried over as ``${VAR}``
    references; the values stay in the Client's file, since a secret never enters an Upstream
    file."""


@dataclass(frozen=True)
class Unread:
    """A config file that was found but not read, and why."""

    path: Path
    reason: str


@dataclass(frozen=True)
class Discovery:
    """What one ``upstream scan`` looked at and what it made of it."""

    files: tuple[Path, ...]
    found: tuple[Found, ...]
    unread: tuple[Unread, ...]


@dataclass(frozen=True)
class _Candidate:
    """A path worth opening, and the Client it belongs to when the path says so."""

    path: Path
    client: Profile | None


# --- where to look -------------------------------------------------------------------------------


def candidates(directories: Sequence[Path]) -> list[_Candidate]:
    """Every config file to open, in a stable order, each named once.

    A Profile's own location is attributed to its Client. The same file name dropped straight
    into a searched directory is opened too, but attributed to nobody: ``mcp.json`` in a
    directory the user named is not proof that Cursor wrote it. A per-project file name is
    looked for in project directories only: the home directory is nobody's project.
    """
    roots = [
        *(root.expanduser().absolute() for root in directories),
        *(Path(where).expanduser().absolute() for where in TYPICAL_DIRECTORIES),
    ]
    home = Path("~").expanduser().absolute()
    projects = [root for root in roots if root != home]
    wanted: list[_Candidate] = []
    for client in PROFILES.values():
        for location in client.locations:
            name = PurePosixPath(location.path).name
            if location.scope == "user":
                wanted.append(_Candidate(Path(location.path).expanduser(), client))
                wanted += [_Candidate(root / name, None) for root in roots]
            else:
                wanted += [_Candidate(root / location.path, client) for root in projects]
                wanted += [_Candidate(root / name, None) for root in projects]
    return _existing(wanted)


def _existing(wanted: Iterable[_Candidate]) -> list[_Candidate]:
    """The candidates that are files, each real path kept once, the first attribution winning."""
    seen: dict[Path, _Candidate] = {}
    for candidate in wanted:
        if not candidate.path.is_file():
            continue
        seen.setdefault(candidate.path.resolve(), candidate)
    return list(seen.values())


# --- what is in them -----------------------------------------------------------------------------


def find(directories: Sequence[Path]) -> Discovery:
    """Every MCP server configured in the files reachable from ``directories``."""
    files: list[Path] = []
    found: list[Found] = []
    unread: list[Unread] = []
    for candidate in candidates(directories):
        files.append(candidate.path)
        document = _read(candidate.path, unread)
        if document is not None:
            found += _servers(document, candidate)
    return Discovery(tuple(files), tuple(found), tuple(unread))


def _read(path: Path, unread: list[Unread]) -> dict[str, Any] | None:
    """The file as a map, or nothing, with the reason recorded in ``unread``.

    A parsed config file is untyped by nature, so it is read as ``Any`` and every step below
    checks what it actually holds before using it.
    """
    file_format = FORMATS.get(path.suffix.lower())
    if file_format == "yaml":
        unread.append(Unread(path, "YAML, which mcpshape does not read"))
        return None
    if file_format is None:
        return None
    try:
        text = path.read_text()
        loaded: Any = json.loads(text) if file_format == "json" else tomlkit.parse(text).unwrap()
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TOMLKitError) as exc:
        unread.append(Unread(path, f"not readable as {file_format.upper()}: {exc}"))
        return None
    if not isinstance(loaded, dict):
        unread.append(Unread(path, f"{file_format.upper()} without a map at the top"))
        return None
    return _as_map(loaded)


def _as_map(node: Any) -> dict[str, Any]:  # noqa: ANN401  # a parsed config file is untyped
    """``node`` as a map of anything, or empty when it is not a map."""
    return cast("dict[str, Any]", node) if isinstance(node, dict) else {}


def _as_list(node: Any) -> list[Any]:  # noqa: ANN401  # a parsed config file is untyped
    """``node`` as a list of anything, or empty when it is not a list."""
    return cast("list[Any]", node) if isinstance(node, list) else []


def _servers(document: dict[str, Any], candidate: _Candidate) -> list[Found]:
    """Every entry under the first key path this file keeps servers under."""
    client = candidate.client
    paths = (client.container, *CONTAINERS) if client else CONTAINERS
    shape = client.entry_shape if client else "map"
    flags = _disable_flags(client)
    for container in dict.fromkeys(paths):
        node = _walk(document, container)
        found = [
            Found(
                name,
                transport,
                candidate.path,
                client.name if client else None,
                disabled=any(entry.get(flag) is True for flag in flags),
                env=tuple(sorted(_as_map(entry.get("env")))),
            )
            for name, entry in _entries(node, shape)
            if (transport := _transport(entry, client)) is not None
        ]
        if found:
            return found
    return []


def _disable_flags(client: Profile | None) -> frozenset[str]:
    """The off switch this Client documents; every Client's when the file is nobody's."""
    if client is None:
        return DISABLE_FLAGS
    return frozenset({client.disable_flag}) if client.disable_flag else frozenset()


def _walk(document: dict[str, Any], container: tuple[str, ...]) -> Any:  # noqa: ANN401  # untyped
    """What sits under ``container`` in ``document``, or nothing when nothing does."""
    node: Any = document
    for key in container:
        node = _as_map(node).get(key)
    return node


def _entries(node: Any, shape: Literal["map", "list"]) -> list[tuple[str, dict[str, Any]]]:  # noqa: ANN401  # untyped
    """The named entries of a container, in the shape ``Profile.entry_shape`` records.

    ``map``: keyed by the server's name. ``list``: entries that carry their own ``name``.
    """
    if shape == "list":
        listed = [_as_map(entry) for entry in _as_list(node)]
        return [(entry["name"], entry) for entry in listed if isinstance(entry.get("name"), str)]
    return [
        (name, _as_map(entry)) for name, entry in _as_map(node).items() if isinstance(entry, dict)
    ]


def _transport(entry: dict[str, Any], client: Profile | None) -> Transport | None:
    """How this entry says its server is reached, or nothing when it does not say."""
    command = entry.get("command")
    if isinstance(command, str) and command:
        return StdioTransport(
            transport="stdio",
            command=command,
            args=[str(arg) for arg in _as_list(entry.get("args"))],
            env={name: f"${{{name}}}" for name in sorted(_as_map(entry.get("env")))},
        )
    keys = (client.url_key, *URL_KEYS) if client else URL_KEYS
    url = next((entry[key] for key in keys if isinstance(entry.get(key), str)), None)
    if url is None:
        return None
    kind = entry.get("type") or entry.get("transport")
    if kind == SSE_TYPE:
        return SseTransport(transport="sse", url=url)
    if kind is None or kind in HTTP_TYPES:
        return HttpTransport(transport="http", url=url)
    return None


# --- what to make of them ------------------------------------------------------------------------


def is_own_proxy(transport: Transport, daemon: tuple[str, int]) -> bool:
    """This entry already points at mcpshape: the stdio shim, or a URL on the Daemon."""
    match transport:
        case StdioTransport():
            words = {Path(transport.command).stem, *transport.args}
            return "mcpshape" in words and "serve" in words
        case HttpTransport() | SseTransport():
            split = urlsplit(transport.url)
            host, port = daemon
            return split.port == port and _same_host(split.hostname or "", host)
        case _:
            return False


def _same_host(one: str, other: str) -> bool:
    return one == other or {one, other} <= LOOPBACK


def slug_for(name: str) -> str | None:
    """``name`` as an Upstream name, or nothing when no valid name is left of it.

    Upstream names are slugs and appear in URLs, but a Client's file names a server whatever
    the user typed. Runs of anything else become one hyphen, and a reserved name is qualified
    rather than refused, so a server called ``mcp`` is still importable.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if slug in RESERVED:
        slug = f"{slug}-server"
    return slug if SLUG.match(slug) else None
