"""``mcpshape doctor``: check every config file without starting anything."""

from __future__ import annotations

from typing import TYPE_CHECKING

import typer

from mcpshape import catalog, config
from mcpshape.cli.common import console, state
from mcpshape.names import InvalidNameError, check_name
from mcpshape.proxy import orphaned_overrides

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


def doctor(ctx: typer.Context) -> None:
    """Validate every config file against its schema and report what is wrong."""
    config_dir, state_dir = state(ctx).config_dir, state(ctx).state_dir
    files = config.all_files(config_dir)
    problems = name_problems(config_dir)
    for path, kind in files:
        problems.extend(config.check_file(path, kind))
    console.print(f"Checked {len(files)} file(s) in {config_dir}")
    for problem in problems:
        console.print(f"[red]✗[/] {problem}")
    if problems:
        raise typer.Exit(1)
    try:
        warnings = orphan_warnings(config_dir, state_dir)
    except catalog.CatalogError as exc:
        warnings = [config.Problem(state_dir, "", str(exc))]
    for warning in warnings:
        console.print(f"[yellow]![/] {warning}")
    console.print("[green]✓[/] All good")
