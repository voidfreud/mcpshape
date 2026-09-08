"""What an OAuth login left behind, kept encrypted in the state directory (story 69).

One file per Upstream under ``oauth/``, holding everything the login produced as a single
JSON document: the token set and the dynamic client registration the provider handed back.
The document is encrypted whole, so a file on disk shows neither, and it is decrypted only to
be handed to the Upstream's connection.

The key sits in ``oauth.key`` next to it, thirty-two random bytes written on first need, and
is refused when anyone but its owner may read it, the way the secrets file is refused.

Nothing here imports FastMCP: the shape of what is stored is the Adapter's business, and the
Adapter is the only module that knows what the token set looks like.
"""

from __future__ import annotations

import base64
import json
import os
import stat
from typing import TYPE_CHECKING, Any

from cryptography.fernet import Fernet, InvalidToken

from mcpshape.secrets import OWNER_ONLY, SecretError, check_mode

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

KEY_FILE = "oauth.key"
"""The file in the state directory that holds the key the token files are encrypted with."""

TOKENS_DIR = "oauth"
"""The directory in the state directory that holds one encrypted file per Upstream."""

KEY_BYTES = 32
"""How much randomness the key is: what Fernet's key derivation takes."""

DOCUMENT_VERSION = 1
"""Format version of the decrypted document, so a later shape can be migrated."""


class TokenError(Exception):
    """A stored login cannot be read or written as it stands.

    Its message names files only, never a token, a key, or a client secret.
    """


class Tokens:
    """One Upstream's stored login: read it, write it, forget it.

    Read gives back what ``write`` was handed, or nothing when the Upstream has never logged
    in. Neither the key nor anything decrypted is held on to between calls, so a login the CLI
    performs is picked up by the next connect with no restart.
    """

    def __init__(self, state_dir: Path, upstream: str) -> None:
        self.state_dir = state_dir
        self.upstream = upstream

    @property
    def path(self) -> Path:
        return self.state_dir / TOKENS_DIR / f"{self.upstream}.json"

    def stored(self) -> bool:
        """Whether a login is on disk at all. Says nothing about whether it still works."""
        return self.path.is_file()

    def read(self) -> dict[str, Any] | None:
        """What the login produced, or nothing when there is none."""
        if not self.path.is_file():
            return None
        try:
            plain = Fernet(_key(self.state_dir)).decrypt(self.path.read_bytes())
        except InvalidToken as exc:
            msg = f"{self.path} cannot be decrypted with {key_file(self.state_dir)}"
            raise TokenError(msg) from exc
        document: dict[str, Any] = json.loads(plain)
        return document

    def write(self, document: Mapping[str, Any]) -> None:
        """Keep ``document`` as this Upstream's login, replacing whatever stood there."""
        payload = {**document, "version": DOCUMENT_VERSION}
        sealed = Fernet(_key(self.state_dir)).encrypt(json.dumps(payload).encode())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _write_owner_only(self.path, sealed)

    def forget(self) -> None:
        """Drop the stored login, so the next connect asks for one again."""
        self.path.unlink(missing_ok=True)

    def concealed(self, text: str) -> str:
        """``text`` with every stored token in it written as what it is instead of its value.

        For what an OAuth Upstream says when reaching it fails: a provider's answer quoted
        into an error can carry a token, and the message goes to the log, the status, and the
        terminal.
        """
        for value in sorted(self._values(), key=len, reverse=True):
            text = text.replace(value, "<token>")
        return text

    def _values(self) -> set[str]:
        """Every value in the stored document that must never be shown."""
        try:
            document = self.read()
        except (TokenError, SecretError):
            return set()
        return _secret_strings(document) if document else set()


def forget(state_dir: Path, upstream: str) -> None:
    """Drop what ``upstream`` logged in with, as ``catalog.forget`` drops what it advertised."""
    Tokens(state_dir, upstream).forget()


def key_file(state_dir: Path) -> Path:
    return state_dir / KEY_FILE


SECRET_KEYS = frozenset({"access_token", "refresh_token", "client_secret", "device_code", "code"})
"""The keys in a stored document whose values never reach a message or the terminal."""


def _secret_strings(document: Any) -> set[str]:  # noqa: ANN401  # a stored document is untyped
    if isinstance(document, dict):
        pairs: list[tuple[Any, Any]] = list(document.items())  # pyright: ignore[reportUnknownArgumentType]  # untyped
        found: set[str] = set()
        for key, value in pairs:
            if key in SECRET_KEYS and isinstance(value, str) and value:
                found.add(value)
            else:
                found |= _secret_strings(value)
        return found
    if isinstance(document, list):
        items: list[Any] = document  # pyright: ignore[reportUnknownVariableType]  # untyped
        return {value for item in items for value in _secret_strings(item)}
    return set()


def _key(state_dir: Path) -> bytes:
    """The Fernet key the token files are sealed with, made on first need.

    The file holds the raw randomness; Fernet takes it base64-encoded, which is an encoding of
    the same bytes and not a second secret.
    """
    path = key_file(state_dir)
    if path.is_file():
        check_mode(path)
        raw = path.read_bytes()
        if len(raw) != KEY_BYTES:
            msg = f"{path} is not a {KEY_BYTES}-byte key; remove it and log in again"
            raise TokenError(msg)
    else:
        raw = os.urandom(KEY_BYTES)
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_owner_only(path, raw)
    return base64.urlsafe_b64encode(raw)


def _write_owner_only(path: Path, content: bytes) -> None:
    """Write ``content`` where nobody but its owner can read it, even for the instant between.

    The file is opened with the mode already on it, rather than chmod-ed after the write, so
    there is no window in which the group or anyone else could read what it holds.
    """
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, OWNER_ONLY)
    with os.fdopen(descriptor, "wb") as sealed:
        sealed.write(content)
    os.chmod(path, stat.S_IMODE(OWNER_ONLY))  # noqa: PTH101  # an existing file keeps its old mode
