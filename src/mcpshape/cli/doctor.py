"""``mcpshape doctor``: check every config file without starting anything."""

from __future__ import annotations

from typing import TYPE_CHECKING

import typer

from mcpshape import config
from mcpshape.cli.common import console, state
from mcpshape.names import InvalidNameError, check_name

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


def doctor(ctx: typer.Context) -> None:
    """Validate every config file against its schema and report what is wrong."""
    config_dir = state(ctx).config_dir
    files = config.all_files(config_dir)
    problems = name_problems(config_dir)
    for path, kind in files:
        problems.extend(config.check_file(path, kind))
    console.print(f"Checked {len(files)} file(s) in {config_dir}")
    for problem in problems:
        console.print(f"[red]✗[/] {problem}")
    if problems:
        raise typer.Exit(1)
    console.print("[green]✓[/] All good")
