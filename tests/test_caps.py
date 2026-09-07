"""Caps: a global master, lowered by Upstream, Proxy, and tool, cuts what a Client sees.

Overrides apply first (tests/test_overrides.py); these tests write Caps on top and check what
a Client receives through the seam: a name, description, argument description, or the Proxy's
instructions ends with the Cap marker once cut, a tool's answer ends with a note saying how
many characters were cut, and a Cap that tries to raise what it inherits is a ``doctor`` error.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from fastmcp import FastMCP

from tests.support.seam import run_cli, running_daemon
from tests.test_overrides import curate, synced

if TYPE_CHECKING:
    from tests.support.seam import ConfigDir

MARKER = "…"


def issues() -> FastMCP[Any]:
    """An Upstream with one tool with a long description and argument description."""
    server = FastMCP("issues", instructions="Track issues in a large shared repository, please.")

    def create_issue(title: str) -> str:  # noqa: ARG001  # part of the schema, unused in the body
        """Create an issue in a repository, after checking it is not a duplicate of one already
        open, and notify every subscriber to the repository once it is created."""
        return "x" * 50

    server.tool(create_issue)
    return server


def set_global_caps(config_dir: ConfigDir, **caps: int) -> None:
    keys = "\n".join(f"{key} = {value}" for key, value in caps.items())
    (config_dir.path / "config.toml").write_text(f"version = 1\n[caps]\n{keys}\n")


def set_upstream_caps(config_dir: ConfigDir, upstream: str, **caps: int) -> None:
    path = config_dir.path / "upstreams" / upstream / "upstream.toml"
    text = path.read_text()
    keys = "\n".join(f"{key} = {value}" for key, value in caps.items())
    path.write_text(f"{text}\n[caps]\n{keys}\n")


def set_proxy_caps(config_dir: ConfigDir, upstream: str, proxy: str, **caps: int) -> None:
    keys = "\n".join(f"{key} = {value}" for key, value in caps.items())
    curate(config_dir, f"[caps]\n{keys}\n", upstream, proxy)


# --- names, descriptions, and instructions ------------------------------------------------------


async def test_the_global_master_cap_cuts_a_tool_description_with_the_marker(
    config_dir: ConfigDir,
) -> None:
    set_global_caps(config_dir, tool_description=20)
    await synced(config_dir, issues())

    async with running_daemon(config_dir) as daemon, daemon.client("/issues/mcp") as client:
        tool = next(t for t in await client.list_tools() if t.name == "create_issue")
        assert tool.description is not None
        assert len(tool.description) == 20
        assert tool.description.endswith(MARKER)


async def test_the_global_master_cap_cuts_an_argument_description(config_dir: ConfigDir) -> None:
    set_global_caps(config_dir, argument_description=10)
    await synced(config_dir, issues())
    curate(config_dir, '[tools.create_issue.args.title]\ndescription = "A short one-line title."\n')

    async with running_daemon(config_dir) as daemon, daemon.client("/issues/mcp") as client:
        tool = next(t for t in await client.list_tools() if t.name == "create_issue")
        description = tool.input_schema["properties"]["title"]["description"]
        assert len(description) == 10
        assert description.endswith(MARKER)


async def test_the_global_master_cap_cuts_the_proxys_instructions(config_dir: ConfigDir) -> None:
    set_global_caps(config_dir, instructions=15)
    await synced(config_dir, issues())

    async with running_daemon(config_dir) as daemon, daemon.client("/issues/mcp") as client:
        assert client.instructions is not None
        assert len(client.instructions) == 15
        assert client.instructions.endswith(MARKER)


async def test_a_tool_name_over_its_cap_is_cut_with_the_marker(config_dir: ConfigDir) -> None:
    set_global_caps(config_dir, tool_name=11)
    await synced(config_dir, issues())

    async with running_daemon(config_dir) as daemon, daemon.client("/issues/mcp") as client:
        names = [t.name for t in await client.list_tools()]

    assert names == ["create_iss" + MARKER]
    assert len(names[0]) == 11


async def test_untouched_text_under_its_cap_is_left_alone(config_dir: ConfigDir) -> None:
    set_global_caps(config_dir, tool_description=10_000, tool_name=64, instructions=10_000)
    await synced(config_dir, issues())

    async with running_daemon(config_dir) as daemon, daemon.client("/issues/mcp") as client:
        tool = next(t for t in await client.list_tools() if t.name == "create_issue")
        assert tool.description is not None
        assert MARKER not in tool.description
        assert client.instructions is not None
        assert MARKER not in client.instructions


# --- the chain: Upstream, Proxy, and tool may only lower ----------------------------------------


async def test_upstream_then_proxy_then_tool_each_lower_the_cap_further(
    config_dir: ConfigDir,
) -> None:
    set_global_caps(config_dir, tool_description=200)
    config_dir.add_memory_upstream("issues", issues())
    result = await asyncio.to_thread(run_cli, config_dir, "upstream", "sync", "issues")
    assert result.exit_code == 0, result.output
    set_upstream_caps(config_dir, "issues", tool_description=100)
    set_proxy_caps(config_dir, "issues", "default", tool_description=50)

    async with running_daemon(config_dir) as daemon, daemon.client("/issues/mcp") as client:
        tool = next(t for t in await client.list_tools() if t.name == "create_issue")
        assert tool.description is not None
        assert len(tool.description) == 50

    cap_command = run_cli(
        config_dir, "tool", "cap", "issues/default", "create_issue", "--description", "20"
    )
    assert cap_command.exit_code == 0, cap_command.output

    async with running_daemon(config_dir) as daemon, daemon.client("/issues/mcp") as client:
        tool = next(t for t in await client.list_tools() if t.name == "create_issue")
        assert tool.description is not None
        assert len(tool.description) == 20


def test_an_upstream_cap_higher_than_the_global_master_is_a_doctor_error(
    config_dir: ConfigDir,
) -> None:
    set_global_caps(config_dir, tool_description=100)
    config_dir.add_memory_upstream("issues", issues())
    run_cli(config_dir, "upstream", "sync", "issues")
    set_upstream_caps(config_dir, "issues", tool_description=200)

    doctor = run_cli(config_dir, "doctor")

    assert doctor.exit_code == 1
    assert "higher" in doctor.output
    assert "may only be lowered" in doctor.output


def test_a_proxy_cap_higher_than_the_upstream_cap_is_a_doctor_error(config_dir: ConfigDir) -> None:
    set_global_caps(config_dir, tool_description=100)
    config_dir.add_memory_upstream("issues", issues())
    run_cli(config_dir, "upstream", "sync", "issues")
    set_upstream_caps(config_dir, "issues", tool_description=50)
    set_proxy_caps(config_dir, "issues", "default", tool_description=80)

    doctor = run_cli(config_dir, "doctor")

    assert doctor.exit_code == 1
    assert "may only be lowered" in doctor.output


def test_a_tool_cap_higher_than_the_proxy_cap_is_a_doctor_error(config_dir: ConfigDir) -> None:
    set_global_caps(config_dir, tool_description=50)
    config_dir.add_memory_upstream("issues", issues())
    run_cli(config_dir, "upstream", "sync", "issues")

    cap_command = run_cli(
        config_dir, "tool", "cap", "issues/default", "create_issue", "--description", "100"
    )
    assert cap_command.exit_code == 0, cap_command.output

    doctor = run_cli(config_dir, "doctor")

    assert doctor.exit_code == 1
    assert "create_issue" in doctor.output
    assert "may only be lowered" in doctor.output


async def test_a_raised_upstream_cap_leaves_the_proxy_unhealthy(config_dir: ConfigDir) -> None:
    set_global_caps(config_dir, tool_description=100)
    config_dir.add_memory_upstream("issues", issues())
    result = await asyncio.to_thread(run_cli, config_dir, "upstream", "sync", "issues")
    assert result.exit_code == 0, result.output
    set_upstream_caps(config_dir, "issues", tool_description=200)

    async with running_daemon(config_dir) as daemon:
        async with daemon.client("/issues/mcp") as client:
            names = [t.name for t in await client.list_tools()]
        status = await daemon.status()

    assert names == []  # never refreshed cleanly, so nothing was ever exposed
    proxy_status = status["upstreams"][0]["proxies"][0]
    assert proxy_status["health"] == "unhealthy"
    assert proxy_status["detail"] is not None
    assert "may only be lowered" in proxy_status["detail"]


# --- tool output --------------------------------------------------------------------------------


async def test_a_tool_output_over_its_cap_ends_with_a_note_of_how_much_was_cut(
    config_dir: ConfigDir,
) -> None:
    set_global_caps(config_dir, tool_output=30)
    await synced(config_dir, issues())

    async with running_daemon(config_dir) as daemon, daemon.client("/issues/mcp") as client:
        result = await client.call_tool("create_issue", {"title": "Bug"})

    assert len(result.data) <= 30
    assert "characters cut" in result.data
    assert MARKER not in result.data


async def test_tool_output_under_its_cap_is_untouched(config_dir: ConfigDir) -> None:
    set_global_caps(config_dir, tool_output=10_000)
    await synced(config_dir, issues())

    async with running_daemon(config_dir) as daemon, daemon.client("/issues/mcp") as client:
        result = await client.call_tool("create_issue", {"title": "Bug"})

    assert result.data == "x" * 50


async def test_a_tool_level_output_cap_lowers_what_a_call_answers(config_dir: ConfigDir) -> None:
    set_global_caps(config_dir, tool_output=10_000)
    config_dir.add_memory_upstream("issues", issues())
    result = await asyncio.to_thread(run_cli, config_dir, "upstream", "sync", "issues")
    assert result.exit_code == 0, result.output
    cap_command = run_cli(
        config_dir, "tool", "cap", "issues/default", "create_issue", "--output", "25"
    )
    assert cap_command.exit_code == 0, cap_command.output

    async with running_daemon(config_dir) as daemon, daemon.client("/issues/mcp") as client:
        called = await client.call_tool("create_issue", {"title": "Bug"})

    assert len(called.data) <= 25
    assert "characters cut" in called.data


# --- name Caps versus uniqueness -----------------------------------------------------------------


def two_similar_tools() -> FastMCP[Any]:
    """Two tools whose names share the first several characters."""
    server = FastMCP("issues")

    def create_issue_alpha(title: str) -> str:  # noqa: ARG001  # part of the schema
        """Create an alpha issue."""
        return "alpha"

    def create_issue_beta(title: str) -> str:  # noqa: ARG001  # part of the schema
        """Create a beta issue."""
        return "beta"

    server.tool(create_issue_alpha)
    server.tool(create_issue_beta)
    return server


async def test_names_a_cap_would_cut_to_the_same_result_stay_unique(config_dir: ConfigDir) -> None:
    set_global_caps(config_dir, tool_name=14)
    await synced(config_dir, two_similar_tools())

    async with running_daemon(config_dir) as daemon, daemon.client("/issues/mcp") as client:
        names = sorted(t.name for t in await client.list_tools())

    assert len(names) == 2
    assert len(set(names)) == 2
    assert all(len(name) <= 14 for name in names)
    assert all(name.startswith("create_issue") for name in names)


def test_a_name_cap_too_small_to_keep_names_unique_is_a_doctor_error(
    config_dir: ConfigDir,
) -> None:
    config_dir.add_memory_upstream("issues", two_similar_tools())
    run_cli(config_dir, "upstream", "sync", "issues")
    set_global_caps(config_dir, tool_name=1)

    doctor = run_cli(config_dir, "doctor")

    assert doctor.exit_code == 1
    assert "cannot be cut to a name unique" in doctor.output
