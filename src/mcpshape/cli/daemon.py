"""``mcpshape daemon``: up and status. The rest of the verbs arrive with Daemon operations."""

from __future__ import annotations

import typer
from rich.markup import escape
from rich.table import Table

from mcpshape import daemon
from mcpshape.cli.common import HELP_OPTIONS, console, example, reporting_errors, state
from mcpshape.cli.listing import health_text, state_text
from mcpshape.cli.live import Live, how_long, read_live
from mcpshape.config import load_settings

app = typer.Typer(
    help="Operate the Daemon: up, down, status, logs, reload, install, uninstall.",
    epilog=example("daemon up"),
    context_settings=HELP_OPTIONS,
    no_args_is_help=True,
)


@app.command("up", epilog=example("daemon up"))
def up(ctx: typer.Context) -> None:
    """Run the Daemon in the foreground until interrupted."""
    config_dir, state_dir = state(ctx).config_dir, state(ctx).state_dir
    with reporting_errors():
        settings = load_settings(config_dir).daemon
        console.print(f"Daemon listening on http://{settings.host}:{settings.port}")
        daemon.run(config_dir, state_dir)


@app.command("status", epilog=example("daemon status"))
def status(ctx: typer.Context) -> None:
    """Show what the running Daemon is doing: every Upstream's connection and Proxy."""
    with reporting_errors():
        live = read_live(state(ctx).config_dir)
    if not live.running:
        console.print(f"Daemon not running at {live.url}")
        console.print("Start it with: [bold]mcpshape daemon up[/bold]")
        return
    console.print(f"Daemon running at [bold]{live.url}[/bold]")
    if live.state is None or not live.state.upstreams:
        console.print("It is serving no Upstreams. Add one with [bold]mcpshape add[/bold].")
        return
    console.print(_table(live))
    for note in _notes(live):
        console.print(f"  [yellow]![/] {escape(note)}")


def _table(live: Live) -> Table:
    table = Table(box=None, pad_edge=False)
    for column in ("Upstream", "State", "For", "Proxy", "Health"):
        table.add_column(column, overflow="fold")
    for upstream in live.state.upstreams if live.state else []:
        for index, proxy in enumerate(upstream.proxies):
            table.add_row(
                upstream.name if index == 0 else "",
                state_text(upstream.state) if index == 0 else "",
                how_long(upstream.seconds) if index == 0 else "",
                proxy.name,
                health_text(proxy.health),
            )
    return table


def _notes(live: Live) -> list[str]:
    """Why anything is unavailable or unhealthy, under the table that says it is."""
    notes: list[str] = []
    for upstream in live.state.upstreams if live.state else []:
        if upstream.error:
            notes.append(f"{upstream.name}: {upstream.error}")
        notes += [
            f"{upstream.name}/{proxy.name}: {proxy.detail}"
            for proxy in upstream.proxies
            if proxy.detail
        ]
    return notes
