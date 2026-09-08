"""``mcpshape doctor``: check every config file and exposed name, without starting anything."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated

import typer
from rich.markup import escape

from mcpshape import autostart, catalog, config
from mcpshape.cli.common import client_profile, console, state, unscanned_note
from mcpshape.commands import command_missing
from mcpshape.hooks import UserCode, UserCodeError, load_user_code
from mcpshape.model import CapError, CapSettings, StdioTransport, given_kinds
from mcpshape.names import InvalidNameError, check_name
from mcpshape.profiles import Profile, entry_name
from mcpshape.proxy import (
    Exposed,
    OverrideError,
    cap,
    expose,
    orphaned_arguments,
    orphaned_hooks,
    orphaned_overrides,
)
from mcpshape.secrets import SecretError

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


def command_problems(config_dir: Path) -> list[config.Problem]:
    """Every stdio Upstream whose command nothing on this shell's PATH is (#48).

    ``doctor`` runs in the user's terminal, so it judges against that terminal's PATH and says
    so: under autostart the Daemon runs with the PATH ``daemon install`` captured, which is
    what ``daemon status`` reports against. Either way the Upstream cannot start, so it is a
    problem, not a warning. A command that is a reference nothing resolves is left to
    ``secret_problems``, which names the unset variable.
    """
    secrets = config.secrets_for(config_dir)
    path = os.environ.get("PATH")
    problems: list[config.Problem] = []
    for file, kind in config.all_files(config_dir):
        if kind != "upstream":
            continue
        try:
            upstream = config.load_upstream(config_dir, file.parent.name)
        except config.ConfigError:
            continue  # unreadable, which check_file reports on its own
        transport = upstream.transport
        if not isinstance(transport, StdioTransport):
            continue
        try:
            resolved = secrets.expanded(transport)
        except SecretError:
            continue  # an unresolved reference, which secret_problems reports
        if missing := command_missing(resolved, path):
            problems.append(
                config.Problem(
                    file,
                    "",
                    f"command {missing!r} is not found on this shell's PATH ({path}); under "
                    "autostart the Daemon uses the PATH written into its unit",
                )
            )
    return problems


CAPS_KEY = "[caps]"
"""The key a Cap finding is filed under, in the file it names."""

DEFAULT_LEVEL = "the default (config.toml [caps])"
"""Named when no level set a kind: the value in force is the built-in default, and the place
to set it is config.toml."""


@dataclass(frozen=True)
class ResolvedCaps:
    """A Proxy's Caps, resolved through global, Upstream, and Proxy, with the level that set
    each kind still on hand so ``doctor --for`` can name it."""

    values: CapSettings
    set_by: dict[str, str]
    """By kind, the level that set it and the file to edit; a kind missing here took the
    default."""

    def level(self, kind: str) -> str:
        return self.set_by.get(kind, DEFAULT_LEVEL)


@dataclass(frozen=True)
class Curation:
    """One Proxy file next to the stored Catalog it curates, and its Python file if any."""

    path: Path
    server: str
    """The name a Client would file the Proxy under."""
    catalog: catalog.Catalog
    proxy: config.ProxyFile
    code: UserCode
    resolved: ResolvedCaps
    """This Proxy's Caps, already resolved through global and Upstream, with provenance."""

    def expose(self) -> Exposed:
        return cap(expose(self.catalog, self.proxy, self.code), self.resolved.values, self.proxy)


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
    global_caps = config.load_settings(config_dir).caps
    global_set_by = dict.fromkeys(given_kinds(global_caps), "config.toml [caps]")
    for upstream in config.load_upstreams(config_dir):
        stored = catalog.load_catalog(state_dir, upstream.name)
        if stored is None:
            continue
        try:
            upstream_caps = upstream.caps.over(global_caps, f"Upstream {upstream.name}")
        except CapError as exc:
            where = config.upstream_dir(config_dir, upstream.name)
            found.append(config.Problem(where, "", str(exc)))
            continue
        upstream_file = f"{config.UPSTREAMS_DIR}/{upstream.name}/{config.UPSTREAM_FILE} [caps]"
        upstream_level = f"Upstream {upstream.name} ({upstream_file})"
        upstream_set_by = {
            **global_set_by,
            **dict.fromkeys(given_kinds(upstream.caps), upstream_level),
        }
        for proxy in upstream.proxies:
            code = load_code(config_dir, upstream.name, proxy)
            path = config.proxy_file(config_dir, upstream.name, proxy)
            if isinstance(code, config.Problem):
                found.append(code)
                continue
            proxy_file = config.load_proxy(config_dir, upstream.name, proxy)
            try:
                proxy_caps = proxy_file.caps.over(upstream_caps, f"Proxy {upstream.name}/{proxy}")
            except CapError as exc:
                found.append(config.Problem(path, "", str(exc)))
                continue
            proxy_file_label = f"{config.UPSTREAMS_DIR}/{upstream.name}/{proxy}.toml [caps]"
            proxy_level = f"Proxy {upstream.name}/{proxy} ({proxy_file_label})"
            proxy_set_by = {
                **upstream_set_by,
                **dict.fromkeys(given_kinds(proxy_file.caps), proxy_level),
            }
            found.append(
                Curation(
                    path,
                    entry_name(upstream.name, proxy),
                    stored,
                    proxy_file,
                    code,
                    ResolvedCaps(proxy_caps, proxy_set_by),
                )
            )
    return found


def override_problems(config_dir: Path, state_dir: Path) -> list[config.Problem]:
    """What would leave a Proxy unhealthy: Overrides or Caps that cannot be applied, user code
    that cannot be loaded, a Virtual Tool colliding with an exposed tool."""
    problems: list[config.Problem] = []
    for curation in curations(config_dir, state_dir):
        if isinstance(curation, config.Problem):
            problems.append(curation)
            continue
        try:
            curation.expose()
        except (OverrideError, CapError) as exc:
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

    def caps_over(self) -> bool:
        """Whether ``check_caps`` found a Proxy's Cap above what the Client cuts at."""
        return any(warning.key == CAPS_KEY for warning in self.warnings)

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

    def check_caps(self, path: Path, resolved: ResolvedCaps) -> None:
        """Warn about every Cap kind this Proxy resolves above what the Client cuts at.

        The Client does not refuse an over-long description or instructions, it silently
        reshapes them, so this is a warning, tied to the level that set the Cap in force. A
        tool's own Cap Override is not checked here: a Proxy resolved above the Profile's
        number is reported once, since every tool without its own Cap inherits it.
        """
        caps = self.client.caps
        if caps.source is None:
            return
        ceilings = {"tool_description": caps.tool_description, "instructions": caps.instructions}
        for kind, ceiling in ceilings.items():
            if ceiling is None:
                continue
            value = getattr(resolved.values, kind)
            if value <= ceiling:
                continue
            label = kind.replace("_", " ")
            self.warnings.append(
                config.Problem(
                    path,
                    CAPS_KEY,
                    f"the {label} Cap in force is {value}, set by {resolved.level(kind)}; "
                    f"{self.client.name} cuts at {ceiling} ({caps.source}). "
                    f"Set {kind} = {ceiling} or lower there.",
                )
            )


def review(config_dir: Path, state_dir: Path, client: Profile) -> Review:
    """Every exposed name and property name a Client would refuse, or quietly reshape, plus
    every Cap resolved above what the Client cuts at."""
    found = Review(client, notes=[f"{client.name}: {note}" for note in client.notes])
    found.notes += [
        unscanned_note(upstream.name, client)
        for upstream in config.load_upstreams(config_dir)
        if catalog.load_catalog(state_dir, upstream.name) is None
    ]
    for curation in curations(config_dir, state_dir):
        if isinstance(curation, config.Problem):
            continue  # reported as a problem already
        found.check_caps(curation.path, curation.resolved)
        try:
            exposed = curation.expose()
        except (OverrideError, CapError):
            continue  # reported as a problem already
        found.check_proxy(curation.path, curation.server, exposed)
    if (caps := client.caps).source is not None and not found.caps_over():
        found.notes.append(
            f"{client.name}: the Caps in force are within the {caps.tool_description} "
            f"characters for a tool description and {caps.instructions} for instructions "
            f"this Profile recommends ({caps.source})."
        )
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


def _print_problems(problems: list[config.Problem]) -> None:
    for problem in problems:
        console.print(f"[red]✗[/] {escape(str(problem))}")


def doctor(ctx: typer.Context, for_client: ForOpt = None) -> None:
    """Validate every config file against its schema and report what is wrong."""
    config_dir, state_dir = state(ctx).config_dir, state(ctx).state_dir
    files = config.all_files(config_dir)
    problems = name_problems(config_dir)
    for path, kind in files:
        problems.extend(config.check_file(path, kind))
    problems.extend(config.secret_problems(config_dir))
    problems.extend(command_problems(config_dir))
    problems.extend(
        config.Problem(unit, "", f"the installed unit runs {executable}, which no longer exists")
        for unit, executable in autostart.stale_units()
    )
    console.print(f"Checked {len(files)} file(s) in {config_dir}")
    _print_problems(problems)
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
    _print_problems(problems)
    for warning in warnings:
        console.print(f"[yellow]![/] {escape(str(warning))}")
    if problems:
        raise typer.Exit(1)
    console.print("[green]✓[/] All good")
