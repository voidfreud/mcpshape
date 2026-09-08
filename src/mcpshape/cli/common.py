"""What every CLI command shares: the config directory, output, and error reporting."""

from __future__ import annotations

import contextlib
import socket
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, NoReturn

import typer
from rich.console import Console

from mcpshape.catalog import CatalogError, pending_drift
from mcpshape.config import ConfigError
from mcpshape.model import CapError
from mcpshape.names import InvalidNameError
from mcpshape.profiles import Profile, UnknownClientError, profile
from mcpshape.proxy import OverrideError
from mcpshape.secrets import SecretError
from mcpshape.tokens import TokenError

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

HELP_OPTIONS = {"help_option_names": ["-h", "--help"]}

PROBE_TIMEOUT = 0.5
"""Seconds to wait for a raw TCP probe of whether something answers at an address."""

console = Console(soft_wrap=True)
errors = Console(stderr=True, soft_wrap=True)


@dataclass
class State:
    config_dir: Path
    state_dir: Path
    reviewed: set[str] = field(default_factory=set[str])
    """Upstreams whose Drift this command showed in full, so the closing notice skips them."""


def state(ctx: typer.Context) -> State:
    return ctx.ensure_object(State)


def drift_notice(state_dir: Path, reviewed: set[str]) -> None:
    """One line on stderr naming every Upstream with unreviewed Drift, or nothing."""
    drifts = {
        name: drift for name, drift in pending_drift(state_dir).items() if name not in reviewed
    }
    if not drifts:
        return
    where = ", ".join(f"{name} ({drift.summary()})" for name, drift in drifts.items())
    review = "mcpshape upstream sync " + (next(iter(drifts)) if len(drifts) == 1 else "<upstream>")
    errors.print(f"[yellow]Drift[/] in {where}. Review with: [bold]{review}[/bold]")


def fail(message: str) -> NoReturn:
    """Report ``message`` in red and exit with code 1."""
    errors.print(f"[red]error:[/] {message}")
    raise typer.Exit(1)


@contextlib.contextmanager
def reporting_errors() -> Generator[None]:
    """Turn config, Catalog, Override, Cap, name, secret, and token errors into one red line.

    Every one of these names a file, a key, or a variable and never a value, so the message is
    safe to print as it stands.
    """
    try:
        yield
    except (
        ConfigError,
        CatalogError,
        InvalidNameError,
        OverrideError,
        CapError,
        SecretError,
        TokenError,
    ) as exc:
        fail(str(exc))


def confirm_or_abort(prompt: str, *, yes: bool) -> None:
    """Ask before a destructive step unless ``--yes`` was given."""
    if not yes and not typer.confirm(prompt):
        raise typer.Abort


def client_profile(slug: str) -> Profile:
    """The Profile ``--to`` or ``--for`` names, or a red line listing the slugs that exist."""
    try:
        return profile(slug)
    except UnknownClientError as exc:
        fail(str(exc))


def unscanned_note(upstream: str, client: Profile) -> str:
    """Why no name of ``upstream`` was checked against ``client``, and what to run."""
    return (
        f"No stored Catalog for {upstream}, so no name was checked against {client.name}. "
        f"Run: mcpshape upstream sync {upstream}"
    )


def example(text: str) -> str:
    """The help epilog every command carries."""
    return f"Example: [bold]mcpshape {text}[/bold]"


def parse_proxy_ref(ref: str) -> tuple[str, str]:
    """``<upstream>/<proxy>`` into its parts."""
    upstream, sep, proxy = ref.partition("/")
    if not sep or not upstream or not proxy or "/" in proxy:
        fail(f"expected <upstream>/<proxy>, got {ref!r}")
    return upstream, proxy


def answering(host: str, port: int) -> bool:
    """Whether anything accepts a connection there right now."""
    try:
        with socket.create_connection((host, port), timeout=PROBE_TIMEOUT):
            return True
    except OSError:
        return False
