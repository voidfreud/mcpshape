"""Rendering Upstreams and Proxies for ``ls``, ``show``, and friends."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.syntax import Syntax
from rich.table import Table

from mcpshape.cli.common import console
from mcpshape.config import load_settings, proxy_file
from mcpshape.model import DEFAULT_PROXY_NAME

if TYPE_CHECKING:
    from pathlib import Path

    from mcpshape.cli.live import Live
    from mcpshape.model import Transport, Upstream


STATE_COLOR = {
    "ready": "green",
    "idle-pending": "green",
    "connecting": "yellow",
    "unavailable": "red",
    "stopping": "yellow",
}
"""How every lifecycle state reads at a glance. Anything else is dim, including "unknown"."""

HEALTH_COLOR = {"ok": "green", "unhealthy": "red"}


def state_text(state: str) -> str:
    return _colored(state, STATE_COLOR.get(state))


def health_text(health: str) -> str:
    return _colored(health, HEALTH_COLOR.get(health))


def _colored(text: str, color: str | None) -> str:
    return f"[{color}]{text}[/{color}]" if color else f"[dim]{text}[/dim]"


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


def upstreams_table(config_dir: Path, upstreams: list[Upstream], live: Live) -> Table:
    """Every Upstream with its Proxies, and what the Daemon says each is doing right now."""
    table = Table(box=None, pad_edge=False)
    for column in ("Upstream", "Transport", "State", "Proxy", "Health", "URL"):
        table.add_column(column, overflow="fold")
    for upstream in upstreams:
        for index, proxy in enumerate(upstream.proxies):
            table.add_row(
                upstream.name if index == 0 else "",
                describe_transport(upstream.transport) if index == 0 else "",
                state_text(live.state_of(upstream.name)) if index == 0 else "",
                proxy,
                health_text(live.health_of(upstream.name, proxy)),
                proxy_url(config_dir, upstream.name, proxy),
            )
    return table


def print_upstreams(config_dir: Path, upstreams: list[Upstream], live: Live) -> None:
    """The ``ls`` view: the table, and a word when there is no Daemon to ask."""
    console.print(upstreams_table(config_dir, upstreams, live))
    if not live.running:
        console.print(
            "[dim]Daemon not running, so State and Health are unknown. "
            "Start it with: mcpshape daemon up[/dim]"
        )


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
