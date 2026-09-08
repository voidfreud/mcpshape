"""The call log: every tool call a Proxy serves, recorded to the ring buffer, the management
API, and ``<state>/log/calls.jsonl``, rotated under the global cap (#16).
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import TYPE_CHECKING, Any

from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from tests.support.seam import run_cli, running_daemon, serving_daemon
from tests.test_overrides import curate
from tests.test_proxy_seam import calculator

if TYPE_CHECKING:
    from tests.support.seam import ConfigDir


def failing() -> FastMCP[Any]:
    """An Upstream whose one tool always raises."""
    server = FastMCP("failing")

    def boom() -> str:
        """Always fail."""
        msg = "boom"
        raise ToolError(msg)

    server.tool(boom)
    return server


def long_result() -> FastMCP[Any]:
    """An Upstream whose one tool answers with a 2000-character string."""
    server = FastMCP("longy")

    def big() -> str:
        """Return a long string."""
        return "x" * 2000

    server.tool(big)
    return server


def calls_log_file(config_dir: ConfigDir) -> list[str]:
    return (config_dir.state / "log" / "calls.jsonl").read_text().splitlines()


async def test_a_call_through_a_proxy_is_recorded(config_dir: ConfigDir) -> None:
    config_dir.add_memory_upstream("calc", calculator())

    async with running_daemon(config_dir) as daemon, daemon.client("/calc/mcp") as client:
        result = await client.call_tool("add", {"a": 2, "b": 3})
        assert result.data == 5
        status, answer = await daemon.api("GET", "/api/calls")

    assert status == 200
    calls = answer["calls"]
    assert len(calls) == 1
    record = calls[0]
    assert record["upstream"] == "calc"
    assert record["proxy"] == "default"
    assert record["name"] == "add"
    assert record["exposed"] == "add"
    assert record["arguments"] == {"a": 2, "b": 3}
    assert record["outcome"] == "ok"
    assert record["result"] == "5"
    assert record["result_chars"] == 1
    assert record["duration_ms"] >= 0
    datetime.fromisoformat(record["at"])

    lines = calls_log_file(config_dir)
    assert len(lines) == 1
    assert json.loads(lines[0]) == record


async def test_a_renamed_tool_records_both_names(config_dir: ConfigDir) -> None:
    config_dir.add_memory_upstream("calc", calculator())
    curate(config_dir, '[tools.add]\nname = "sum"\n', upstream="calc")

    async with running_daemon(config_dir) as daemon, daemon.client("/calc/mcp") as client:
        result = await client.call_tool("sum", {"a": 2, "b": 3})
        assert result.data == 5
        _, answer = await daemon.api("GET", "/api/calls")

    record = answer["calls"][0]
    assert record["exposed"] == "sum"
    assert record["name"] == "add"


async def test_an_upstream_error_is_recorded(config_dir: ConfigDir) -> None:
    config_dir.add_memory_upstream("calc", failing())

    async with running_daemon(config_dir) as daemon, daemon.client("/calc/mcp") as client:
        failed = await client.call_tool("boom", {}, raise_on_error=False)
        assert failed.is_error
        _, answer = await daemon.api("GET", "/api/calls")

    record = answer["calls"][0]
    assert record["outcome"] == "error"
    assert "boom" in record["result"]


async def test_a_call_to_an_unhealthy_proxy_is_recorded(config_dir: ConfigDir) -> None:
    config_dir.add_memory_upstream("calc", calculator())

    async with running_daemon(config_dir) as daemon, daemon.client("/calc/mcp") as client:
        await client.list_tools()  # the Catalog exists before the Proxy's file goes bad
        (config_dir.path / "upstreams" / "calc" / "default.py").write_text(
            "def broken(:\n    pass\n"
        )
        await asyncio.sleep(0.01)  # a new mtime, on file systems that count in whole seconds
        failed = await client.call_tool("add", {"a": 1, "b": 2}, raise_on_error=False)
        assert failed.is_error
        _, answer = await daemon.api("GET", "/api/calls")

    record = answer["calls"][0]
    assert record["outcome"] == "error"
    assert "calc/default is unhealthy" in record["result"]


async def test_a_long_result_is_cut_to_500_characters(config_dir: ConfigDir) -> None:
    config_dir.add_memory_upstream("calc", long_result())

    async with running_daemon(config_dir) as daemon, daemon.client("/calc/mcp") as client:
        result = await client.call_tool("big", {})
        assert result.data == "x" * 2000
        _, answer = await daemon.api("GET", "/api/calls")

    record = answer["calls"][0]
    assert len(record["result"]) == 500
    assert record["result_chars"] == 2000


async def test_filtering_by_upstream_and_limit(config_dir: ConfigDir) -> None:
    config_dir.add_memory_upstream("calc", calculator())
    config_dir.add_memory_upstream("calc2", calculator())

    async with running_daemon(config_dir) as daemon:
        async with daemon.client("/calc/mcp") as client:
            await client.call_tool("add", {"a": 1, "b": 1})
        async with daemon.client("/calc2/mcp") as client:
            await client.call_tool("add", {"a": 2, "b": 2})

        _, only_calc2 = await daemon.api("GET", "/api/calls", params={"upstream": "calc2"})
        assert [call["upstream"] for call in only_calc2["calls"]] == ["calc2"]

        _, latest = await daemon.api("GET", "/api/calls", params={"limit": "1"})
        assert len(latest["calls"]) == 1
        assert latest["calls"][0]["upstream"] == "calc2"

        _, both = await daemon.api("GET", "/api/calls")
        assert [call["upstream"] for call in both["calls"]] == ["calc", "calc2"]


async def test_rotation_under_the_global_cap(config_dir: ConfigDir) -> None:
    # A quiet app log, so the call log is what fills the directory.
    (config_dir.path / "config.toml").write_text(
        'version = 1\n[log]\nlevel = "warning"\nmax_bytes = 16384\n'
    )
    config_dir.add_memory_upstream("calc", calculator())

    async with running_daemon(config_dir) as daemon, daemon.client("/calc/mcp") as client:
        for i in range(150):
            await client.call_tool("add", {"a": i, "b": 1})
        _, answer = await daemon.api("GET", "/api/calls", params={"limit": "1000"})

    log_dir = config_dir.state / "log"
    calls_file = log_dir / "calls.jsonl"
    assert calls_file.exists()
    assert calls_file.stat().st_size < 4096
    assert list(log_dir.glob("calls.jsonl.*"))
    total = sum(path.stat().st_size for path in log_dir.iterdir() if path.is_file())
    assert total <= 16384

    last_line = calls_file.read_text().splitlines()[-1]
    last_record = json.loads(last_line)
    assert last_record["arguments"] == {"a": 149, "b": 1}

    ring_calls = answer["calls"]
    assert len(ring_calls) <= 200
    assert ring_calls[-1] == last_record


async def test_daemon_logs_calls_reads_from_the_api_then_from_the_file(
    config_dir: ConfigDir,
) -> None:
    config_dir.add_memory_upstream("calc", calculator())

    async with serving_daemon(config_dir) as url:
        async with Client(f"{url}/calc/mcp") as client:
            await client.call_tool("add", {"a": 2, "b": 3})
        live = await asyncio.to_thread(run_cli, config_dir, "daemon", "logs", "--calls")

    assert "calc/default add" in live.stdout
    assert "-> '5'" in live.stdout

    after = run_cli(config_dir, "daemon", "logs", "--calls")
    assert "calc/default add" in after.stdout
    assert "-> '5'" in after.stdout


def test_daemon_logs_calls_says_none_yet(config_dir: ConfigDir) -> None:
    result = run_cli(config_dir, "daemon", "logs", "--calls")

    assert "No calls yet" in result.stdout
