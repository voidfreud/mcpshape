"""The management API under ``/api`` (#16)."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from fastmcp import FastMCP

from mcpshape.api import CALLS_PATH, LOGS_PATH, RELOAD_PATH, STATUS_PATH, UPSTREAMS_PATH
from tests.support.seam import run_cli, running_daemon
from tests.test_proxy_seam import calculator

if TYPE_CHECKING:
    from tests.support.seam import ConfigDir

TOKEN = "s3cret"  # a test fixture, not a real credential


def calculator_with_mul() -> FastMCP[Any]:
    server = FastMCP("calculator")

    def add(a: int, b: int) -> int:
        """Add two integers."""
        return a + b

    def mul(a: int, b: int) -> int:
        """Multiply two integers."""
        return a * b

    server.tool(add)
    server.tool(mul)
    return server


# --- catalog -------------------------------------------------------------------------------


async def test_catalog_answers_the_stored_catalog_and_404s_an_unknown_name(
    config_dir: ConfigDir,
) -> None:
    config_dir.add_memory_upstream("calc", calculator())

    async with running_daemon(config_dir) as daemon:
        status, answer = await daemon.api("GET", f"{UPSTREAMS_PATH}/calc/catalog")
        assert status == 200
        assert "add" in answer["catalog"]["tools"]
        assert answer["catalog"]["scanned_at"]

        status, answer = await daemon.api("GET", f"{UPSTREAMS_PATH}/nope/catalog")
        assert status == 404
        assert "nope" in answer["error"]


async def test_catalog_is_null_for_an_upstream_broken_before_the_daemon_starts(
    config_dir: ConfigDir,
) -> None:
    config_dir.add_memory_upstream("calc", calculator())
    config_dir.break_upstream("calc")

    async with running_daemon(config_dir) as daemon:
        status, answer = await daemon.api("GET", f"{UPSTREAMS_PATH}/calc/catalog")
        assert status == 200
        assert answer == {"catalog": None}

        live = await daemon.status()
        assert [u["name"] for u in live["upstreams"]] == ["calc"]


# --- drift -----------------------------------------------------------------------------------


async def test_drift_through_the_api_until_accepted(config_dir: ConfigDir) -> None:
    config_dir.add_memory_upstream("calc", calculator())

    async with running_daemon(config_dir) as daemon, daemon.client("/calc/mcp") as client:
        status, answer = await daemon.api("GET", f"{UPSTREAMS_PATH}/calc/drift")
        assert (status, answer) == (200, {"drift": None})

        config_dir.restore_upstream("calc", calculator_with_mul())

        status, answer = await daemon.api("POST", f"{UPSTREAMS_PATH}/calc/sync")
        assert status == 200
        assert answer == {
            "first": False,
            "drift": {
                "added": [{"kind": "tool", "name": "mul"}],
                "removed": [],
                "changed": [],
                "instructions_changed": False,
                "summary": "+1",
            },
        }

        status, answer = await daemon.api("GET", f"{UPSTREAMS_PATH}/calc/drift")
        assert status == 200
        assert answer["drift"]["added"] == [{"kind": "tool", "name": "mul"}]

        assert [tool.name for tool in await client.list_tools()] == ["add"]

        accepted = await asyncio.to_thread(
            run_cli, config_dir, "upstream", "sync", "calc", "--accept"
        )
        assert accepted.exit_code == 0, accepted.output

        status, answer = await daemon.api("GET", f"{UPSTREAMS_PATH}/calc/drift")
        assert (status, answer) == (200, {"drift": None})

        status, answer = await daemon.api("GET", f"{UPSTREAMS_PATH}/calc/catalog")
        assert status == 200
        assert "mul" in answer["catalog"]["tools"]


async def test_sync_with_no_drift_answers_none_and_writes_no_drift_file(
    config_dir: ConfigDir,
) -> None:
    config_dir.add_memory_upstream("calc", calculator())

    async with running_daemon(config_dir) as daemon:
        status, answer = await daemon.api("POST", f"{UPSTREAMS_PATH}/calc/sync")
        assert status == 200
        assert answer == {"first": False, "drift": None}
        assert not (config_dir.state / "upstreams" / "calc" / "drift.json").exists()


async def test_sync_over_an_open_connection_leaves_it_connected(config_dir: ConfigDir) -> None:
    config_dir.add_memory_upstream("calc", calculator())

    async with running_daemon(config_dir) as daemon, daemon.client("/calc/mcp") as client:
        assert (await client.call_tool("add", {"a": 2, "b": 3})).data == 5

        status, _answer = await daemon.api("POST", f"{UPSTREAMS_PATH}/calc/sync")
        assert status == 200

        assert await daemon.upstream_state("calc") in ("ready", "idle-pending")


async def test_sync_of_an_unreachable_upstream_answers_502(config_dir: ConfigDir) -> None:
    config_dir.add_memory_upstream("calc", calculator())

    async with running_daemon(config_dir) as daemon:
        config_dir.break_upstream("calc")

        status, answer = await daemon.api("POST", f"{UPSTREAMS_PATH}/calc/sync")
        assert status == 502
        assert "calc" in answer["error"]


# --- oauth -----------------------------------------------------------------------------------


async def test_oauth_routes_refuse_a_non_oauth_upstream_and_an_unknown_one(
    config_dir: ConfigDir,
) -> None:
    config_dir.add_memory_upstream("calc", calculator())

    async with running_daemon(config_dir) as daemon:
        status, answer = await daemon.api("GET", f"{UPSTREAMS_PATH}/calc/oauth")
        assert status == 400
        assert 'auth = "oauth"' in answer["error"]

        status, answer = await daemon.api("POST", f"{UPSTREAMS_PATH}/calc/oauth")
        assert status == 400
        assert 'auth = "oauth"' in answer["error"]

        status, answer = await daemon.api("GET", f"{UPSTREAMS_PATH}/nope/oauth")
        assert status == 404
        assert "nope" in answer["error"]


async def test_connect_answers_the_connection_state_and_404s_an_unknown_name(
    config_dir: ConfigDir,
) -> None:
    config_dir.add_memory_upstream("calc", calculator())

    async with running_daemon(config_dir) as daemon:
        status, answer = await daemon.api("POST", f"{UPSTREAMS_PATH}/calc/connect")
        assert (status, answer) == (200, {"state": "cold"}), "a cold Upstream is left alone"

        status, answer = await daemon.api("POST", f"{UPSTREAMS_PATH}/nope/connect")
        assert status == 404
        assert "nope" in answer["error"]


# --- calls and logs ----------------------------------------------------------------------------


async def test_bad_calls_and_logs_query_parameters_answer_400(config_dir: ConfigDir) -> None:
    config_dir.add_memory_upstream("calc", calculator())

    async with running_daemon(config_dir) as daemon:
        status, _answer = await daemon.api("GET", CALLS_PATH, params={"limit": "0"})
        assert status == 400

        status, _answer = await daemon.api("GET", CALLS_PATH, params={"limit": "x"})
        assert status == 400

        status, _answer = await daemon.api("GET", LOGS_PATH, params={"lines": "-1"})
        assert status == 400


# --- bearer token ------------------------------------------------------------------------------


async def test_bearer_token_gates_every_api_route(config_dir: ConfigDir) -> None:
    config_dir.add_memory_upstream("calc", calculator())

    async with running_daemon(config_dir, token=TOKEN) as daemon:
        # Every route the API has, shutdown aside: the answer it gives with the token is the
        # answer it gives at all, so a 400 for the OAuth flow of a plain Upstream counts.
        routes = (
            ("GET", STATUS_PATH),
            ("POST", RELOAD_PATH),
            ("GET", CALLS_PATH),
            ("GET", LOGS_PATH),
            ("GET", f"{UPSTREAMS_PATH}/calc/catalog"),
            ("GET", f"{UPSTREAMS_PATH}/calc/drift"),
            ("POST", f"{UPSTREAMS_PATH}/calc/sync"),
            ("GET", f"{UPSTREAMS_PATH}/calc/oauth"),
            ("POST", f"{UPSTREAMS_PATH}/calc/oauth"),
            ("POST", f"{UPSTREAMS_PATH}/calc/connect"),
        )
        for method, path in routes:
            status, answer = await daemon.api(method, path)
            assert (status, answer) == (401, {"error": "a bearer token is required"}), path

            status, _answer = await daemon.api(
                method, path, headers={"Authorization": f"Bearer {TOKEN}"}
            )
            assert status in (200, 400), path


# --- reload --------------------------------------------------------------------------------


async def test_reload_answers_the_live_state(config_dir: ConfigDir) -> None:
    config_dir.add_memory_upstream("calc", calculator())

    async with running_daemon(config_dir) as daemon:
        status, answer = await daemon.api("POST", RELOAD_PATH)
        assert status == 200
        upstream = next(u for u in answer["upstreams"] if u["name"] == "calc")
        proxy = next(p for p in upstream["proxies"] if p["name"] == "default")
        assert proxy["health"] == "ok"
