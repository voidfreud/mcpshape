"""Real Upstreams: a child process over stdio, and a server reached by URL over HTTP or SSE.

The seam is the same as everywhere else (a temp config directory, the Daemon app in-process,
a FastMCP Client over ASGI, the CLI through Typer's runner); what changes is what sits behind
the Upstream. A real subprocess is the exception the design brief allows, because only a real
process shows that one child serves every Proxy and every Client session, that it is given
the environment the Upstream file asks for, and that a connect given up on leaves none.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
from typing import TYPE_CHECKING, Any, cast

from fastmcp import Client
from mcp_types import TextContent

from tests.support import child_upstream
from tests.support.clock import FakeClock
from tests.support.seam import (
    restartable_upstream,
    run_cli,
    running_daemon,
    serving_daemon,
    serving_upstream,
    until,
)
from tests.test_catalog_drift import cli, drift_file
from tests.test_proxy_seam import calculator

if TYPE_CHECKING:
    from pathlib import Path

    import pytest
    from fastmcp.client.client import CallToolResult

    from tests.support.seam import ConfigDir

CONNECTED = ("ready", "idle-pending")
PAST_THE_BACKOFF = 5.0
STORED = "s3cret-only-the-file-knows"
"""A value that lives in the secrets file and must turn up nowhere else."""


def error_text(result: CallToolResult) -> str:
    content = result.content[0]
    assert isinstance(content, TextContent)
    return content.text


async def upstream_error(daemon_status: dict[str, object], name: str) -> str:
    upstreams = daemon_status["upstreams"]
    assert isinstance(upstreams, list)
    found = next(u for u in upstreams if u["name"] == name)  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
    return str(found["error"])  # pyright: ignore[reportUnknownArgumentType]


# --- stdio: one child process, shared ----------------------------------------------------------


async def test_two_proxies_and_several_sessions_share_one_child_process(
    config_dir: ConfigDir, tmp_path: Path
) -> None:
    """One connection per Upstream (story 74): every Proxy and every session calls through it."""
    spawns = tmp_path / "spawns"
    config_dir.add_stdio_upstream(
        "child",
        child_upstream.command(),
        child_upstream.args(),
        env=child_upstream.env(**{child_upstream.SPAWNS: str(spawns)}),
    )
    config_dir.add_proxy("child", "review")

    async with running_daemon(config_dir) as daemon:
        async with (
            daemon.client("/child/mcp") as one,
            daemon.client("/child/review/mcp") as two,
            daemon.client("/child/default/mcp") as three,
        ):
            answers = await asyncio.gather(
                *(client.call_tool("pid", {}) for client in (one, two, three))
            )
        pids = {answer.data for answer in answers}

    assert len(pids) == 1, "three sessions across two Proxies reached three processes"
    started = child_upstream.spawned(spawns)
    assert len(started) == 2, "one child scanned at Daemon start, one shared by every call"
    assert pids == {started[-1]}
    await until(
        lambda: not child_upstream.alive(started[-1]), "the child going with the connection"
    )


async def test_many_sessions_across_two_proxies_call_one_child_at_once(
    config_dir: ConfigDir,
) -> None:
    """The one connection an Upstream has carries every session's calls together: none waits
    for another, and one child answers them all."""
    config_dir.add_stdio_upstream("child", child_upstream.command(), child_upstream.args())
    config_dir.add_proxy("child", "review")
    paths = ["/child/mcp", "/child/review/mcp"] * 4

    async with running_daemon(config_dir) as daemon, contextlib.AsyncExitStack() as sessions:
        clients = [await sessions.enter_async_context(daemon.client(path)) for path in paths]
        answers = await asyncio.gather(
            *(client.call_tool("slow", {"seconds": 0.4}) for client in clients)
        )
    reports = [reported(answer.structured_content) for answer in answers]

    assert len(reports) == len(paths)
    assert len({report["pid"] for report in reports}) == 1, "the sessions reached more than one child"
    assert max(report["started"] for report in reports) < min(
        report["ended"] for report in reports
    ), "the calls queued behind each other instead of overlapping"


def reported(content: dict[str, Any] | None) -> dict[str, float]:
    """What the child's ``slow`` tool answered, unwrapped from the result FastMCP wraps it in."""
    assert content is not None
    report = content.get("result", content)
    assert isinstance(report, dict)
    return {str(key): float(value) for key, value in cast("dict[str, Any]", report).items()}


async def test_a_child_process_is_given_the_environment_the_upstream_file_asks_for(
    config_dir: ConfigDir, tmp_path: Path
) -> None:
    """``${VAR}`` resolves from the 0600 secrets file, and the value never enters a file."""
    config_dir.write_secrets({"DEMO_VALUE": STORED})
    config_dir.add_stdio_upstream(
        "child",
        child_upstream.command(),
        child_upstream.args(),
        env=child_upstream.env(
            CARRIED="${DEMO_VALUE}",
            PLAIN="kept as written",
            **{child_upstream.SPAWNS: str(tmp_path / "spawns")},
        ),
    )

    async with running_daemon(config_dir) as daemon, daemon.client("/child/mcp") as client:
        assert (await client.call_tool("env_value", {"name": "CARRIED"})).data == STORED
        assert (await client.call_tool("env_value", {"name": "PLAIN"})).data == "kept as written"

    written = (config_dir.path / "upstreams" / "child" / "upstream.toml").read_text()
    assert "${DEMO_VALUE}" in written
    assert STORED not in written


async def test_the_daemon_environment_answers_before_the_secrets_file(
    config_dir: ConfigDir, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir.write_secrets({"DEMO_VALUE": STORED})
    config_dir.add_stdio_upstream(
        "child",
        child_upstream.command(),
        child_upstream.args(),
        env=child_upstream.env(CARRIED="${DEMO_VALUE}"),
    )
    monkeypatch.setenv("DEMO_VALUE", "from the environment")

    async with running_daemon(config_dir) as daemon, daemon.client("/child/mcp") as client:
        answer = await client.call_tool("env_value", {"name": "CARRIED"})

    assert answer.data == "from the environment"


async def test_an_upstream_whose_reference_is_unset_never_connects_and_names_the_variable(
    config_dir: ConfigDir,
) -> None:
    config_dir.add_stdio_upstream(
        "child",
        child_upstream.command(),
        child_upstream.args(),
        env=child_upstream.env(CARRIED="${NOWHERE_VALUE}"),
        lifecycle={"warm": True},
    )

    async with running_daemon(config_dir) as daemon:
        assert await daemon.awaiting_state("child", "unavailable") == "unavailable"
        reason = await upstream_error(await daemon.status(), "child")

    assert "NOWHERE_VALUE" in reason
    assert "secrets.toml" in reason


async def test_what_a_failed_connect_says_names_the_reference_and_never_its_value(
    config_dir: ConfigDir, caplog: pytest.LogCaptureFixture
) -> None:
    """A command or URL carrying a resolved value turns up in what a failure says; the status,
    the log, and the terminal get the reference back instead.

    The Upstream is scanned while it works, so its Proxy has a Catalog to serve and a call
    reaches the connect that fails.
    """
    config_dir.add_stdio_upstream("child", child_upstream.command(), child_upstream.args())
    async with running_daemon(config_dir):
        pass
    config_dir.write_secrets({"SECRET_CMD": STORED})
    file = config_dir.path / "upstreams" / "child" / "upstream.toml"
    file.write_text(
        file.read_text().replace(json.dumps(child_upstream.command()), '"${SECRET_CMD}"')
    )

    with caplog.at_level("WARNING", logger="mcpshape"):
        async with running_daemon(config_dir) as daemon, daemon.client("/child/mcp") as client:
            result = await client.call_tool("add", {"a": 1, "b": 1}, raise_on_error=False)
            assert result.is_error
            await daemon.awaiting_state("child", "unavailable")
            error = await upstream_error(await daemon.status(), "child")

    assert "${SECRET_CMD}" in error
    assert STORED not in error
    assert STORED not in caplog.text
    assert "${SECRET_CMD}" in caplog.text  # the start-up scan, and the connect the call woke


# --- stdio: the lifecycle, on a real process ---------------------------------------------------


async def test_a_connect_given_up_on_leaves_no_child_process_behind(
    config_dir: ConfigDir, tmp_path: Path
) -> None:
    """A connect cancelled by the connect timeout takes the process it half spawned with it."""
    clock = FakeClock()
    spawns, hang = tmp_path / "spawns", tmp_path / "hang"
    message = "The child never finished starting."
    config_dir.add_stdio_upstream(
        "child",
        child_upstream.command(),
        child_upstream.args(),
        env=child_upstream.env(
            **{child_upstream.SPAWNS: str(spawns), child_upstream.HANG: str(hang)},
        ),
        lifecycle={"connect_timeout": 10, "unavailable_message": message},
    )

    async with running_daemon(config_dir, clock) as daemon, daemon.client("/child/mcp") as client:
        hang.touch()  # the Daemon's start-up scan is done; the next child never answers
        call = asyncio.create_task(client.call_tool("add", {"a": 1, "b": 1}, raise_on_error=False))
        await daemon.awaiting_state("child", "connecting")
        await until(
            lambda: len(child_upstream.spawned(spawns)) == 2, "the child the connect spawned"
        )

        await clock.advance(11)

        result = await call
        assert result.is_error
        assert message in error_text(result)
        assert await daemon.upstream_state("child") == "unavailable"
        await until(
            lambda: not child_upstream.alive(child_upstream.spawned(spawns)[-1]),
            "the half-spawned child being let go",
        )


async def test_what_a_child_writes_to_stderr_reaches_the_app_log_under_its_name(
    config_dir: ConfigDir,
) -> None:
    """#42: one line per line, tagged with the Upstream's name, and ``daemon logs`` shows it."""
    config_dir.add_stdio_upstream(
        "child", child_upstream.command(), child_upstream.args(), env=child_upstream.env()
    )

    async with serving_daemon(config_dir) as url:
        async with Client(f"{url}/child/mcp") as client:
            assert (await client.call_tool("add", {"a": 1, "b": 1})).data == 2
        app_log = config_dir.state / "log" / "daemon.log"
        await until(
            lambda: child_upstream.STDERR_LINE in app_log.read_text(),
            "the child's stderr line in the app log",
        )
        shown = await asyncio.to_thread(run_cli, config_dir, "daemon", "logs")

    assert f"mcpshape.upstream: child: {child_upstream.STDERR_LINE}" in app_log.read_text()
    assert child_upstream.STDERR_LINE in shown.stdout


async def test_upstream_sync_scans_an_stdio_upstream_under_the_runner(
    config_dir: ConfigDir,
) -> None:
    """#42: the child's stderr no longer needs the runner's streams to have a file descriptor."""
    config_dir.add_stdio_upstream(
        "child", child_upstream.command(), child_upstream.args(), env=child_upstream.env()
    )

    scanned = await cli(config_dir, "upstream", "sync", "child")

    assert "Scanned" in scanned.output
    assert "4 tools" in scanned.output


async def test_a_reconnect_looks_again_over_the_connection_it_already_has(
    config_dir: ConfigDir, tmp_path: Path
) -> None:
    """The rescan a reconnect triggers borrows the shared client: no second child process."""
    clock = FakeClock()
    spawns, grown = tmp_path / "spawns", tmp_path / "grown"
    config_dir.add_stdio_upstream(
        "child",
        child_upstream.command(),
        child_upstream.args(),
        env=child_upstream.env(
            **{child_upstream.SPAWNS: str(spawns), child_upstream.GROWN: str(grown)},
        ),
        lifecycle={"warm": True, "ping_interval": 30},
    )

    async with running_daemon(config_dir, clock) as daemon:
        await daemon.awaiting_state("child", "ready")
        await until(lambda: len(child_upstream.spawned(spawns)) == 2, "the warm connection's child")
        grown.touch()  # the next child advertises one tool more
        os.kill(child_upstream.spawned(spawns)[-1], signal.SIGKILL)

        await clock.advance(31)  # the ping that finds it gone
        assert await daemon.awaiting_state("child", "unavailable") == "unavailable"
        await clock.advance(PAST_THE_BACKOFF)  # the retry that gets it back
        assert await daemon.awaiting_state("child", *CONNECTED) in CONNECTED
        await until(
            lambda: drift_file(config_dir, "child") is not None,
            "the rescan a reconnect triggers",
        )

        assert len(child_upstream.spawned(spawns)) == 3, "the rescan opened a second connection"

    review = await cli(config_dir, "upstream", "sync", "child")
    assert "+ tool subtract" in review.output


async def test_an_stdio_upstream_that_dies_mid_call_is_answered_with_the_message(
    config_dir: ConfigDir, tmp_path: Path
) -> None:
    """#22: a lazy Upstream that dies while connected is noticed by the call, not the ping.

    The child ends itself while answering, so the call that reaches it dies with it. That is
    what moves the Upstream to ``unavailable`` at once; the backoff and a fresh child follow.
    """
    clock = FakeClock()
    spawns = tmp_path / "spawns"
    message = "The child is not up; nothing was added."
    config_dir.add_stdio_upstream(
        "child",
        child_upstream.command(),
        child_upstream.args(),
        env=child_upstream.env(**{child_upstream.SPAWNS: str(spawns)}),
        lifecycle={"unavailable_message": message},
    )

    async with running_daemon(config_dir, clock) as daemon, daemon.client("/child/mcp") as client:
        assert (await client.call_tool("add", {"a": 2, "b": 3})).data == 5

        dying = await client.call_tool("die", {}, raise_on_error=False)
        assert dying.is_error
        assert message in error_text(dying)
        assert await daemon.upstream_state("child") == "unavailable"
        assert "connection dead" in await upstream_error(await daemon.status(), "child")

        again = await client.call_tool("add", {"a": 1, "b": 1}, raise_on_error=False)
        assert message in error_text(again)

        await clock.advance(PAST_THE_BACKOFF)
        # a lazy Upstream is reconnected by the next call, not on its own (#64)
        assert (await client.call_tool("add", {"a": 1, "b": 1})).data == 2
        assert await daemon.upstream_state("child") in CONNECTED

    assert len(child_upstream.spawned(spawns)) == 3, "the scan, the call, and the child after it"


# --- reached by URL ----------------------------------------------------------------------------


async def test_a_streamable_http_upstream_sleeps_wakes_and_is_let_go(
    config_dir: ConfigDir,
) -> None:
    """The lifecycle of #8, on an Upstream reached by URL instead of in memory."""
    clock = FakeClock()

    async with serving_upstream(calculator) as url:
        config_dir.add_url_upstream("calc", url, "http", {"idle_timeout": 600})
        async with (
            running_daemon(config_dir, clock) as daemon,
            daemon.client("/calc/mcp") as client,
        ):
            assert [tool.name for tool in await client.list_tools()] == ["add"]
            assert await daemon.upstream_state("calc") == "cold"

            assert (await client.call_tool("add", {"a": 2, "b": 3})).data == 5
            assert await daemon.upstream_state("calc") in CONNECTED

            await clock.advance(601)
            assert await daemon.awaiting_state("calc", "cold") == "cold"

            assert (await client.call_tool("add", {"a": 1, "b": 1})).data == 2
            assert await daemon.upstream_state("calc") in CONNECTED


async def test_an_sse_upstream_is_reached_by_url(config_dir: ConfigDir) -> None:
    async with serving_upstream(calculator, "sse") as url:
        config_dir.add_url_upstream("calc", url, "sse")
        async with running_daemon(config_dir) as daemon, daemon.client("/calc/mcp") as client:
            assert [tool.name for tool in await client.list_tools()] == ["add"]
            assert (await client.call_tool("add", {"a": 2, "b": 3})).data == 5
            assert await daemon.upstream_state("calc") in CONNECTED


async def test_a_url_upstream_that_is_not_there_fails_only_calls(config_dir: ConfigDir) -> None:
    message = "The Upstream is not up; nothing was written."
    async with serving_upstream(calculator) as url:
        config_dir.add_url_upstream("calc", url, "http", {"unavailable_message": message})
        await cli(config_dir, "upstream", "sync", "calc")

    async with running_daemon(config_dir) as daemon, daemon.client("/calc/mcp") as client:
        assert [tool.name for tool in await client.list_tools()] == ["add"]

        result = await client.call_tool("add", {"a": 1, "b": 1}, raise_on_error=False)
        assert result.is_error
        assert message in error_text(result)
        assert await daemon.upstream_state("calc") == "unavailable"


async def test_a_lazy_url_upstream_that_dies_while_connected_is_noticed_by_the_next_call(
    config_dir: ConfigDir,
) -> None:
    """#22: nothing pings a lazy Upstream, so the call that finds it gone is what reports it."""
    clock = FakeClock()
    message = "The calculator is not up; nothing was added."
    async with restartable_upstream() as served:
        config_dir.add_url_upstream("calc", served.url, "http", {"unavailable_message": message})
        async with (
            running_daemon(config_dir, clock) as daemon,
            daemon.client("/calc/mcp") as client,
        ):
            assert (await client.call_tool("add", {"a": 2, "b": 3})).data == 5
            assert await daemon.upstream_state("calc") in CONNECTED

            await served.kill()

            failed = await client.call_tool("add", {"a": 1, "b": 1}, raise_on_error=False)
            assert failed.is_error
            assert message in error_text(failed)
            assert await daemon.upstream_state("calc") == "unavailable"
            assert "connection dead" in await upstream_error(await daemon.status(), "calc")
            app_log = (config_dir.state / "log" / "daemon.log").read_text()
            assert "Upstream calc is unavailable (a call found the connection dead" in app_log
            assert "Upstream calc let its dead connection go: " in app_log
            assert "did not close cleanly" not in app_log, "a dead close was news (#52)"

            await served.revive()
            await clock.advance(PAST_THE_BACKOFF)
            # a lazy Upstream is reconnected by the next call, not on its own (#64)
            assert (await client.call_tool("add", {"a": 1, "b": 1})).data == 2
            assert await daemon.upstream_state("calc") in CONNECTED


async def test_a_warm_url_upstream_that_dies_is_found_by_its_ping_and_comes_back(
    config_dir: ConfigDir,
) -> None:
    """#22: what a warm Upstream's ping is for, on an Upstream that really goes away."""
    clock = FakeClock()
    async with restartable_upstream() as served:
        config_dir.add_url_upstream("calc", served.url, "http", {"warm": True, "ping_interval": 30})
        async with running_daemon(config_dir, clock) as daemon:
            assert await daemon.awaiting_state("calc", "ready") == "ready"

            await served.kill()
            await clock.advance(31)

            assert await daemon.awaiting_state("calc", "unavailable") == "unavailable"
            reason = await upstream_error(await daemon.status(), "calc")
            assert "ping failed" in reason, "the ping is what noticed, not a call"

            await served.revive()
            await clock.advance(PAST_THE_BACKOFF)
            assert await daemon.awaiting_state("calc", "ready") == "ready"


# --- what doctor says --------------------------------------------------------------------------


def test_doctor_reports_a_reference_nothing_resolves(config_dir: ConfigDir) -> None:
    run_cli(config_dir, "add", "github", "--stdio", "github-mcp")
    path = config_dir.path / "upstreams" / "github" / "upstream.toml"
    path.write_text(
        'version = 1\ntransport = "stdio"\ncommand = "github-mcp"\n\n'
        '[env]\nGITHUB_TOKEN = "${NOWHERE_VALUE}"\n'
    )

    result = run_cli(config_dir, "doctor")

    assert result.exit_code == 1
    assert "NOWHERE_VALUE" in result.output
    assert "secrets.toml" in result.output


def test_doctor_refuses_a_secrets_file_anyone_else_can_read(config_dir: ConfigDir) -> None:
    config_dir.write_secrets({"DEMO_TOKEN": STORED}, mode=0o644)

    result = run_cli(config_dir, "doctor")

    assert result.exit_code == 1
    assert "secrets.toml" in result.output
    assert "0600" in result.output
    assert STORED not in result.output


def test_doctor_reads_a_secrets_file_that_answers_every_reference(config_dir: ConfigDir) -> None:
    config_dir.write_secrets({"DEMO_VALUE": STORED})
    run_cli(config_dir, "add", "github", "--stdio", "sh")
    path = config_dir.path / "upstreams" / "github" / "upstream.toml"
    path.write_text(
        'version = 1\ntransport = "stdio"\ncommand = "sh"\n\n'
        '[env]\nGITHUB_TOKEN = "${DEMO_VALUE}"\n'
    )

    result = run_cli(config_dir, "doctor")

    assert result.exit_code == 0, result.output
    assert "3 file(s)" in result.output, "the secrets file is one of the files checked"
    assert STORED not in result.output


def test_doctor_reports_a_secrets_file_that_does_not_say_what_it_must(
    config_dir: ConfigDir,
) -> None:
    path = config_dir.path / "secrets.toml"
    path.write_text("version = 1\n\n[secrets]\nDEMO_VALUE = 12\n")
    path.chmod(0o600)

    result = run_cli(config_dir, "doctor")

    assert result.exit_code == 1
    assert "secrets.toml: secrets.DEMO_VALUE" in result.output
