"""``mcpshape upstream scan``: the MCP servers already configured on this machine.

Reads, never writes, the Client config files. What it finds it offers, one server at a time;
nothing is added without the user saying so, or saying ``--yes`` once for all of them.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path  # typer resolves annotations at runtime
from typing import TYPE_CHECKING, Annotated

import typer
from rich.markup import escape
from rich.table import Table

from mcpshape import config, discovery
from mcpshape.cli.common import console, example, reporting_errors, state
from mcpshape.cli.listing import describe_transport, proxy_url
from mcpshape.names import InvalidNameError, check_name

if TYPE_CHECKING:
    from mcpshape.discovery import Found

DirectoriesArg = Annotated[
    list[Path] | None,
    typer.Argument(metavar="[DIR]...", show_default=False, help="Also look in these directories."),
]
YesOpt = Annotated[
    bool, typer.Option("-y", "--yes", help="Add every server found without asking about each.")
]
ListOpt = Annotated[
    bool, typer.Option("--list", help="Only list what was found; add nothing and ask nothing.")
]

EPILOG = example("upstream scan ~/projects/api")


@dataclass(frozen=True)
class Candidate:
    """A found server that could become an Upstream, under the name it would get."""

    found: Found
    name: str

    @property
    def renamed(self) -> bool:
        return self.name != self.found.name


def scan(
    ctx: typer.Context,
    directories: DirectoriesArg = None,
    *,
    yes: YesOpt = False,
    list_only: ListOpt = False,
) -> None:
    """Find MCP servers in Client configs and offer to add them as Upstreams.

    Looks in every Client config location mcpshape knows, in the current and home
    directories, and in every DIR named. Asks about each server it can add, unless --yes says
    to add them all or --list says to add none.
    """
    config_dir = state(ctx).config_dir
    with reporting_errors():
        daemon = config.load_settings(config_dir).daemon
        taken = {upstream.name for upstream in config.load_upstreams(config_dir)}
    result = discovery.find(directories or [])
    console.print(f"Read {plural(len(result.files), 'Client config file')}.")
    for unread in result.unread:
        console.print(f"[yellow]![/] {unread.path} is {unread.reason}, so it was not read.")

    candidates, skipped = triage(result.found, taken, (daemon.host, daemon.port))
    for note in skipped:
        console.print(f"  [dim]-[/] {escape(note)}")
    if not candidates:
        console.print("No MCP server to add. Add one by hand with [bold]mcpshape add[/bold].")
        return
    console.print(table(candidates))
    for candidate in candidates:
        if candidate.found.env:
            console.print(
                f"[yellow]![/] {escape(candidate.found.name)} sets "
                f"{', '.join(candidate.found.env)} in its Client's file. Only the names are "
                f"carried over, as ${{VAR}} references: set each in the Daemon's environment "
                f"or in {config.SECRETS_FILE}."
            )
    if list_only:
        console.print("Nothing was added. Drop [bold]--list[/bold] to be asked about each.")
        return
    added = sum(offer(config_dir, candidate, yes=yes) for candidate in candidates)
    console.print(f"Added {plural(added, 'Upstream')}.")


def triage(
    found: tuple[Found, ...], taken: set[str], daemon: tuple[str, int]
) -> tuple[list[Candidate], list[str]]:
    """The servers worth offering, and one line each for the ones that were passed over."""
    candidates: list[Candidate] = []
    skipped: list[str] = []
    seen = set(taken)
    for server in found:
        name = discovery.slug_for(server.name)
        if discovery.is_own_proxy(server.transport, daemon):
            skipped.append(f"{server.name} in {server.path} is an mcpshape Proxy already")
        elif server.disabled:
            skipped.append(f"{server.name} in {server.path} is switched off there")
        elif name is None:
            skipped.append(f"{server.name!r} in {server.path} yields no valid Upstream name")
        elif name in seen:
            skipped.append(f"{server.name} in {server.path} is already the Upstream {name}")
        else:
            seen.add(name)
            candidates.append(Candidate(server, name))
    return candidates, skipped


def table(candidates: list[Candidate]) -> Table:
    rendered = Table(box=None, pad_edge=False)
    for column in ("Upstream", "Server", "Transport", "Client", "File"):
        rendered.add_column(column, overflow="fold")
    for candidate in candidates:
        found = candidate.found
        rendered.add_row(
            candidate.name,
            escape(found.name),
            escape(describe_transport(found.transport)),
            found.client or "",
            str(found.path),
        )
    return rendered


def offer(config_dir: Path, candidate: Candidate, *, yes: bool) -> bool:
    """Ask about ``candidate`` unless ``--yes``, add it, and say what was added."""
    named = f" as Upstream {candidate.name}" if candidate.renamed else ""
    if not yes and not typer.confirm(f"Add {candidate.found.name}{named}?", default=True):
        return False
    try:
        added = config.add_upstream(
            config_dir, check_name(candidate.name, "Upstream"), candidate.found.transport
        )
    except (config.ConfigError, InvalidNameError) as exc:
        console.print(f"[yellow]![/] {candidate.name} was not added: {escape(str(exc))}")
        return False
    origin = f" (named after {escape(candidate.found.name)})" if candidate.renamed else ""
    console.print(
        f"Added Upstream [bold]{added.name}[/bold]{origin} with its default Proxy at "
        f"{proxy_url(config_dir, added.name, 'default')}"
    )
    return True


def plural(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"
