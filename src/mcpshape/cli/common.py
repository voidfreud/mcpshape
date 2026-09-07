"""What every CLI command shares: the config directory, output, and error reporting."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, NoReturn

import typer
from rich.console import Console

from mcpshape.config import ConfigError
from mcpshape.names import InvalidNameError

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

HELP_OPTIONS = {"help_option_names": ["-h", "--help"]}

console = Console(soft_wrap=True)
errors = Console(stderr=True, soft_wrap=True)


@dataclass(frozen=True)
class State:
    config_dir: Path


def state(ctx: typer.Context) -> State:
    return ctx.ensure_object(State)


def fail(message: str) -> NoReturn:
    """Report ``message`` in red and exit with code 1."""
    errors.print(f"[red]error:[/] {message}")
    raise typer.Exit(1)


@contextlib.contextmanager
def reporting_errors() -> Generator[None]:
    """Turn config and name errors into one red line and exit code 1."""
    try:
        yield
    except (ConfigError, InvalidNameError) as exc:
        fail(str(exc))


def confirm_or_abort(prompt: str, *, yes: bool) -> None:
    """Ask before a destructive step unless ``--yes`` was given."""
    if not yes and not typer.confirm(prompt):
        raise typer.Abort


def example(text: str) -> str:
    """The help epilog every command carries."""
    return f"Example: [bold]mcpshape {text}[/bold]"


def parse_proxy_ref(ref: str) -> tuple[str, str]:
    """``<upstream>/<proxy>`` into its parts."""
    upstream, sep, proxy = ref.partition("/")
    if not sep or not upstream or not proxy or "/" in proxy:
        fail(f"expected <upstream>/<proxy>, got {ref!r}")
    return upstream, proxy
