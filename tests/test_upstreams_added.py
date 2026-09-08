"""An Upstream or Proxy added while the Daemon runs is served without a restart (#67).

Routes resolve their owner at request time, so an Upstream directory (or a Proxy file inside
one) that appears after the Daemon started is found on the next request, on `daemon status`,
and on `daemon reload`, launched the same way an Upstream present at start is. A removed and
re-added Upstream of the same name is a new one: a fresh owner, a fresh connection, a first
scan.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import TYPE_CHECKING, Any

from fastmcp import FastMCP

from mcpshape.api import RELOAD_PATH, UPSTREAMS_PATH
from tests.support.seam import free_port, run_cli, running_daemon
from tests.test_catalog_drift import notes
from tests.test_proxy_seam import calculator
from tests.test_upstream_files import daemon_log, until_unlisted

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from tests.support.seam import ConfigDir, RunningDaemon


class Connects:
    """How many times an Upstream was connected to: its lifespan is entered once per session.

    The Daemon's start-up scan is one connection of its own; the first call through any Proxy
    is the next; a second Proxy sharing the connection adds none.
    """

    def __init__(self) -> None:
        self.count = 0


def counting_calculator(connects: Connects) -> FastMCP[Any]:
    """``calculator()`` that counts every session opened on it."""

    @contextlib.asynccontextmanager
    async def lifespan(_server: FastMCP[Any]) -> AsyncGenerator[None]:
        connects.count += 1
        yield

    server: FastMCP[Any] = FastMCP("calc", lifespan=lifespan)

    def add(a: int, b: int) -> int:
        """Add two numbers."""
        return a + b

    server.tool(add)
    return server


async def until_scanned(daemon: RunningDaemon, name: str, patience: float = 5.0) -> None:
    """Wait until the catalog endpoint answers a non-null Catalog for ``name``.

    The background launch's own scan runs on its own task, so this is what a caller waits on
    instead of the file system or a status flag (#67).
    """
    deadline = time.monotonic() + patience
    while True:
        _, answer = await daemon.api("GET", f"{UPSTREAMS_PATH}/{name}/catalog")
        if answer["catalog"] is not None:
            return
        if time.monotonic() > deadline:
            msg = f"the background launch's first scan of {name} never finished"
            raise AssertionError(msg)
        await asyncio.sleep(0.01)


async def test_an_upstream_added_while_the_daemon_runs_is_served_on_the_next_request(
    config_dir: ConfigDir,
) -> None:
    config_dir.add_memory_upstream("calc", calculator())

    async with running_daemon(config_dir) as daemon:
        config_dir.add_memory_upstream("notes", notes())

        async with daemon.client("/notes/mcp") as client:
            assert [tool.name for tool in await client.list_tools()] == ["add_note"]
            assert (await client.call_tool("add_note", {"text": "hi"})).data == "hi"

        async with daemon.client("/notes/default/mcp") as client:
            assert [tool.name for tool in await client.list_tools()] == ["add_note"]

        status = await daemon.status()
        assert {upstream["name"] for upstream in status["upstreams"]} == {"calc", "notes"}

        code, answer = await daemon.api("GET", f"{UPSTREAMS_PATH}/notes/catalog")
        assert code == 200
        assert answer["catalog"] is not None

        code, answer = await daemon.api("GET", f"{UPSTREAMS_PATH}/notes/drift")
        assert code == 200
        assert answer["drift"] is None


async def test_an_upstream_added_while_the_daemon_runs_is_found_by_status_and_reload(
    config_dir: ConfigDir,
) -> None:
    config_dir.add_memory_upstream("calc", calculator())

    async with running_daemon(config_dir) as daemon:
        config_dir.add_memory_upstream("notes", notes())

        status = await daemon.status()
        assert "notes" in {upstream["name"] for upstream in status["upstreams"]}

        await until_scanned(daemon, "notes")

        code, answer = await daemon.api("POST", RELOAD_PATH)
        assert code == 200
        assert "notes" in {upstream["name"] for upstream in answer["upstreams"]}

        async with daemon.client("/notes/mcp") as client:
            assert [tool.name for tool in await client.list_tools()] == ["add_note"]
            assert (await client.call_tool("add_note", {"text": "hi"})).data == "hi"


async def test_a_proxy_added_while_the_daemon_runs_is_served_and_shares_the_connection(
    config_dir: ConfigDir,
) -> None:
    connects = Connects()
    config_dir.add_memory_upstream("calc", counting_calculator(connects))

    async with running_daemon(config_dir) as daemon:
        async with daemon.client("/calc/mcp") as client:
            assert (await client.call_tool("add", {"a": 2, "b": 3})).data == 5
        assert connects.count == 2, "the start-up scan, then the first call's connect"

        config_dir.add_proxy("calc", "review")

        calc = await daemon.upstream("calc")
        assert [proxy["name"] for proxy in calc["proxies"]] == ["default", "review"]

        async with daemon.client("/calc/review/mcp") as client:
            assert [tool.name for tool in await client.list_tools()] == ["add"]
            assert (await client.call_tool("add", {"a": 4, "b": 5})).data == 9

        assert connects.count == 2, "the new Proxy called over the connection already open"
        assert await daemon.upstream_state("calc") in ("ready", "idle-pending")


async def test_a_removed_and_re_added_upstream_is_a_new_one(config_dir: ConfigDir) -> None:
    """``upstream rm`` signals the Daemon on the configured port; here nothing listens on it,
    so the Daemon in-process notices the removal on its next look instead."""
    config_dir.add_memory_upstream("notes", notes())
    (config_dir.path / "config.toml").write_text(f"version = 1\n[daemon]\nport = {free_port()}\n")
    state_dir = config_dir.state / "upstreams" / "notes"

    async with running_daemon(config_dir) as daemon:
        async with daemon.client("/notes/mcp") as client:
            assert (await client.call_tool("add_note", {"text": "hi"})).data == "hi"

        removed = run_cli(config_dir, "upstream", "rm", "notes", "--yes")
        assert removed.exit_code == 0, removed.output

        await until_unlisted(daemon, "notes")

        config_dir.add_memory_upstream("notes", calculator())

        async with daemon.client("/notes/mcp") as client:
            assert [tool.name for tool in await client.list_tools()] == ["add"]

        code, answer = await daemon.api("GET", f"{UPSTREAMS_PATH}/notes/drift")
        assert code == 200
        assert answer["drift"] is None

        code, answer = await daemon.api("GET", f"{UPSTREAMS_PATH}/notes/catalog")
        assert code == 200
        assert set(answer["catalog"]["tools"]) == {"add"}

        assert state_dir.exists()

    assert "Traceback" not in daemon_log(config_dir)


async def test_unknown_names_answer_not_found(config_dir: ConfigDir) -> None:
    config_dir.add_memory_upstream("calc", calculator())

    async with running_daemon(config_dir) as daemon:
        code, answer = await daemon.api("POST", "/nothing/mcp")
        assert code == 404
        assert answer == {"error": "no Upstream named 'nothing'"}

        code, answer = await daemon.api("POST", "/calc/nothing/mcp")
        assert code == 404
        assert answer == {"error": "no Proxy calc/nothing"}


async def test_paths_beside_a_proxy_answer_as_they_did_with_a_mount_per_proxy(
    config_dir: ConfigDir,
) -> None:
    """The one route that resolves at request time is not a new shape of URL: a trailing
    slash still reaches the Proxy app, and an unknown ``/api`` path is not an Upstream."""
    config_dir.add_memory_upstream("calc", calculator())
    config_dir.add_proxy("calc", "review")

    async with running_daemon(config_dir) as daemon:
        default = await daemon.request("POST", "/calc/mcp/")
        named = await daemon.request("POST", "/calc/review/mcp/")
        assert default.status_code == named.status_code
        assert default.status_code != 404

        unknown_api = await daemon.request("POST", "/api/nope")
        assert unknown_api.status_code == 404
        assert "Upstream" not in unknown_api.text

        # upstream.toml is a TOML file in the directory too, and is not a Proxy
        code, answer = await daemon.api("POST", "/calc/upstream/mcp")
        assert code == 404
        assert answer == {"error": "no Proxy calc/upstream"}
        assert [proxy["name"] for proxy in (await daemon.upstream("calc"))["proxies"]] == [
            "default",
            "review",
        ]


async def test_an_added_upstream_whose_file_cannot_be_read_is_not_served(
    config_dir: ConfigDir,
) -> None:
    config_dir.add_memory_upstream("calc", calculator())

    async with running_daemon(config_dir) as daemon:
        broken = config_dir.path / "upstreams" / "broken"
        broken.mkdir(parents=True)
        (broken / "upstream.toml").write_text("this is not TOML at all [\n")
        (broken / "default.toml").write_text("version = 1\n")

        code, _ = await daemon.api("POST", "/broken/mcp")
        assert code == 404

        status = await daemon.status()
        assert "broken" not in {upstream["name"] for upstream in status["upstreams"]}

    assert "Upstream broken was found but cannot be read" in daemon_log(config_dir)
