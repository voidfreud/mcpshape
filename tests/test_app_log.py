"""The app log's level and rotation: ``<state>/log/daemon.log``, configurable and rotated
under the global cap it shares with the call log (#16).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from fastmcp import Client

from tests.support.seam import run_cli, running_daemon, serving_daemon
from tests.test_proxy_seam import calculator

if TYPE_CHECKING:
    from tests.support.seam import ConfigDir


def daemon_log_text(config_dir: ConfigDir) -> str:
    path = config_dir.state / "log" / "daemon.log"
    return path.read_text() if path.exists() else ""


async def test_upstream_connect_is_logged_at_info_by_default(config_dir: ConfigDir) -> None:
    config_dir.add_memory_upstream("calc", calculator())

    async with running_daemon(config_dir) as daemon, daemon.client("/calc/mcp") as client:
        await client.call_tool("add", {"a": 1, "b": 1})

    assert "Upstream calc is connected" in daemon_log_text(config_dir)


async def test_a_warning_level_hides_the_connect_line(config_dir: ConfigDir) -> None:
    (config_dir.path / "config.toml").write_text('version = 1\n[log]\nlevel = "warning"\n')
    config_dir.add_memory_upstream("calc", calculator())

    async with running_daemon(config_dir) as daemon, daemon.client("/calc/mcp") as client:
        await client.call_tool("add", {"a": 1, "b": 1})

    assert "Upstream calc is connected" not in daemon_log_text(config_dir)


async def test_daemon_logs_reads_from_the_api_while_up(config_dir: ConfigDir) -> None:
    config_dir.add_memory_upstream("calc", calculator())

    async with serving_daemon(config_dir) as url:
        async with Client(f"{url}/calc/mcp") as client:
            await client.call_tool("add", {"a": 1, "b": 1})
        full = await asyncio.to_thread(run_cli, config_dir, "daemon", "logs")
        one = await asyncio.to_thread(run_cli, config_dir, "daemon", "logs", "-n", "1")

    assert "is connected" in full.stdout
    assert len(one.stdout.strip().splitlines()) == 1


def test_an_invalid_log_level_fails_naming_the_key(config_dir: ConfigDir) -> None:
    (config_dir.path / "config.toml").write_text('version = 1\n[log]\nlevel = "loud"\n')

    result = run_cli(config_dir, "daemon", "status")

    assert result.exit_code == 1
    assert "log.level" in result.output


async def test_api_logs_validates_and_bounds_lines(config_dir: ConfigDir) -> None:
    async with running_daemon(config_dir) as daemon:
        status, answer = await daemon.api("GET", "/api/logs", params={"lines": "abc"})
        assert status == 400
        assert "error" in answer

        status, answer = await daemon.api("GET", "/api/logs", params={"lines": "2"})
        assert status == 200
        assert len(answer["lines"]) <= 2
