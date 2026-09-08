"""Autostart: a launchd user agent on macOS, or a ``systemd --user`` unit with linger on Linux.

Writing the unit and registering it are kept apart on purpose: writing is pure and file-based,
so it is tested with golden fixtures (an allowed exception to the seam); registering shells
out to ``launchctl`` or ``systemctl``, the one thin edge no test executes.
"""

from __future__ import annotations

import getpass
import platform
import plistlib
import shlex
import shutil
import subprocess  # the one thin edge: registering the unit with the OS
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from mcpshape.paths import CONFIG_DIR_ENV, STATE_DIR_ENV

LABEL = "org.voidfreud.mcpshape"
SYSTEMD_UNIT_NAME = "mcpshape.service"


@dataclass(frozen=True)
class AutostartPaths:
    """What the written unit needs to know to run the Daemon the way this machine does.

    ``command`` is how the unit invokes mcpshape, by absolute path: launchd and systemd run
    units with a PATH of their own that never holds ``~/.local/bin``, where ``uv tool``
    installs, so a bare name would not be found. ``path`` is that same PATH problem one level
    down: the MCP SDK passes the Daemon's PATH to every stdio child, so an Upstream started by
    a bare ``npx`` or ``uvx`` is found under autostart only if the unit carries a PATH that
    holds it. ``daemon install`` captures the installing shell's, the one the user already saw
    work in a terminal, once (#48). ``config_dir``/``state_dir`` are set only when the user
    overrode the defaults, so the common case writes no environment beyond the PATH.
    """

    log_dir: Path
    command: tuple[str, ...]
    config_dir: Path | None = None
    state_dir: Path | None = None
    path: str | None = None


def installed_command() -> tuple[str, ...]:
    """How to run mcpshape from a unit: the installed console script by absolute path, or,
    when none is on this PATH, this interpreter running the package."""
    if found := shutil.which("mcpshape"):
        return (str(Path(found).resolve()),)
    return (sys.executable, "-m", "mcpshape")


def launchd_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def systemd_unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / SYSTEMD_UNIT_NAME


def _env(paths: AutostartPaths) -> dict[str, str]:
    env: dict[str, str] = {}
    if paths.config_dir is not None:
        env[CONFIG_DIR_ENV] = str(paths.config_dir)
    if paths.state_dir is not None:
        env[STATE_DIR_ENV] = str(paths.state_dir)
    if paths.path is not None:
        env["PATH"] = paths.path
    return env


def render_launchd_plist(paths: AutostartPaths) -> bytes:
    """The launchd user agent ``daemon install`` writes on macOS."""
    plist: dict[str, object] = {
        "Label": LABEL,
        "ProgramArguments": [*paths.command, "daemon", "up"],
        "RunAtLoad": True,
        # Relaunch a Daemon that died, not one `daemon down` stopped: that exits 0.
        "KeepAlive": {"SuccessfulExit": False},
        "StandardOutPath": str(paths.log_dir / "daemon.out.log"),
        "StandardErrorPath": str(paths.log_dir / "daemon.err.log"),
    }
    if env := _env(paths):
        plist["EnvironmentVariables"] = env
    return plistlib.dumps(plist, sort_keys=True)


def render_systemd_unit(paths: AutostartPaths) -> str:
    """The ``systemd --user`` unit ``daemon install`` writes on Linux."""
    lines = [
        "[Unit]",
        "Description=mcpshape Daemon",
        "After=network.target",
        "",
        "[Service]",
        f"ExecStart={shlex.join([*paths.command, 'daemon', 'up'])}",
        "Restart=on-failure",
        f"StandardOutput=append:{paths.log_dir / 'daemon.out.log'}",
        f"StandardError=append:{paths.log_dir / 'daemon.err.log'}",
    ]
    lines += [f"Environment={key}={value}" for key, value in sorted(_env(paths).items())]
    lines += ["", "[Install]", "WantedBy=default.target", ""]
    return "\n".join(lines)


# --- writing: pure, file-based, golden-tested ---------------------------------------------------


def write_launchd(paths: AutostartPaths) -> Path:
    target = launchd_plist_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(render_launchd_plist(paths))
    return target


def write_systemd(paths: AutostartPaths) -> Path:
    target = systemd_unit_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_systemd_unit(paths))
    return target


def remove_launchd() -> bool:
    """Whether a launchd unit was there to remove."""
    target = launchd_plist_path()
    existed = target.is_file()
    target.unlink(missing_ok=True)
    return existed


def remove_systemd() -> bool:
    """Whether a systemd unit was there to remove."""
    target = systemd_unit_path()
    existed = target.is_file()
    target.unlink(missing_ok=True)
    return existed


# --- comparing: whether an installed unit still matches what this version writes (#47) ------------


def installed_unit_differs(path: Path, rendered: bytes | str) -> bool:
    """Whether the unit already at ``path`` differs from ``rendered``, what the writer would
    produce now. A missing file counts as differing, so a first install and a stale rewrite
    look the same to the caller."""
    if not path.is_file():
        return True
    try:
        current: bytes | str = (
            path.read_bytes() if isinstance(rendered, bytes) else path.read_text()
        )
    except OSError:
        return True
    return current != rendered


def unit_executable(path: Path) -> Path | None:
    """The executable an installed unit invokes, read from the unit itself: a launchd plist's
    ``ProgramArguments[0]``, or a systemd unit's ``ExecStart`` first token. ``None`` when the
    unit cannot be read or does not name one."""
    try:
        if path.suffix == ".plist":
            plist: dict[str, object] = plistlib.loads(path.read_bytes())
            arguments = plist.get("ProgramArguments")
            if not isinstance(arguments, list):
                return None
            command = [str(argument) for argument in cast("list[object]", arguments)]
            return Path(command[0]) if command else None
        for line in path.read_text().splitlines():
            if line.startswith("ExecStart="):
                tokens = shlex.split(line.removeprefix("ExecStart="))
                return Path(tokens[0]) if tokens else None
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None
    return None


def stale_unit_problems() -> list[str]:
    """Whichever autostart unit is installed on this platform, one message per unit whose
    executable has since moved or been removed: a Daemon that cannot start is not a warning."""
    if platform.system() == "Darwin":
        candidates = [launchd_plist_path()]
    elif platform.system() == "Linux":
        candidates = [systemd_unit_path()]
    else:
        candidates = []
    problems: list[str] = []
    for path in candidates:
        if not path.is_file():
            continue
        executable = unit_executable(path)
        if executable is not None and not executable.exists():
            problems.append(f"{path}: the installed unit runs {executable}, which no longer exists")
    return problems


# --- registering: the thin edge, never called in tests -------------------------------------------


def register_launchd(path: Path) -> None:
    subprocess.run(["launchctl", "load", "-w", str(path)], check=True)  # noqa: S603, S607


def unregister_launchd(path: Path) -> None:
    subprocess.run(["launchctl", "unload", str(path)], check=False)  # noqa: S603, S607


def register_systemd() -> None:
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)  # noqa: S607
    subprocess.run(  # noqa: S603
        ["systemctl", "--user", "enable", "--now", SYSTEMD_UNIT_NAME],  # noqa: S607
        check=True,
    )
    subprocess.run(["loginctl", "enable-linger", getpass.getuser()], check=False)  # noqa: S603, S607


def unregister_systemd() -> None:
    subprocess.run(  # noqa: S603
        ["systemctl", "--user", "disable", "--now", SYSTEMD_UNIT_NAME],  # noqa: S607
        check=False,
    )
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)  # noqa: S607
