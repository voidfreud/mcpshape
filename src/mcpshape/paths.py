"""Where mcpshape keeps its files: the XDG layout on macOS and Linux alike."""

from __future__ import annotations

import os
from pathlib import Path

CONFIG_DIR_ENV = "MCPSHAPE_CONFIG_DIR"
STATE_DIR_ENV = "MCPSHAPE_STATE_DIR"


def default_config_dir() -> Path:
    """``$MCPSHAPE_CONFIG_DIR``, else ``$XDG_CONFIG_HOME/mcpshape``, else ``~/.config/mcpshape``."""
    return _xdg_dir(CONFIG_DIR_ENV, "XDG_CONFIG_HOME", Path.home() / ".config")


def default_state_dir() -> Path:
    """``$MCPSHAPE_STATE_DIR``, else ``$XDG_STATE_HOME/mcpshape``, else ``~/.local/state/mcpshape``.

    Catalogs, Drift, tokens, and logs live here: what mcpshape learned, as opposed to what the
    user wrote.
    """
    return _xdg_dir(STATE_DIR_ENV, "XDG_STATE_HOME", Path.home() / ".local" / "state")


def _xdg_dir(override_env: str, xdg_env: str, fallback: Path) -> Path:
    if override := os.environ.get(override_env):
        return Path(override).expanduser()
    xdg = os.environ.get(xdg_env)
    base = Path(xdg).expanduser() if xdg else fallback
    return base / "mcpshape"


def log_dir(state_dir: Path) -> Path:
    """Where the Daemon's app log lives."""
    return state_dir / "log"


def daemon_log_file(state_dir: Path) -> Path:
    return log_dir(state_dir) / "daemon.log"


def daemon_lock_file(state_dir: Path) -> Path:
    """Held for the life of a running Daemon, so a second start sees the first (#13)."""
    return state_dir / "daemon.lock"


def daemon_pid_file(state_dir: Path) -> Path:
    return state_dir / "daemon.pid"
