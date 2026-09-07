"""``mcpshape proxy``: new, ls, show, rm, install, export."""

from __future__ import annotations

import json
from pathlib import Path  # typer resolves annotations at runtime
from typing import TYPE_CHECKING, Annotated, Any, cast

import tomlkit
import typer
from rich.markup import escape
from tomlkit.exceptions import TOMLKitError

from mcpshape import catalog, config
from mcpshape.cli.common import (
    HELP_OPTIONS,
    client_profile,
    confirm_or_abort,
    console,
    example,
    fail,
    parse_proxy_ref,
    reporting_errors,
    state,
    unscanned_note,
)
from mcpshape.cli.listing import proxies_table, proxy_url, toml_file
from mcpshape.names import check_name
from mcpshape.profiles import entry_name
from mcpshape.proxy import expose

if TYPE_CHECKING:
    from mcpshape.profiles import Profile

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
            [config.load_upstream(config_dir, upstream)]
            if upstream
            else config.load_upstreams(config_dir)
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
        confirm_or_abort(f"Remove Proxy {upstream}/{proxy}?", yes=yes)
        config.remove_proxy(config_dir, upstream, proxy)
    console.print(f"Removed Proxy [bold]{upstream}/{proxy}[/bold]")


# --- install and export -------------------------------------------------------------------------

ToOpt = Annotated[
    str,
    typer.Option(
        "--to", metavar="CLIENT", show_default=False, help="Client to install into, by slug."
    ),
]
ConfigOpt = Annotated[
    Path | None,
    typer.Option(
        "--config",
        metavar="PATH",
        show_default=False,
        help="Write into this file instead of the Client's default location.",
    ),
]
DisableOpt = Annotated[
    str | None,
    typer.Option(
        "--disable",
        metavar="NAME",
        show_default=False,
        help="Also turn off the Client's own entry NAME, so the Upstream is not loaded twice.",
    ),
]
NameOpt = Annotated[
    str | None,
    typer.Option(
        "--name",
        metavar="NAME",
        show_default=False,
        help="Name the entry NAME instead of <upstream>, or <upstream>-<proxy>.",
    ),
]
ForOpt = Annotated[
    str | None,
    typer.Option(
        "--for",
        metavar="CLIENT",
        show_default=False,
        help="Shape the entry, and the section it sits in, the way this Client wants it.",
    ),
]


def plain(text: str) -> None:
    """Print text that may hold brackets or braces, with no Rich markup applied to it."""
    console.print(text, markup=False, highlight=False)


def budget_report(
    ctx: typer.Context, upstream: str, proxy: str, server: str, profile: Profile
) -> list[str]:
    """What the Client would make of the exposed names, read from the stored Catalog."""
    config_dir, state_dir = state(ctx).config_dir, state(ctx).state_dir
    stored = catalog.load_catalog(state_dir, upstream)
    if stored is None:
        return [unscanned_note(upstream, profile)]
    exposed = expose(stored, config.load_proxy(config_dir, upstream, proxy))
    return profile.name_violations(server, exposed.catalog.tools)


def json_snippet(container: tuple[str, ...], name: str, entry: dict[str, Any]) -> str:
    """``entry`` under ``name``, wrapped in the Client's key path, as strict JSON."""
    document: Any = {name: entry}
    for key in reversed(container):
        document = {key: document}
    return json.dumps(document, indent=2)


def yaml_snippet(profile: Profile, name: str, entry: dict[str, Any]) -> str:
    """The same entry as YAML. Every value is a JSON scalar, which YAML reads the same way."""
    fields = list(entry.items())
    if profile.entry_shape == "list":
        head, *rest = fields
        indent = "  " * len(profile.container)
        lines = [f"{'  ' * depth}{key}:" for depth, key in enumerate(profile.container)]
        lines.append(f"{indent}- {head[0]}: {json.dumps(head[1])}")
        lines += [f"{indent}  {key}: {json.dumps(value)}" for key, value in rest]
        return "\n".join(lines)
    lines = [f"{'  ' * depth}{key}:" for depth, key in enumerate((*profile.container, name))]
    indent = "  " * (len(profile.container) + 1)
    lines += [f"{indent}{key}: {json.dumps(value)}" for key, value in fields]
    return "\n".join(lines)


def read_json(path: Path) -> dict[str, Any]:
    """The Client's file as a dict, empty when it is not there yet. Comments are not JSON."""
    if not path.is_file() or not path.read_text().strip():
        return {}
    try:
        loaded: object = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"{path} is not strict JSON, so nothing was changed: {exc}")
    if not isinstance(loaded, dict):
        fail(f"{path} does not hold a JSON object, so nothing was changed")
    return cast("dict[str, Any]", loaded)


def read_toml(path: Path) -> tomlkit.TOMLDocument:
    if not path.is_file():
        return tomlkit.document()
    try:
        return tomlkit.parse(path.read_text())
    except (OSError, TOMLKitError) as exc:
        fail(f"{path} is not valid TOML, so nothing was changed: {exc}")


def servers_of(document: Any, container: tuple[str, ...], *, create: bool) -> Any:  # noqa: ANN401
    """Walk ``container`` down to the map of servers, making the tables on the way when asked."""
    node: Any = document
    for key in container:
        child: Any = node.get(key)
        if not isinstance(child, dict):
            if not create:
                return None
            node[key] = tomlkit.table() if isinstance(document, tomlkit.TOMLDocument) else {}
        node = node[key]
    return node


def write_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2) + "\n")


def install_entry(path: Path, profile: Profile, name: str, entry: dict[str, Any]) -> None:
    """Merge ``entry`` into the Client's file under its key path, leaving the rest alone."""
    if profile.file_format == "toml":
        document = read_toml(path)
        table = tomlkit.table()
        for key, value in entry.items():
            table[key] = value
        servers_of(document, profile.container, create=True)[name] = table
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(tomlkit.dumps(document))
        return
    loaded = read_json(path)
    servers_of(loaded, profile.container, create=True)[name] = entry
    write_json(path, loaded)


def disable_entry(path: Path, profile: Profile, name: str) -> dict[str, Any] | None:
    """Turn the Client's own entry ``name`` off, however that Client does it.

    Where the Client documents a per-server off switch the flag is set and the entry stays;
    where none is documented the entry is taken out and handed back so nothing is lost.
    """
    toml = profile.file_format == "toml"
    document: Any = read_toml(path) if toml else read_json(path)
    servers = servers_of(document, profile.container, create=False)
    if servers is None or name not in servers:
        return None
    if profile.disable_flag:
        servers[name][profile.disable_flag] = True
        taken: dict[str, Any] = {profile.disable_flag: True}
    else:
        taken = dict(servers[name])
        del servers[name]
    if toml:
        path.write_text(tomlkit.dumps(document))
    else:
        write_json(path, document)
    return taken


def target_file(profile: Profile, given: Path | None) -> Path:
    if given is not None:
        return given
    location = profile.default_location()
    if location is None:
        fail(
            f"the location of {profile.name}'s config file is not recorded; "
            f"say where to write with --config <path>"
        )
    return Path(location.path).expanduser()


@app.command("install", epilog=example("proxy install github/default --to claude-code"))
def install(  # noqa: PLR0913  # every one of these is a documented option of the command
    ctx: typer.Context,
    ref: RefArg,
    to: ToOpt,
    *,
    config_path: ConfigOpt = None,
    disable: DisableOpt = None,
    name: NameOpt = None,
) -> None:
    """Point a Client at a Proxy by writing the entry that Client expects."""
    config_dir = state(ctx).config_dir
    upstream, proxy = parse_proxy_ref(ref)
    profile = client_profile(to)
    if not profile.installable:
        fail(f"{profile.name} cannot reach a Proxy on this machine: {' '.join(profile.notes)}")
    with reporting_errors():
        config.load_proxy(config_dir, upstream, proxy)
        server = name or entry_name(upstream, proxy)
        warnings = budget_report(ctx, upstream, proxy, server, profile)
    entry = profile.entry(proxy_url(config_dir, upstream, proxy), f"{upstream}/{proxy}", server)
    for warning in warnings:
        console.print(f"[yellow]![/] {escape(warning)}")

    if not profile.writable:
        location = profile.default_location()
        where = f" of {location.path}" if location else ""
        console.print(
            f"mcpshape does not rewrite {profile.file_format.upper()} files, because it would "
            f"lose your comments. Paste this into the "
            f"[bold]{'.'.join(profile.container)}[/bold] section{where}:"
        )
        plain(yaml_snippet(profile, server, entry))
        return

    path = target_file(profile, config_path)
    install_entry(path, profile, server, entry)
    console.print(f"Installed [bold]{upstream}/{proxy}[/bold] into {profile.name} as {server}")
    plain(json_snippet(profile.container, server, entry))
    if disable:
        report_disabled(path, profile, disable)
    console.print(f"Wrote {path}")
    if not profile.refreshes_on_list_changed:
        console.print(f"{profile.name} needs a reconnect or a restart to see the Proxy's tools.")
    for key, what in profile.extras.items():
        console.print(f"{profile.name} also offers [bold]{key}[/bold]: {what}")


def report_disabled(path: Path, profile: Profile, disable: str) -> None:
    """Turn the Client's own entry off and say exactly what was done with it."""
    removed = disable_entry(path, profile, disable)
    if removed is None:
        console.print(f"[yellow]![/] No entry called {disable!r} in {path}; nothing was disabled")
        return
    if profile.disable_flag:
        console.print(
            f"Set [bold]{profile.disable_flag}[/bold] on the {profile.name} entry {disable!r}, "
            f"which is the off switch that Client documents. The entry itself is untouched."
        )
        return
    console.print(
        f"Took the {profile.name} entry {disable!r} out of the file: that Client documents no "
        f"per-server off switch, so removing it is the only way to stop it loading. "
        f"To put it back, paste this into {path}:"
    )
    plain(json_snippet(profile.container, disable, removed))


@app.command("export", epilog=example("proxy export github/default"))
def export(
    ctx: typer.Context, ref: RefArg, for_client: ForOpt = None, name: NameOpt = None
) -> None:
    """Print strict mcpServers JSON for a Proxy; with --for, the entry as that Client wants it."""
    config_dir = state(ctx).config_dir
    upstream, proxy = parse_proxy_ref(ref)
    with reporting_errors():
        config.load_proxy(config_dir, upstream, proxy)
    url = proxy_url(config_dir, upstream, proxy)
    server = name or entry_name(upstream, proxy)
    if not for_client:
        plain(json_snippet(("mcpServers",), server, {"type": "http", "url": url}))
        return
    profile = client_profile(for_client)
    entry = profile.entry(url, f"{upstream}/{proxy}", server)
    if profile.writable or profile.file_format == "json":
        plain(json_snippet(profile.container, server, entry))
    else:
        plain(yaml_snippet(profile, server, entry))
