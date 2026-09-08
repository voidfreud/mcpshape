"""A running Daemon notices a change to, or the removal of, an Upstream file.

The Upstream file is watched the way a Proxy's own files are (#10): its stamp is checked when
a request comes in and on ``daemon reload``, with no watcher between requests. A Cap edit is
in force on the next request, a transport or lifecycle edit reconnects the shared connection
under the new settings, a file that cannot be read leaves the connection where it is and marks
the Upstream's Proxies unhealthy, and a file that is gone retires the Upstream: its Proxies
answer not found, its connection is let go, and nothing is ever written to its state directory
again (#46, #62).
"""

from __future__ import annotations

import asyncio
import json
import shutil
import time
from typing import TYPE_CHECKING, Any

import httpx2
from fastmcp import Client

from mcpshape.api import RELOAD_PATH, STATUS_PATH, UPSTREAMS_PATH
from tests.support import upstreams
from tests.support.clock import FakeClock, settle
from tests.support.seam import RunningDaemon, run_cli, running_daemon, serving_daemon, until
from tests.test_caps import MARKER, issues, set_upstream_caps
from tests.test_catalog_drift import cli, drift_file, grow, notes
from tests.test_proxy_seam import calculator

if TYPE_CHECKING:
    from pathlib import Path

    from tests.support.seam import ConfigDir

LONG_IDLE = 600
SHORT_IDLE = 60
PAST_SHORT_IDLE = 61.0
"""Seconds to move the clock on by: past the edited ``idle_timeout``, not the one before it."""

PAST_THE_BACKOFF = 5.0
"""Seconds to move the clock on by, comfortably past the first retry delay."""

DESCRIPTION_CAP = 20
CONNECTED = ("ready", "idle-pending")


def upstream_file(config_dir: ConfigDir, upstream: str) -> Path:
    return config_dir.path / "upstreams" / upstream / "upstream.toml"


def set_lifecycle(config_dir: ConfigDir, upstream: str, **settings: object) -> None:
    """Rewrite the Upstream file's ``[lifecycle]`` block, as a user editing the file would."""
    path = upstream_file(config_dir, upstream)
    kept = path.read_text().split("\n[lifecycle]")[0].rstrip("\n")
    keys = "".join(f"{key} = {json.dumps(value)}\n" for key, value in settings.items())
    path.write_text(f"{kept}\n\n[lifecycle]\n{keys}")


def set_target(config_dir: ConfigDir, upstream: str, target: str) -> None:
    """Point the Upstream file at another in-memory server, as an edited transport would."""
    path = upstream_file(config_dir, upstream)
    kept = [line for line in path.read_text().splitlines() if not line.startswith("target =")]
    path.write_text("\n".join([*kept, f"target = {json.dumps(target)}", ""]))


async def until_unlisted(daemon: RunningDaemon, name: str, patience: float = 5.0) -> None:
    """Wait until the Daemon no longer lists ``name`` at all, or say it never stopped."""
    deadline = time.monotonic() + patience
    while any(found["name"] == name for found in (await daemon.status())["upstreams"]):
        if time.monotonic() > deadline:
            msg = f"the Daemon never stopped listing Upstream {name}"
            raise AssertionError(msg)
        await asyncio.sleep(0.01)


def daemon_log(config_dir: ConfigDir) -> str:
    return (config_dir.state / "log" / "daemon.log").read_text()


def described(tools: list[Any], name: str) -> str:
    tool = next(found for found in tools if found.name == name)
    assert tool.description is not None
    return str(tool.description)


# --- an edit while the Daemon runs ---------------------------------------------------------


async def test_an_upstream_cap_edited_while_the_daemon_runs_is_in_force_on_the_next_request(
    config_dir: ConfigDir,
) -> None:
    config_dir.add_memory_upstream("issues", issues())
    await cli(config_dir, "upstream", "sync", "issues")

    async with running_daemon(config_dir) as daemon, daemon.client("/issues/mcp") as client:
        assert MARKER not in described(await client.list_tools(), "create_issue")
        assert (await client.call_tool("create_issue", {"title": "x"})).data == "x" * 50

        set_upstream_caps(config_dir, "issues", tool_description=DESCRIPTION_CAP)

        description = described(await client.list_tools(), "create_issue")
        assert len(description) == DESCRIPTION_CAP
        assert description.endswith(MARKER)
        assert await daemon.upstream_state("issues") in CONNECTED, "a Cap edit is no reconnect"


async def test_reload_re_reads_the_upstream_files_too(config_dir: ConfigDir) -> None:
    """``daemon reload`` re-reads every Upstream file too, changed or not (#46)."""
    config_dir.add_memory_upstream("issues", issues())
    await cli(config_dir, "upstream", "sync", "issues")

    async with running_daemon(config_dir) as daemon, daemon.client("/issues/mcp") as client:
        assert MARKER not in described(await client.list_tools(), "create_issue")

        set_upstream_caps(config_dir, "issues", tool_description=DESCRIPTION_CAP)
        status, _ = await daemon.api("POST", RELOAD_PATH)
        assert status == 200

        assert described(await client.list_tools(), "create_issue").endswith(MARKER)


async def test_a_lifecycle_edit_reconnects_the_upstream_under_the_new_settings(
    config_dir: ConfigDir,
) -> None:
    clock = FakeClock()
    server = notes()
    config_dir.add_memory_upstream("notes", server, {"idle_timeout": LONG_IDLE})
    await cli(config_dir, "upstream", "sync", "notes")

    async with running_daemon(config_dir, clock) as daemon, daemon.client("/notes/mcp") as client:
        assert (await client.call_tool("add_note", {"text": "hi"})).data == "hi"
        assert await daemon.upstream_state("notes") in ("ready", "idle-pending")

        grow(server)
        set_lifecycle(config_dir, "notes", idle_timeout=SHORT_IDLE)
        await daemon.status()  # the look that notices the edit
        assert await daemon.upstream_state("notes") == "cold"

        assert (await client.call_tool("add_note", {"text": "hi"})).data == "hi"
        assert await daemon.awaiting_state("notes", "ready", "idle-pending")
        await until(
            lambda: drift_file(config_dir, "notes") is not None,
            "the rescan the reconnect under the new settings triggers",
        )

        await clock.advance(PAST_SHORT_IDLE)
        assert await daemon.awaiting_state("notes", "cold") == "cold"


async def test_a_transport_edit_reconnects_to_what_the_file_now_names(
    config_dir: ConfigDir,
) -> None:
    clock = FakeClock()
    config_dir.add_memory_upstream("x", calculator())
    config_dir.add_memory_upstream("other", notes())
    await cli(config_dir, "upstream", "sync")

    async with running_daemon(config_dir, clock) as daemon, daemon.client("/x/mcp") as client:
        assert (await client.call_tool("add", {"a": 2, "b": 3})).data == 5

        set_target(config_dir, "x", f"{upstreams.MODULE_PATH}:other")
        await daemon.status()  # the look that notices the edit
        assert await daemon.upstream_state("x") == "cold"

        await client.call_tool("add", {"a": 2, "b": 3}, raise_on_error=False)  # wakes it again
        assert await daemon.awaiting_state("x", "ready", "idle-pending")

        await until(
            lambda: drift_file(config_dir, "x") is not None,
            "the rescan the reconnect to the new target triggers",
        )
        _, answer = await daemon.api("GET", f"{UPSTREAMS_PATH}/x/drift")
        assert {item["name"] for item in answer["drift"]["removed"]} == {"add"}
        assert "add_note" in {item["name"] for item in answer["drift"]["added"]}


async def test_an_upstream_file_that_cannot_be_read_marks_its_proxies_unhealthy_and_keeps_the_connection(  # noqa: E501
    config_dir: ConfigDir,
) -> None:
    config_dir.add_memory_upstream("issues", issues())
    await cli(config_dir, "upstream", "sync", "issues")
    path = upstream_file(config_dir, "issues")
    good = path.read_text()

    async with running_daemon(config_dir) as daemon, daemon.client("/issues/mcp") as client:
        assert [tool.name for tool in await client.list_tools()] == ["create_issue"]

        path.write_text("this is not TOML at all [\n")

        proxy = (await daemon.upstream("issues"))["proxies"][0]
        assert proxy["health"] == "unhealthy"
        assert "upstream.toml" in proxy["detail"]
        failed = await client.call_tool("create_issue", {"title": "x"}, raise_on_error=False)
        assert failed.is_error
        assert [tool.name for tool in await client.list_tools()] == ["create_issue"]

        path.write_text(good)
        assert (await daemon.upstream("issues"))["proxies"][0]["health"] == "ok"
        assert (await client.call_tool("create_issue", {"title": "x"})).data == "x" * 50


async def test_an_edit_to_warm_connects_the_upstream_on_the_next_look(
    config_dir: ConfigDir,
) -> None:
    config_dir.add_memory_upstream("calc", calculator())

    async with running_daemon(config_dir) as daemon:
        assert await daemon.upstream_state("calc") == "cold"

        set_lifecycle(config_dir, "calc", warm=True)
        status, _ = await daemon.api("POST", RELOAD_PATH)
        assert status == 200

        assert await daemon.awaiting_state("calc", "ready") == "ready"


# --- a removed Upstream --------------------------------------------------------------------


async def test_upstream_rm_retires_the_upstream_in_a_running_daemon(
    config_dir: ConfigDir,
) -> None:
    """#62: ``upstream rm`` signals the Daemon, which stops serving what is no longer there."""
    clock = FakeClock()
    config_dir.add_memory_upstream("notes", notes())
    await cli(config_dir, "upstream", "sync", "notes")
    state_dir = config_dir.state / "upstreams" / "notes"

    async with serving_daemon(config_dir, clock) as url:
        async with Client(f"{url}/notes/mcp") as client:
            assert (await client.call_tool("add_note", {"text": "hi"})).data == "hi"

        removed = await asyncio.to_thread(run_cli, config_dir, "upstream", "rm", "notes", "--yes")
        assert removed.exit_code == 0, removed.output

        live = await asyncio.to_thread(lambda: httpx2.get(f"{url}{STATUS_PATH}").json())
        assert [upstream["name"] for upstream in live["upstreams"]] == []

        answer = await asyncio.to_thread(lambda: httpx2.post(f"{url}/notes/mcp"))
        assert answer.status_code == 404
        assert answer.json() == {"error": "no Upstream named 'notes'"}

        assert not state_dir.exists()
        await clock.advance(PAST_THE_BACKOFF)  # nothing left to reconnect, so nothing rescans
        await settle()
        assert not state_dir.exists()


async def test_a_removed_upstream_is_noticed_by_the_reconnect_that_would_have_rescanned(
    config_dir: ConfigDir,
) -> None:
    """#62: a rescan cannot tell a removed Upstream from a new one, so the Daemon tells it.

    The Upstream is removed by hand, as an ``rm -rf`` would, so nothing signals the Daemon:
    the reconnect that comes out of the backoff is what finds the Upstream file gone, drops
    what its rescan saw rather than writing a state directory back, and retires the Upstream.
    The Upstream is warm, since only a warm one reconnects with nobody calling (#64), and is
    broken before the Daemon starts, so its first connect is the failure the backoff follows.
    """
    clock = FakeClock()
    server = notes()
    config_dir.add_memory_upstream("notes", server, lifecycle={"warm": True})
    await cli(config_dir, "upstream", "sync", "notes")
    state_dir = config_dir.state / "upstreams" / "notes"
    config_dir.break_upstream("notes")

    async with running_daemon(config_dir, clock) as daemon:
        assert await daemon.awaiting_state("notes", "unavailable") == "unavailable"

        shutil.rmtree(config_dir.path / "upstreams" / "notes")
        shutil.rmtree(state_dir)
        config_dir.restore_upstream("notes")

        await clock.advance(PAST_THE_BACKOFF)
        await until(
            lambda: "dropping what its reconnect saw" in daemon_log(config_dir),
            "the reconnect's rescan finding the Upstream file gone",
        )
        await settle()
        assert not state_dir.exists()

        await until_unlisted(daemon, "notes")
        assert not state_dir.exists()

    # the start-up scan of a broken Upstream logs its traceback as it always has; the
    # retirement that follows the reconnect's rescan must not
    _, retiring, after_retiring = daemon_log(config_dir).partition(
        "dropping what its reconnect saw"
    )
    assert retiring, "the retirement was never logged"
    assert "Traceback" not in after_retiring
