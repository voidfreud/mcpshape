"""Reading the config directory: ``upstreams/<name>/upstream.toml``."""

from __future__ import annotations

import tomllib
from typing import TYPE_CHECKING, Any

from mcpshape.model import MemoryTarget, Upstream, UpstreamTarget

if TYPE_CHECKING:
    from pathlib import Path

UPSTREAM_FILE = "upstream.toml"
UPSTREAM_FILE_VERSION = 1


class ConfigError(Exception):
    """A config file could not be read or does not say what it must."""


def load_upstreams(config_dir: Path) -> list[Upstream]:
    """Every Upstream registered under ``config_dir``, in name order."""
    upstreams_dir = config_dir / "upstreams"
    if not upstreams_dir.is_dir():
        return []
    return [
        _load_upstream(upstream_dir)
        for upstream_dir in sorted(upstreams_dir.iterdir())
        if (upstream_dir / UPSTREAM_FILE).is_file()
    ]


def _load_upstream(upstream_dir: Path) -> Upstream:
    path = upstream_dir / UPSTREAM_FILE
    try:
        data: dict[str, Any] = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as exc:
        msg = f"{path}: {exc}"
        raise ConfigError(msg) from exc
    if data.get("version") != UPSTREAM_FILE_VERSION:
        msg = f"{path}: expected version = {UPSTREAM_FILE_VERSION}"
        raise ConfigError(msg)
    return Upstream(name=upstream_dir.name, target=_load_target(path, data))


def _load_target(path: Path, data: dict[str, Any]) -> UpstreamTarget:
    transport = data.get("transport")
    if transport == "memory":
        target = data.get("target")
        if not isinstance(target, str) or ":" not in target:
            msg = f'{path}: transport "memory" needs target = "module:attribute"'
            raise ConfigError(msg)
        return MemoryTarget(import_path=target)
    msg = f"{path}: unknown transport {transport!r}"
    raise ConfigError(msg)
