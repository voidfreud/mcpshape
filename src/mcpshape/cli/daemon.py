"""``mcpshape daemon``: up. The rest of the verbs arrive with Daemon operations."""

from __future__ import annotations

import typer

from mcpshape import daemon
from mcpshape.cli.common import HELP_OPTIONS, console, example, reporting_errors, state
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
