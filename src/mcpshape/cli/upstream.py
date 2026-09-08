"""``mcpshape upstream``: add, ls, show, sync, rm, scan."""

from __future__ import annotations

import asyncio
import shlex
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Literal

import typer

from mcpshape import catalog, config
from mcpshape import tokens as token_store
from mcpshape.adapters.fastmcp import login, login_message, scan
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
OAuthOpt = Annotated[
    bool,
    typer.Option("--oauth", help="With --url: log in with the server's OAuth provider now."),
]
DeviceOpt = Annotated[
    bool,
    typer.Option(
        "--device",
        help="With OAuth: pair by device code instead of a browser, for a headless machine.",
    ),
]
YesOpt = Annotated[bool, typer.Option("-y", "--yes", help="Do not ask for confirmation.")]
AcceptOpt = Annotated[
    bool, typer.Option("--accept", help="Make what the Upstream advertises now the Catalog.")
]


def transport_from_options(
    stdio: str | None, url: str | None, *, sse: bool, oauth: bool = False, device: bool = False
) -> Transport:
    if (stdio is None) == (url is None):
        fail("give exactly one of --stdio or --url")
    if device and not oauth:
        fail("--device only applies to --oauth")
    if stdio is not None:
        if sse:
            fail("--sse only applies to --url")
        if oauth:
            fail("--oauth only applies to --url")
        command, *args = shlex.split(stdio)
        if not command:
            fail("--stdio needs a command")
        return StdioTransport(transport="stdio", command=command, args=args)
    assert url is not None  # noqa: S101  # the check above guarantees it
    auth: Literal["oauth"] | None = "oauth" if oauth else None
    if sse:
        return SseTransport(transport="sse", url=url, auth=auth)
    return HttpTransport(transport="http", url=url, auth=auth)


def add_upstream(
    ctx: typer.Context, name: str, transport: Transport, *, device: bool = False
) -> None:
    config_dir = state(ctx).config_dir
    with reporting_errors():
        upstream = config.add_upstream(config_dir, check_name(name, "Upstream"), transport)
    console.print(
        f"Added Upstream [bold]{upstream.name}[/bold] with its default Proxy at "
        f"{proxy_url(config_dir, upstream.name, 'default')}"
    )
    if oauth_upstream(upstream.transport):
        log_in(config_dir, state(ctx).state_dir, upstream, device=device)


def oauth_upstream(transport: Transport) -> bool:
    """Whether this Upstream is one the user logs in to with an OAuth provider."""
    return isinstance(transport, HttpTransport | SseTransport) and transport.auth == "oauth"


def log_in(config_dir: Path, state_dir: Path, upstream: Upstream, *, device: bool) -> None:
    """Run the login the Upstream needs, printing what the user must do, never a token."""
    try:
        asyncio.run(
            login(
                upstream.transport,
                config.secrets_for(config_dir),
                token_store.Tokens(state_dir, upstream.name),
                console.print,
                device=device,
            )
        )
    except Exception as exc:  # noqa: BLE001  # however the provider refused, the user gets the why
        fail(f"cannot log in to {upstream.name}: {exc}")


@app.command(
    "add",
    epilog=example("upstream add github --stdio 'npx -y @modelcontextprotocol/server-github'"),
)
def add(  # noqa: PLR0913  # one option per way of naming and authorizing an Upstream
    ctx: typer.Context,
    name: NameArg,
    stdio: StdioOpt = None,
    url: UrlOpt = None,
    *,
    sse: SseOpt = False,
    oauth: OAuthOpt = False,
    device: DeviceOpt = False,
) -> None:
    """Add an Upstream and its default Proxy."""
    transport = transport_from_options(stdio, url, sse=sse, oauth=oauth, device=device)
    add_upstream(ctx, name, transport, device=device)


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
    console.print(login_line(state(ctx).state_dir, upstream))


def login_line(state_dir: Path, upstream: Upstream) -> str:
    """How the Upstream is authorized, and whether a login is stored. Never a value."""
    if not oauth_upstream(upstream.transport):
        return "Auth: none"
    if token_store.Tokens(state_dir, upstream.name).stored():
        return "Auth: OAuth, logged in (the token is encrypted in the state directory)"
    return (
        f"Auth: OAuth, not logged in. Log in with: "
        f"[bold]mcpshape upstream sync {upstream.name}[/bold]"
    )


@app.command("sync", epilog=example("upstream sync github --accept"))
def sync(
    ctx: typer.Context,
    name: Annotated[
        str | None, typer.Argument(help="The Upstream to scan; every Upstream when omitted.")
    ] = None,
    *,
    accept: AcceptOpt = False,
    device: DeviceOpt = False,
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
            sync_one(config_dir, state_dir, upstream, accept=accept, device=device)
            state(ctx).reviewed.add(upstream.name)


def scanned(
    config_dir: Path, state_dir: Path, upstream: Upstream, *, device: bool
) -> catalog.Catalog:
    """What the Upstream advertises now, logging it in first when that is what it is missing.

    An OAuth Upstream with no stored login is logged in before the scan; one whose stored
    login has stopped working is logged in again, since ``sync`` is the command every
    unavailable OAuth Upstream is told to run. Only that failure costs the stored login: an
    Upstream that cannot be reached for any other reason keeps a token that may still work.
    """
    tokens = token_store.Tokens(state_dir, upstream.name)
    if oauth_upstream(upstream.transport) and not tokens.stored():
        log_in(config_dir, state_dir, upstream, device=device)
    try:
        return _scan(config_dir, upstream, tokens)
    except Exception as exc:  # noqa: BLE001  # however the Upstream failed, the user gets the why
        if login_message(upstream.name) not in str(exc):
            fail(f"cannot scan {upstream.name}: {exc}")
        console.print(f"[yellow]![/] {upstream.name}: {exc}")
    tokens.forget()
    log_in(config_dir, state_dir, upstream, device=device)
    try:
        return _scan(config_dir, upstream, tokens)
    except Exception as exc:  # noqa: BLE001  # however the Upstream failed, the user gets the why
        fail(f"cannot scan {upstream.name}: {exc}")


def _scan(config_dir: Path, upstream: Upstream, tokens: token_store.Tokens) -> catalog.Catalog:
    return asyncio.run(scan(upstream.transport, config.secrets_for(config_dir), tokens))


def sync_one(
    config_dir: Path, state_dir: Path, upstream: Upstream, *, accept: bool, device: bool = False
) -> None:
    observed = scanned(config_dir, state_dir, upstream, device=device)
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
        token_store.forget(state_dir, name)
    console.print(f"Removed Upstream [bold]{name}[/bold]")


app.command("scan", epilog=scanning.EPILOG)(scanning.scan)
