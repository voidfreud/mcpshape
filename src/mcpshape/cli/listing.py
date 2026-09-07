"""Rendering Upstreams and Proxies for ``ls``, ``show``, and friends."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.syntax import Syntax
from rich.table import Table

from mcpshape.config import load_settings, proxy_file
from mcpshape.model import DEFAULT_PROXY_NAME

if TYPE_CHECKING:
    from pathlib import Path

    from mcpshape.model import Transport, Upstream


def proxy_url(config_dir: Path, upstream: str, proxy: str) -> str:
    daemon = load_settings(config_dir).daemon
    path = f"/{upstream}/mcp" if proxy == DEFAULT_PROXY_NAME else f"/{upstream}/{proxy}/mcp"
    return f"http://{daemon.host}:{daemon.port}{path}"


def describe_transport(transport: Transport) -> str:
    match transport.transport:
        case "stdio":
            return " ".join([transport.command, *transport.args])
        case "http" | "sse":
            return transport.url
        case "memory":
            return transport.target


def upstreams_table(config_dir: Path, upstreams: list[Upstream]) -> Table:
    table = Table(box=None, pad_edge=False)
    for column in ("Upstream", "Transport", "Proxy", "URL"):
        table.add_column(column, overflow="fold")
    for upstream in upstreams:
        for index, proxy in enumerate(upstream.proxies):
            table.add_row(
                upstream.name if index == 0 else "",
                describe_transport(upstream.transport) if index == 0 else "",
                proxy,
                proxy_url(config_dir, upstream.name, proxy),
            )
    return table


def proxies_table(config_dir: Path, upstreams: list[Upstream]) -> Table:
    table = Table(box=None, pad_edge=False)
    for column in ("Proxy", "URL", "File"):
        table.add_column(column, overflow="fold")
    for upstream in upstreams:
        for proxy in upstream.proxies:
            table.add_row(
                f"{upstream.name}/{proxy}",
                proxy_url(config_dir, upstream.name, proxy),
                str(proxy_file(config_dir, upstream.name, proxy)),
            )
    return table


def toml_file(path: Path) -> Syntax:
    return Syntax(path.read_text(), "toml", background_color="default")
