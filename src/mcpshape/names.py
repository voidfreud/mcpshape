"""Upstream and Proxy names: user-chosen slugs that appear in URLs."""

from __future__ import annotations

import re

SLUG = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
RESERVED = frozenset({"mcp", "api"})


class InvalidNameError(ValueError):
    """A name that cannot be an Upstream or Proxy name."""


def check_name(name: str, kind: str) -> str:
    """Return ``name`` if it is a valid ``kind`` name, else raise ``InvalidNameError``."""
    if name in RESERVED:
        msg = f"{name!r} is reserved; choose another {kind} name"
        raise InvalidNameError(msg)
    if not SLUG.match(name):
        msg = f"{name!r} is not a valid {kind} name: use lowercase letters, digits, and hyphens"
        raise InvalidNameError(msg)
    return name
