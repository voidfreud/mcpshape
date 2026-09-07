"""The stdio shim, driven the way a stdio-only Client drives it: a real subprocess.

Outside the seam on purpose (the spec says so): the point of ``serve`` is that a Client with
its own process speaks MCP over pipes and reaches a Proxy over HTTP, and only a real process
proves stdout carried nothing but the protocol.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import signal
import subprocess  # a subprocess is what this test is about
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from fastmcp import Client
from fastmcp.client.transports import StdioTransport

from tests.support.seam import free_port, run_cli, serving_daemon
from tests.support.shim_upstream import TARGET

if TYPE_CHECKING:
    from collections.abc import Generator

    from tests.support.seam import ConfigDir

REPO = Path(__file__).parent.parent
STARTED = re.compile(r"Started the Daemon \(pid (\d+)\)")
SHUTDOWN_TIMEOUT = 10.0


def mcpshape_is_importable() -> bool:
    """Whether the interpreter the shim would run under can import mcpshape."""
    probe = subprocess.run(  # sys.executable, with a fixed argument
        [sys.executable, "-c", "import mcpshape"], check=False, capture_output=True
    )
    return probe.returncode == 0


pytestmark = pytest.mark.skipif(
    not mcpshape_is_importable(), reason="the running interpreter cannot import mcpshape"
)


def add_upstream_a_separate_daemon_can_import(cfg: ConfigDir, name: str) -> None:
    """Register an Upstream by import target, so a Daemon in another process resolves it too."""
    directory = cfg.path / "upstreams" / name
    directory.mkdir(parents=True)
    (directory / "upstream.toml").write_text(
        f'version = 1\ntransport = "memory"\ntarget = "{TARGET}"\n'
    )
    (directory / "default.toml").write_text("version = 1\n")


def shim(cfg: ConfigDir, log: Path, *args: str) -> Client[StdioTransport]:
    """A Client speaking to ``mcpshape serve`` over pipes, exactly as a Client would."""
    transport = StdioTransport(
        command=sys.executable,
        args=[
            "-m",
            "mcpshape",
            "--config-dir",
            str(cfg.path),
            "--state-dir",
            str(cfg.state),
            "serve",
            *args,
        ],
        env={**os.environ, "PYTHONPATH": str(REPO)},
        keep_alive=False,
        log_file=log,
    )
    return Client(transport)


@contextlib.contextmanager
def stopping_a_daemon_the_shim_started(log: Path) -> Generator[None]:
    """Kill the Daemon the shim detached, however far the test got. It outlives the shim."""
    try:
        yield
    finally:
        for pid in daemons_in(log):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pid, signal.SIGTERM)


def daemons_in(log: Path) -> list[int]:
    """Every pid the shim reported starting, read from the stderr it was given."""
    if not log.is_file():
        return []
    return [int(pid) for pid in STARTED.findall(log.read_text())]


def wait_for_exit(pid: int) -> None:
    deadline = time.monotonic() + SHUTDOWN_TIMEOUT
    while time.monotonic() < deadline:
        try:
            os.killpg(pid, 0)
        except (ProcessLookupError, PermissionError):
            return
        time.sleep(0.05)
    pytest.fail(f"the Daemon the shim started (pid {pid}) is still running")


async def test_the_shim_bridges_stdio_to_a_running_daemon(
    config_dir: ConfigDir, tmp_path: Path
) -> None:
    add_upstream_a_separate_daemon_can_import(config_dir, "calc")
    log = tmp_path / "shim.log"

    async with serving_daemon(config_dir), shim(config_dir, log, "calc") as client:
        assert [tool.name for tool in await client.list_tools()] == ["add"]
        assert (await client.call_tool("add", {"a": 2, "b": 3})).data == 5

    assert daemons_in(log) == [], "a Daemon was already up, so the shim started none"


async def test_the_shim_takes_the_proxy_as_one_argument_or_two(
    config_dir: ConfigDir, tmp_path: Path
) -> None:
    add_upstream_a_separate_daemon_can_import(config_dir, "calc")

    async with serving_daemon(config_dir):
        for args in (("calc/default",), ("calc", "default")):
            async with shim(config_dir, tmp_path / "shim.log", *args) as client:
                assert (await client.call_tool("add", {"a": 1, "b": 1})).data == 2


async def test_the_shim_starts_the_daemon_when_nothing_answers(
    config_dir: ConfigDir, tmp_path: Path
) -> None:
    add_upstream_a_separate_daemon_can_import(config_dir, "calc")
    (config_dir.path / "config.toml").write_text(
        f"version = 1\n[daemon]\nport = {free_port()}\n",
    )
    log = tmp_path / "shim.log"

    with stopping_a_daemon_the_shim_started(log):
        async with shim(config_dir, log, "calc") as client:
            assert (await client.call_tool("add", {"a": 2, "b": 3})).data == 5
        started = daemons_in(log)
        assert len(started) == 1, log.read_text()

    wait_for_exit(started[0])


async def test_two_shims_starting_at_once_do_not_race_to_bind(
    config_dir: ConfigDir, tmp_path: Path
) -> None:
    """#13: ``daemon up`` takes a lock so the loser notices the winner instead of failing.

    Two Clients starting shims at the same instant both spawn a ``daemon up``; the fix is
    that the one that does not win the bind waits on the lock, sees the winner is already
    answering, and exits cleanly rather than reporting a failure while the winner is in fact
    up (the bug landing #8 noted).
    """
    add_upstream_a_separate_daemon_can_import(config_dir, "calc")
    (config_dir.path / "config.toml").write_text(
        f"version = 1\n[daemon]\nport = {free_port()}\n",
    )
    log_a, log_b = tmp_path / "shim-a.log", tmp_path / "shim-b.log"

    async def one(log: Path) -> int:
        async with shim(config_dir, log, "calc") as client:
            result = await client.call_tool("add", {"a": 2, "b": 3})
            return int(result.data)

    with stopping_a_daemon_the_shim_started(log_a), stopping_a_daemon_the_shim_started(log_b):
        results = await asyncio.gather(one(log_a), one(log_b))

    assert results == [5, 5]
    started = daemons_in(log_a) + daemons_in(log_b)
    assert started, "at least one shim should have started a Daemon"
    for pid in started:
        wait_for_exit(pid)


async def test_the_entry_proxy_export_writes_is_one_the_shim_accepts(
    config_dir: ConfigDir, tmp_path: Path
) -> None:
    """The shim entry a stdio-only Client gets has to be a command line ``serve`` takes."""
    add_upstream_a_separate_daemon_can_import(config_dir, "calc")
    exported = run_cli(config_dir, "proxy", "export", "calc/default", "--for", "zed")
    assert exported.exit_code == 0, exported.output
    entry = json.loads(exported.stdout)["context_servers"]["calc"]
    verb, *rest = entry["args"]
    assert (entry["command"], verb) == ("mcpshape", "serve")

    async with (
        serving_daemon(config_dir),
        shim(config_dir, tmp_path / "shim.log", *rest) as client,
    ):
        assert (await client.call_tool("add", {"a": 2, "b": 3})).data == 5


async def test_the_shim_refuses_a_proxy_that_does_not_exist(
    config_dir: ConfigDir, tmp_path: Path
) -> None:
    add_upstream_a_separate_daemon_can_import(config_dir, "calc")
    log = tmp_path / "shim.log"

    # The shim exits before it ever speaks MCP, so the Client sees a dead pipe, however
    # its own transport chooses to report that. What matters is the reason on stderr.
    with contextlib.suppress(Exception):
        async with serving_daemon(config_dir), shim(config_dir, log, "calc/nope") as client:
            await client.list_tools()

    assert "no Proxy calc/nope" in log.read_text()
