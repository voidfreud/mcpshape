"""Autostart unit writers: golden fixtures, an allowed exception to the seam.

Writing the unit is pure and file-based; registering it shells out to ``launchctl`` or
``systemctl``, which no test here, or in ``tests/test_cli_daemon.py``, ever executes.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from mcpshape.autostart import (
    AutostartPaths,
    installed_command,
    launchd_plist_path,
    remove_launchd,
    remove_systemd,
    render_launchd_plist,
    render_systemd_unit,
    systemd_unit_path,
    write_launchd,
    write_systemd,
)

if TYPE_CHECKING:
    import pytest

GOLDEN = Path(__file__).parent / "golden"
FIXED_LOG_DIR = Path("/home/tester/.local/state/mcpshape/log")
FIXED_COMMAND = ("/home/tester/.local/bin/mcpshape",)
PLAIN = AutostartPaths(log_dir=FIXED_LOG_DIR, command=FIXED_COMMAND)
WITH_ENV = AutostartPaths(
    log_dir=FIXED_LOG_DIR,
    command=FIXED_COMMAND,
    config_dir=Path("/home/tester/.config/mcpshape-alt"),
    state_dir=Path("/home/tester/.local/state/mcpshape-alt"),
)


def test_launchd_plist_matches_the_golden_fixture() -> None:
    assert render_launchd_plist(PLAIN) == (GOLDEN / "launchd.plist").read_bytes()


def test_systemd_unit_matches_the_golden_fixture() -> None:
    assert render_systemd_unit(PLAIN) == (GOLDEN / "systemd.service").read_text()


def test_launchd_plist_carries_overridden_directories_as_environment() -> None:
    assert render_launchd_plist(WITH_ENV) == (GOLDEN / "launchd-with-env.plist").read_bytes()


def test_systemd_unit_carries_overridden_directories_as_environment() -> None:
    assert render_systemd_unit(WITH_ENV) == (GOLDEN / "systemd-with-env.service").read_text()


def test_write_launchd_writes_the_rendered_plist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "LaunchAgents" / "org.voidfreud.mcpshape.plist"
    monkeypatch.setattr("mcpshape.autostart.launchd_plist_path", lambda: target)

    written = write_launchd(PLAIN)

    assert written == target
    assert target.read_bytes() == render_launchd_plist(PLAIN)


def test_write_systemd_writes_the_rendered_unit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "systemd" / "user" / "mcpshape.service"
    monkeypatch.setattr("mcpshape.autostart.systemd_unit_path", lambda: target)

    written = write_systemd(PLAIN)

    assert written == target
    assert target.read_text() == render_systemd_unit(PLAIN)


def test_remove_launchd_says_whether_anything_was_there(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "org.voidfreud.mcpshape.plist"
    monkeypatch.setattr("mcpshape.autostart.launchd_plist_path", lambda: target)

    assert remove_launchd() is False

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"")
    assert remove_launchd() is True
    assert not target.exists()


def test_remove_systemd_says_whether_anything_was_there(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "mcpshape.service"
    monkeypatch.setattr("mcpshape.autostart.systemd_unit_path", lambda: target)

    assert remove_systemd() is False

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("")
    assert remove_systemd() is True
    assert not target.exists()


def test_the_command_a_unit_runs_is_an_absolute_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """launchd and systemd run units with a PATH of their own, so a bare name is never found."""

    def nowhere(_name: str) -> str | None:
        return None

    def installed(_name: str) -> str | None:
        return "/usr/local/bin/mcpshape"

    monkeypatch.setattr("mcpshape.autostart.shutil.which", nowhere)
    assert Path(installed_command()[0]).is_absolute()
    assert installed_command()[1:] == ("-m", "mcpshape")

    monkeypatch.setattr("mcpshape.autostart.shutil.which", installed)
    assert installed_command() == (str(Path("/usr/local/bin/mcpshape").resolve()),)


def test_launchd_plist_path_and_systemd_unit_path_live_under_home() -> None:
    assert launchd_plist_path().is_relative_to(Path.home())
    assert systemd_unit_path().is_relative_to(Path.home())
