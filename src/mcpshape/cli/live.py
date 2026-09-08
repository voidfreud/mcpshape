"""Live state from the Daemon, for the commands that show health.

Every CLI command works with the Daemon down (story 80), so nothing answering is a state to
report, not an error. This is the only place the CLI speaks HTTP: a loopback GET of
``/api/status``, ``/api/logs``, or ``/api/calls``, or a POST to ``/api/reload``,
``/api/shutdown``, or ``/api/upstreams/<name>/connect``, at the address ``config.toml`` names,
read back through the Daemon's own models (``mcpshape.api``).
"""

from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel, ValidationError

from mcpshape.api import (
    CALLS_PATH,
    LOGS_PATH,
    RELOAD_PATH,
    SHUTDOWN_PATH,
    STATUS_PATH,
    UPSTREAMS_PATH,
    CallsAnswer,
    ConnectAnswer,
    LiveState,
    LogsAnswer,
)
from mcpshape.config import DaemonSettings, load_settings

if TYPE_CHECKING:
    from pathlib import Path

    from mcpshape.api import ProxyState, UpstreamState
    from mcpshape.calls import CallRecord

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
    return _live_from(config_dir, STATUS_PATH, "GET")


def reload_daemon(config_dir: Path) -> Live:
    """Ask the Daemon to re-read every Proxy's files now, and what came of it (#10).

    A Daemon that is down is not an error either: it reads every file when it starts.
    """
    return _live_from(config_dir, RELOAD_PATH, "POST")


def _live_from(config_dir: Path, path: str, method: str) -> Live:
    daemon = load_settings(config_dir).daemon
    return Live(_url(daemon), _read(daemon, path, method, LiveState))


def read_log_tail(config_dir: Path, lines: int) -> list[str] | None:
    """The last ``lines`` of the app log as the Daemon reads them, or nothing when it is down."""
    daemon = load_settings(config_dir).daemon
    answer = _read(daemon, _query(LOGS_PATH, lines=lines), "GET", LogsAnswer)
    return None if answer is None else answer.lines


def read_calls(config_dir: Path, limit: int) -> list[CallRecord] | None:
    """The latest ``limit`` calls from the Daemon's ring buffer, or nothing when it is down."""
    daemon = load_settings(config_dir).daemon
    answer = _read(daemon, _query(CALLS_PATH, limit=limit), "GET", CallsAnswer)
    return None if answer is None else answer.calls


def connect_upstream(config_dir: Path, name: str) -> bool:
    """Tell a running Daemon to connect ``name`` now instead of waiting out its backoff (#58).

    What a login from the CLI is followed by; ``False`` when no Daemon answered, which is
    not an error: a Daemon reads the stored login when it starts.
    """
    return connect_now(config_dir, name) is not None


def page_status(config_dir: Path) -> int | None:
    """What the Daemon answers a bare ``GET /`` with, or nothing when nothing answered:
    what ``ui`` asks before opening a browser, since the Daemon read ``dashboard`` at its
    start and a token is a header a browser does not send on its own (#17)."""
    daemon = load_settings(config_dir).daemon
    request = urllib.request.Request(f"{_url(daemon)}/", method="GET")  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as answer:  # noqa: S310
            return int(answer.status)
    except urllib.error.HTTPError as refused:
        return refused.code
    except OSError:
        return None


def connect_now(config_dir: Path, name: str) -> str | None:
    """``upstream connect``: the connection's state right after a running Daemon was told to
    try now, or nothing when no Daemon answered (#64)."""
    daemon = load_settings(config_dir).daemon
    answer = _read(daemon, f"{UPSTREAMS_PATH}/{name}/connect", "POST", ConnectAnswer)
    return None if answer is None else answer.state


def _query(path: str, **params: int) -> str:
    return f"{path}?{urllib.parse.urlencode(params)}"


def _url(daemon: DaemonSettings) -> str:
    return f"http://{daemon.host}:{daemon.port}"


def _read[M: BaseModel](daemon: DaemonSettings, path: str, method: str, model: type[M]) -> M | None:
    """What the Daemon answers at ``path``, as ``model``, or nothing when nothing answered."""
    request = urllib.request.Request(  # noqa: S310
        f"{_url(daemon)}{path}", method=method, headers=_headers(daemon)
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as answer:  # noqa: S310
            body: bytes = answer.read()
        return model.model_validate_json(body)
    except (OSError, ValidationError, ValueError):
        return None


def stop_daemon(config_dir: Path) -> bool:
    """Ask the Daemon to stop. ``False`` when nothing answered to ask."""
    daemon = load_settings(config_dir).daemon
    request = urllib.request.Request(  # noqa: S310
        f"{_url(daemon)}{SHUTDOWN_PATH}", method="POST", headers=_headers(daemon)
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
