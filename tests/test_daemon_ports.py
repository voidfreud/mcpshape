"""A Proxy with a ``port`` override is served on that port too, besides its path (#13).

The signal test runs the Daemon as a real process, an exception to the seam the brief lists:
a signal cannot be sent to the test process itself, and what it checks is the process ending.
"""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import httpx2
import pytest
from fastmcp import Client

from mcpshape.api import STATUS_PATH
from tests.support.seam import free_port, serving_daemon, until
from tests.test_proxy_seam import calculator
from tests.test_stdio_shim import add_upstream_a_separate_daemon_can_import, mcpshape_is_importable

if TYPE_CHECKING:
    from tests.support.seam import ConfigDir

REPO = Path(__file__).parent.parent
EXIT_TIMEOUT = 15.0
STARTUP_PATIENCE = 60.0
"""Seconds a real Daemon process may take to import everything, scan its stdio Upstream, and
listen on every port: a loaded runner has taken well over five (#76)."""


def listening(port: int) -> bool:
    """Whether something answers on ``port`` right now."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


async def test_a_proxy_with_a_port_override_is_served_on_that_port_besides_its_path(
    config_dir: ConfigDir,
) -> None:
    config_dir.add_memory_upstream("calc", calculator())
    extra_port = free_port()
    (config_dir.path / "upstreams" / "calc" / "default.toml").write_text(
        f"version = 1\nport = {extra_port}\n"
    )

    async with serving_daemon(config_dir) as url:
        await until(lambda: listening(extra_port), "the Proxy's port override listener")

        async with Client(f"{url}/calc/mcp") as client:
            assert (await client.call_tool("add", {"a": 2, "b": 3})).data == 5

        async with Client(f"http://127.0.0.1:{extra_port}/mcp") as client:
            assert (await client.call_tool("add", {"a": 1, "b": 1})).data == 2


@pytest.mark.skipif(
    not mcpshape_is_importable(), reason="the running interpreter cannot import mcpshape"
)
async def test_a_signal_stops_a_daemon_after_a_port_override_came_and_went(
    config_dir: ConfigDir,
) -> None:
    """SIGTERM ends the Daemon process, whatever listeners came and went before it (#70).

    uvicorn takes the process's signal handlers for every server it runs and puts back, when
    that server ends, whatever it found; two listeners closed in the order they started leave
    the handlers on the first, a server that is gone, and the signal is swallowed. Only the
    main server owns them. A Daemon it stops ends with the signal's own code: uvicorn raises
    the signal again after its graceful shutdown, so a parent sees what stopped it.
    """
    add_upstream_a_separate_daemon_can_import(config_dir, "calc")
    config_dir.add_proxy("calc", "review")
    main_port, first_port, second_port = free_port(), free_port(), free_port()
    (config_dir.path / "config.toml").write_text(f"version = 1\n[daemon]\nport = {main_port}\n")
    first = config_dir.path / "upstreams" / "calc" / "default.toml"
    second = config_dir.path / "upstreams" / "calc" / "review.toml"
    first.write_text(f"version = 1\nport = {first_port}\n")
    second.write_text(f"version = 1\nport = {second_port}\n")
    daemon = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "mcpshape",
        "--config-dir",
        str(config_dir.path),
        "--state-dir",
        str(config_dir.state),
        "daemon",
        "up",
        env={**os.environ, "PYTHONPATH": str(REPO)},
        start_new_session=True,
    )
    try:
        await until(
            lambda: listening(main_port) and listening(first_port) and listening(second_port),
            "every listener",
            patience=STARTUP_PATIENCE,
        )
        log = config_dir.state / "log" / "daemon.log"
        for file, port in ((first, first_port), (second, second_port)):
            file.write_text("version = 1\n")
            await asyncio.to_thread(httpx2.get, f"http://127.0.0.1:{main_port}{STATUS_PATH}")
            # the log line is written once the listener's server has fully ended, handlers
            # put back and all; the port alone closes a moment earlier
            closed = f"no longer listening on port {port}"
            await until(lambda closed=closed: closed in log.read_text(), f"port {port} ending")

        daemon.send_signal(signal.SIGTERM)

        await asyncio.wait_for(daemon.wait(), EXIT_TIMEOUT)
        assert daemon.returncode in (0, -signal.SIGTERM)
        assert not listening(main_port)
    finally:
        if daemon.returncode is None:
            daemon.kill()
            await daemon.wait()
