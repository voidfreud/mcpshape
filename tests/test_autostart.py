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
    installed_unit_differs,
    launchd_plist_path,
    remove_launchd,
    remove_systemd,
    render_launchd_plist,
    render_systemd_unit,
    stale_unit_problems,
    systemd_unit_path,
    unit_executable,
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
FIXED_PATH = "/home/tester/.local/bin:/usr/local/bin:/usr/bin:/bin"
WITH_PATH = AutostartPaths(log_dir=FIXED_LOG_DIR, command=FIXED_COMMAND, path=FIXED_PATH)


def test_launchd_plist_matches_the_golden_fixture() -> None:
    assert render_launchd_plist(PLAIN) == (GOLDEN / "launchd.plist").read_bytes()


def test_systemd_unit_matches_the_golden_fixture() -> None:
    assert render_systemd_unit(PLAIN) == (GOLDEN / "systemd.service").read_text()


def test_launchd_plist_carries_overridden_directories_as_environment() -> None:
    assert render_launchd_plist(WITH_ENV) == (GOLDEN / "launchd-with-env.plist").read_bytes()


def test_systemd_unit_carries_overridden_directories_as_environment() -> None:
    assert render_systemd_unit(WITH_ENV) == (GOLDEN / "systemd-with-env.service").read_text()


def test_launchd_plist_carries_the_installing_shells_path() -> None:
    """#48: install captures PATH once, so a child spawned by a bare name is found."""
    assert render_launchd_plist(WITH_PATH) == (GOLDEN / "launchd-with-path.plist").read_bytes()


def test_systemd_unit_carries_the_installing_shells_path() -> None:
    assert render_systemd_unit(WITH_PATH) == (GOLDEN / "systemd-with-path.service").read_text()


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


# --- comparing an installed unit against what the writer would render now (#47) -------------------


def test_installed_unit_differs_is_false_for_a_file_matching_the_golden_fixture(
    tmp_path: Path,
) -> None:
    rendered = (GOLDEN / "launchd.plist").read_bytes()
    target = tmp_path / "launchd.plist"
    target.write_bytes(rendered)

    assert installed_unit_differs(target, rendered) is False


def test_installed_unit_differs_is_true_for_a_fixture_with_one_changed_line(
    tmp_path: Path,
) -> None:
    rendered = (GOLDEN / "systemd.service").read_text()
    changed = rendered.replace("Restart=on-failure", "Restart=always")
    target = tmp_path / "mcpshape.service"
    target.write_text(changed)

    assert installed_unit_differs(target, rendered) is True


def test_installed_unit_differs_is_true_for_a_missing_file(tmp_path: Path) -> None:
    assert installed_unit_differs(tmp_path / "missing.plist", b"anything") is True


# --- reading the executable a unit invokes, and finding a unit whose executable moved (#47) -------


def test_unit_executable_reads_the_launchd_golden_fixture() -> None:
    assert unit_executable(GOLDEN / "launchd.plist") == Path(FIXED_COMMAND[0])


def test_unit_executable_reads_the_systemd_golden_fixture() -> None:
    assert unit_executable(GOLDEN / "systemd.service") == Path(FIXED_COMMAND[0])


def test_unit_executable_is_none_for_a_file_that_names_none(tmp_path: Path) -> None:
    target = tmp_path / "mcpshape.service"
    target.write_text("[Service]\nRestart=on-failure\n")

    assert unit_executable(target) is None


def test_stale_unit_problems_names_the_unit_and_the_missing_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plist = tmp_path / "org.voidfreud.mcpshape.plist"
    unit = tmp_path / "mcpshape.service"
    missing = Path("/does/not/exist/mcpshape")
    paths = AutostartPaths(log_dir=FIXED_LOG_DIR, command=(str(missing),))
    monkeypatch.setattr("mcpshape.autostart.launchd_plist_path", lambda: plist)
    monkeypatch.setattr("mcpshape.autostart.systemd_unit_path", lambda: unit)
    monkeypatch.setattr("mcpshape.autostart.platform.system", lambda: "Darwin")
    plist.write_bytes(render_launchd_plist(paths))

    problems = stale_unit_problems()

    assert len(problems) == 1
    assert str(plist) in problems[0]
    assert str(missing) in problems[0]


def test_stale_unit_problems_is_empty_when_no_unit_is_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("mcpshape.autostart.launchd_plist_path", lambda: tmp_path / "none.plist")
    monkeypatch.setattr("mcpshape.autostart.systemd_unit_path", lambda: tmp_path / "none.service")
    monkeypatch.setattr("mcpshape.autostart.platform.system", lambda: "Darwin")

    assert stale_unit_problems() == []
