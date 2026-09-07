"""An Upstream sleeps, wakes on a call, goes away, and comes back, with the Proxy always up.

Time-based transitions run on a clock the test moves by hand; an Upstream is made to fail by
taking its in-memory server away, which is all a Client can tell apart anyway.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

from fastmcp import Client
from mcp_types import TextContent

from mcpshape.connection import Connection
from tests.support.clock import FakeClock, settle
from tests.support.seam import (
    RunningDaemon,
    free_port,
    run_cli,
    running_daemon,
    serving_daemon,
    until,
)
from tests.support.upstreams import slow_server
from tests.test_catalog_drift import cli, drift_file, grow, notes
from tests.test_proxy_seam import calculator

if TYPE_CHECKING:
    import pytest
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


async def test_a_hung_upstream_at_start_does_not_block_the_daemon_past_its_connect_timeout(
    config_dir: ConfigDir,
) -> None:
    """#20: the start-up rescan is bounded per Upstream, on the clock the lifecycle runs on.

    Without the fix, ``slow``'s never-set gate hangs the initial scan forever, on no clock at
    all, so advancing the fake clock would never unblock it and this test would time out.
    """
    clock = FakeClock()
    gate = asyncio.Event()  # never set: the initial scan hangs, exactly like a dead connect
    config_dir.add_memory_upstream("slow", slow_server(gate), {"connect_timeout": 5})
    config_dir.add_memory_upstream("calc", calculator())

    stack = contextlib.AsyncExitStack()
    entering = asyncio.create_task(stack.enter_async_context(running_daemon(config_dir, clock)))
    await settle()
    assert not entering.done(), "the Daemon should still be waiting out slow's connect_timeout"

    await clock.advance(6)
    daemon: RunningDaemon = await asyncio.wait_for(entering, timeout=5)
    try:
        async with daemon.client("/calc/mcp") as client:
            assert (await client.call_tool("add", {"a": 2, "b": 3})).data == 5
        async with daemon.client("/slow/mcp") as client:
            # The timed-out scan never recorded a Catalog, so slow's Proxy answers with
            # nothing yet, exactly like a failed scan; what matters is that it answers at all,
            # promptly, instead of the whole Daemon hanging on slow's connect.
            assert await client.list_tools() == []
    finally:
        await stack.aclose()


async def test_a_keeper_that_raises_is_restarted_up_to_a_cap(
    config_dir: ConfigDir, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#20: a keeper that hits an unexpected exception keeps supervising, up to a cap.

    ``Connection._plan`` is patched to fail a bounded number of times: the fault the keeper's
    own guarded paths (ping, connect, shutdown) cannot produce, since those already catch
    their own exceptions. The Daemon's behaviour is asserted only through ``/api/status``.
    """
    clock = FakeClock()
    config_dir.add_memory_upstream("calc", calculator(), {"idle_timeout": 100})

    # fault injection, not a state assertion: reaching an unguarded exception in the keeper
    # needs a fault at a point no legitimate Client-driven scenario can reach.
    real_plan = Connection._plan  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
    failures = iter([True, True])  # two restarts, then it behaves

    def flaky_plan(self: Connection) -> tuple[str, float | None]:
        if next(failures, False):
            msg = "injected keeper fault"
            raise RuntimeError(msg)
        return real_plan(self)

    monkeypatch.setattr(Connection, "_plan", flaky_plan)

    async with running_daemon(config_dir, clock) as daemon, daemon.client("/calc/mcp") as client:
        assert (await client.call_tool("add", {"a": 2, "b": 3})).data == 5
        await settle()
        assert await daemon.upstream_state("calc") in CONNECTED

        await clock.advance(101)
        assert await daemon.awaiting_state("calc", "cold") == "cold"


async def test_a_keeper_that_raises_past_the_cap_marks_the_upstream_unavailable(
    config_dir: ConfigDir, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    config_dir.add_memory_upstream("calc", calculator())

    def always_fails(self: Connection) -> tuple[str, float | None]:  # noqa: ARG001
        msg = "injected keeper fault"
        raise RuntimeError(msg)

    monkeypatch.setattr(Connection, "_plan", always_fails)

    async with running_daemon(config_dir, clock) as daemon:
        state = await daemon.awaiting_state("calc", "unavailable")
        assert state == "unavailable"
        status = await daemon.status()
        error = next(u["error"] for u in status["upstreams"] if u["name"] == "calc")
        assert error is not None
        assert "keeper" in error


async def seconds_in_state(daemon: RunningDaemon, name: str) -> float:
    """How long the Daemon says ``name`` has been in its state, on the Daemon's clock."""
    upstream = next(u for u in (await daemon.status())["upstreams"] if u["name"] == name)
    return float(upstream["seconds"])


async def test_retries_back_off_exponentially_until_the_upstream_returns(
    config_dir: ConfigDir,
) -> None:
    """Retries come after 1 s, then 2 s, then 4 s: a retry restarts the clock on the state."""
    clock = FakeClock()
    config_dir.add_memory_upstream("calc", calculator())
    await cli(config_dir, "upstream", "sync", "calc")
    config_dir.break_upstream("calc")

    async with running_daemon(config_dir, clock) as daemon, daemon.client("/calc/mcp") as client:
        failed = await client.call_tool("add", {"a": 1, "b": 1}, raise_on_error=False)
        assert failed.is_error
        assert await daemon.upstream_state("calc") == "unavailable"

        await clock.advance(1.0)  # the first retry, which fails again
        assert await daemon.upstream_state("calc") == "unavailable"
        await clock.advance(1.5)  # not yet 2 s since that failure: no retry
        assert await seconds_in_state(daemon, "calc") == 1.5
        await clock.advance(0.6)  # past 2 s: the second retry ran and failed
        assert await seconds_in_state(daemon, "calc") < 0.5

        config_dir.restore_upstream("calc")
        await clock.advance(4.0)  # the third retry, which succeeds
        assert await daemon.awaiting_state("calc", *CONNECTED) in CONNECTED
        assert (await client.call_tool("add", {"a": 1, "b": 1})).data == 2


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
