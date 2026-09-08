"""The call log: every tool call a Proxy serves, as the dashboard and ``daemon logs`` read it.

Always on, separate from the app log. Each call is one ``CallRecord``: when it started, which
Proxy served it, the tool's Catalog name and exposed name, the arguments under Catalog names
as the Client sent them, how long the Client waited, whether it got a result or an error, and
the start of what it got. Every record goes to a ring buffer per Proxy, which ``/api/calls``
answers from, and to one line of ``<state>/log/calls.jsonl``, rotated under the log
directory's global size cap with the app log (``mcpshape.logs``). No database.

Nothing here imports FastMCP: the Adapter records, this keeps.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict, deque
from datetime import datetime  # noqa: TC003  # pydantic resolves annotations at runtime
from typing import TYPE_CHECKING, Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mcpshape.logs import tail
from mcpshape.paths import call_log_file

if TYPE_CHECKING:
    from pathlib import Path

    from mcpshape.logs import RotatingFile

log = logging.getLogger("mcpshape.calls")

RING_SIZE = 200
"""How many of a Proxy's latest calls the ring buffer keeps."""

RESULT_CHARS = 500
"""How much of a result the record keeps; ``result_chars`` says how long it was. Every string
in the arguments is cut to the same, so one call with a document in it cannot make a line
that outgrows the log's rotation."""

CUT = "..."
"""What ends a string the record cut."""

Outcome = Literal["ok", "error"]


class CallRecord(BaseModel):
    """One tool call through one Proxy, as it went."""

    model_config = ConfigDict(extra="forbid")

    at: datetime
    """When the call started, in UTC."""
    upstream: str
    proxy: str
    name: str
    """The tool's Catalog name; a Virtual Tool's exposed name is its identity."""
    exposed: str
    """The name the Client called it by."""
    arguments: dict[str, Any] = Field(default_factory=dict)
    """Under Catalog names, as the Client sent them, before any Hook; long strings cut."""
    duration_ms: float
    outcome: Outcome
    result: str
    """The start of the result's text, or the error the Client was answered with."""
    result_chars: int
    """How long the whole result was, so a cut one is known to be cut."""

    @classmethod
    def build(  # noqa: PLR0913, PLR0917  # every one of these is what a record is
        cls,
        at: datetime,
        upstream: str,
        proxy: str,
        name: str,
        exposed: str,
        arguments: dict[str, Any],
        duration_ms: float,
        outcome: Outcome,
        result: str,
    ) -> CallRecord:
        return cls(
            at=at,
            upstream=upstream,
            proxy=proxy,
            name=name,
            exposed=exposed,
            arguments=_cut(arguments),
            duration_ms=round(duration_ms, 3),
            outcome=outcome,
            result=result[:RESULT_CHARS],
            result_chars=len(result),
        )


def _cut[T](value: T) -> T:
    """``value`` with every string in it cut to ``RESULT_CHARS``, however deep it sits."""
    cut: Any
    match value:
        case str() if len(value) > RESULT_CHARS:
            cut = value[:RESULT_CHARS] + CUT
        case dict():
            items: dict[Any, Any] = value  # pyright: ignore[reportUnknownVariableType]  # wire data
            cut = {key: _cut(item) for key, item in items.items()}
        case list():
            entries: list[Any] = value  # pyright: ignore[reportUnknownVariableType]  # wire data
            cut = [_cut(item) for item in entries]
        case _:
            cut = value
    return cast("T", cut)


class CallLog:
    """Where every Proxy's calls go: a ring per Proxy, and one file for all of them."""

    def __init__(self, file: RotatingFile | None = None, ring: int = RING_SIZE) -> None:
        self._file = file
        self._rings: defaultdict[tuple[str, str], deque[CallRecord]] = defaultdict(
            lambda: deque(maxlen=ring)
        )

    def record(self, record: CallRecord) -> None:
        """Keep ``record``. Never raises: a call that ran is not failed by its own log line."""
        self._rings[record.upstream, record.proxy].append(record)
        if self._file is None:
            return
        try:
            self._file.write(record.model_dump_json())
        except Exception:  # the file may be unwritable; the ring still has it
            log.warning("a call could not be written to the call log", exc_info=True)

    def recent(
        self, upstream: str | None = None, proxy: str | None = None, limit: int = 100
    ) -> list[CallRecord]:
        """The latest ``limit`` calls, oldest first, of every Proxy or the ones named."""
        rings = [
            ring
            for (upstream_name, proxy_name), ring in self._rings.items()
            if (upstream is None or upstream_name == upstream)
            and (proxy is None or proxy_name == proxy)
        ]
        merged = sorted((record for ring in rings for record in ring), key=lambda r: r.at)
        return merged[-limit:] if limit > 0 else []


def read_recent(state_dir: Path, limit: int) -> list[CallRecord]:
    """The latest ``limit`` calls in the call log on disk, oldest first: what ``daemon logs
    --calls`` shows while no Daemon is up to ask. A line that is not a record is skipped."""
    records: list[CallRecord] = []
    for line in tail(call_log_file(state_dir), limit):
        try:
            records.append(CallRecord.model_validate_json(line))
        except (ValidationError, ValueError):
            continue
    return records


def render(record: CallRecord) -> str:
    """One line of a record for a terminal: when, in local time as the app log's lines are,
    where, what, how long, and how it went."""
    name = record.exposed if record.exposed == record.name else f"{record.exposed}<-{record.name}"
    cut = " ..." if record.result_chars > len(record.result) else ""
    return (
        f"{record.at.astimezone().strftime('%Y-%m-%d %H:%M:%S')} "
        f"{record.upstream}/{record.proxy} {name} "
        f"{record.duration_ms:.0f}ms {record.outcome} {json.dumps(record.arguments)} "
        f"-> {record.result!r}{cut}"
    )
