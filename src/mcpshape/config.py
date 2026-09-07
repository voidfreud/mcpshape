"""The config directory: TOML files read and rewritten with comments intact, and validated.

Layout: ``config.toml`` for global settings, ``upstreams/<name>/upstream.toml`` per Upstream,
and ``upstreams/<name>/<proxy>.toml`` per Proxy. Every file carries ``version``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any, Literal

import tomlkit
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError
from tomlkit.exceptions import TOMLKitError

from mcpshape.model import (
    DEFAULT_PROXY_NAME,
    HttpTransport,
    MemoryTransport,
    SseTransport,
    StdioTransport,
    Transport,
    Upstream,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

FILE_VERSION = 1
SETTINGS_FILE = "config.toml"
UPSTREAMS_DIR = "upstreams"
UPSTREAM_FILE = "upstream.toml"
SCHEMA_URL_BASE = "https://raw.githubusercontent.com/voidfreud/mcpshape/main/src/mcpshape/schemas/"

FileKind = Literal["settings", "upstream", "proxy"]


class _File(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = Field(description="Format version of this file.")


class DaemonSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = Field(default="127.0.0.1", description="Address the Daemon binds to.")
    port: int = Field(default=8321, ge=1, le=65535, description="Port the Daemon listens on.")


class SettingsFile(_File):
    """``config.toml``: global settings."""

    daemon: DaemonSettings = Field(default_factory=DaemonSettings)


class ProxyFile(_File):
    """``<proxy>.toml``: one curation of an Upstream."""


class StdioUpstreamFile(_File, StdioTransport):
    pass


class HttpUpstreamFile(_File, HttpTransport):
    pass


class SseUpstreamFile(_File, SseTransport):
    pass


class MemoryUpstreamFile(_File, MemoryTransport):
    pass


UpstreamFile = Annotated[
    StdioUpstreamFile | HttpUpstreamFile | SseUpstreamFile | MemoryUpstreamFile,
    Field(discriminator="transport"),
]
"""``upstream.toml``: how the Upstream is reached."""

FILE_MODELS: dict[FileKind, TypeAdapter[Any]] = {
    "settings": TypeAdapter(SettingsFile),
    "upstream": TypeAdapter(UpstreamFile),
    "proxy": TypeAdapter(ProxyFile),
}


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
    try:
        FILE_MODELS[kind].validate_python(data)
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


def list_proxies(config_dir: Path, upstream: str) -> tuple[str, ...]:
    """Proxy names of ``upstream``, ``default`` first, the rest in name order."""
    names = sorted(
        path.stem
        for path in upstream_dir(config_dir, upstream).glob("*.toml")
        if path.name != UPSTREAM_FILE
    )
    return tuple(sorted(names, key=lambda name: (name != DEFAULT_PROXY_NAME, name)))


def load_upstream(config_dir: Path, name: str) -> Upstream:
    path = upstream_dir(config_dir, name) / UPSTREAM_FILE
    if not path.is_file():
        msg = f"no Upstream named {name!r} in {config_dir}"
        raise ConfigError(msg)
    transport: Transport = FILE_MODELS["upstream"].validate_python(_load(path, "upstream"))
    return Upstream(name=name, transport=transport, proxies=list_proxies(config_dir, name))


def load_upstreams(config_dir: Path) -> list[Upstream]:
    """Every Upstream registered under ``config_dir``, in name order."""
    upstreams_dir = config_dir / UPSTREAMS_DIR
    if not upstreams_dir.is_dir():
        return []
    return [
        load_upstream(config_dir, path.name)
        for path in sorted(upstreams_dir.iterdir())
        if (path / UPSTREAM_FILE).is_file()
    ]


def all_files(config_dir: Path) -> list[tuple[Path, FileKind]]:
    """Every config file under ``config_dir`` with its kind, for ``doctor``."""
    files: list[tuple[Path, FileKind]] = []
    if (settings := config_dir / SETTINGS_FILE).is_file():
        files.append((settings, "settings"))
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


def remove_proxy(config_dir: Path, upstream: str, proxy: str) -> None:
    if proxy == DEFAULT_PROXY_NAME:
        msg = f"the default Proxy cannot be removed; remove the Upstream {upstream!r} instead"
        raise ConfigError(msg)
    path = proxy_file(config_dir, upstream, proxy)
    if not path.is_file():
        msg = f"no Proxy {upstream}/{proxy}"
        raise ConfigError(msg)
    path.unlink()
