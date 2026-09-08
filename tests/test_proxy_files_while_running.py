"""A Proxy file removed, or given a port override, while the Daemon runs (#68, #70).

A Proxy whose file is gone is let go on the next look at its Upstream: its URL answers not
found naming it, it is no longer listed, and its app is closed, while the Upstream's
connection and its other Proxies are untouched; a file back under the same name is a new
Proxy. A ``port`` set, changed, or removed in a Proxy file is a listener started, moved, or
closed after the next look at the file, or on ``daemon reload``, and one that cannot be
bound is an unhealthy Proxy with the reason, its path served as before.
"""

from __future__ import annotations

import asyncio
import socket
from typing import TYPE_CHECKING, Any

import httpx2
from fastmcp import Client

from mcpshape.api import RELOAD_PATH, STATUS_PATH
from tests.support.seam import free_port, run_cli, running_daemon, serving_daemon, until
from tests.test_daemon_ports import listening
from tests.test_proxy_seam import calculator

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from tests.support.seam import ConfigDir, RunningDaemon

CONNECTED = ("ready", "idle-pending")


def proxy_file(config_dir: ConfigDir, upstream: str, proxy: str, port: int | None) -> None:
    """Rewrite the Proxy file with, or without, a port override, as a user editing it would."""
    body = "version = 1\n" + (f"port = {port}\n" if port is not None else "")
    (config_dir.path / "upstreams" / upstream / f"{proxy}.toml").write_text(body)


async def live_at(url: str) -> dict[str, Any]:
    """``/api/status`` of a Daemon served on a socket, read from a thread so it can answer."""
    return await asyncio.to_thread(lambda: httpx2.get(f"{url}{STATUS_PATH}").json())


async def proxy_state(url: str, upstream: str, proxy: str) -> dict[str, Any]:
    live = await live_at(url)
    found = next(u for u in live["upstreams"] if u["name"] == upstream)
    return next(p for p in found["proxies"] if p["name"] == proxy)


async def until_listening(port: int, *, up: bool = True) -> None:
    what = f"a listener on port {port}" if up else f"port {port} closing"
    await until(lambda: listening(port) == up, what)


# --- a removed Proxy (#68) ------------------------------------------------------------------


async def test_a_proxy_removed_while_the_daemon_runs_is_let_go(config_dir: ConfigDir) -> None:
    config_dir.add_memory_upstream("calc", calculator())
    config_dir.add_proxy("calc", "review")

    async with running_daemon(config_dir) as daemon:
        async with daemon.client("/calc/review/mcp") as client:
            assert (await client.call_tool("add", {"a": 2, "b": 3})).data == 5

        removed = run_cli(config_dir, "proxy", "rm", "calc/review", "--yes")
        assert removed.exit_code == 0, removed.output

        calc = await daemon.upstream("calc")
        assert [proxy["name"] for proxy in calc["proxies"]] == ["default"]
        assert calc["state"] in CONNECTED, "the Upstream's connection is untouched"

        code, answer = await daemon.api("POST", "/calc/review/mcp")
        assert code == 404
        assert answer == {"error": "no Proxy calc/review"}

        async with daemon.client("/calc/mcp") as client:
            assert (await client.call_tool("add", {"a": 1, "b": 1})).data == 2


async def test_a_proxy_removed_is_let_go_on_its_own_next_request(config_dir: ConfigDir) -> None:
    """The request to the removed Proxy itself is a look at its Upstream, and enough."""
    config_dir.add_memory_upstream("calc", calculator())
    config_dir.add_proxy("calc", "review")

    async with running_daemon(config_dir) as daemon:
        async with daemon.client("/calc/review/mcp") as client:
            assert [tool.name for tool in await client.list_tools()] == ["add"]

        (config_dir.path / "upstreams" / "calc" / "review.toml").unlink()

        code, answer = await daemon.api("POST", "/calc/review/mcp")
        assert code == 404
        assert answer == {"error": "no Proxy calc/review"}
        assert [proxy["name"] for proxy in (await daemon.upstream("calc"))["proxies"]] == [
            "default"
        ]


async def test_a_proxy_removed_is_let_go_on_daemon_reload(config_dir: ConfigDir) -> None:
    config_dir.add_memory_upstream("calc", calculator())
    config_dir.add_proxy("calc", "review")

    async with running_daemon(config_dir) as daemon:
        async with daemon.client("/calc/review/mcp") as client:
            assert [tool.name for tool in await client.list_tools()] == ["add"]

        (config_dir.path / "upstreams" / "calc" / "review.toml").unlink()

        code, answer = await daemon.api("POST", RELOAD_PATH)
        assert code == 200
        calc = next(u for u in answer["upstreams"] if u["name"] == "calc")
        assert [proxy["name"] for proxy in calc["proxies"]] == ["default"]


async def test_a_proxy_removed_and_re_added_under_its_name_is_a_new_one(
    config_dir: ConfigDir,
) -> None:
    config_dir.add_memory_upstream("calc", calculator())
    config_dir.add_proxy("calc", "review")

    async with running_daemon(config_dir) as daemon:
        async with daemon.client("/calc/review/mcp") as client:
            assert [tool.name for tool in await client.list_tools()] == ["add"]

        removed = run_cli(config_dir, "proxy", "rm", "calc/review", "--yes")
        assert removed.exit_code == 0, removed.output
        code, _ = await daemon.api("POST", "/calc/review/mcp")
        assert code == 404

        config_dir.add_proxy("calc", "review")

        async with daemon.client("/calc/review/mcp") as client:
            assert (await client.call_tool("add", {"a": 4, "b": 5})).data == 9
        assert (await proxy_state_of(daemon, "calc", "review"))["health"] == "ok"


async def proxy_state_of(daemon: RunningDaemon, upstream: str, proxy: str) -> dict[str, Any]:
    found = await daemon.upstream(upstream)
    return next(p for p in found["proxies"] if p["name"] == proxy)


# --- a port override while the Daemon runs (#70) -------------------------------------------


async def test_a_port_set_while_the_daemon_runs_is_listened_on_after_the_next_look(
    config_dir: ConfigDir,
) -> None:
    config_dir.add_memory_upstream("calc", calculator())
    port = free_port()

    async with serving_daemon(config_dir) as url:
        assert not listening(port)

        proxy_file(config_dir, "calc", "default", port)
        await live_at(url)  # the next look at the file

        await until_listening(port)
        async with Client(f"http://127.0.0.1:{port}/mcp") as client:
            assert (await client.call_tool("add", {"a": 2, "b": 3})).data == 5
        assert (await proxy_state(url, "calc", "default"))["health"] == "ok"


async def test_a_port_changed_moves_the_listener_and_one_removed_closes_it(
    config_dir: ConfigDir,
) -> None:
    config_dir.add_memory_upstream("calc", calculator())
    first, second = free_port(), free_port()
    proxy_file(config_dir, "calc", "default", first)

    async with serving_daemon(config_dir) as url:
        await until_listening(first)

        proxy_file(config_dir, "calc", "default", second)
        await live_at(url)
        await until_listening(second)
        await until_listening(first, up=False)
        async with Client(f"http://127.0.0.1:{second}/mcp") as client:
            assert (await client.call_tool("add", {"a": 1, "b": 1})).data == 2

        proxy_file(config_dir, "calc", "default", None)
        await live_at(url)
        await until_listening(second, up=False)
        async with Client(f"{url}/calc/mcp") as client:
            assert (await client.call_tool("add", {"a": 1, "b": 2})).data == 3


async def test_a_proxy_added_while_the_daemon_runs_with_a_port_is_served_on_it(
    config_dir: ConfigDir,
) -> None:
    config_dir.add_memory_upstream("calc", calculator())
    port = free_port()

    async with serving_daemon(config_dir) as url:
        config_dir.add_proxy("calc", "review")
        proxy_file(config_dir, "calc", "review", port)

        async with Client(f"{url}/calc/review/mcp") as client:
            assert [tool.name for tool in await client.list_tools()] == ["add"]

        await until_listening(port)
        async with Client(f"http://127.0.0.1:{port}/mcp") as client:
            assert (await client.call_tool("add", {"a": 3, "b": 4})).data == 7


async def test_a_removed_proxy_with_a_port_closes_its_listener(config_dir: ConfigDir) -> None:
    config_dir.add_memory_upstream("calc", calculator())
    config_dir.add_proxy("calc", "review")
    port = free_port()
    proxy_file(config_dir, "calc", "review", port)

    async with serving_daemon(config_dir) as url:
        await until_listening(port)

        removed = await asyncio.to_thread(
            run_cli, config_dir, "proxy", "rm", "calc/review", "--yes"
        )
        assert removed.exit_code == 0, removed.output
        await live_at(url)

        await until_listening(port, up=False)


async def test_a_port_that_cannot_be_bound_is_an_unhealthy_proxy_with_its_path_still_served(
    config_dir: ConfigDir,
) -> None:
    config_dir.add_memory_upstream("calc", calculator())
    taken = socket.socket()
    taken.bind(("127.0.0.1", 0))
    taken.listen()
    port = taken.getsockname()[1]

    async with serving_daemon(config_dir) as url:
        proxy_file(config_dir, "calc", "default", port)
        await live_at(url)

        async def unhealthy() -> bool:
            state = await proxy_state(url, "calc", "default")
            return state["health"] == "unhealthy"

        await until_async(unhealthy, "the Proxy reporting the port it cannot bind")
        state = await proxy_state(url, "calc", "default")
        assert f"port {port}" in state["detail"]
        assert "cannot be bound" in state["detail"]
        async with Client(f"{url}/calc/mcp") as client:
            assert (await client.call_tool("add", {"a": 2, "b": 3})).data == 5

        taken.close()
        await asyncio.to_thread(lambda: httpx2.post(f"{url}{RELOAD_PATH}"))

        await until_listening(port)
        assert (await proxy_state(url, "calc", "default"))["health"] == "ok"


async def until_async(
    ready: Callable[[], Awaitable[bool]], what: str, patience: float = 5.0
) -> None:
    """``until`` for something only an HTTP answer can say."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + patience
    while not await ready():
        if loop.time() > deadline:
            msg = f"{what} never happened"
            raise AssertionError(msg)
        await asyncio.sleep(0.02)
