"""The config directory: TOML files read and rewritten with comments intact, and validated.

Layout: ``config.toml`` for global settings, ``upstreams/<name>/upstream.toml`` per Upstream,
and ``upstreams/<name>/<proxy>.toml`` per Proxy. Every file carries ``version``.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Annotated, Any, Literal, cast

import tomlkit
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator
from tomlkit.exceptions import TOMLKitError

from mcpshape.logs import MIN_ROTATE_BYTES, Level
from mcpshape.model import (
    DEFAULT_PROXY_NAME,
    CapOverrides,
    CapSettings,
    HttpTransport,
    LifecycleOverrides,
    LifecycleSettings,
    MemoryTransport,
    SseTransport,
    StdioTransport,
    ToolCapOverrides,
    Transport,
    Upstream,
)
from mcpshape.secrets import (
    SECRETS_FILE,
    SecretError,
    Secrets,
    check_mode,
    unset_message,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Iterable, Mapping
    from pathlib import Path

    from mcpshape.catalog import Item, Kind

FILE_VERSION = 1
SETTINGS_FILE = "config.toml"
UPSTREAMS_DIR = "upstreams"
UPSTREAM_FILE = "upstream.toml"
SCHEMA_URL_BASE = "https://raw.githubusercontent.com/voidfreud/mcpshape/main/src/mcpshape/schemas/"

FileKind = Literal["settings", "upstream", "proxy", "secrets"]


class _File(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = Field(description="Format version of this file.")


class DaemonSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = Field(default="127.0.0.1", description="Address the Daemon binds to.")
    port: int = Field(default=8321, ge=1, le=65535, description="Port the Daemon listens on.")
    token: str | None = Field(
        default=None,
        description=(
            "Bearer token every request to Proxies, the API, and the dashboard must carry. "
            "Required to bind a non-loopback host."
        ),
    )
    dashboard: bool = Field(
        default=True,
        description=(
            "Serve the read-only dashboard at /. Off, the Daemon does not mount it at all: "
            "for a Daemon nobody browses to. Read at Daemon start."
        ),
    )


LOG_CAP_BYTES = 50 * 1024 * 1024
"""The most every log file together may take on disk, unless ``config.toml`` says otherwise."""


class LogSettings(BaseModel):
    """The app log's level, and the one size cap every log file shares."""

    model_config = ConfigDict(extra="forbid")

    level: Level = Field(
        default="debug",
        description=(
            "The least severe app log level kept: debug, info, warning, or error. "
            "Verbose by default; the call log is always on whatever this says."
        ),
    )
    max_bytes: int = Field(
        default=LOG_CAP_BYTES,
        ge=MIN_ROTATE_BYTES * 2,
        description=(
            "The most the app log and the call log together may take on disk, rotated "
            "files included; the oldest rotated files go first. At the smallest value no "
            "rotated file is kept at all."
        ),
    )


class DriftSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    new_items: Literal["hidden", "visible"] = Field(
        default="hidden",
        description="Whether items an Upstream adds later are hidden or visible once accepted.",
    )


class SettingsFile(_File):
    """``config.toml``: global settings."""

    daemon: DaemonSettings = Field(default_factory=DaemonSettings)
    log: LogSettings = Field(default_factory=LogSettings)
    drift: DriftSettings = Field(default_factory=DriftSettings)
    lifecycle: LifecycleSettings = Field(
        default_factory=LifecycleSettings,
        description="Defaults for every Upstream connection; an Upstream file may override.",
    )
    caps: CapSettings = Field(
        default_factory=CapSettings,
        description=(
            "The global master Cap per kind. An Upstream, a Proxy, or a tool may only lower "
            "what it inherits."
        ),
    )


class SecretsFile(_File):
    """``secrets.toml``: what a ``${VAR}`` reference resolves to when the environment has none.

    Mode 0600, in the config directory. The Daemon environment wins over what is written here.
    """

    secrets: dict[str, str] = Field(
        default_factory=dict[str, str],
        description="Values by variable name, as ${VAR} references in Upstream files name them.",
    )


SCHEMA_KEYS = frozenset(
    {"type", "schema", "input_schema", "inputSchema", "output_schema", "outputSchema", "items"}
)
"""Keys that would change a schema type. An Override never does; the brief rules it out."""


class _Override(BaseModel):
    """What every Override shares: an unknown key is refused, a schema key with its own reason."""

    model_config = ConfigDict(extra="forbid")

    hidden: bool = Field(default=False, description="Do not expose this item at all.")

    @model_validator(mode="before")
    @classmethod
    def _refuse_schema_keys(cls, data: object) -> object:
        given: object = data
        if isinstance(data, dict):
            keys = [str(key) for key in cast("dict[object, object]", data)]
            if found := sorted(key for key in keys if key in SCHEMA_KEYS):
                msg = (
                    f"{', '.join(found)}: schema types cannot be changed by an Override; "
                    "rename, re-describe, default, or hide instead"
                )
                raise ValueError(msg)
        return given


class ArgumentOverride(_Override):
    """How one argument of a tool is presented, keyed by its name in the Catalog."""

    name: str | None = Field(default=None, description="Exposed argument name.")
    description: str | None = Field(default=None, description="Replaces the description.")
    default: Any | None = Field(
        default=None,
        description=(
            "Default the Client sees, and what is sent when the Client omits the argument. "
            "Required for a hidden argument the Upstream requires."
        ),
    )
    required: bool | None = Field(
        default=None, description="Whether the Client must give it. Moot for a hidden argument."
    )


class Annotations(BaseModel):
    """Tool annotations set by the user; an unset hint keeps what the Upstream advertises."""

    model_config = ConfigDict(extra="forbid")

    read_only: bool | None = Field(default=None, description="The tool changes nothing.")
    destructive: bool | None = Field(default=None, description="The tool may destroy data.")
    idempotent: bool | None = Field(default=None, description="Repeating a call changes nothing.")
    open_world: bool | None = Field(default=None, description="The tool reaches outside systems.")

    def as_mcp(self) -> dict[str, bool]:
        """The hints as MCP names them on the wire, unset ones left out."""
        hints = {
            "readOnlyHint": self.read_only,
            "destructiveHint": self.destructive,
            "idempotentHint": self.idempotent,
            "openWorldHint": self.open_world,
        }
        return {key: value for key, value in hints.items() if value is not None}


class ToolOverride(_Override):
    """How one tool is presented, keyed by its Catalog name."""

    name: str | None = Field(default=None, description="Exposed tool name.")
    title: str | None = Field(default=None, description="Replaces the title.")
    description: str | None = Field(default=None, description="Replaces the description.")
    annotations: Annotations | None = Field(default=None, description="Hints to set.")
    args: dict[str, ArgumentOverride] = Field(
        default_factory=dict, description="Argument Overrides by Catalog argument name."
    )
    caps: ToolCapOverrides = Field(
        default_factory=ToolCapOverrides,
        description="This tool's Caps, over what it inherits from the Proxy.",
    )


class ResourceOverride(_Override):
    """How one resource or resource template is presented, keyed by its Catalog URI."""

    uri: str | None = Field(
        default=None,
        description=(
            "Exposed URI, or URI template with the same parameters. Clients read it by this."
        ),
    )
    name: str | None = Field(default=None, description="Replaces the display name.")
    description: str | None = Field(default=None, description="Replaces the description.")


class PromptOverride(_Override):
    """How one prompt is presented, keyed by its Catalog name."""

    name: str | None = Field(default=None, description="Exposed prompt name.")
    description: str | None = Field(default=None, description="Replaces the description.")


ItemOverride = ToolOverride | ResourceOverride | PromptOverride


class ProxyFile(_File):
    """``<proxy>.toml``: one curation of an Upstream."""

    name: str | None = Field(
        default=None, description="Server name Clients see; default: <upstream>/<proxy>."
    )
    instructions: str | None = Field(
        default=None, description="Replaces the Upstream's instructions."
    )
    caps: CapOverrides = Field(
        default_factory=CapOverrides,
        description="This Proxy's Caps, over what it inherits from the Upstream.",
    )
    port: int | None = Field(
        default=None,
        ge=1,
        le=65535,
        description="Serve this Proxy on an additional port, besides its path on the main one.",
    )
    tools: dict[str, ToolOverride] = Field(
        default_factory=dict, description="Overrides by tool name."
    )
    resources: dict[str, ResourceOverride] = Field(
        default_factory=dict, description="Overrides by resource URI or resource template."
    )
    prompts: dict[str, PromptOverride] = Field(
        default_factory=dict, description="Overrides by prompt name."
    )

    def overrides(self, kind: Kind) -> Mapping[str, ItemOverride]:
        return cast("Mapping[str, ItemOverride]", getattr(self, OVERRIDE_SECTION[kind]))


OVERRIDE_SECTION: dict[Kind, str] = {
    "tool": "tools",
    "resource": "resources",
    "resource_template": "resources",
    "prompt": "prompts",
}
"""The Proxy file section that holds Overrides for each kind of Catalog item."""


class _UpstreamFile(_File):
    """What every Upstream file carries besides the transport that discriminates it."""

    lifecycle: LifecycleOverrides = Field(
        default_factory=LifecycleOverrides,
        description="This Upstream's lifecycle settings, over the defaults in config.toml.",
    )
    caps: CapOverrides = Field(
        default_factory=CapOverrides,
        description="This Upstream's Caps, over the global master Caps in config.toml.",
    )


class StdioUpstreamFile(_UpstreamFile, StdioTransport):
    pass


class HttpUpstreamFile(_UpstreamFile, HttpTransport):
    pass


class SseUpstreamFile(_UpstreamFile, SseTransport):
    pass


class MemoryUpstreamFile(_UpstreamFile, MemoryTransport):
    pass


UpstreamFile = Annotated[
    StdioUpstreamFile | HttpUpstreamFile | SseUpstreamFile,
    Field(discriminator="transport"),
]
"""``upstream.toml``: how the Upstream is reached. What a user's file may say, and what the
shipped schema describes."""

_TestUpstreamFile = Annotated[
    StdioUpstreamFile | HttpUpstreamFile | SseUpstreamFile | MemoryUpstreamFile,
    Field(discriminator="transport"),
]
"""``upstream.toml`` as the test seam reads it: ``transport = "memory"`` as well (#18)."""

FILE_MODELS: dict[FileKind, TypeAdapter[Any]] = {
    "settings": TypeAdapter(SettingsFile),
    "upstream": TypeAdapter(UpstreamFile),
    "proxy": TypeAdapter(ProxyFile),
    "secrets": TypeAdapter(SecretsFile),
}

_MEMORY_UPSTREAM_MODEL: TypeAdapter[Any] = TypeAdapter(_TestUpstreamFile)

MEMORY_TRANSPORT = "memory"
MEMORY_REFUSED = (
    "'memory' is the test seam's transport, an MCP server living in the Daemon process; "
    "an Upstream is reached over stdio, http, or sse"
)
"""What a user's Upstream file is told when it says ``transport = "memory"`` (#18)."""

_memory_allowed = False


@contextmanager
def memory_upstreams_allowed() -> Generator[None]:
    """Let ``upstream.toml`` say ``transport = "memory"`` while the block runs.

    The test seam's alone (#18): a memory Upstream imports Python into the Daemon process by
    naming it in a config file, which no user's file may do. Nothing but Python code entering
    this block enables it: no config value, no environment variable, no CLI flag.
    """
    global _memory_allowed  # noqa: PLW0603  # the one switch the seam flips
    previous, _memory_allowed = _memory_allowed, True
    try:
        yield
    finally:
        _memory_allowed = previous


def _upstream_model() -> TypeAdapter[Any]:
    return _MEMORY_UPSTREAM_MODEL if _memory_allowed else FILE_MODELS["upstream"]


def schema_url(kind: FileKind) -> str:
    return f"{SCHEMA_URL_BASE}{kind}.schema.json"


class ConfigError(Exception):
    """A config file could not be read or does not say what it must."""


@dataclass(frozen=True)
class Problem:
    """One thing ``doctor`` found wrong with a file."""

    path: Path
    key: str
    message: str

    def __str__(self) -> str:
        where = f"{self.path}: {self.key}" if self.key else str(self.path)
        return f"{where}: {self.message}"


# --- reading -----------------------------------------------------------------------------------


def read_document(path: Path) -> tomlkit.TOMLDocument:
    try:
        return tomlkit.parse(path.read_text())
    except (OSError, TOMLKitError) as exc:
        msg = f"{path}: {exc}"
        raise ConfigError(msg) from exc


def _validate(path: Path, kind: FileKind, data: dict[str, Any]) -> list[Problem]:
    if kind == "upstream" and data.get("transport") == MEMORY_TRANSPORT and not _memory_allowed:
        return [Problem(path, "transport", MEMORY_REFUSED)]
    model = _upstream_model() if kind == "upstream" else FILE_MODELS[kind]
    try:
        model.validate_python(data)
    except ValidationError as exc:
        return [Problem(path, _key(error["loc"], data), error["msg"]) for error in exc.errors()]
    return []


def _key(loc: tuple[int | str, ...], data: dict[str, Any]) -> str:
    """The dotted key a validation error points at, without the union tag pydantic adds."""
    if loc and loc[0] == data.get("transport"):
        loc = loc[1:]
    return ".".join(str(part) for part in loc)


def check_file(path: Path, kind: FileKind) -> list[Problem]:
    """Every problem with ``path`` as a ``kind`` file; empty when it is valid."""
    try:
        document = read_document(path)
    except ConfigError as exc:
        return [Problem(path, "", str(exc).removeprefix(f"{path}: "))]
    return _validate(path, kind, document.unwrap())


def _load(path: Path, kind: FileKind) -> dict[str, Any]:
    data = read_document(path).unwrap()
    if problems := _validate(path, kind, data):
        raise ConfigError("\n".join(str(problem) for problem in problems))
    return data


def load_settings(config_dir: Path) -> SettingsFile:
    path = config_dir / SETTINGS_FILE
    if not path.is_file():
        return SettingsFile(version=FILE_VERSION)
    return SettingsFile.model_validate(_load(path, "settings"))


def upstream_dir(config_dir: Path, name: str) -> Path:
    return config_dir / UPSTREAMS_DIR / name


def proxy_file(config_dir: Path, upstream: str, proxy: str) -> Path:
    return upstream_dir(config_dir, upstream) / f"{proxy}.toml"


def proxy_code_file(config_dir: Path, upstream: str, proxy: str) -> Path:
    """The Proxy's Python file, next to its TOML: Hooks and Virtual Tools live there."""
    return upstream_dir(config_dir, upstream) / f"{proxy}.py"


def list_proxies(config_dir: Path, upstream: str) -> tuple[str, ...]:
    """Proxy names of ``upstream``, ``default`` first, the rest in name order."""
    names = sorted(
        path.stem
        for path in upstream_dir(config_dir, upstream).glob("*.toml")
        if path.name != UPSTREAM_FILE
    )
    return tuple(sorted(names, key=lambda name: (name != DEFAULT_PROXY_NAME, name)))


def load_proxy(config_dir: Path, upstream: str, proxy: str) -> ProxyFile:
    path = proxy_file(config_dir, upstream, proxy)
    if not path.is_file():
        msg = f"no Proxy {upstream}/{proxy}"
        raise ConfigError(msg)
    return ProxyFile.model_validate(_load(path, "proxy"))


def load_upstream(
    config_dir: Path, name: str, defaults: LifecycleSettings | None = None
) -> Upstream:
    """The Upstream ``name``, with its lifecycle settings over the global defaults."""
    path = upstream_dir(config_dir, name) / UPSTREAM_FILE
    if not path.is_file():
        msg = f"no Upstream named {name!r} in {config_dir}"
        raise ConfigError(msg)
    file: _UpstreamFile = _upstream_model().validate_python(_load(path, "upstream"))
    if defaults is None:
        defaults = load_settings(config_dir).lifecycle
    return Upstream(
        name=name,
        transport=cast("Transport", file),
        proxies=list_proxies(config_dir, name),
        lifecycle=file.lifecycle.over(defaults),
        caps=file.caps,
    )


def load_upstreams(config_dir: Path) -> list[Upstream]:
    """Every Upstream registered under ``config_dir``, in name order."""
    upstreams_dir = config_dir / UPSTREAMS_DIR
    if not upstreams_dir.is_dir():
        return []
    defaults = load_settings(config_dir).lifecycle
    return [
        load_upstream(config_dir, path.name, defaults)
        for path in sorted(upstreams_dir.iterdir())
        if (path / UPSTREAM_FILE).is_file()
    ]


# --- secrets -----------------------------------------------------------------------------------


def secrets_file(config_dir: Path) -> Path:
    return config_dir / SECRETS_FILE


def load_secret_values(config_dir: Path) -> dict[str, str]:
    """What the secrets file holds, or nothing when there is none.

    Raises ``SecretError`` when the file may be read by anyone else, or does not say what it
    must. Neither message carries a value.
    """
    path = secrets_file(config_dir)
    if not path.is_file():
        return {}
    check_mode(path)
    try:
        return SecretsFile.model_validate(_load(path, "secrets")).secrets
    except ConfigError as exc:
        raise SecretError(str(exc)) from exc


def secrets_for(config_dir: Path) -> Secrets:
    """Where ``${VAR}`` is looked up for the Upstreams under ``config_dir``, read on each use."""
    return Secrets(partial(load_secret_values, config_dir))


def secret_problems(config_dir: Path) -> list[Problem]:
    """Every reference no environment variable and no secrets entry answers, file by file.

    Only the transport is looked at, since that is where a reference is resolved: when the
    Upstream is reached, not when its file is loaded, so a value edited while the Daemon runs
    counts the next time. A secrets file anyone else can read is the first problem, and the
    only one reported then: nothing was read, so nothing else can be judged.
    """
    try:
        values = load_secret_values(config_dir)
    except SecretError as exc:
        return [Problem(secrets_file(config_dir), "", str(exc))]
    secrets = Secrets(lambda: values)
    problems: list[Problem] = []
    for path, kind in all_files(config_dir):
        if kind != "upstream":
            continue
        try:
            upstream = load_upstream(config_dir, path.parent.name)
        except ConfigError:
            continue  # unreadable, which check_file reports on its own
        if missing := secrets.missing(upstream.transport.model_dump()):
            problems.append(Problem(path, "", unset_message(missing)))
    return problems


def all_files(config_dir: Path) -> list[tuple[Path, FileKind]]:
    """Every config file under ``config_dir`` with its kind, for ``doctor``."""
    files: list[tuple[Path, FileKind]] = []
    if (settings := config_dir / SETTINGS_FILE).is_file():
        files.append((settings, "settings"))
    if (secrets := secrets_file(config_dir)).is_file():
        files.append((secrets, "secrets"))
    upstreams_dir = config_dir / UPSTREAMS_DIR
    if upstreams_dir.is_dir():
        for directory in sorted(upstreams_dir.iterdir()):
            files.extend(
                (path, "upstream" if path.name == UPSTREAM_FILE else "proxy")
                for path in sorted(directory.glob("*.toml"))
            )
    return files


# --- writing -----------------------------------------------------------------------------------


def new_document(kind: FileKind, data: dict[str, Any]) -> tomlkit.TOMLDocument:
    """A fresh ``kind`` file: schema comment, ``version``, then ``data``."""
    # Parsed from text: tomlkit.comment() would insert a space after the hash, and editors
    # only recognise the schema directive as exactly ``#:schema``.
    document = tomlkit.parse(f"#:schema {schema_url(kind)}\nversion = {FILE_VERSION}\n")
    for key, value in data.items():
        document[key] = value
    return document


def write_document(path: Path, document: tomlkit.TOMLDocument) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tomlkit.dumps(document))


def rewrite(path: Path, edit: Callable[[tomlkit.TOMLDocument], None]) -> None:
    """Apply ``edit`` to the file at ``path``, keeping comments and key order."""
    document = read_document(path)
    edit(document)
    write_document(path, document)


def add_upstream(config_dir: Path, name: str, transport: Transport) -> Upstream:
    """Register ``name`` with ``transport`` and give it its ``default`` Proxy."""
    directory = upstream_dir(config_dir, name)
    if directory.exists():
        msg = f"Upstream {name!r} already exists"
        raise ConfigError(msg)
    write_document(
        directory / UPSTREAM_FILE,
        new_document("upstream", transport.model_dump(exclude_defaults=True)),
    )
    add_proxy(config_dir, name, DEFAULT_PROXY_NAME)
    return load_upstream(config_dir, name)


def remove_upstream(config_dir: Path, name: str) -> None:
    directory = upstream_dir(config_dir, name)
    if not (directory / UPSTREAM_FILE).is_file():
        msg = f"no Upstream named {name!r}"
        raise ConfigError(msg)
    for path in directory.iterdir():
        path.unlink()
    directory.rmdir()


def add_proxy(config_dir: Path, upstream: str, proxy: str) -> Path:
    if not (upstream_dir(config_dir, upstream) / UPSTREAM_FILE).is_file():
        msg = f"no Upstream named {upstream!r}"
        raise ConfigError(msg)
    path = proxy_file(config_dir, upstream, proxy)
    if path.exists():
        msg = f"Proxy {upstream}/{proxy} already exists"
        raise ConfigError(msg)
    write_document(path, new_document("proxy", {}))
    return path


def set_env(config_dir: Path, name: str, env: Mapping[str, str]) -> None:
    """Set each of ``env`` in the Upstream ``name``'s ``[env]`` block, replacing a key already
    there; comments elsewhere in the file survive."""

    def edit(document: tomlkit.TOMLDocument) -> None:
        table = _table_at(document, "env")
        for key, value in env.items():
            table[key] = value

    rewrite(upstream_dir(config_dir, name) / UPSTREAM_FILE, edit)


def _table_at(document: tomlkit.TOMLDocument, *keys: str) -> Any:  # noqa: ANN401  # tomlkit's containers are untyped
    """The table under ``keys``, made on the way as ``[a.b.c]`` headers rather than inline."""
    node: Any = document
    for index, key in enumerate(keys):
        if key not in node:
            node[key] = tomlkit.table(is_super_table=index < len(keys) - 1)
        node = node[key]
    return node


def set_override(path: Path, item: Item, key: str, value: object, note: str = "") -> None:
    """Write ``key = value`` on the Override for ``item`` in the Proxy file at ``path``."""
    _set_key(path, (OVERRIDE_SECTION[item.kind], item.name), key, value, note)


def set_argument_override(path: Path, tool: str, argument: str, key: str, value: object) -> None:
    """Write ``key = value`` on the Override for ``argument`` of ``tool``."""
    _set_key(path, ("tools", tool, "args", argument), key, value, "")


def set_tool_cap(path: Path, tool: str, key: str, value: object) -> None:
    """Write ``key = value`` on the Cap Overrides for ``tool``."""
    _set_key(path, ("tools", tool, "caps"), key, value, "")


def _set_key(path: Path, keys: tuple[str, ...], key: str, value: object, note: str) -> None:
    def edit(document: tomlkit.TOMLDocument) -> None:
        written = tomlkit.item(value)  # pyright: ignore[reportUnknownMemberType]  # tomlkit's item() is untyped
        if note:
            written.comment(note)
        _table_at(document, *keys)[key] = written

    rewrite(path, edit)


def hide_items(path: Path, items: Iterable[Item], note: str) -> None:
    """Set ``hidden = true`` on each of ``items`` in the Proxy file at ``path``, with ``note``."""

    def edit(document: tomlkit.TOMLDocument) -> None:
        for item in items:
            flag = tomlkit.item(True)  # noqa: FBT003  # the value being written
            flag.comment(note)
            _table_at(document, OVERRIDE_SECTION[item.kind], item.name)["hidden"] = flag

    rewrite(path, edit)


def remove_proxy(config_dir: Path, upstream: str, proxy: str) -> None:
    if proxy == DEFAULT_PROXY_NAME:
        msg = f"the default Proxy cannot be removed; remove the Upstream {upstream!r} instead"
        raise ConfigError(msg)
    path = proxy_file(config_dir, upstream, proxy)
    if not path.is_file():
        msg = f"no Proxy {upstream}/{proxy}"
        raise ConfigError(msg)
    path.unlink()
    proxy_code_file(config_dir, upstream, proxy).unlink(missing_ok=True)
