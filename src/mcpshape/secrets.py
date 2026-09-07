"""``${VAR}`` references in an Upstream file, and where they are resolved from.

A reference is written ``${NAME}`` and may stand anywhere a string does in an Upstream file:
in a URL, in an argument of a command, in an ``env`` value. It is resolved from the Daemon's
own environment first, then from the secrets file in the config directory, which nobody but
its owner may read.

A resolved value never leaves this module: it goes into the child process or the request, and
never into a log line, an error message, or the terminal. What is named is the variable.

The lookup happens on every connect and every scan, not once at start-up, so a value edited
while the Daemon runs is picked up the next time the Upstream is reached.
"""

from __future__ import annotations

import os
import re
import stat
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from pydantic import BaseModel

SECRETS_FILE = "secrets.toml"
"""The file in the config directory that holds what the environment does not."""

REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
"""One ``${NAME}`` reference. Anything else keeping a dollar sign is left alone."""

OWNER_ONLY = 0o600
"""The mode the secrets file must have: nothing for the group, nothing for anyone else."""


class SecretError(Exception):
    """A reference cannot be resolved, or the secrets file cannot be read as it stands.

    Its message names variables and paths only, never a value.
    """


class Secrets:
    """Where ``${VAR}`` is looked up: the Daemon environment first, then the secrets file."""

    def __init__(self, stored: Callable[[], Mapping[str, str]] | None = None) -> None:
        self._stored = stored

    def expanded[T: BaseModel](self, model: T) -> T:
        """``model`` with every reference in it resolved, or ``SecretError`` naming what is not."""
        return type(model).model_validate(self.expand(model.model_dump()))

    def expand(self, data: Any) -> Any:  # noqa: ANN401  # a config file's contents are untyped
        """``data`` with every reference in every string resolved, however deeply it sits."""
        values = self._values()
        if unset := _missing(data, values):
            raise SecretError(unset_message(unset))
        return _substituted(data, values)

    def missing(self, data: Any) -> list[str]:  # noqa: ANN401  # a config file's contents are untyped
        """Every variable ``data`` refers to that neither the environment nor the file answers."""
        return _missing(data, self._values())

    def _values(self) -> dict[str, str]:
        stored = dict(self._stored()) if self._stored is not None else {}
        return {**stored, **os.environ}


def unset_message(names: list[str]) -> str:
    """Why a reference could not be resolved, in the variable's words and never its value."""
    is_are = "is" if len(names) == 1 else "are"
    return (
        f"{', '.join(names)} {is_are} not set in the environment "
        f"and not in {SECRETS_FILE}, so ${{{names[0]}}} cannot be resolved"
    )


def check_mode(path: Path) -> None:
    """Refuse a secrets file the group or anyone else can read, without reading it."""
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & ~OWNER_ONLY:
        msg = f"{path} is mode {mode:04o}; it must be {OWNER_ONLY:04o}, readable by you alone"
        raise SecretError(msg)


def _missing(data: Any, values: Mapping[str, str]) -> list[str]:  # noqa: ANN401  # untyped
    return sorted(_references(data) - values.keys())


def _references(data: Any) -> set[str]:  # noqa: ANN401  # a config file's contents are untyped
    """Every variable named anywhere in ``data``."""
    if isinstance(data, str):
        return set(REFERENCE.findall(data))
    if isinstance(data, dict):
        values: list[Any] = list(data.values())  # pyright: ignore[reportUnknownArgumentType]  # untyped
        return {name for value in values for name in _references(value)}
    if isinstance(data, list):
        items: list[Any] = data  # pyright: ignore[reportUnknownVariableType]  # untyped
        return {name for item in items for name in _references(item)}
    return set()


def _substituted(data: Any, values: Mapping[str, str]) -> Any:  # noqa: ANN401  # untyped
    if isinstance(data, str):
        return REFERENCE.sub(lambda found: values[found.group(1)], data)
    if isinstance(data, dict):
        pairs: list[tuple[Any, Any]] = list(data.items())  # pyright: ignore[reportUnknownArgumentType]  # untyped
        return {key: _substituted(value, values) for key, value in pairs}
    if isinstance(data, list):
        items: list[Any] = data  # pyright: ignore[reportUnknownVariableType]  # untyped
        return [_substituted(item, values) for item in items]
    return data
