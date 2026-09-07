"""``mcpshape doctor``: check every config file and exposed name, without starting anything."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated, Any, cast

import typer
from rich.markup import escape

from mcpshape import catalog, config
from mcpshape.cli.common import console, fail, state
from mcpshape.cli.proxy import entry_name
from mcpshape.names import InvalidNameError, check_name
from mcpshape.profiles import Profile, UnknownClientError, profile
from mcpshape.proxy import exposed_catalog, orphaned_overrides

if TYPE_CHECKING:
    from pathlib import Path


def name_problems(config_dir: Path) -> list[config.Problem]:
    problems: list[config.Problem] = []
    upstreams_dir = config_dir / config.UPSTREAMS_DIR
    if not upstreams_dir.is_dir():
        return problems
    for directory in sorted(upstreams_dir.iterdir()):
        if not (directory / config.UPSTREAM_FILE).is_file():
            continue
        try:
            check_name(directory.name, "Upstream")
        except InvalidNameError as exc:
            problems.append(config.Problem(directory, "", str(exc)))
        for proxy in config.list_proxies(config_dir, directory.name):
            try:
                check_name(proxy, "Proxy")
            except InvalidNameError as exc:
                problems.append(config.Problem(directory / f"{proxy}.toml", "", str(exc)))
    return problems


def orphan_warnings(config_dir: Path, state_dir: Path) -> list[config.Problem]:
    """Overrides whose Catalog item vanished: kept, but worth knowing about."""
    warnings: list[config.Problem] = []
    for upstream in config.load_upstreams(config_dir):
        stored = catalog.load_catalog(state_dir, upstream.name)
        if stored is None:
            continue
        for proxy in upstream.proxies:
            path = config.proxy_file(config_dir, upstream.name, proxy)
            proxy_file = config.load_proxy(config_dir, upstream.name, proxy)
            warnings.extend(
                config.Problem(
                    path,
                    f"{config.OVERRIDE_SECTION[item.kind]}.{item.name}",
                    f"orphaned Override: no {item} in the Catalog",
                )
                for item in orphaned_overrides(stored, proxy_file)
            )
    return warnings


def property_names(definition: dict[str, Any]) -> list[str]:
    """The input-schema property names of one raw MCP tool definition."""
    schema: object = definition.get("inputSchema")
    if not isinstance(schema, dict):
        return []
    properties: object = cast("dict[str, Any]", schema).get("properties")
    if not isinstance(properties, dict):
        return []
    return sorted(cast("dict[str, Any]", properties))


@dataclass
class Review:
    """One Client Profile's reading of what the Proxies expose.

    A name a Client rejects is a problem: it would refuse the whole request. A name a Client
    silently reshapes is a warning, because the model then sees a name nobody chose.
    """

    client: Profile
    problems: list[config.Problem] = field(default_factory=list[config.Problem])
    warnings: list[config.Problem] = field(default_factory=list[config.Problem])
    notes: list[str] = field(default_factory=list[str])

    def check_proxy(self, path: Path, server: str, exposed: catalog.Catalog) -> None:
        """Judge one Proxy's exposed tools by the Client's naming scheme and property rule."""
        scheme, properties = self.client.scheme, self.client.properties
        refused = self.problems if scheme.overflow == "reject" else self.warnings
        if broken := scheme.server_violation(server):
            refused.append(config.Problem(path, "", broken))
        for tool, definition in exposed.tools.items():
            if broken := scheme.violation(server, tool):
                refused.append(config.Problem(path, f"tools.{tool}", broken))
            if properties is None:
                continue
            self.problems.extend(
                config.Problem(path, f"tools.{tool}", broken)
                for name in property_names(definition)
                if (broken := properties.violation(name))
            )


def review(config_dir: Path, state_dir: Path, client: Profile) -> Review:
    """Every exposed name and property name a Client would refuse, or quietly reshape."""
    found = Review(client, notes=[f"{client.name}: {note}" for note in client.notes])
    if (caps := client.caps).source is not None:
        found.notes.append(
            f"{client.name}: the Caps this Profile recommends are {caps.tool_description} "
            f"characters for a tool description and {caps.instructions} for instructions "
            f"({caps.source}). Nothing applies them yet."
        )
    for upstream in config.load_upstreams(config_dir):
        stored = catalog.load_catalog(state_dir, upstream.name)
        if stored is None:
            found.notes.append(
                f"No stored Catalog for {upstream.name}, so no name was checked against "
                f"{client.name}. Run: mcpshape upstream sync {upstream.name}"
            )
            continue
        for proxy in upstream.proxies:
            found.check_proxy(
                config.proxy_file(config_dir, upstream.name, proxy),
                entry_name(upstream.name, proxy, None),
                exposed_catalog(stored, config.load_proxy(config_dir, upstream.name, proxy)),
            )
    return found


def client_profile(slug: str) -> Profile:
    try:
        return profile(slug)
    except UnknownClientError as exc:
        fail(str(exc))


ForOpt = Annotated[
    str | None,
    typer.Option(
        "--for",
        metavar="CLIENT",
        show_default=False,
        help="Also check every exposed name against this Client's Profile, by slug.",
    ),
]


def doctor(ctx: typer.Context, for_client: ForOpt = None) -> None:
    """Validate every config file against its schema and report what is wrong."""
    config_dir, state_dir = state(ctx).config_dir, state(ctx).state_dir
    files = config.all_files(config_dir)
    problems = name_problems(config_dir)
    for path, kind in files:
        problems.extend(config.check_file(path, kind))
    console.print(f"Checked {len(files)} file(s) in {config_dir}")
    for problem in problems:
        console.print(f"[red]✗[/] {escape(str(problem))}")
    if problems:
        raise typer.Exit(1)
    try:
        warnings = orphan_warnings(config_dir, state_dir)
    except catalog.CatalogError as exc:
        warnings = [config.Problem(state_dir, "", str(exc))]
    if for_client is not None:
        found = review(config_dir, state_dir, client_profile(for_client))
        problems, warnings = found.problems, warnings + found.warnings
        for note in found.notes:
            console.print(escape(note))
        for problem in problems:
            console.print(f"[red]✗[/] {escape(str(problem))}")
    for warning in warnings:
        console.print(f"[yellow]![/] {escape(str(warning))}")
    if problems:
        raise typer.Exit(1)
    console.print("[green]✓[/] All good")
