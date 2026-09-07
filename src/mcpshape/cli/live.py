"""Live state from the Daemon, for the commands that show health.

Every CLI command works with the Daemon down (story 80), so nothing answering is a state to
report, not an error. This is the only place the CLI speaks HTTP: one loopback GET of
``/api/status`` at the address ``config.toml`` names, read back through the Daemon's own model.
"""

from __future__ import annotations

import urllib.request
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import ValidationError

from mcpshape.config import DaemonSettings, load_settings
from mcpshape.daemon import SHUTDOWN_PATH, STATUS_PATH, LiveState

if TYPE_CHECKING:
    from pathlib import Path

    from mcpshape.daemon import ProxyState, UpstreamState

TIMEOUT = 2.0
"""Seconds to wait for the Daemon. A Daemon that is up answers a status GET at once."""

UNKNOWN = "-"
"""What every live column says while nothing answers."""


@dataclass(frozen=True)
class Live:
    """What a running Daemon reports, or the fact that nothing answered."""

    url: str
    state: LiveState | None = None

    @property
    def running(self) -> bool:
        return self.state is not None

    def upstream(self, name: str) -> UpstreamState | None:
        if self.state is None:
            return None
        return next((found for found in self.state.upstreams if found.name == name), None)

    def proxy(self, upstream: str, proxy: str) -> ProxyState | None:
        found = self.upstream(upstream)
        if found is None:
            return None
        return next((state for state in found.proxies if state.name == proxy), None)

    def state_of(self, upstream: str) -> str:
        found = self.upstream(upstream)
        return found.state if found is not None else UNKNOWN

    def health_of(self, upstream: str, proxy: str) -> str:
        found = self.proxy(upstream, proxy)
        return found.health if found is not None else UNKNOWN


def _headers(daemon: DaemonSettings) -> dict[str, str]:
    """``Authorization`` when a bearer token is configured. Never logs it."""
    return {"Authorization": f"Bearer {daemon.token}"} if daemon.token else {}


def read_live(config_dir: Path) -> Live:
    """Ask the Daemon what everything is doing. A Daemon that is down is not an error."""
    daemon = load_settings(config_dir).daemon
    url = f"http://{daemon.host}:{daemon.port}"
    request = urllib.request.Request(f"{url}{STATUS_PATH}", headers=_headers(daemon))  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as answer:  # noqa: S310
            body: bytes = answer.read()
        return Live(url, LiveState.model_validate_json(body))
    except (OSError, ValidationError, ValueError):
        return Live(url)


def stop_daemon(config_dir: Path) -> bool:
    """Ask the Daemon to stop. ``False`` when nothing answered to ask."""
    daemon = load_settings(config_dir).daemon
    url = f"http://{daemon.host}:{daemon.port}"
    request = urllib.request.Request(  # noqa: S310
        f"{url}{SHUTDOWN_PATH}", method="POST", headers=_headers(daemon)
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT):  # noqa: S310
            pass
    except OSError:
        return False
    return True


def how_long(seconds: float) -> str:
    """``4s``, ``3m``, ``2h``: long enough to be useful, short enough for a table cell."""
    if seconds < 60:  # noqa: PLR2004  # the units are what this function is
        return f"{seconds:.0f}s"
    if seconds < 3600:  # noqa: PLR2004
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.0f}h"
