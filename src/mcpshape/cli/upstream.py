"""``mcpshape upstream``: add, ls, show, rm."""

from __future__ import annotations

import shlex
from typing import Annotated

import typer

from mcpshape import config
from mcpshape.cli.common import (
    HELP_OPTIONS,
    confirm_or_abort,
    console,
    example,
    fail,
    reporting_errors,
    state,
)
from mcpshape.cli.listing import proxy_url, toml_file, upstreams_table
from mcpshape.model import HttpTransport, SseTransport, StdioTransport, Transport
from mcpshape.names import check_name

app = typer.Typer(
    help="Manage Upstreams: the MCP servers mcpshape sits in front of.",
    epilog=example("upstream ls"),
    context_settings=HELP_OPTIONS,
    no_args_is_help=True,
)

NameArg = Annotated[
    str, typer.Argument(help="The Upstream's name: a slug that appears in its URLs.")
]
StdioOpt = Annotated[
    str | None,
    typer.Option("--stdio", metavar="COMMAND", help="Spawn this command and speak stdio to it."),
]
UrlOpt = Annotated[
    str | None, typer.Option("--url", metavar="URL", help="Connect to this Streamable HTTP server.")
]
SseOpt = Annotated[bool, typer.Option("--sse", help="With --url: the server speaks legacy SSE.")]
YesOpt = Annotated[bool, typer.Option("-y", "--yes", help="Do not ask for confirmation.")]


def transport_from_options(stdio: str | None, url: str | None, *, sse: bool) -> Transport:
    if (stdio is None) == (url is None):
        fail("give exactly one of --stdio or --url")
    if stdio is not None:
        if sse:
            fail("--sse only applies to --url")
        command, *args = shlex.split(stdio)
        if not command:
            fail("--stdio needs a command")
        return StdioTransport(transport="stdio", command=command, args=args)
    assert url is not None  # noqa: S101  # the check above guarantees it
    if sse:
        return SseTransport(transport="sse", url=url)
    return HttpTransport(transport="http", url=url)


def add_upstream(ctx: typer.Context, name: str, transport: Transport) -> None:
    config_dir = state(ctx).config_dir
    with reporting_errors():
        upstream = config.add_upstream(config_dir, check_name(name, "Upstream"), transport)
    console.print(
        f"Added Upstream [bold]{upstream.name}[/bold] with its default Proxy at "
        f"{proxy_url(config_dir, upstream.name, 'default')}"
    )


@app.command(
    "add",
    epilog=example("upstream add github --stdio 'npx -y @modelcontextprotocol/server-github'"),
)
def add(
    ctx: typer.Context,
    name: NameArg,
    stdio: StdioOpt = None,
    url: UrlOpt = None,
    *,
    sse: SseOpt = False,
) -> None:
    """Add an Upstream and its default Proxy."""
    add_upstream(ctx, name, transport_from_options(stdio, url, sse=sse))


@app.command("ls", epilog=example("upstream ls"))
def ls(ctx: typer.Context) -> None:
    """List every Upstream with its Proxies."""
    config_dir = state(ctx).config_dir
    with reporting_errors():
        upstreams = config.load_upstreams(config_dir)
    if not upstreams:
        console.print("No Upstreams yet. Add one with [bold]mcpshape add[/bold].")
        return
    console.print(upstreams_table(config_dir, upstreams))


@app.command("show", epilog=example("upstream show github"))
def show(ctx: typer.Context, name: NameArg) -> None:
    """Show an Upstream's file and its Proxies."""
    config_dir = state(ctx).config_dir
    with reporting_errors():
        upstream = config.load_upstream(config_dir, name)
    path = config.upstream_dir(config_dir, name) / config.UPSTREAM_FILE
    console.print(f"[bold]{path}[/bold]")
    console.print(toml_file(path))
    console.print(upstreams_table(config_dir, [upstream]))


@app.command("rm", epilog=example("upstream rm github"))
def rm(ctx: typer.Context, name: NameArg, *, yes: YesOpt = False) -> None:
    """Remove an Upstream and every one of its Proxies."""
    config_dir = state(ctx).config_dir
    with reporting_errors():
        upstream = config.load_upstream(config_dir, name)
        proxies = ", ".join(upstream.proxies)
        confirm_or_abort(f"Remove Upstream {name} and its Proxies ({proxies})?", yes=yes)
        config.remove_upstream(config_dir, name)
    console.print(f"Removed Upstream [bold]{name}[/bold]")
