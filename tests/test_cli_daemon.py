"""``daemon down``, ``logs``, ``install``, and ``uninstall``, driven through the CLI (#13).

Autostart registration (``launchctl``/``systemctl``) is monkeypatched out here: the writer is
covered by golden fixtures in ``tests/test_autostart.py``, and the registration call is the one
thin edge no test executes for real.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from mcpshape import autostart
from mcpshape.paths import daemon_log_file
from tests.support.seam import run_cli, serving_daemon
from tests.test_proxy_seam import calculator

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from tests.support.seam import ConfigDir


async def test_daemon_down_stops_a_running_daemon(config_dir: ConfigDir) -> None:
    config_dir.add_memory_upstream("calc", calculator())

    async with serving_daemon(config_dir) as url:
        still_up = await asyncio.to_thread(run_cli, config_dir, "daemon", "status")
        assert f"Daemon running at {url}" in still_up.stdout

        stopped = await asyncio.to_thread(run_cli, config_dir, "daemon", "down")
        assert stopped.exit_code == 0, stopped.output
        assert "Daemon stopped" in stopped.stdout

        after = await asyncio.to_thread(run_cli, config_dir, "daemon", "status")
        assert "Daemon not running" in after.stdout


def test_daemon_down_says_so_when_nothing_is_running(config_dir: ConfigDir) -> None:
    result = run_cli(config_dir, "daemon", "down")

    assert result.exit_code == 0, result.output
    assert "Daemon not running" in result.stdout


def test_daemon_logs_says_so_when_there_is_no_log_yet(config_dir: ConfigDir) -> None:
    result = run_cli(config_dir, "daemon", "logs")

    assert result.exit_code == 0
    assert "No log yet" in result.stdout


def test_daemon_logs_shows_the_tail_of_the_log_file(config_dir: ConfigDir) -> None:
    path = daemon_log_file(config_dir.state)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(f"line {n}" for n in range(1, 6)) + "\n")

    result = run_cli(config_dir, "daemon", "logs", "-n", "2")

    assert result.exit_code == 0, result.output
    assert "line 4" in result.stdout
    assert "line 5" in result.stdout
    assert "line 3" not in result.stdout


def test_daemon_install_writes_and_registers_the_unit(
    config_dir: ConfigDir, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    written: list[Path] = []
    registered: list[str] = []
    plist = tmp_path / "org.voidfreud.mcpshape.plist"
    unit = tmp_path / "mcpshape.service"
    monkeypatch.setattr("mcpshape.autostart.launchd_plist_path", lambda: plist)
    monkeypatch.setattr("mcpshape.autostart.systemd_unit_path", lambda: unit)

    def fake_register_launchd(path: Path) -> None:
        written.append(path)
        registered.append("launchd")

    monkeypatch.setattr(autostart, "register_launchd", fake_register_launchd)
    monkeypatch.setattr(autostart, "register_systemd", lambda: registered.append("systemd"))

    result = run_cli(config_dir, "daemon", "install")

    assert result.exit_code == 0, result.output
    assert plist.is_file() or unit.is_file()
    assert registered, "the (monkeypatched) registration step should have been called"


def test_daemon_uninstall_says_so_when_nothing_is_installed(
    tmp_path: Path, config_dir: ConfigDir, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("mcpshape.autostart.launchd_plist_path", lambda: tmp_path / "none.plist")
    monkeypatch.setattr("mcpshape.autostart.systemd_unit_path", lambda: tmp_path / "none.service")

    result = run_cli(config_dir, "daemon", "uninstall")

    assert result.exit_code == 0, result.output
    assert "No" in result.stdout
    assert "installed" in result.stdout


def test_daemon_uninstall_removes_a_written_unit(
    config_dir: ConfigDir, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plist = tmp_path / "org.voidfreud.mcpshape.plist"
    unit = tmp_path / "mcpshape.service"

    def noop_path(_path: Path) -> None:
        return None

    def noop() -> None:
        return None

    monkeypatch.setattr("mcpshape.autostart.launchd_plist_path", lambda: plist)
    monkeypatch.setattr("mcpshape.autostart.systemd_unit_path", lambda: unit)
    monkeypatch.setattr(autostart, "register_launchd", noop_path)
    monkeypatch.setattr(autostart, "register_systemd", noop)
    monkeypatch.setattr(autostart, "unregister_launchd", noop_path)
    monkeypatch.setattr(autostart, "unregister_systemd", noop)
    run_cli(config_dir, "daemon", "install")
    assert plist.is_file() or unit.is_file()

    result = run_cli(config_dir, "daemon", "uninstall")

    assert result.exit_code == 0, result.output
    assert not plist.is_file()
    assert not unit.is_file()
