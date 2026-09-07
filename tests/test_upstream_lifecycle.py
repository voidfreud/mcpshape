"""An Upstream sleeps, wakes on a call, goes away, and comes back, with the Proxy always up.

Time-based transitions run on a clock the test moves by hand; an Upstream is made to fail by
taking its in-memory server away, which is all a Client can tell apart anyway.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from fastmcp import Client
from mcp_types import TextContent

from tests.support.clock import FakeClock
from tests.support.seam import free_port, run_cli, running_daemon, serving_daemon, until
from tests.support.upstreams import slow_server
from tests.test_catalog_drift import cli, drift_file, grow, notes
from tests.test_proxy_seam import calculator

if TYPE_CHECKING:
    from fastmcp.client.client import CallToolResult

    from tests.support.seam import ConfigDir

CONNECTED = ("ready", "idle-pending")
PAST_THE_BACKOFF = 5.0
"""Seconds to move the clock on by, comfortably past the first retry delay."""


def error_text(result: CallToolResult) -> str:
    content = result.content[0]
    assert isinstance(content, TextContent)
    return content.text


def quiet_port(config_dir: ConfigDir) -> str:
    """Point the CLI at a port nothing listens on, so the Daemon is definitely down."""
    port = free_port()
    (config_dir.path / "config.toml").write_text(f"version = 1\n[daemon]\nport = {port}\n")
    return f"http://127.0.0.1:{port}"


# --- waking and sleeping -------------------------------------------------------------------


async def test_a_cold_upstream_answers_lists_and_the_first_call_wakes_it(
    config_dir: ConfigDir,
) -> None:
    config_dir.add_memory_upstream("calc", calculator())

    async with running_daemon(config_dir) as daemon, daemon.client("/calc/mcp") as client:
        assert [tool.name for tool in await client.list_tools()] == ["add"]
        assert await daemon.upstream_state("calc") == "cold"

        assert (await client.call_tool("add", {"a": 2, "b": 3})).data == 5
        assert await daemon.upstream_state("calc") in CONNECTED


async def test_an_idle_connection_is_let_go_and_the_next_call_wakes_it(
    config_dir: ConfigDir,
) -> None:
    clock = FakeClock()
    config_dir.add_memory_upstream("calc", calculator(), {"idle_timeout": 600})

    async with running_daemon(config_dir, clock) as daemon, daemon.client("/calc/mcp") as client:
        assert (await client.call_tool("add", {"a": 2, "b": 3})).data == 5

        await clock.advance(601)
        assert await daemon.awaiting_state("calc", "cold") == "cold"

        assert (await client.call_tool("add", {"a": 1, "b": 1})).data == 2
        assert await daemon.upstream_state("calc") in CONNECTED


async def test_an_idle_timeout_of_zero_never_lets_the_connection_go(
    config_dir: ConfigDir,
) -> None:
    clock = FakeClock()
    config_dir.add_memory_upstream("calc", calculator(), {"idle_timeout": 0})

    async with running_daemon(config_dir, clock) as daemon, daemon.client("/calc/mcp") as client:
        assert (await client.call_tool("add", {"a": 2, "b": 3})).data == 5

        await clock.advance(10_000)
        assert await daemon.upstream_state("calc") in CONNECTED


# --- warm ------------------------------------------------------------------------------------


async def test_a_warm_upstream_is_connected_at_daemon_start(config_dir: ConfigDir) -> None:
    config_dir.add_memory_upstream("calc", calculator(), {"warm": True})

    async with running_daemon(config_dir) as daemon:
        assert await daemon.awaiting_state("calc", "ready") == "ready"


async def test_a_warm_upstream_is_pinged_and_never_goes_idle(config_dir: ConfigDir) -> None:
    clock = FakeClock()
    config_dir.add_memory_upstream("calc", calculator(), {"warm": True, "ping_interval": 30})

    async with running_daemon(config_dir, clock) as daemon:
        await daemon.awaiting_state("calc", "ready")

        await clock.advance(10_000)
        assert await daemon.upstream_state("calc") == "ready"
        async with daemon.client("/calc/mcp") as client:
            assert (await client.call_tool("add", {"a": 2, "b": 3})).data == 5
        assert await daemon.upstream_state("calc") == "ready"


async def test_an_upstream_overrides_the_global_lifecycle_defaults(config_dir: ConfigDir) -> None:
    config_dir.add_memory_upstream("warmed", calculator())
    config_dir.add_memory_upstream("lazy", calculator(), {"warm": False})
    (config_dir.path / "config.toml").write_text("version = 1\n[lifecycle]\nwarm = true\n")

    async with running_daemon(config_dir) as daemon:
        assert await daemon.awaiting_state("warmed", "ready") == "ready"
        assert await daemon.upstream_state("lazy") == "cold"


# --- outages ----------------------------------------------------------------------------------


async def test_a_down_upstream_fails_only_calls_and_keeps_the_client_session(
    config_dir: ConfigDir,
) -> None:
    message = "The notes server is not up; nothing was written."
    config_dir.add_memory_upstream("notes", notes(), {"unavailable_message": message})
    await cli(config_dir, "upstream", "sync", "notes")
    config_dir.break_upstream("notes")

    async with running_daemon(config_dir) as daemon, daemon.client("/notes/mcp") as client:
        assert [tool.name for tool in await client.list_tools()] == ["add_note"]

        result = await client.call_tool("add_note", {"text": "hi"}, raise_on_error=False)
        assert result.is_error
        assert message in error_text(result)
        assert await daemon.upstream_state("notes") == "unavailable"

        assert [tool.name for tool in await client.list_tools()] == ["add_note"]
        assert client.instructions == "Keep notes short."
        again = await client.call_tool("add_note", {"text": "hi"}, raise_on_error=False)
        assert message in error_text(again)


async def test_a_connect_that_hangs_is_given_up_on_after_the_connect_timeout(
    config_dir: ConfigDir,
) -> None:
    clock = FakeClock()
    gate = asyncio.Event()
    gate.set()
    message = "The slow server never finished answering."
    config_dir.add_memory_upstream(
        "slow",
        slow_server(gate),
        {"connect_timeout": 10, "unavailable_message": message},
    )
    await cli(config_dir, "upstream", "sync", "slow")

    async with running_daemon(config_dir, clock) as daemon, daemon.client("/slow/mcp") as client:
        gate.clear()
        call = asyncio.create_task(client.call_tool("echo", {"text": "hi"}, raise_on_error=False))
        await daemon.awaiting_state("slow", "connecting")

        await clock.advance(11)

        result = await call
        assert result.is_error
        assert message in error_text(result)
        assert await daemon.upstream_state("slow") == "unavailable"
        gate.set()


async def test_a_reconnect_after_the_backoff_rescans_the_catalog(config_dir: ConfigDir) -> None:
    clock = FakeClock()
    server = notes()
    config_dir.add_memory_upstream("notes", server)
    await cli(config_dir, "upstream", "sync", "notes")
    config_dir.break_upstream("notes")

    async with running_daemon(config_dir, clock) as daemon, daemon.client("/notes/mcp") as client:
        assert (await client.call_tool("add_note", {"text": "hi"}, raise_on_error=False)).is_error
        assert await daemon.upstream_state("notes") == "unavailable"

        grow(server)
        config_dir.restore_upstream("notes")
        await clock.advance(PAST_THE_BACKOFF)

        assert await daemon.awaiting_state("notes", *CONNECTED) in CONNECTED
        await until(
            lambda: drift_file(config_dir, "notes") is not None,
            "the rescan a reconnect triggers",
        )
        assert (await client.call_tool("add_note", {"text": "hi"})).data == "hi"

    review = await cli(config_dir, "upstream", "sync", "notes")
    assert "+ tool delete_note" in review.output


# --- what the user sees -------------------------------------------------------------------------


async def test_ls_and_daemon_status_show_the_state_of_every_upstream_and_proxy(
    config_dir: ConfigDir,
) -> None:
    config_dir.add_memory_upstream("calc", calculator())

    async with serving_daemon(config_dir) as url:
        cold = await asyncio.to_thread(run_cli, config_dir, "ls")
        assert cold.exit_code == 0, cold.output
        assert "cold" in cold.stdout
        assert "Daemon not running" not in cold.output

        async with Client(f"{url}/calc/mcp") as client:
            assert (await client.call_tool("add", {"a": 2, "b": 3})).data == 5

        listed = await asyncio.to_thread(run_cli, config_dir, "ls")
        assert any(state in listed.stdout for state in CONNECTED), listed.stdout
        assert "ok" in listed.stdout

        status = await asyncio.to_thread(run_cli, config_dir, "daemon", "status")
        assert status.exit_code == 0, status.output
        assert f"Daemon running at {url}" in status.stdout
        assert "calc" in status.stdout
        assert "default" in status.stdout
        assert any(state in status.stdout for state in CONNECTED), status.stdout


def test_ls_works_and_says_so_when_the_daemon_is_not_running(config_dir: ConfigDir) -> None:
    quiet_port(config_dir)
    run_cli(config_dir, "add", "github", "--stdio", "cmd")

    result = run_cli(config_dir, "ls")

    assert result.exit_code == 0, result.output
    assert "github" in result.stdout
    assert "Daemon not running" in result.stdout


def test_daemon_status_says_the_daemon_is_not_running(config_dir: ConfigDir) -> None:
    url = quiet_port(config_dir)

    result = run_cli(config_dir, "daemon", "status")

    assert result.exit_code == 0, result.output
    assert f"Daemon not running at {url}" in result.stdout
    assert "mcpshape daemon up" in result.stdout


def test_doctor_reports_a_lifecycle_setting_that_makes_no_sense(config_dir: ConfigDir) -> None:
    run_cli(config_dir, "add", "github", "--stdio", "cmd")
    path = config_dir.path / "upstreams" / "github" / "upstream.toml"
    path.write_text(
        'version = 1\ntransport = "stdio"\ncommand = "cmd"\n\n[lifecycle]\nidle_timeout = -1\n'
    )

    result = run_cli(config_dir, "doctor")

    assert result.exit_code == 1
    assert "lifecycle.idle_timeout" in result.output
