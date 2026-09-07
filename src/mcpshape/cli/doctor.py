"""``mcpshape doctor``: check every config file and exposed name, without starting anything."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated

import typer
from rich.markup import escape

from mcpshape import catalog, config
from mcpshape.cli.common import client_profile, console, state, unscanned_note
from mcpshape.hooks import UserCode, UserCodeError, load_user_code
from mcpshape.names import InvalidNameError, check_name
from mcpshape.profiles import Profile, entry_name
from mcpshape.proxy import (
    Exposed,
    OverrideError,
    expose,
    orphaned_arguments,
    orphaned_hooks,
    orphaned_overrides,
)

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


@dataclass(frozen=True)
class Curation:
    """One Proxy file next to the stored Catalog it curates, and its Python file if any."""

    path: Path
    server: str
    """The name a Client would file the Proxy under."""
    catalog: catalog.Catalog
    proxy: config.ProxyFile
    code: UserCode

    def expose(self) -> Exposed:
        return expose(self.catalog, self.proxy, self.code)


def load_code(config_dir: Path, upstream: str, proxy: str) -> UserCode | config.Problem:
    """Load the Proxy's Python file without starting anything, or say why it cannot be."""
    path = config.proxy_code_file(config_dir, upstream, proxy)
    try:
        return load_user_code(path, f"{upstream}/{proxy}")
    except UserCodeError as exc:
        return config.Problem(path, "", str(exc))


def curations(config_dir: Path, state_dir: Path) -> list[Curation | config.Problem]:
    """Every Proxy file with its Upstream's stored Catalog; unscanned Upstreams are skipped."""
    found: list[Curation | config.Problem] = []
    for upstream in config.load_upstreams(config_dir):
        stored = catalog.load_catalog(state_dir, upstream.name)
        if stored is None:
            continue
        for proxy in upstream.proxies:
            code = load_code(config_dir, upstream.name, proxy)
            if isinstance(code, config.Problem):
                found.append(code)
                continue
            found.append(
                Curation(
                    config.proxy_file(config_dir, upstream.name, proxy),
                    entry_name(upstream.name, proxy),
                    stored,
                    config.load_proxy(config_dir, upstream.name, proxy),
                    code,
                )
            )
    return found


def override_problems(config_dir: Path, state_dir: Path) -> list[config.Problem]:
    """What would leave a Proxy unhealthy: Overrides that cannot be applied, user code that
    cannot be loaded, a Virtual Tool colliding with an exposed tool."""
    problems: list[config.Problem] = []
    for curation in curations(config_dir, state_dir):
        if isinstance(curation, config.Problem):
            problems.append(curation)
            continue
        try:
            curation.expose()
        except OverrideError as exc:
            problems.append(config.Problem(curation.path, "", str(exc)))
    return problems


def orphan_warnings(config_dir: Path, state_dir: Path) -> list[config.Problem]:
    """Overrides whose Catalog item or argument vanished: kept, but worth knowing about."""
    warnings: list[config.Problem] = []
    for curation in curations(config_dir, state_dir):
        if isinstance(curation, config.Problem):
            continue
        warnings.extend(
            config.Problem(
                curation.path,
                f"{config.OVERRIDE_SECTION[item.kind]}.{item.name}",
                f"orphaned Override: no {item} in the Catalog",
            )
            for item in orphaned_overrides(curation.catalog, curation.proxy)
        )
        warnings.extend(
            config.Problem(
                curation.path,
                f"tools.{tool}.args.{argument}",
                f"orphaned argument Override: tool {tool} has no argument {argument!r}",
            )
            for tool, argument in orphaned_arguments(curation.catalog, curation.proxy)
        )
        warnings.extend(
            config.Problem(
                curation.path.with_suffix(".py"),
                "",
                f"orphaned Hook: no {item} in the Catalog, so it never runs",
            )
            for item in orphaned_hooks(curation.catalog, curation.code)
        )
    return warnings


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

    def check_proxy(self, path: Path, server: str, exposed: Exposed) -> None:
        """Judge one Proxy's exposed tools by the Client's naming scheme and property rule."""
        refused = self.problems if self.client.scheme.overflow == "reject" else self.warnings
        refused.extend(
            config.Problem(path, "", broken)
            for broken in self.client.name_violations(server, exposed.tool_names())
        )
        if (properties := self.client.properties) is None:
            return
        for tool, definition in exposed.catalog.tools.items():
            self.problems.extend(
                config.Problem(path, f"tools.{tool}", broken)
                for name in sorted(catalog.arguments(definition))
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
    found.notes += [
        unscanned_note(upstream.name, client)
        for upstream in config.load_upstreams(config_dir)
        if catalog.load_catalog(state_dir, upstream.name) is None
    ]
    for curation in curations(config_dir, state_dir):
        if isinstance(curation, config.Problem):
            continue  # reported as a problem already
        try:
            exposed = curation.expose()
        except OverrideError:
            continue  # reported as a problem already
        found.check_proxy(curation.path, curation.server, exposed)
    return found


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
        problems = override_problems(config_dir, state_dir)
        warnings = orphan_warnings(config_dir, state_dir)
    except catalog.CatalogError as exc:
        problems, warnings = [], [config.Problem(state_dir, "", str(exc))]
    if for_client is not None:
        found = review(config_dir, state_dir, client_profile(for_client))
        problems, warnings = problems + found.problems, warnings + found.warnings
        for note in found.notes:
            console.print(escape(note))
    for problem in problems:
        console.print(f"[red]✗[/] {escape(str(problem))}")
    for warning in warnings:
        console.print(f"[yellow]![/] {escape(str(warning))}")
    if problems:
        raise typer.Exit(1)
    console.print("[green]✓[/] All good")
