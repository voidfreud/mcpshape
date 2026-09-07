"""Where mcpshape keeps its files: the XDG layout on macOS and Linux alike."""

from __future__ import annotations

import os
from pathlib import Path

CONFIG_DIR_ENV = "MCPSHAPE_CONFIG_DIR"


def default_config_dir() -> Path:
    """``$MCPSHAPE_CONFIG_DIR``, else ``$XDG_CONFIG_HOME/mcpshape``, else ``~/.config/mcpshape``."""
    if override := os.environ.get(CONFIG_DIR_ENV):
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "mcpshape"
