"""The dashboard: static files the Daemon serves at ``/``, read-only, fed by ``/api`` (#17).

No build step, settled in the design session of 2026-09-08: the page is plain HTML, CSS, and
JavaScript in ``static/`` beside this file, shipped in the package as the JSON schemas are. It
polls ``/api/status`` and ``/api/calls`` on an interval, with a refresh button, and asks for a
Catalog, its Drift, or a Proxy's exposed set on request. It edits nothing; the CLI is where
changes are made.
"""

from __future__ import annotations

from pathlib import Path


def directory() -> Path:
    """Where the page's files live, and nothing else: what the Daemon mounts at ``/``."""
    return Path(__file__).parent / "static"
