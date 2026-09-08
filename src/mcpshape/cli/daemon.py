"""``mcpshape daemon``: up, down, status, logs, reload, install, uninstall."""

from __future__ import annotations

import contextlib
import fcntl
import platform
import sys
import time
from typing import TYPE_CHECKING, Annotated

import typer
from rich.markup import escape
from rich.table import Table

from mcpshape import autostart, daemon
from mcpshape.cli.common import (
    HELP_OPTIONS,
    answering,
    console,
    example,
    fail,
    reporting_errors,
    state,
)
from mcpshape.cli.listing import health_text, state_text
from mcpshape.cli.live import Live, how_long, read_live, reload_daemon, stop_daemon
from mcpshape.config import load_settings
from mcpshape.paths import daemon_lock_file, daemon_log_file, log_dir

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path


app = typer.Typer(
    help="Operate the Daemon: up, down, status, logs, reload, install, uninstall.",
    epilog=example("daemon up"),
    context_settings=HELP_OPTIONS,
    no_args_is_help=True,
)

STARTUP_WAIT = 15.0
"""Seconds a second ``up`` waits for the one holding the lock to start answering."""

SHUTDOWN_WAIT = 10.0
"""Seconds ``daemon down`` waits for the Daemon to stop answering."""

POLL_INTERVAL = 0.05

LOG_LINES = 100
"""How many lines ``daemon logs`` shows by default."""


@contextlib.contextmanager
def _lock(state_dir: Path) -> Generator[bool]:
    """Hold an exclusive lock on the Daemon's lock file for as long as the block runs.

    ``True`` when this call took the lock: nobody else is starting or running right now.
    ``False`` when another ``daemon up`` already holds it, so a second start waits on it
    instead of racing it (#13).
    """
    path = daemon_lock_file(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _wait_for(host: str, port: int, patience: float) -> bool:
    deadline = time.monotonic() + patience
    while time.monotonic() < deadline:
        if answering(host, port):
            return True
        time.sleep(POLL_INTERVAL)
    return answering(host, port)


@app.command("up", epilog=example("daemon up"))
def up(ctx: typer.Context) -> None:
    """Run the Daemon in the foreground until stopped, taking a lock so a second start waits."""
    config_dir, state_dir = state(ctx).config_dir, state(ctx).state_dir
    with reporting_errors():
        settings = load_settings(config_dir).daemon
        daemon.check_bind(settings)
    with _lock(state_dir) as owned:
        if not owned:
            console.print("Another Daemon is starting or already running; waiting for it...")
            if _wait_for(settings.host, settings.port, STARTUP_WAIT):
                console.print(f"Daemon already running at http://{settings.host}:{settings.port}")
                return
            fail(
                "another Daemon appeared to be starting but never answered on "
                f"{settings.host}:{settings.port}"
            )
        if answering(settings.host, settings.port):
            console.print(f"Daemon already running at http://{settings.host}:{settings.port}")
            return
        _offer_install(config_dir, state_dir)
        console.print(f"Daemon listening on http://{settings.host}:{settings.port}")
        console.print(f"Logging to {daemon_log_file(state_dir)}")
        daemon.run(config_dir, state_dir)


@app.command("down", epilog=example("daemon down"))
def down(ctx: typer.Context) -> None:
    """Stop the running Daemon."""
    config_dir = state(ctx).config_dir
    with reporting_errors():
        settings = load_settings(config_dir).daemon
    if not answering(settings.host, settings.port):
        console.print(f"Daemon not running at http://{settings.host}:{settings.port}")
        return
    if not stop_daemon(config_dir):
        fail(f"could not reach the Daemon at http://{settings.host}:{settings.port} to stop it")
    deadline = time.monotonic() + SHUTDOWN_WAIT
    while time.monotonic() < deadline and answering(settings.host, settings.port):
        time.sleep(POLL_INTERVAL)
    if answering(settings.host, settings.port):
        fail("the Daemon did not stop in time")
    console.print("Daemon stopped")


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


@app.command("reload", epilog=example("daemon reload"))
def reload(ctx: typer.Context) -> None:
    """Make the running Daemon re-read every Proxy's files now, and show what came of it.

    A Proxy re-reads a changed file on the next request anyway; this forces the matter for
    every Proxy at once and reports each one's health.
    """
    with reporting_errors():
        live = reload_daemon(state(ctx).config_dir)
    if not live.running:
        console.print(f"Daemon not running at {live.url}")
        console.print("Nothing to reload: the Daemon reads every file when it starts.")
        return
    console.print(f"Reloaded every Proxy of the Daemon at [bold]{live.url}[/bold]")
    if live.state is None or not live.state.upstreams:
        console.print("It is serving no Upstreams. Add one with [bold]mcpshape add[/bold].")
        return
    console.print(_table(live))
    for note in _notes(live):
        console.print(f"  [yellow]![/] {escape(note)}")


@app.command("logs", epilog=example("daemon logs"))
def logs(
    ctx: typer.Context,
    lines: Annotated[
        int, typer.Option("-n", "--lines", help="How many lines to show, from the end.")
    ] = LOG_LINES,
) -> None:
    """Show the tail of the Daemon's app log."""
    path = daemon_log_file(state(ctx).state_dir)
    if not path.is_file():
        console.print("No log yet. Start the Daemon with: [bold]mcpshape daemon up[/bold]")
        return
    for line in path.read_text().splitlines()[-lines:]:
        console.print(line, markup=False, highlight=False)


@app.command("install", epilog=example("daemon install"))
def install(ctx: typer.Context) -> None:
    """Install autostart: a launchd user agent (macOS) or a systemd user unit (Linux)."""
    config_dir, state_dir = state(ctx).config_dir, state(ctx).state_dir
    _install_autostart(config_dir, state_dir, quiet=False)


@app.command("uninstall", epilog=example("daemon uninstall"))
def uninstall() -> None:
    """Remove autostart, however it was installed."""
    system = platform.system()
    if system == "Darwin":
        path = autostart.launchd_plist_path()
        if not path.is_file():
            console.print("No launchd autostart is installed.")
            return
        autostart.unregister_launchd(path)
        autostart.remove_launchd()
        console.print(f"Removed {path}")
        return
    if system == "Linux":
        path = autostart.systemd_unit_path()
        if not path.is_file():
            console.print("No systemd autostart is installed.")
            return
        autostart.unregister_systemd()
        autostart.remove_systemd()
        console.print(f"Removed {path}")
        return
    fail(f"autostart is not supported on {system}")


def _autostart_paths(config_dir: Path, state_dir: Path) -> autostart.AutostartPaths:
    """Only the directories the user overrode from the XDG defaults are passed as environment."""
    from mcpshape.paths import default_config_dir, default_state_dir  # noqa: PLC0415

    return autostart.AutostartPaths(
        log_dir=log_dir(state_dir),
        command=autostart.installed_command(),
        config_dir=config_dir if config_dir != default_config_dir() else None,
        state_dir=state_dir if state_dir != default_state_dir() else None,
    )


def _install_autostart(config_dir: Path, state_dir: Path, *, quiet: bool) -> None:
    system = platform.system()
    paths = _autostart_paths(config_dir, state_dir)
    if system == "Darwin":
        path = autostart.write_launchd(paths)
        try:
            autostart.register_launchd(path)
        except OSError as exc:
            console.print(f"[yellow]![/] wrote {path} but could not load it: {exc}")
        else:
            console.print(f"Installed and loaded the launchd agent at {path}")
        return
    if system == "Linux":
        path = autostart.write_systemd(paths)
        try:
            autostart.register_systemd()
        except OSError as exc:
            console.print(f"[yellow]![/] wrote {path} but could not enable it: {exc}")
        else:
            console.print(f"Installed and enabled the systemd unit at {path}, with linger.")
        return
    if not quiet:
        console.print(f"[dim]Autostart is not supported on {system}.[/dim]")


def _offer_install(config_dir: Path, state_dir: Path) -> None:
    """Offer autostart on a Daemon's first ``up``. Skippable, never blocks a non-interactive run."""
    system = platform.system()
    if system == "Darwin" and autostart.launchd_plist_path().is_file():
        return
    if system == "Linux" and autostart.systemd_unit_path().is_file():
        return
    if system not in ("Darwin", "Linux") or not sys.stdin.isatty():
        return
    if typer.confirm("Install autostart, so the Daemon starts automatically?", default=False):
        _install_autostart(config_dir, state_dir, quiet=True)


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
