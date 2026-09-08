"""The log directory: every log file the Daemon writes, rotated under one global size cap.

Two logs live under ``<state>/log/``: the app log, ``daemon.log``, standard levels and
configurable, and the call log, ``calls.jsonl``, always on. Both are written through
``RotatingFile``, so one rule bounds them together: a file that reaches a quarter of the cap
is rotated aside (``daemon.log.1``, ``.2``, ...), and after every rotation the oldest rotated
files under the directory are removed, whichever log they belong to, until they and the room
the two active files need to reach their next rotation fit under the cap. No database, and
nothing else in the directory is touched.

``AppLogHandler`` is the ``logging`` handler over a ``RotatingFile``; ``configure_app_log``
installs it once, at the level ``config.toml`` sets.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Literal

from mcpshape.paths import call_log_file, daemon_log_file

ROTATE_AT = 4
"""A file is rotated aside once it reaches the cap divided by this: two active files together
never take more than half the cap, and the rest is history."""

HEADROOM = 2
"""How many files may be growing towards their rotation between one rotation and the next: the
app log and the call log. The rotated files are trimmed to the cap less that room, so the
directory stays under the cap between rotations too, not only right after one."""

MIN_ROTATE_BYTES = 4096
"""The smallest a file is let grow to before rotating, however small the cap is set."""

Level = Literal["debug", "info", "warning", "error"]
LEVELS: dict[Level, int] = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}

LOG_NAMES = frozenset({daemon_log_file(Path()).name, call_log_file(Path()).name})
"""The files under the log directory that rotate: only their rotations are ever removed."""

_DIRECTORY_LOCK = threading.Lock()
"""One lock for the directory, not one per file: both logs rotate over and trim the same
directory, so a rename in one and a removal in the other must not interleave."""

APP_LOGGER = "mcpshape"
"""The logger every module of mcpshape logs under, so one handler catches them all."""

FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


class RotatingFile:
    """One log file, appended line by line and rotated under the directory's global cap.

    ``write`` appends one line, opening the file for it, so nothing holds the file open
    between writes and a rotated-away file is never written into. The directory's lock makes
    it safe from any thread: a sync Hook logs from a worker thread while the event loop
    writes the call log. The write is synchronous where it is called, as ``logging``'s own
    handlers are: one append, and once per rotation a look at a directory of a few files.
    """

    def __init__(self, path: Path, cap_bytes: int) -> None:
        self.path = path
        self.cap_bytes = cap_bytes

    @property
    def rotate_bytes(self) -> int:
        return max(self.cap_bytes // ROTATE_AT, MIN_ROTATE_BYTES)

    def write(self, line: str) -> None:
        """Append ``line`` (a newline is added), rotating the file aside once it is full."""
        data = (line + "\n").encode("utf-8", errors="replace")
        with _DIRECTORY_LOCK:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("ab") as handle:
                handle.write(data)
            if self._size() >= self.rotate_bytes:
                self._rotate()
                enforce_cap(self.path.parent, self.cap_bytes - HEADROOM * self.rotate_bytes)

    def _size(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:
            return 0

    def _rotate(self) -> None:
        """``file`` becomes ``file.1``, ``file.1`` becomes ``file.2``, and so on."""
        rotated = sorted(rotated_files(self.path), key=_index, reverse=True)
        for path in rotated:
            path.rename(self.path.with_name(f"{self.path.name}.{_index(path) + 1}"))
        if self.path.exists():
            self.path.rename(self.path.with_name(f"{self.path.name}.1"))


def rotated_files(path: Path) -> list[Path]:
    """Every ``<name>.<n>`` beside ``path``: what earlier rotations left behind."""
    found: list[Path] = []
    for candidate in path.parent.glob(f"{path.name}.*"):
        suffix = candidate.name[len(path.name) + 1 :]
        if suffix.isdigit():
            found.append(candidate)
    return found


def _index(path: Path) -> int:
    return int(path.name.rpartition(".")[2])


def enforce_cap(directory: Path, budget: int) -> None:
    """Remove the oldest rotated files under ``directory`` until the rest fit in ``budget``.

    Only rotations of the known logs (``LOG_NAMES``, as ``<name>.<n>``) are counted and only
    they are ever removed, oldest first by modification time, whichever log they belong to.
    The active files are never touched: the ``HEADROOM`` the budget leaves under the cap is
    theirs to grow into.
    """
    try:
        rotated = [path for path in directory.iterdir() if path.is_file() and _is_rotated(path)]
    except OSError:
        return
    total = sum(_size_of(path) for path in rotated)
    for path in sorted(rotated, key=lambda path: (_mtime(path), -_index(path))):
        if total <= budget:
            return
        size = _size_of(path)
        try:
            path.unlink()
        except OSError:
            continue
        total -= size


def _is_rotated(path: Path) -> bool:
    stem, _, suffix = path.name.rpartition(".")
    return stem in LOG_NAMES and suffix.isdigit()


def _size_of(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def tail(path: Path, lines: int) -> list[str]:
    """The last ``lines`` lines of ``path``, then of the file rotated before it when the
    file is shorter than that. Empty when there is no such file."""
    collected: list[str] = []
    for candidate in [path, *sorted(rotated_files(path), key=_index)]:
        wanted = lines - len(collected)
        if wanted <= 0:
            break
        try:
            text = candidate.read_text(errors="replace")
        except OSError:
            continue
        collected = text.splitlines()[-wanted:] + collected
    return collected


class AppLogHandler(logging.Handler):
    """The app log's handler: every record, formatted, as one line of a ``RotatingFile``."""

    def __init__(self, file: RotatingFile) -> None:
        super().__init__()
        self.file = file
        self.setFormatter(logging.Formatter(FORMAT))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.file.write(self.format(record))
        except Exception:  # noqa: BLE001  # a log that cannot be written must not raise into the caller
            self.handleError(record)


def configure_app_log(state_dir: Path, level: Level, cap_bytes: int) -> None:
    """Send the app log to ``<state>/log/daemon.log`` at ``level``, rotated under ``cap_bytes``.

    Where ``daemon logs`` reads it. One handler per process: a Daemon built again, as the
    tests build one per state directory, replaces the handler rather than adding a second,
    which would double every line.
    """
    app_log = logging.getLogger(APP_LOGGER)
    for handler in list(app_log.handlers):
        if isinstance(handler, AppLogHandler):
            app_log.removeHandler(handler)
    app_log.addHandler(AppLogHandler(RotatingFile(daemon_log_file(state_dir), cap_bytes)))
    app_log.setLevel(LEVELS[level])
