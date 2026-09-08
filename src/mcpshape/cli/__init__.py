"""The mcpshape command line: a small first-order CLI with verbs nested under nouns."""

from __future__ import annotations

import webbrowser
from pathlib import Path  # noqa: TC003  # typer resolves annotations at runtime
from typing import Annotated

import typer

from mcpshape.cli import daemon, doctor, proxy, tool, upstream
from mcpshape.cli import serve as serving
from mcpshape.cli.common import HELP_OPTIONS, State, console, drift_notice, example, fail, state
from mcpshape.cli.live import page_status, read_live
from mcpshape.paths import CONFIG_DIR_ENV, STATE_DIR_ENV, default_config_dir, default_state_dir

app = typer.Typer(
    name="mcpshape",
    help="Reshape what MCP servers expose before a model sees it.",
    epilog=example("add github --stdio 'npx -y @modelcontextprotocol/server-github'"),
    context_settings=HELP_OPTIONS,
    no_args_is_help=True,
    rich_markup_mode="rich",
    pretty_exceptions_enable=False,
)


@app.callback()
def main(
    ctx: typer.Context,
    config_dir: Annotated[
        Path | None,
        typer.Option(
            "--config-dir",
            envvar=CONFIG_DIR_ENV,
            show_default=False,
            help="Config directory (default: $XDG_CONFIG_HOME/mcpshape or ~/.config/mcpshape).",
        ),
    ] = None,
    state_dir: Annotated[
        Path | None,
        typer.Option(
            "--state-dir",
            envvar=STATE_DIR_ENV,
            show_default=False,
            help="State directory (default: $XDG_STATE_HOME/mcpshape or ~/.local/state/mcpshape).",
        ),
    ] = None,
) -> None:
    ctx.obj = State(
        config_dir=config_dir or default_config_dir(),
        state_dir=state_dir or default_state_dir(),
    )
    ctx.call_on_close(lambda: drift_notice(ctx.obj.state_dir, ctx.obj.reviewed))


@app.command(
    "add", epilog=example("add github --stdio 'npx -y @modelcontextprotocol/server-github'")
)
def add(  # noqa: PLR0913  # one option per way of naming and authorizing an Upstream
    ctx: typer.Context,
    name: upstream.NameArg,
    stdio: upstream.StdioOpt = None,
    url: upstream.UrlOpt = None,
    *,
    sse: upstream.SseOpt = False,
    oauth: upstream.OAuthOpt = False,
    device: upstream.DeviceOpt = False,
    env: upstream.EnvOpt = None,
) -> None:
    """Add an Upstream and its default Proxy. Same as `upstream add`."""
    transport = upstream.transport_from_options(
        stdio, url, sse=sse, oauth=oauth, device=device, env=env
    )
    upstream.add_upstream(ctx, name, transport, device=device)


@app.command("ls", epilog=example("ls"))
def ls(ctx: typer.Context) -> None:
    """List every Upstream and Proxy. Same as `upstream ls`."""
    upstream.ls(ctx)


app.add_typer(upstream.app, name="upstream")
app.add_typer(proxy.app, name="proxy")
app.add_typer(tool.app, name="tool")
app.add_typer(daemon.app, name="daemon")
app.command("doctor", epilog=example("doctor"))(doctor.doctor)


@app.command("ui", epilog=example("ui"))
def ui(ctx: typer.Context) -> None:
    """Open the dashboard in a browser: the read-only page a running Daemon serves at /."""
    config_dir = state(ctx).config_dir
    live = read_live(config_dir)
    page = f"{live.url}/"
    if not live.running:
        console.print(
            "Daemon not running, so there is no dashboard to open. "
            "Start it with: mcpshape daemon up"
        )
        return
    answered = page_status(config_dir)
    if answered == NOT_FOUND:
        fail(
            "the dashboard is off: the Daemon started with [daemon] dashboard = false in "
            "config.toml; set it to true and restart the Daemon"
        )
    if answered == UNAUTHORIZED:
        console.print(
            f"The Daemon has a [daemon] token, and a browser does not send it on its own: "
            f"open {page} through your own routing, with the Authorization header."
        )
        return
    if webbrowser.open(page):
        console.print(f"Opened {page}")
        return
    console.print(f"No browser could be opened here; open {page} yourself.")


app.command("serve", hidden=True, epilog=serving.EPILOG)(serving.serve)

NOT_FOUND, UNAUTHORIZED = 404, 401

__all__ = ["app", "console"]
