"""``mcpshape proxy``: new, ls, show, rm."""

from __future__ import annotations

from typing import Annotated

import typer

from mcpshape import config
from mcpshape.cli.common import (
    HELP_OPTIONS,
    console,
    example,
    fail,
    parse_proxy_ref,
    reporting_errors,
    state,
)
from mcpshape.cli.listing import all_upstreams, proxies_table, proxy_url, toml_file
from mcpshape.names import check_name

app = typer.Typer(
    help="Manage Proxies: the curated servers Clients connect to.",
    epilog=example("proxy ls"),
    context_settings=HELP_OPTIONS,
    no_args_is_help=True,
)

RefArg = Annotated[
    str, typer.Argument(metavar="UPSTREAM/PROXY", help="The Proxy, as <upstream>/<proxy>.")
]
YesOpt = Annotated[bool, typer.Option("-y", "--yes", help="Do not ask for confirmation.")]


@app.command("new", epilog=example("proxy new github/review"))
def new(ctx: typer.Context, ref: RefArg) -> None:
    """Create another Proxy of an Upstream."""
    config_dir = state(ctx).config_dir
    upstream, proxy = parse_proxy_ref(ref)
    with reporting_errors():
        path = config.add_proxy(config_dir, upstream, check_name(proxy, "Proxy"))
    url = proxy_url(config_dir, upstream, proxy)
    console.print(
        f"Created Proxy [bold]{upstream}/{proxy}[/bold] at {url}\nEdit {path} to curate it."
    )


@app.command("ls", epilog=example("proxy ls github"))
def ls(
    ctx: typer.Context,
    upstream: Annotated[str | None, typer.Argument(help="Only this Upstream's Proxies.")] = None,
) -> None:
    """List Proxies with their URLs."""
    config_dir = state(ctx).config_dir
    with reporting_errors():
        upstreams = (
            [config.load_upstream(config_dir, upstream)] if upstream else all_upstreams(config_dir)
        )
    if not upstreams:
        console.print("No Proxies yet. Add an Upstream with [bold]mcpshape add[/bold].")
        return
    console.print(proxies_table(config_dir, upstreams))


@app.command("show", epilog=example("proxy show github/default"))
def show(ctx: typer.Context, ref: RefArg) -> None:
    """Show a Proxy's file."""
    config_dir = state(ctx).config_dir
    upstream, proxy = parse_proxy_ref(ref)
    path = config.proxy_file(config_dir, upstream, proxy)
    if not path.is_file():
        fail(f"no Proxy {upstream}/{proxy}")
    console.print(f"[bold]{path}[/bold]  {proxy_url(config_dir, upstream, proxy)}")
    console.print(toml_file(path))


@app.command("rm", epilog=example("proxy rm github/review"))
def rm(ctx: typer.Context, ref: RefArg, *, yes: YesOpt = False) -> None:
    """Remove a Proxy. The default Proxy goes with its Upstream."""
    config_dir = state(ctx).config_dir
    upstream, proxy = parse_proxy_ref(ref)
    with reporting_errors():
        if not yes and not typer.confirm(f"Remove Proxy {upstream}/{proxy}?"):
            raise typer.Abort
        config.remove_proxy(config_dir, upstream, proxy)
    console.print(f"Removed Proxy [bold]{upstream}/{proxy}[/bold]")
