"""The Catalog is scanned, persisted, served, and changed only through reviewed Drift."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

from fastmcp import FastMCP
from mcp_types import TextContent, TextResourceContents

from tests.support.seam import run_cli, running_daemon

if TYPE_CHECKING:
    from tests.support.seam import CliResult, ConfigDir


def notes() -> FastMCP[Any]:
    """An Upstream with one of everything and server instructions."""
    server = FastMCP("notes", instructions="Keep notes short.")

    def add_note(text: str) -> str:
        """Add a note."""
        return text

    def greeting(name: str) -> str:
        """Greet someone."""
        return f"Hello {name}"

    def note(id: str) -> str:  # noqa: A002
        return f"note {id}"

    server.tool(add_note)
    server.resource("notes://all")(lambda: "no notes")
    server.resource("notes://{id}")(note)
    server.prompt(greeting)
    return server


def grow(server: FastMCP[Any]) -> None:
    """The Upstream now advertises a tool it did not have at the last scan."""

    def delete_note(id: int) -> None:  # noqa: A002
        """Delete a note."""

    server.tool(delete_note)


def catalog_file(config_dir: ConfigDir, upstream: str) -> dict[str, Any]:
    return json.loads((config_dir.state / "upstreams" / upstream / "catalog.json").read_text())


def drift_file(config_dir: ConfigDir, upstream: str) -> dict[str, Any] | None:
    path = config_dir.state / "upstreams" / upstream / "drift.json"
    return json.loads(path.read_text()) if path.exists() else None


async def cli(config_dir: ConfigDir, *args: str) -> CliResult:
    """Run the CLI from an async test, in its own thread as a user's shell would."""
    result = await asyncio.to_thread(run_cli, config_dir, *args)
    assert result.exit_code == 0, result.output
    return result


# --- scanning ----------------------------------------------------------------------------------


def test_sync_persists_the_catalog_with_a_scan_timestamp(config_dir: ConfigDir) -> None:
    config_dir.add_memory_upstream("notes", notes())

    result = run_cli(config_dir, "upstream", "sync", "notes")

    assert result.exit_code == 0, result.output
    stored = catalog_file(config_dir, "notes")
    assert stored["scanned_at"]
    assert stored["instructions"] == "Keep notes short."
    assert list(stored["tools"]) == ["add_note"]
    assert stored["tools"]["add_note"]["inputSchema"]["properties"]["text"]["type"] == "string"
    assert list(stored["resources"]) == ["notes://all"]
    assert list(stored["resource_templates"]) == ["notes://{id}"]
    assert list(stored["prompts"]) == ["greeting"]
    assert "1 tool" in result.output
    assert drift_file(config_dir, "notes") is None


def test_sync_without_a_name_scans_every_upstream(config_dir: ConfigDir) -> None:
    config_dir.add_memory_upstream("notes", notes())
    config_dir.add_memory_upstream("other", notes())

    result = run_cli(config_dir, "upstream", "sync")

    assert result.exit_code == 0, result.output
    assert catalog_file(config_dir, "notes")["tools"]
    assert catalog_file(config_dir, "other")["tools"]


def test_sync_of_an_unknown_upstream_fails(config_dir: ConfigDir) -> None:
    result = run_cli(config_dir, "upstream", "sync", "nope")

    assert result.exit_code == 1
    assert "nope" in result.output


def test_sync_of_an_unreachable_upstream_fails_and_keeps_the_catalog(config_dir: ConfigDir) -> None:
    config_dir.add_memory_upstream("notes", notes())
    run_cli(config_dir, "upstream", "sync", "notes")
    (config_dir.path / "upstreams" / "notes" / "upstream.toml").write_text(
        'version = 1\ntransport = "memory"\ntarget = "tests.support.upstreams:gone"\n'
    )

    result = run_cli(config_dir, "upstream", "sync", "notes")

    assert result.exit_code == 1
    assert "gone" in result.output
    assert catalog_file(config_dir, "notes")["tools"]


async def test_daemon_start_scans_an_upstream_with_no_catalog(config_dir: ConfigDir) -> None:
    config_dir.add_memory_upstream("notes", notes())

    async with running_daemon(config_dir) as daemon, daemon.client("/notes/mcp") as client:
        assert [tool.name for tool in await client.list_tools()] == ["add_note"]
    assert list(catalog_file(config_dir, "notes")["tools"]) == ["add_note"]


# --- serving from the stored Catalog -----------------------------------------------------------


async def test_lists_and_initialize_are_served_from_the_stored_catalog(
    config_dir: ConfigDir,
) -> None:
    server = notes()
    config_dir.add_memory_upstream("notes", server)
    await cli(config_dir, "upstream", "sync", "notes")
    grow(server)

    async with running_daemon(config_dir) as daemon, daemon.client("/notes/mcp") as client:
        assert [tool.name for tool in await client.list_tools()] == ["add_note"]
        assert [str(r.uri) for r in await client.list_resources()] == ["notes://all"]
        assert [t.uri_template for t in await client.list_resource_templates()] == ["notes://{id}"]
        assert [p.name for p in await client.list_prompts()] == ["greeting"]
        assert client.instructions == "Keep notes short."

        assert (await client.call_tool("add_note", {"text": "hi"})).data == "hi"
        contents = (await client.read_resource("notes://7"))[0]
        assert isinstance(contents, TextResourceContents)
        assert contents.text == "note 7"
        content = (await client.get_prompt("greeting", {"name": "Ann"})).messages[0].content
        assert isinstance(content, TextContent)
        assert content.text == "Hello Ann"


# --- Drift -------------------------------------------------------------------------------------


async def test_daemon_start_records_drift_without_touching_the_catalog(
    config_dir: ConfigDir,
) -> None:
    server = notes()
    config_dir.add_memory_upstream("notes", server)
    await cli(config_dir, "upstream", "sync", "notes")
    before = catalog_file(config_dir, "notes")
    grow(server)

    async with running_daemon(config_dir):
        pass

    assert catalog_file(config_dir, "notes")["tools"] == before["tools"]
    pending = drift_file(config_dir, "notes")
    assert pending is not None
    assert sorted(pending["tools"]) == ["add_note", "delete_note"]


def test_sync_shows_the_diff_and_leaves_the_catalog_alone(config_dir: ConfigDir) -> None:
    server = notes()
    config_dir.add_memory_upstream("notes", server)
    run_cli(config_dir, "upstream", "sync", "notes")
    grow(server)
    server.local_provider.remove_prompt("greeting")
    server.instructions = "Keep notes very short."

    result = run_cli(config_dir, "upstream", "sync", "notes")

    assert result.exit_code == 0, result.output
    assert "+ tool delete_note" in result.output
    assert "- prompt greeting" in result.output
    assert "instructions" in result.output
    assert "--accept" in result.output
    assert list(catalog_file(config_dir, "notes")["tools"]) == ["add_note"]
    assert drift_file(config_dir, "notes") is not None


def test_sync_reports_a_changed_definition(config_dir: ConfigDir) -> None:
    server = notes()
    config_dir.add_memory_upstream("notes", server)
    run_cli(config_dir, "upstream", "sync", "notes")
    server.local_provider.remove_tool("add_note")

    def add_note(text: str, *, pinned: bool = False) -> str:
        """Add a note, pinned or not."""
        return f"{'pinned ' if pinned else ''}{text}"

    server.tool(add_note)

    result = run_cli(config_dir, "upstream", "sync", "notes")

    assert result.exit_code == 0, result.output
    assert "~ tool add_note" in result.output


def test_every_command_prints_one_drift_notice_until_reviewed(config_dir: ConfigDir) -> None:
    server = notes()
    config_dir.add_memory_upstream("notes", server)
    run_cli(config_dir, "upstream", "sync", "notes")
    assert "Drift" not in run_cli(config_dir, "ls").output
    grow(server)
    run_cli(config_dir, "upstream", "sync", "notes")

    for args in (("ls",), ("proxy", "ls"), ("doctor",)):
        result = run_cli(config_dir, *args)
        lines = [line for line in result.output.splitlines() if "Drift" in line]
        assert len(lines) == 1, result.output
        assert "notes" in lines[0]
        assert "upstream sync notes" in lines[0]

    accepted = run_cli(config_dir, "upstream", "sync", "notes", "--accept")
    assert accepted.exit_code == 0, accepted.output
    assert "Drift" not in run_cli(config_dir, "ls").output


def test_sync_of_one_upstream_still_notices_drift_in_another(config_dir: ConfigDir) -> None:
    notes_server, other_server = notes(), notes()
    config_dir.add_memory_upstream("notes", notes_server)
    config_dir.add_memory_upstream("other", other_server)
    run_cli(config_dir, "upstream", "sync")
    grow(notes_server)
    grow(other_server)
    run_cli(config_dir, "upstream", "sync")

    result = run_cli(config_dir, "upstream", "sync", "notes")

    assert result.exit_code == 0, result.output
    notice = [line for line in result.output.splitlines() if "Review with:" in line]
    assert notice == ["Drift in other (+1). Review with: mcpshape upstream sync other"]


def test_drift_resolves_itself_when_the_upstream_reverts(config_dir: ConfigDir) -> None:
    server = notes()
    config_dir.add_memory_upstream("notes", server)
    run_cli(config_dir, "upstream", "sync", "notes")
    grow(server)
    run_cli(config_dir, "upstream", "sync", "notes")
    server.local_provider.remove_tool("delete_note")

    result = run_cli(config_dir, "upstream", "sync", "notes")

    assert result.exit_code == 0, result.output
    assert drift_file(config_dir, "notes") is None
    assert "Drift" not in run_cli(config_dir, "ls").output


# --- accepting ---------------------------------------------------------------------------------


def test_accept_applies_the_diff_and_hides_new_items_by_default(config_dir: ConfigDir) -> None:
    server = notes()
    config_dir.add_memory_upstream("notes", server)
    run_cli(config_dir, "proxy", "new", "notes/short")
    run_cli(config_dir, "upstream", "sync", "notes")
    grow(server)
    server.resource("notes://recent")(lambda: "recent")

    result = run_cli(config_dir, "upstream", "sync", "notes", "--accept")

    assert result.exit_code == 0, result.output
    assert sorted(catalog_file(config_dir, "notes")["tools"]) == ["add_note", "delete_note"]
    assert drift_file(config_dir, "notes") is None
    for proxy in ("default", "short"):
        text = (config_dir.path / "upstreams" / "notes" / f"{proxy}.toml").read_text()
        assert "[tools.delete_note]" in text
        assert '[resources."notes://recent"]' in text
        assert text.count("hidden = true") == 2
    assert "hidden" in result.output
    assert "reconnect" in result.output


def test_accept_with_nothing_pending_says_so(config_dir: ConfigDir) -> None:
    config_dir.add_memory_upstream("notes", notes())
    run_cli(config_dir, "upstream", "sync", "notes")

    result = run_cli(config_dir, "upstream", "sync", "notes", "--accept")

    assert result.exit_code == 0, result.output
    assert "nothing to accept" in result.output.lower()


async def test_clients_see_a_change_only_after_accept(config_dir: ConfigDir) -> None:
    server = notes()
    config_dir.add_memory_upstream("notes", server)
    await cli(config_dir, "upstream", "sync", "notes")
    (config_dir.path / "config.toml").write_text('version = 1\n[drift]\nnew_items = "visible"\n')

    async with running_daemon(config_dir) as daemon, daemon.client("/notes/mcp") as client:
        grow(server)
        assert [tool.name for tool in await client.list_tools()] == ["add_note"]

        await cli(config_dir, "upstream", "sync", "notes")
        assert [tool.name for tool in await client.list_tools()] == ["add_note"]

        accepted = await cli(config_dir, "upstream", "sync", "notes", "--accept")
        assert accepted.exit_code == 0, accepted.output
        names = sorted(tool.name for tool in await client.list_tools())
        assert names == ["add_note", "delete_note"]
        assert (await client.call_tool("delete_note", {"id": 1})).content == []


async def test_new_items_are_hidden_from_clients_unless_the_setting_says_otherwise(
    config_dir: ConfigDir,
) -> None:
    server = notes()
    config_dir.add_memory_upstream("notes", server)
    await cli(config_dir, "upstream", "sync", "notes")
    grow(server)
    await cli(config_dir, "upstream", "sync", "notes", "--accept")

    async with running_daemon(config_dir) as daemon, daemon.client("/notes/mcp") as client:
        assert [tool.name for tool in await client.list_tools()] == ["add_note"]


async def test_a_changed_definition_reaches_clients_on_accept(config_dir: ConfigDir) -> None:
    server = notes()
    config_dir.add_memory_upstream("notes", server)
    await cli(config_dir, "upstream", "sync", "notes")
    server.instructions = "Keep notes very short."

    async with running_daemon(config_dir) as daemon:
        async with daemon.client("/notes/mcp") as client:
            assert client.instructions == "Keep notes short."
        await cli(config_dir, "upstream", "sync", "notes", "--accept")
        async with daemon.client("/notes/mcp") as client:
            assert client.instructions == "Keep notes very short."


def test_accept_keeps_overrides_of_vanished_items_and_flags_them_orphaned(
    config_dir: ConfigDir,
) -> None:
    server = notes()
    config_dir.add_memory_upstream("notes", server)
    run_cli(config_dir, "upstream", "sync", "notes")
    proxy_file = config_dir.path / "upstreams" / "notes" / "default.toml"
    proxy_file.write_text("version = 1\n\n# my curation\n[tools.add_note]\nhidden = true\n")
    server.local_provider.remove_tool("add_note")

    result = run_cli(config_dir, "upstream", "sync", "notes", "--accept")

    assert result.exit_code == 0, result.output
    assert "orphaned" in result.output
    assert "add_note" in result.output
    assert "# my curation" in proxy_file.read_text()
    assert "[tools.add_note]" in proxy_file.read_text()

    doctor = run_cli(config_dir, "doctor")
    assert doctor.exit_code == 0, doctor.output
    assert "orphaned" in doctor.output


def test_accept_preserves_hand_written_comments_in_proxy_files(config_dir: ConfigDir) -> None:
    server = notes()
    config_dir.add_memory_upstream("notes", server)
    run_cli(config_dir, "upstream", "sync", "notes")
    proxy_file = config_dir.path / "upstreams" / "notes" / "default.toml"
    proxy_file.write_text(
        "version = 1  # keep\n\n# curated by hand\n[tools.add_note]\nhidden = false\n"
    )
    grow(server)

    run_cli(config_dir, "upstream", "sync", "notes", "--accept")

    text = proxy_file.read_text()
    assert text.startswith("version = 1  # keep\n")
    assert "# curated by hand\n[tools.add_note]\nhidden = false\n" in text
    assert "[tools.delete_note]\nhidden = true" in text


# --- housekeeping ------------------------------------------------------------------------------


def test_upstream_rm_forgets_the_catalog(config_dir: ConfigDir) -> None:
    config_dir.add_memory_upstream("notes", notes())
    run_cli(config_dir, "upstream", "sync", "notes")

    result = run_cli(config_dir, "upstream", "rm", "notes", "--yes")

    assert result.exit_code == 0, result.output
    assert not (config_dir.state / "upstreams" / "notes").exists()


def test_a_broken_catalog_file_is_reported_not_served(config_dir: ConfigDir) -> None:
    config_dir.add_memory_upstream("notes", notes())
    run_cli(config_dir, "upstream", "sync", "notes")
    (config_dir.state / "upstreams" / "notes" / "catalog.json").write_text("{")

    result = run_cli(config_dir, "upstream", "sync", "notes")

    assert result.exit_code == 1
    assert "catalog.json" in result.output
