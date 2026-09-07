"""``mcpshape upstream``: add, ls, show, sync, rm, scan."""

from __future__ import annotations

import asyncio
import shlex
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated

import typer

from mcpshape import catalog, config
from mcpshape.adapters.fastmcp import UpstreamTargetError, scan
from mcpshape.cli import scan as scanning
from mcpshape.cli.common import (
    HELP_OPTIONS,
    confirm_or_abort,
    console,
    example,
    fail,
    reporting_errors,
    state,
)
from mcpshape.cli.listing import print_upstreams, proxy_url, toml_file
from mcpshape.cli.live import read_live
from mcpshape.model import HttpTransport, SseTransport, StdioTransport, Transport, Upstream
from mcpshape.names import check_name
from mcpshape.profiles import clients_needing_reconnect
from mcpshape.proxy import orphaned_overrides

if TYPE_CHECKING:
    from pathlib import Path

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
AcceptOpt = Annotated[
    bool, typer.Option("--accept", help="Make what the Upstream advertises now the Catalog.")
]


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
    print_upstreams(config_dir, upstreams, read_live(config_dir))


@app.command("show", epilog=example("upstream show github"))
def show(ctx: typer.Context, name: NameArg) -> None:
    """Show an Upstream's file and its Proxies."""
    config_dir = state(ctx).config_dir
    with reporting_errors():
        upstream = config.load_upstream(config_dir, name)
    path = config.upstream_dir(config_dir, name) / config.UPSTREAM_FILE
    console.print(f"[bold]{path}[/bold]")
    console.print(toml_file(path))
    print_upstreams(config_dir, [upstream], read_live(config_dir))


@app.command("sync", epilog=example("upstream sync github --accept"))
def sync(
    ctx: typer.Context,
    name: Annotated[
        str | None, typer.Argument(help="The Upstream to scan; every Upstream when omitted.")
    ] = None,
    *,
    accept: AcceptOpt = False,
) -> None:
    """Scan an Upstream into its Catalog, show the Drift since, and accept it on request."""
    config_dir, state_dir = state(ctx).config_dir, state(ctx).state_dir
    with reporting_errors():
        upstreams = (
            [config.load_upstream(config_dir, name)] if name else config.load_upstreams(config_dir)
        )
        if not upstreams:
            console.print("No Upstreams yet. Add one with [bold]mcpshape add[/bold].")
            return
        for upstream in upstreams:
            sync_one(config_dir, state_dir, upstream, accept=accept)
            state(ctx).reviewed.add(upstream.name)


def sync_one(config_dir: Path, state_dir: Path, upstream: Upstream, *, accept: bool) -> None:
    try:
        observed = asyncio.run(scan(upstream.transport))
    except UpstreamTargetError as exc:
        fail(f"cannot scan {upstream.name}: {exc}")
    result = (
        catalog.accept_scan(state_dir, upstream.name, observed)
        if accept
        else catalog.record_scan(state_dir, upstream.name, observed)
    )
    label = f"[bold]{upstream.name}[/bold]"
    if result.first:
        console.print(f"Scanned {label}: {counts(result.catalog)}")
        return
    if not result.drift:
        console.print(f"No Drift in {label}: {counts(result.catalog)}")
        if accept:
            console.print("Nothing to accept.")
        return
    if not accept:
        console.print(f"Drift in {label} since {when(result.catalog)}:")
        print_drift(result.drift)
        console.print(f"Accept with: [bold]mcpshape upstream sync {upstream.name} --accept[/bold]")
        return
    console.print(f"Accepted Drift in {label}:")
    print_drift(result.drift)
    apply_drift_default(config_dir, upstream, result.drift.added)
    report_orphans(config_dir, upstream, result.catalog)
    console.print(
        "Clients connected to its Proxies see the change on their next request. "
        f"These Clients need a reconnect to notice: {', '.join(clients_needing_reconnect())}."
    )


def apply_drift_default(
    config_dir: Path, upstream: Upstream, added: tuple[catalog.Item, ...]
) -> None:
    """Hide ``added`` in every Proxy of ``upstream`` unless the global setting says visible."""
    if not added:
        return
    names = ", ".join(str(item) for item in added)
    if config.load_settings(config_dir).drift.new_items == "visible":
        console.print(
            f'  New items are visible in every Proxy (drift.new_items = "visible"): {names}'
        )
        return
    note = f"new in the Upstream since {datetime.now(UTC):%Y-%m-%d}; set to false to expose it"
    for proxy in upstream.proxies:
        config.hide_items(config.proxy_file(config_dir, upstream.name, proxy), added, note)
    console.print(f"  New items are hidden in every Proxy until you expose them: {names}")


def report_orphans(config_dir: Path, upstream: Upstream, accepted: catalog.Catalog) -> None:
    """Overrides whose item vanished are kept; say so, per Proxy."""
    for proxy in upstream.proxies:
        for item in orphaned_overrides(
            accepted, config.load_proxy(config_dir, upstream.name, proxy)
        ):
            console.print(
                f"  [yellow]![/] {upstream.name}/{proxy}: orphaned Override for {item}, kept"
            )


def print_drift(drift: catalog.Drift) -> None:
    for sign, items in drift.by_sign():
        for item in items:
            console.print(f"  {sign} {item}")
    if drift.instructions_changed:
        console.print("  ~ instructions")


def counts(stored: catalog.Catalog) -> str:
    parts = [
        (len(stored.tools), "tool"),
        (len(stored.resources) + len(stored.resource_templates), "resource"),
        (len(stored.prompts), "prompt"),
    ]
    return ", ".join(f"{count} {noun}{'' if count == 1 else 's'}" for count, noun in parts)


def when(stored: catalog.Catalog) -> str:
    return f"{stored.scanned_at.astimezone():%Y-%m-%d %H:%M}"


@app.command("rm", epilog=example("upstream rm github"))
def rm(ctx: typer.Context, name: NameArg, *, yes: YesOpt = False) -> None:
    """Remove an Upstream, every one of its Proxies, and its Catalog."""
    config_dir, state_dir = state(ctx).config_dir, state(ctx).state_dir
    with reporting_errors():
        upstream = config.load_upstream(config_dir, name)
        proxies = ", ".join(upstream.proxies)
        confirm_or_abort(f"Remove Upstream {name} and its Proxies ({proxies})?", yes=yes)
        config.remove_upstream(config_dir, name)
        catalog.forget(state_dir, name)
    console.print(f"Removed Upstream [bold]{name}[/bold]")


app.command("scan", epilog=scanning.EPILOG)(scanning.scan)
