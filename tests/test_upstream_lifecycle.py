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

from mcpshape.connection import KEEPER_RESTART_WINDOW, Connection
from tests.support.clock import FakeClock, settle
from tests.support.seam import (
    RunningDaemon,
    awaiting_state_at,
    free_port,
    run_cli,
    running_daemon,
    serving_daemon,
    until,
)
from tests.support.upstreams import slow_server
from tests.test_catalog_drift import cli, drift_file, grow, notes
from tests.test_proxy_seam import calculator
from tests.test_upstream_files import daemon_log
from tests.test_upstreams_added import Connects, counting_calculator

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


async def test_a_close_that_fails_for_any_other_reason_is_logged_with_its_traceback(
    config_dir: ConfigDir, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#52: only a dead connection's close is one line; any other close failing is news."""
    clock = FakeClock()
    config_dir.add_memory_upstream("calc", calculator(), {"idle_timeout": 100})

    # fault injection: no Client-driven path makes a healthy connection's close fail.
    async def failing_close(_self: object) -> None:
        msg = "injected close fault"
        raise RuntimeError(msg)

    monkeypatch.setattr("mcpshape.adapters.fastmcp._Link.close", failing_close)

    async with running_daemon(config_dir, clock) as daemon, daemon.client("/calc/mcp") as client:
        assert (await client.call_tool("add", {"a": 2, "b": 3})).data == 5
        await clock.advance(101)  # idle: the connection is let go, and closing it fails
        assert await daemon.awaiting_state("calc", "cold") == "cold"

    app_log = (config_dir.state / "log" / "daemon.log").read_text()
    assert "Upstream calc did not close cleanly" in app_log
    assert "Traceback" in app_log
    assert "injected close fault" in app_log


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


def failing_plan(budget: list[int], monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``Connection._plan`` raise while ``budget`` says so, and count each one down.

    The same sanctioned fault injection the two tests above use: no Client-driven path makes
    the keeper raise, so the fault has to be put where the keeper's own guarded paths are not.
    """
    real_plan = Connection._plan  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]

    def flaky_plan(self: Connection) -> tuple[str, float | None]:
        if budget[0] != 0:
            budget[0] -= 1
            msg = "injected keeper fault"
            raise RuntimeError(msg)
        return real_plan(self)

    monkeypatch.setattr(Connection, "_plan", flaky_plan)


async def test_keeper_failures_spread_over_time_never_reach_the_cap(
    config_dir: ConfigDir, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#50: the cap is a rate, so failures a window apart never end supervision.

    Four failures, a quiet window, then four more: eight in all, which the old count-only-ever-
    grows rule would have given up on, and which this one forgets between the two bursts.
    """
    clock = FakeClock()
    budget = [4]
    config_dir.add_memory_upstream("calc", calculator(), {"idle_timeout": 100})
    failing_plan(budget, monkeypatch)

    async with running_daemon(config_dir, clock) as daemon, daemon.client("/calc/mcp") as client:
        assert (await client.call_tool("add", {"a": 2, "b": 3})).data == 5
        await settle()
        assert budget[0] == 0, "the first four failures never happened"
        assert await daemon.upstream_state("calc") in CONNECTED

        await clock.advance(KEEPER_RESTART_WINDOW + 1)  # the record of them goes quiet
        await settle()

        budget[0] = 4
        assert (await client.call_tool("add", {"a": 1, "b": 1})).data == 2
        await settle()
        assert budget[0] == 0, "the second four failures never happened"
        assert await daemon.awaiting_state("calc", *CONNECTED) in CONNECTED

        await clock.advance(101)
        assert await daemon.awaiting_state("calc", "cold") == "cold"
        calc = await daemon.upstream("calc")
        assert calc["supervised"] is True
        assert calc["error"] is None


async def test_daemon_reload_brings_back_a_keeper_that_gave_up(
    config_dir: ConfigDir, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#50: past the cap the Upstream stays unavailable until ``daemon reload`` supervises it."""
    clock = FakeClock()
    budget = [-1]  # every plan fails, until the test says otherwise
    config_dir.add_memory_upstream("calc", calculator())
    failing_plan(budget, monkeypatch)

    async with running_daemon(config_dir, clock) as daemon:
        assert await daemon.awaiting_state("calc", "unavailable") == "unavailable"
        gave_up = await daemon.upstream("calc")
        assert gave_up["supervised"] is False
        assert "keeper" in str(gave_up["error"])

        budget[0] = 0
        code, _ = await daemon.api("POST", "/api/reload")
        assert code == 200

        assert await daemon.awaiting_state("calc", *CONNECTED) in CONNECTED
        back = await daemon.upstream("calc")
        assert back["supervised"] is True
        assert back["error"] is None


async def test_daemon_status_says_the_keeper_stopped_supervising(
    config_dir: ConfigDir, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#50: the note under the table names the keeper and the command that brings it back."""
    config_dir.add_memory_upstream("calc", calculator())
    failing_plan([-1], monkeypatch)

    async with serving_daemon(config_dir) as url:
        await awaiting_state_at(url, "calc", "unavailable")

        status = await asyncio.to_thread(run_cli, config_dir, "daemon", "status")

    assert status.exit_code == 0, status.output
    assert "the keeper stopped supervising" in status.stdout
    assert "mcpshape daemon reload" in status.stdout


async def test_daemon_status_says_a_stdio_command_is_not_on_the_daemons_path(
    config_dir: ConfigDir, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#48: the note names the Upstream, the command, and the PATH the Daemon looked in."""
    monkeypatch.setenv("PATH", "/nowhere/mcpshape-bin")
    config_dir.add_stdio_upstream(
        "ghost", "mcpshape-no-such-command", [], lifecycle={"connect_timeout": 1}
    )

    async with serving_daemon(config_dir):
        status = await asyncio.to_thread(run_cli, config_dir, "daemon", "status")

    assert status.exit_code == 0, status.output
    assert (
        "ghost: command 'mcpshape-no-such-command' is not found on the Daemon's PATH "
        "(/nowhere/mcpshape-bin)" in status.stdout
    )


async def seconds_in_state(daemon: RunningDaemon, name: str) -> float:
    """How long the Daemon says ``name`` has been in its state, on the Daemon's clock."""
    return float((await daemon.upstream(name))["seconds"])


async def test_a_warm_upstream_retries_with_a_backoff_that_doubles_to_its_ceiling(
    config_dir: ConfigDir,
) -> None:
    """#64: warm means keep it up. Retries come after 1 s, then 2 s, then 4 s, and stay at
    ``backoff_cap`` from there; a retry restarts the clock on the state."""
    clock = FakeClock()
    config_dir.add_memory_upstream("calc", calculator(), lifecycle={"warm": True, "backoff_cap": 4})
    await cli(config_dir, "upstream", "sync", "calc")
    config_dir.break_upstream("calc")

    async with running_daemon(config_dir, clock) as daemon:
        assert await daemon.awaiting_state("calc", "unavailable") == "unavailable"

        await clock.advance(1.0)  # the first retry, which fails again
        assert await daemon.upstream_state("calc") == "unavailable"
        await clock.advance(1.5)  # not yet 2 s since that failure: no retry
        assert await seconds_in_state(daemon, "calc") == 1.5
        await clock.advance(0.6)  # past 2 s: the second retry ran and failed
        assert await seconds_in_state(daemon, "calc") < 0.5
        await clock.advance(4.0)  # the third retry, after 4 s
        assert await seconds_in_state(daemon, "calc") < 0.5
        await clock.advance(3.9)  # the ceiling: still 4 s, not 8
        assert await seconds_in_state(daemon, "calc") == 3.9
        await clock.advance(0.2)
        assert await seconds_in_state(daemon, "calc") < 0.5

        config_dir.restore_upstream("calc")
        await clock.advance(4.0)  # the next retry, which succeeds, with nobody calling
        assert await daemon.awaiting_state("calc", *CONNECTED) in CONNECTED


async def test_a_lazy_upstream_never_retries_on_its_own_and_the_next_call_tries_again(
    config_dir: ConfigDir,
) -> None:
    """#64: lazy means connect when asked, at failure as at start. Nothing is spent on an
    Upstream nobody is calling; a call within the backoff is answered at once, one after it
    tries again."""
    clock = FakeClock()
    connects = Connects()
    config_dir.add_memory_upstream("calc", counting_calculator(connects))
    await cli(config_dir, "upstream", "sync", "calc")
    config_dir.break_upstream("calc")

    async with running_daemon(config_dir, clock) as daemon, daemon.client("/calc/mcp") as client:
        failed = await client.call_tool("add", {"a": 1, "b": 1}, raise_on_error=False)
        assert failed.is_error
        assert await daemon.upstream_state("calc") == "unavailable"
        opened = connects.count

        await clock.advance(600.0)  # ten minutes: not one retry
        assert await daemon.upstream_state("calc") == "unavailable"
        assert await seconds_in_state(daemon, "calc") == 600.0
        assert connects.count == opened

        config_dir.restore_upstream("calc")
        await clock.advance(600.0)  # the Upstream is back, and still nothing looks
        assert await daemon.upstream_state("calc") == "unavailable"
        assert connects.count == opened

        assert (await client.call_tool("add", {"a": 1, "b": 1})).data == 2  # the call looks
        assert await daemon.upstream_state("calc") in CONNECTED
        assert connects.count == opened + 1


async def test_a_call_within_the_backoff_is_answered_at_once_and_one_after_it_tries_again(
    config_dir: ConfigDir,
) -> None:
    """A failed attempt restarts the clock on the state; one answered at once does not."""
    clock = FakeClock()
    config_dir.add_memory_upstream("calc", calculator())
    await cli(config_dir, "upstream", "sync", "calc")
    config_dir.break_upstream("calc")

    async with running_daemon(config_dir, clock) as daemon, daemon.client("/calc/mcp") as client:
        assert (await client.call_tool("add", {"a": 1, "b": 1}, raise_on_error=False)).is_error
        assert await daemon.upstream_state("calc") == "unavailable"

        await clock.advance(0.5)  # within the first backoff of 1 s
        assert (await client.call_tool("add", {"a": 1, "b": 1}, raise_on_error=False)).is_error
        assert await seconds_in_state(daemon, "calc") == 0.5, "answered at once, nothing tried"

        await clock.advance(0.6)  # past it
        assert (await client.call_tool("add", {"a": 1, "b": 1}, raise_on_error=False)).is_error
        assert await seconds_in_state(daemon, "calc") < 0.5, "tried again, and failed again"


async def test_the_log_says_it_once_per_reason_and_once_per_doubling(
    config_dir: ConfigDir,
) -> None:
    """#64: not one line per attempt. Three failed connects at 1 s, 2 s, and 4 s, then three
    more at the ceiling of 4 s, are three lines, not six; a failure for another reason at the
    same wait is one more; connecting again is exactly one more."""
    clock = FakeClock()
    config_dir.add_memory_upstream(
        "calc", calculator(), lifecycle={"warm": True, "backoff_cap": 4, "connect_timeout": 1}
    )
    await cli(config_dir, "upstream", "sync", "calc")
    config_dir.break_upstream("calc")

    def unavailable_lines() -> list[str]:
        return [line for line in daemon_log(config_dir).splitlines() if "is unavailable" in line]

    async with running_daemon(config_dir, clock) as daemon:
        assert await daemon.awaiting_state("calc", "unavailable") == "unavailable"
        for delay in (1.0, 2.0, 4.0, 4.0, 4.0):
            await clock.advance(delay + 0.1)
        assert await seconds_in_state(daemon, "calc") < 0.5

        unavailable = unavailable_lines()
        assert len(unavailable) == 3, unavailable
        assert "retrying in 1s" in unavailable[0]
        assert "retrying in 2s" in unavailable[1]
        assert "retrying in 4s" in unavailable[2]

        # the same wait, another reason: an Upstream that hangs instead of one that is gone
        config_dir.restore_upstream("calc", slow_server(asyncio.Event()))
        await clock.advance(4.1)  # the retry, which hangs
        await clock.advance(1.1)  # past connect_timeout: it fails for the new reason
        assert await daemon.upstream_state("calc") == "unavailable"
        unavailable = unavailable_lines()
        assert len(unavailable) == 4, unavailable
        assert "connect timed out" in unavailable[3]

        connected_before = daemon_log(config_dir).count("is connected")
        config_dir.restore_upstream("calc", calculator())
        await clock.advance(4.1)
        assert await daemon.awaiting_state("calc", *CONNECTED) in CONNECTED
        assert daemon_log(config_dir).count("is connected") == connected_before + 1


async def test_status_says_when_the_next_attempt_is(config_dir: ConfigDir) -> None:
    """#64: what a given-up state would have told the user, status says without one."""
    clock = FakeClock()
    config_dir.add_memory_upstream("warm", calculator(), lifecycle={"warm": True, "backoff_cap": 4})
    config_dir.add_memory_upstream("lazy", calculator())
    await cli(config_dir, "upstream", "sync")
    config_dir.break_upstream("warm")
    config_dir.break_upstream("lazy")

    async with running_daemon(config_dir, clock) as daemon:
        assert await daemon.awaiting_state("warm", "unavailable") == "unavailable"
        async with daemon.client("/lazy/mcp") as client:
            assert (await client.call_tool("add", {"a": 1, "b": 1}, raise_on_error=False)).is_error

        await clock.advance(0.25)
        warm, lazy = await daemon.upstream("warm"), await daemon.upstream("lazy")
        assert warm["warm"] is True
        assert warm["retry_in"] == 0.75
        assert lazy["warm"] is False
        assert lazy["retry_in"] is None


async def test_daemon_status_names_the_next_attempt_and_upstream_connect(
    config_dir: ConfigDir,
) -> None:
    clock = FakeClock()  # nothing moves, so the warm Upstream stays in its first backoff
    config_dir.add_memory_upstream("warm", calculator(), lifecycle={"warm": True})
    config_dir.add_memory_upstream("lazy", calculator())
    await cli(config_dir, "upstream", "sync")
    config_dir.break_upstream("warm")
    config_dir.break_upstream("lazy")

    async with serving_daemon(config_dir, clock) as url:
        await awaiting_state_at(url, "warm", "unavailable")
        async with Client(f"{url}/lazy/mcp") as client:
            assert (await client.call_tool("add", {"a": 1, "b": 1}, raise_on_error=False)).is_error
        await awaiting_state_at(url, "lazy", "unavailable")

        status = await asyncio.to_thread(run_cli, config_dir, "daemon", "status")
        assert status.exit_code == 0, status.output
        assert "warm: retrying in" in status.stdout
        assert "mcpshape upstream connect warm" in status.stdout
        assert "lazy: the next call tries again" in status.stdout
        assert "mcpshape upstream connect lazy" in status.stdout

        listed = await asyncio.to_thread(run_cli, config_dir, "ls")
        assert "retry in" in listed.stdout
        assert "next call" in listed.stdout

        tried = await asyncio.to_thread(run_cli, config_dir, "upstream", "connect", "lazy")
        assert tried.exit_code == 0, tried.output
        assert "Upstream lazy is connecting" in tried.stdout

        unknown = await asyncio.to_thread(run_cli, config_dir, "upstream", "connect", "nothing")
        assert unknown.exit_code != 0
        assert "no Upstream named 'nothing'" in unknown.output


async def test_upstream_connect_connects_a_cold_upstream_too(config_dir: ConfigDir) -> None:
    """A lazy Upstream nobody has called is the usual state; ``connect`` connects it (#64)."""
    config_dir.add_memory_upstream("calc", calculator())

    async with serving_daemon(config_dir) as url:
        await awaiting_state_at(url, "calc", "cold")

        tried = await asyncio.to_thread(run_cli, config_dir, "upstream", "connect", "calc")
        assert tried.exit_code == 0, tried.output
        assert "Upstream calc is connecting" in tried.stdout

        assert await awaiting_state_at(url, "calc", *CONNECTED) in CONNECTED


def test_upstream_connect_says_so_when_the_daemon_is_not_running(config_dir: ConfigDir) -> None:
    config_dir.add_memory_upstream("calc", calculator())
    (config_dir.path / "config.toml").write_text(f"version = 1\n[daemon]\nport = {free_port()}\n")

    result = run_cli(config_dir, "upstream", "connect", "calc")

    assert result.exit_code == 0, result.output
    assert "Daemon not running" in result.output


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

        # a lazy Upstream is reconnected by the next call, not on its own (#64)
        assert (await client.call_tool("add_note", {"text": "hi"})).data == "hi"
        assert await daemon.upstream_state("notes") in CONNECTED
        await until(
            lambda: drift_file(config_dir, "notes") is not None,
            "the rescan a reconnect triggers",
        )

    review = await cli(config_dir, "upstream", "sync", "notes")
    assert "+ tool delete_note" in review.output


async def test_the_first_connect_rescans_when_the_start_up_scan_reached_nothing(
    config_dir: ConfigDir,
) -> None:
    """#57: an Upstream down when the Daemon starts is looked at on its first connect, since
    that is the first look the Daemon gets; the clock never moves, so no reconnect can."""
    clock = FakeClock()
    server = notes()
    config_dir.add_memory_upstream("notes", server)
    await cli(config_dir, "upstream", "sync", "notes")
    grow(server)
    config_dir.break_upstream("notes")

    async with running_daemon(config_dir, clock) as daemon, daemon.client("/notes/mcp") as client:
        assert drift_file(config_dir, "notes") is None, "the start-up scan reached the Upstream"
        config_dir.restore_upstream("notes")

        assert (await client.call_tool("add_note", {"text": "hi"})).data == "hi"
        assert await daemon.upstream_state("notes") in CONNECTED
        await until(
            lambda: drift_file(config_dir, "notes") is not None,
            "the rescan the first connect triggers",
        )

    review = await cli(config_dir, "upstream", "sync", "notes")
    assert "+ tool delete_note" in review.output


async def test_the_first_connect_does_not_rescan_after_a_start_up_scan_that_reached(
    config_dir: ConfigDir,
) -> None:
    """The start-up scan covers the first connect when it reached the Upstream, as before."""
    clock = FakeClock()
    server = notes()
    config_dir.add_memory_upstream("notes", server)

    async with running_daemon(config_dir, clock) as daemon, daemon.client("/notes/mcp") as client:
        grow(server)  # after the start-up scan, before the first connect
        assert (await client.call_tool("add_note", {"text": "hi"})).data == "hi"
        await settle()
        assert drift_file(config_dir, "notes") is None


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
