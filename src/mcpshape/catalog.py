"""The Catalog: what an Upstream advertises, as last observed, and the Drift since.

Persisted per Upstream in the state directory. ``upstreams/<name>/catalog.json`` is the
accepted Catalog every Proxy of the Upstream serves from; ``upstreams/<name>/drift.json`` is
a newer observation waiting for review. Accepting replaces the first with the second; until
then the accepted Catalog does not change, whatever the Upstream advertises.

Item identity is the Catalog name: a tool's or prompt's ``name``, a resource's ``uri``, a
resource template's ``uriTemplate``. Definitions are kept as the raw MCP JSON observed.

The Daemon's reconnect rescans and the CLI's ``upstream sync`` are separate processes, both
of which read then write ``catalog.json`` and ``drift.json`` for the same Upstream (#21): a
rescan and an ``accept`` racing unlocked can each work from a stale read of the other's file
and undo it. ``_locked`` holds an ``fcntl.flock`` on a per-Upstream lock file across each
function's whole read-then-write, so one writer finishes before the next one starts reading,
in this process and across processes alike (macOS and Linux only, matching the rest of
mcpshape). Readers stay lock-free; ``_write`` writes to a temp file in the same directory and
``os.replace``s it over the target, so a reader never sees a torn file either way.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime  # noqa: TC003  # pydantic resolves annotations at runtime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

if TYPE_CHECKING:
    from collections.abc import Generator

Kind = Literal["tool", "resource", "resource_template", "prompt"]
KINDS: tuple[Kind, ...] = ("tool", "resource", "resource_template", "prompt")
Items = dict[str, dict[str, Any]]
"""Raw MCP definitions keyed by Catalog name."""

UPSTREAMS_DIR = "upstreams"
CATALOG_FILE = "catalog.json"
DRIFT_FILE = "drift.json"
LOCK_FILE = ".lock"


class CatalogError(Exception):
    """A Catalog file could not be read, or there is no Drift to accept."""


class Catalog(BaseModel):
    """Everything an Upstream advertised at ``scanned_at``."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    scanned_at: datetime
    instructions: str | None = None
    tools: Items = Field(default_factory=dict)
    resources: Items = Field(default_factory=dict)
    resource_templates: Items = Field(default_factory=dict)
    prompts: Items = Field(default_factory=dict)

    def items(self, kind: Kind) -> Items:
        match kind:
            case "tool":
                return self.tools
            case "resource":
                return self.resources
            case "resource_template":
                return self.resource_templates
            case "prompt":
                return self.prompts


def arguments(definition: dict[str, Any]) -> dict[str, Any]:
    """The input-schema properties of one raw MCP tool definition, by argument name."""
    schema: object = definition.get("inputSchema")
    if not isinstance(schema, dict):
        return {}
    properties: object = cast("dict[str, Any]", schema).get("properties")
    return cast("dict[str, Any]", properties) if isinstance(properties, dict) else {}


@dataclass(frozen=True, order=True)
class Item:
    """One Catalog item, by kind and Catalog name."""

    kind: Kind
    name: str

    def __str__(self) -> str:
        return f"{self.kind.replace('_', ' ')} {self.name}"


@dataclass(frozen=True)
class Drift:
    """The difference between a stored Catalog and a newer observation."""

    added: tuple[Item, ...] = ()
    removed: tuple[Item, ...] = ()
    changed: tuple[Item, ...] = ()
    instructions_changed: bool = False

    def __bool__(self) -> bool:
        return bool(self.added or self.removed or self.changed or self.instructions_changed)

    def by_sign(self) -> tuple[tuple[str, tuple[Item, ...]], ...]:
        """Added, removed, and changed items behind their ``+``, ``-``, and ``~`` marks."""
        return (("+", self.added), ("-", self.removed), ("~", self.changed))

    def summary(self) -> str:
        """``+2 -1 ~1``, the parts that are non-zero, ``instructions`` when they changed."""
        parts = [f"{sign}{len(items)}" for sign, items in self.by_sign() if items]
        if self.instructions_changed:
            parts.append("instructions")
        return " ".join(parts)


def drift_between(stored: Catalog, observed: Catalog) -> Drift:
    added: list[Item] = []
    removed: list[Item] = []
    changed: list[Item] = []
    for kind in KINDS:
        before, after = stored.items(kind), observed.items(kind)
        added += [Item(kind, name) for name in after if name not in before]
        removed += [Item(kind, name) for name in before if name not in after]
        changed += [
            Item(kind, name)
            for name, definition in after.items()
            if name in before and before[name] != definition
        ]
    return Drift(
        added=tuple(added),
        removed=tuple(removed),
        changed=tuple(changed),
        instructions_changed=stored.instructions != observed.instructions,
    )


# --- persistence -------------------------------------------------------------------------------


def upstream_state_dir(state_dir: Path, upstream: str) -> Path:
    return state_dir / UPSTREAMS_DIR / upstream


def catalog_path(state_dir: Path, upstream: str) -> Path:
    return upstream_state_dir(state_dir, upstream) / CATALOG_FILE


def drift_path(state_dir: Path, upstream: str) -> Path:
    return upstream_state_dir(state_dir, upstream) / DRIFT_FILE


def _read(path: Path) -> Catalog | None:
    if not path.is_file():
        return None
    try:
        return Catalog.model_validate_json(path.read_text())
    except (OSError, ValidationError, ValueError) as exc:
        msg = f"{path}: {exc}"
        raise CatalogError(msg) from exc


def _write(path: Path, catalog: Catalog) -> None:
    """Write ``catalog`` to ``path`` atomically: a temp file in the same directory, then replace.

    So a reader (the Daemon's own request handling, ``ls``, ``doctor``, ...) never opens
    ``path`` mid-write and sees a torn, unparseable file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    data = catalog.model_dump_json(indent=2, exclude_defaults=True) + "\n"
    handle, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(handle, "w") as tmp_file:
            tmp_file.write(data)
        tmp_path.replace(path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise


def _lock_path(state_dir: Path, upstream: str) -> Path:
    return upstream_state_dir(state_dir, upstream) / LOCK_FILE


@contextlib.contextmanager
def _locked(state_dir: Path, upstream: str) -> Generator[None]:
    """Hold the one lock for ``upstream``'s state directory across a read-then-write.

    ``fcntl.flock`` on a dedicated lock file: it works across processes (the Daemon and the
    CLI), and, unlike POSIX record locks taken through ``fcntl.lockf``, a second lock request
    from the very same process still blocks, which matters for the Daemon's own background
    rescans and its request handling sharing one process.
    """
    directory = upstream_state_dir(state_dir, upstream)
    directory.mkdir(parents=True, exist_ok=True)
    with _lock_path(state_dir, upstream).open("a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def load_catalog(state_dir: Path, upstream: str) -> Catalog | None:
    """The accepted Catalog of ``upstream``, or ``None`` before its first scan."""
    return _read(catalog_path(state_dir, upstream))


def load_drift(state_dir: Path, upstream: str) -> Drift | None:
    """The unreviewed Drift of ``upstream``, or ``None`` when there is none."""
    pending = _read(drift_path(state_dir, upstream))
    stored = load_catalog(state_dir, upstream)
    if pending is None or stored is None:
        return None
    return drift_between(stored, pending)


def pending_drift(state_dir: Path) -> dict[str, Drift]:
    """Every Upstream with unreviewed Drift, in name order."""
    upstreams_dir = state_dir / UPSTREAMS_DIR
    if not upstreams_dir.is_dir():
        return {}
    drifts: dict[str, Drift] = {}
    for directory in sorted(upstreams_dir.iterdir()):
        if (directory / DRIFT_FILE).is_file():
            try:
                drift = load_drift(state_dir, directory.name)
            except CatalogError:
                continue
            if drift:
                drifts[directory.name] = drift
    return drifts


@dataclass(frozen=True)
class Scan:
    """What a scan found: the accepted Catalog as it stands, and the Drift from it, if any."""

    catalog: Catalog
    drift: Drift
    first: bool
    """This scan created the Catalog; there was nothing to drift from."""


def record_scan(state_dir: Path, upstream: str, observed: Catalog) -> Scan:
    """Store ``observed`` as the Catalog on a first scan, else record its Drift for review."""
    with _locked(state_dir, upstream):
        stored = load_catalog(state_dir, upstream)
        if stored is None:
            _write(catalog_path(state_dir, upstream), observed)
            return Scan(catalog=observed, drift=Drift(), first=True)
        drift = drift_between(stored, observed)
        if not drift:
            drift_path(state_dir, upstream).unlink(missing_ok=True)
            _write(catalog_path(state_dir, upstream), observed)
            return Scan(catalog=observed, drift=drift, first=False)
        _write(drift_path(state_dir, upstream), observed)
        return Scan(catalog=stored, drift=drift, first=False)


def accept_scan(state_dir: Path, upstream: str, observed: Catalog) -> Scan:
    """Make ``observed`` the Catalog, in one locked step: what ``upstream sync --accept`` does.

    Recording the observation and then accepting whatever is pending would be two locked
    steps, and a Daemon rescan landing between them would make the accepted Catalog one the
    user was never shown. What is accepted is exactly what this scan saw.

    Between writing the Catalog and unlinking the pending file a lock-free reader sees the
    new Catalog with a pending observation identical to it, whose Drift is empty: nothing.
    """
    with _locked(state_dir, upstream):
        stored = load_catalog(state_dir, upstream)
        drift = Drift() if stored is None else drift_between(stored, observed)
        _write(catalog_path(state_dir, upstream), observed)
        drift_path(state_dir, upstream).unlink(missing_ok=True)
        return Scan(catalog=observed, drift=drift, first=stored is None)


def forget(state_dir: Path, upstream: str) -> None:
    """Drop everything stored about ``upstream``."""
    directory = upstream_state_dir(state_dir, upstream)
    if not directory.is_dir():
        return
    for path in directory.iterdir():
        path.unlink()
    directory.rmdir()
