"""Overrides: a Client sees exactly the curated result, and calls reach the Catalog item."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from fastmcp import FastMCP
from mcp_types import TextContent, TextResourceContents

from tests.support.seam import run_cli, running_daemon

if TYPE_CHECKING:
    from tests.support.seam import ConfigDir


def issues() -> FastMCP[Any]:
    """An Upstream with tools that echo their arguments, plus resources and a prompt."""
    server = FastMCP("issues", instructions="Track issues.")

    def create_issue(
        title: str, body: str = "", labels: list[str] | None = None, repo: str = "octo/repo"
    ) -> str:
        """Create an issue in a repository."""
        return f"{repo}: {title} [{body}] {labels}"

    def close_issue(id: int) -> str:  # noqa: A002
        """Close an issue."""
        return f"closed {id}"

    def list_issues() -> list[str]:
        """List issues."""
        return ["#1", "#2"]

    def issue(id: str) -> str:  # noqa: A002
        return f"issue {id}"

    def triage(name: str) -> str:
        """Triage an issue."""
        return f"Triage {name}"

    server.tool(create_issue)
    server.tool(close_issue)
    server.tool(list_issues)
    server.resource("issues://open")(lambda: "open issues")
    server.resource("issues://{id}")(issue)
    server.prompt(triage)
    return server


def curate(
    config_dir: ConfigDir, text: str, upstream: str = "issues", proxy: str = "default"
) -> None:
    """Write the Proxy file by hand, as a user editing it would."""
    (config_dir.path / "upstreams" / upstream / f"{proxy}.toml").write_text("version = 1\n" + text)


async def synced(config_dir: ConfigDir, server: FastMCP[Any]) -> None:
    config_dir.add_memory_upstream("issues", server)
    result = await asyncio.to_thread(run_cli, config_dir, "upstream", "sync", "issues")
    assert result.exit_code == 0, result.output


# --- hiding and renaming -----------------------------------------------------------------------


async def test_hidden_items_of_every_kind_are_absent(config_dir: ConfigDir) -> None:
    await synced(config_dir, issues())
    curate(
        config_dir,
        "[tools.close_issue]\nhidden = true\n"
        '[resources."issues://open"]\nhidden = true\n'
        '[resources."issues://{id}"]\nhidden = true\n'
        "[prompts.triage]\nhidden = true\n",
    )

    async with running_daemon(config_dir) as daemon, daemon.client("/issues/mcp") as client:
        assert sorted(t.name for t in await client.list_tools()) == ["create_issue", "list_issues"]
        assert await client.list_resources() == []
        assert await client.list_resource_templates() == []
        assert await client.list_prompts() == []


async def test_a_renamed_tool_is_listed_under_its_exposed_name_and_reaches_the_catalog_tool(
    config_dir: ConfigDir,
) -> None:
    await synced(config_dir, issues())
    curate(config_dir, '[tools.create_issue]\nname = "new_issue"\n')

    async with running_daemon(config_dir) as daemon, daemon.client("/issues/mcp") as client:
        names = sorted(t.name for t in await client.list_tools())
        assert names == ["close_issue", "list_issues", "new_issue"]
        result = await client.call_tool("new_issue", {"title": "Bug"})
        assert result.data == "octo/repo: Bug [] None"
        missing = await client.call_tool("create_issue", {"title": "Bug"}, raise_on_error=False)
        assert missing.is_error


async def test_title_description_and_annotations_are_replaced(config_dir: ConfigDir) -> None:
    await synced(config_dir, issues())
    curate(
        config_dir,
        "[tools.close_issue]\n"
        'title = "Close"\n'
        'description = "Close one issue by number."\n'
        "[tools.close_issue.annotations]\n"
        "read_only = false\n"
        "destructive = true\n"
        "idempotent = true\n",
    )

    async with running_daemon(config_dir) as daemon, daemon.client("/issues/mcp") as client:
        tool = next(t for t in await client.list_tools() if t.name == "close_issue")
        assert tool.title == "Close"
        assert tool.description == "Close one issue by number."
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is False
        assert tool.annotations.destructive_hint is True
        assert tool.annotations.idempotent_hint is True
        assert tool.annotations.open_world_hint is None
        untouched = next(t for t in await client.list_tools() if t.name == "list_issues")
        assert untouched.description == "List issues."


# --- arguments ---------------------------------------------------------------------------------


async def test_arguments_are_renamed_described_defaulted_made_optional_and_hidden(
    config_dir: ConfigDir,
) -> None:
    await synced(config_dir, issues())
    curate(
        config_dir,
        "[tools.create_issue.args.title]\n"
        'name = "summary"\n'
        'description = "One line."\n'
        "[tools.create_issue.args.body]\n"
        'default = "(no body)"\n'
        "[tools.create_issue.args.labels]\n"
        "hidden = true\n"
        "[tools.create_issue.args.repo]\n"
        "hidden = true\n"
        'default = "octo/curated"\n',
    )

    async with running_daemon(config_dir) as daemon, daemon.client("/issues/mcp") as client:
        tool = next(t for t in await client.list_tools() if t.name == "create_issue")
        properties = tool.input_schema["properties"]
        assert list(properties) == ["summary", "body"]
        assert properties["summary"]["type"] == "string"
        assert properties["summary"]["description"] == "One line."
        assert properties["body"]["default"] == "(no body)"
        assert tool.input_schema["required"] == ["summary"]

        assert (await client.call_tool("create_issue", {"summary": "Bug"})).data == (
            "octo/curated: Bug [(no body)] None"
        )
        given = await client.call_tool("create_issue", {"summary": "Bug", "body": "text"})
        assert given.data == "octo/curated: Bug [text] None"


async def test_an_argument_can_be_made_optional_or_required(config_dir: ConfigDir) -> None:
    await synced(config_dir, issues())
    curate(
        config_dir,
        "[tools.create_issue.args.title]\nrequired = false\n"
        "[tools.create_issue.args.body]\nrequired = true\n",
    )

    async with running_daemon(config_dir) as daemon, daemon.client("/issues/mcp") as client:
        tool = next(t for t in await client.list_tools() if t.name == "create_issue")
        assert tool.input_schema["required"] == ["body"]


# --- resources and prompts ---------------------------------------------------------------------


async def test_resources_and_prompts_are_renamed_and_re_described(config_dir: ConfigDir) -> None:
    await synced(config_dir, issues())
    curate(
        config_dir,
        '[resources."issues://open"]\n'
        'uri = "issues://all"\n'
        'name = "All open issues"\n'
        'description = "Every open issue."\n'
        '[resources."issues://{id}"]\n'
        'uri = "issue://{id}"\n'
        'description = "One issue."\n'
        "[prompts.triage]\n"
        'name = "sort"\n'
        'description = "Sort an issue."\n',
    )

    async with running_daemon(config_dir) as daemon, daemon.client("/issues/mcp") as client:
        resources = await client.list_resources()
        assert [(str(r.uri), r.name, r.description) for r in resources] == [
            ("issues://all", "All open issues", "Every open issue.")
        ]
        contents = (await client.read_resource("issues://all"))[0]
        assert isinstance(contents, TextResourceContents)
        assert contents.text == "open issues"

        templates = await client.list_resource_templates()
        assert [(t.uri_template, t.description) for t in templates] == [
            ("issue://{id}", "One issue.")
        ]
        contents = (await client.read_resource("issue://7"))[0]
        assert isinstance(contents, TextResourceContents)
        assert contents.text == "issue 7"

        assert [(p.name, p.description) for p in await client.list_prompts()] == [
            ("sort", "Sort an issue.")
        ]
        content = (await client.get_prompt("sort", {"name": "#1"})).messages[0].content
        assert isinstance(content, TextContent)
        assert content.text == "Triage #1"


# --- the Proxy itself --------------------------------------------------------------------------


async def test_the_proxy_exposes_its_own_server_name_and_instructions(
    config_dir: ConfigDir,
) -> None:
    await synced(config_dir, issues())
    curate(config_dir, 'name = "Issues, curated"\ninstructions = "Open issues only."\n')

    async with running_daemon(config_dir) as daemon, daemon.client("/issues/mcp") as client:
        assert client.server_info is not None
        assert client.server_info.name == "Issues, curated"
        assert client.instructions == "Open issues only."


async def test_without_overrides_the_proxy_is_named_after_itself_and_keeps_the_instructions(
    config_dir: ConfigDir,
) -> None:
    await synced(config_dir, issues())

    async with running_daemon(config_dir) as daemon, daemon.client("/issues/mcp") as client:
        assert client.server_info is not None
        assert client.server_info.name == "issues/default"
        assert client.instructions == "Track issues."


async def test_a_changed_server_name_reaches_new_sessions(config_dir: ConfigDir) -> None:
    await synced(config_dir, issues())
    curate(config_dir, 'name = "First"\n')

    async with running_daemon(config_dir) as daemon:
        async with daemon.client("/issues/mcp") as client:
            assert client.server_info is not None
            assert client.server_info.name == "First"
        curate(config_dir, 'name = "Second"\n[tools.close_issue]\nhidden = true\n')
        async with daemon.client("/issues/mcp") as client:
            assert client.server_info is not None
            assert client.server_info.name == "Second"
            assert sorted(t.name for t in await client.list_tools()) == [
                "create_issue",
                "list_issues",
            ]


# --- what cannot be done -----------------------------------------------------------------------


async def test_colliding_exposed_names_are_refused_and_the_last_exposed_set_stays(
    config_dir: ConfigDir,
) -> None:
    await synced(config_dir, issues())
    curate(config_dir, '[tools.close_issue]\nname = "issue"\n')

    async with running_daemon(config_dir) as daemon, daemon.client("/issues/mcp") as client:
        assert sorted(t.name for t in await client.list_tools()) == [
            "create_issue",
            "issue",
            "list_issues",
        ]
        curate(
            config_dir, '[tools.close_issue]\nname = "issue"\n[tools.list_issues]\nname = "issue"\n'
        )
        assert sorted(t.name for t in await client.list_tools()) == [
            "create_issue",
            "issue",
            "list_issues",
        ]

    doctor = run_cli(config_dir, "doctor")
    assert doctor.exit_code == 1
    assert "close_issue" in doctor.output
    assert "list_issues" in doctor.output


def test_a_rename_onto_an_exposed_catalog_name_is_refused(config_dir: ConfigDir) -> None:
    config_dir.add_memory_upstream("issues", issues())
    run_cli(config_dir, "upstream", "sync", "issues")
    curate(config_dir, '[tools.close_issue]\nname = "list_issues"\n')

    doctor = run_cli(config_dir, "doctor")

    assert doctor.exit_code == 1
    assert "list_issues" in doctor.output


def test_hiding_a_required_argument_needs_a_default(config_dir: ConfigDir) -> None:
    config_dir.add_memory_upstream("issues", issues())
    run_cli(config_dir, "upstream", "sync", "issues")
    curate(config_dir, "[tools.create_issue.args.title]\nhidden = true\n")

    doctor = run_cli(config_dir, "doctor")

    assert doctor.exit_code == 1
    assert "title" in doctor.output
    assert "default" in doctor.output


def test_schema_types_cannot_be_changed(config_dir: ConfigDir) -> None:
    config_dir.add_memory_upstream("issues", issues())
    run_cli(config_dir, "upstream", "sync", "issues")
    curate(
        config_dir,
        '[tools.create_issue]\ninput_schema = { type = "object" }\n'
        '[tools.close_issue.args.id]\ntype = "string"\n',
    )

    doctor = run_cli(config_dir, "doctor")

    assert doctor.exit_code == 1
    assert "tools.create_issue" in doctor.output
    assert "tools.close_issue.args.id" in doctor.output
    assert "schema" in doctor.output.lower()


def test_a_template_rename_must_keep_its_parameters(config_dir: ConfigDir) -> None:
    config_dir.add_memory_upstream("issues", issues())
    run_cli(config_dir, "upstream", "sync", "issues")
    curate(config_dir, '[resources."issues://{id}"]\nuri = "issue://{number}"\n')

    doctor = run_cli(config_dir, "doctor")

    assert doctor.exit_code == 1
    assert "{id}" in doctor.output


def test_an_override_for_an_unknown_argument_is_kept_and_flagged(config_dir: ConfigDir) -> None:
    config_dir.add_memory_upstream("issues", issues())
    run_cli(config_dir, "upstream", "sync", "issues")
    curate(config_dir, '[tools.create_issue.args.assignee]\ndescription = "Who."\n')

    doctor = run_cli(config_dir, "doctor")

    assert doctor.exit_code == 0, doctor.output
    assert "orphaned" in doctor.output
    assert "assignee" in doctor.output
