"""Hooks and Virtual Tools: a Python file next to the Proxy file changes what a Client sees.

Every test writes a user file into the temp config directory, then asserts on the arguments
the in-memory Upstream received and on the results the Client got. The user files import
only ``mcpshape``.
"""

from __future__ import annotations

import asyncio
import re
import textwrap
from typing import TYPE_CHECKING, Any

from fastmcp import FastMCP
from mcp_types import TextContent, TextResourceContents

from tests.support.seam import run_cli, running_daemon
from tests.test_overrides import curate, synced

if TYPE_CHECKING:
    from fastmcp.client.client import CallToolResult

    from tests.support.seam import ConfigDir

Received = list[tuple[str, dict[str, Any]]]


def tracker() -> tuple[FastMCP[Any], Received]:
    """An Upstream that records every call, read, and get it receives."""
    server = FastMCP("issues", instructions="Track issues.")
    received: Received = []

    def create_issue(title: str, labels: list[str] | None = None) -> str:
        """Create an issue."""
        received.append(("create_issue", {"title": title, "labels": labels}))
        return f"created {title} {labels}"

    def close_issue(id: int):  # noqa: A002, ANN202  # no return annotation: no output schema
        """Close an issue."""
        received.append(("close_issue", {"id": id}))
        return f"closed {id}"

    def explode() -> str:
        """Always fails."""
        msg = "boom"
        raise ValueError(msg)

    def open_issues() -> str:
        received.append(("issues://open", {}))
        return "open: #1, #2"

    def issue(id: str) -> str:  # noqa: A002
        received.append(("issues://{id}", {"id": id}))
        return f"issue {id}"

    def triage(name: str) -> str:
        """Triage an issue."""
        received.append(("triage", {"name": name}))
        return f"Triage {name}"

    server.tool(create_issue)
    server.tool(close_issue)
    server.tool(explode)
    server.resource("issues://open")(open_issues)
    server.resource("issues://{id}")(issue)
    server.prompt(triage)
    return server, received


def user_code(
    config_dir: ConfigDir, text: str, upstream: str = "issues", proxy: str = "default"
) -> None:
    """Write the Proxy's Python file, as a user would."""
    path = config_dir.path / "upstreams" / upstream / f"{proxy}.py"
    path.write_text(
        "from mcpshape import hook, tool, upstream, Message, UpstreamError\n"
        + textwrap.dedent(text)
    )


def text_of(result: CallToolResult) -> str:
    content = result.content[0]
    assert isinstance(content, TextContent)
    return content.text


def error_text(result: CallToolResult) -> str:
    assert result.is_error
    return text_of(result)


def resource_text(contents: list[Any]) -> str:
    assert len(contents) == 1
    assert isinstance(contents[0], TextResourceContents)
    return contents[0].text


# --- tool Hooks --------------------------------------------------------------------------------


async def test_a_before_hook_rewrites_the_arguments_the_upstream_receives(
    config_dir: ConfigDir,
) -> None:
    server, received = tracker()
    await synced(config_dir, server)
    user_code(
        config_dir,
        """
        @hook.before("create_issue")
        def label(call):
            assert call.kind == "tool" and call.name == "create_issue"
            call.args["labels"] = [*(call.args.get("labels") or []), "proxied"]
        """,
    )

    async with running_daemon(config_dir) as daemon, daemon.client("/issues/mcp") as client:
        result = await client.call_tool("create_issue", {"title": "Bug", "labels": ["p1"]})

    assert received == [("create_issue", {"title": "Bug", "labels": ["p1", "proxied"]})]
    assert result.data == "created Bug ['p1', 'proxied']"


async def test_a_before_hook_short_circuits_without_reaching_the_upstream(
    config_dir: ConfigDir,
) -> None:
    server, received = tracker()
    await synced(config_dir, server)
    user_code(
        config_dir,
        """
        @hook.before("close_issue")
        async def refuse(call):
            return f"issue {call.args['id']} stays open here"
        """,
    )

    async with running_daemon(config_dir) as daemon, daemon.client("/issues/mcp") as client:
        result = await client.call_tool("close_issue", {"id": 7})

    assert received == []
    assert text_of(result) == "issue 7 stays open here"


async def test_an_after_hook_rewrites_the_result_the_client_gets(config_dir: ConfigDir) -> None:
    server, received = tracker()
    await synced(config_dir, server)
    user_code(
        config_dir,
        """
        @hook.after("create_issue")
        async def shout(call, result):
            result.text = result.text.upper()

        @hook.after("close_issue")
        def replace(call, result):
            return {"closed": call.args["id"], "was": result.text}
        """,
    )

    async with running_daemon(config_dir) as daemon, daemon.client("/issues/mcp") as client:
        created = await client.call_tool("create_issue", {"title": "Bug"})
        closed = await client.call_tool("close_issue", {"id": 3})

    assert received == [
        ("create_issue", {"title": "Bug", "labels": None}),
        ("close_issue", {"id": 3}),
    ]
    assert created.data == "CREATED BUG NONE"
    assert closed.structured_content == {"closed": 3, "was": "closed 3"}


async def test_a_hook_that_raises_answers_a_tool_error_carrying_its_message(
    config_dir: ConfigDir,
) -> None:
    server, received = tracker()
    await synced(config_dir, server)
    user_code(
        config_dir,
        """
        @hook.before("create_issue")
        def block(call):
            raise PermissionError("issues are read-only from this Proxy")

        @hook.after("close_issue")
        async def stumble(call, result):
            raise RuntimeError("after went wrong")
        """,
    )

    async with running_daemon(config_dir) as daemon, daemon.client("/issues/mcp") as client:
        blocked = await client.call_tool("create_issue", {"title": "Bug"}, raise_on_error=False)
        stumbled = await client.call_tool("close_issue", {"id": 1}, raise_on_error=False)
        still_fine = await client.call_tool("close_issue", {"id": 2}, raise_on_error=False)

    assert error_text(blocked) == "issues are read-only from this Proxy"
    assert error_text(stumbled) == "after went wrong"
    assert error_text(still_fine) == "after went wrong"
    assert received == [("close_issue", {"id": 1}), ("close_issue", {"id": 2})]


async def test_hooks_are_keyed_by_catalog_name_and_see_catalog_arguments(
    config_dir: ConfigDir,
) -> None:
    server, received = tracker()
    await synced(config_dir, server)
    curate(
        config_dir,
        '[tools.create_issue]\nname = "file_bug"\n'
        '[tools.create_issue.args.title]\nname = "summary"\n',
    )
    user_code(
        config_dir,
        """
        @hook.before("create_issue")
        def tag(call):
            call.args["labels"] = [call.name, *call.args]
        """,
    )

    async with running_daemon(config_dir) as daemon, daemon.client("/issues/mcp") as client:
        result = await client.call_tool("file_bug", {"summary": "Bug"})

    assert received == [("create_issue", {"title": "Bug", "labels": ["create_issue", "title"]})]
    assert result.data == "created Bug ['create_issue', 'title']"


async def test_several_hooks_on_one_tool_run_in_file_order(config_dir: ConfigDir) -> None:
    server, received = tracker()
    await synced(config_dir, server)
    user_code(
        config_dir,
        """
        @hook.before("create_issue")
        def first(call):
            call.args["title"] = call.args["title"] + " one"

        @hook.before("create_issue")
        def second(call):
            call.args["title"] = call.args["title"] + " two"

        @hook.after("create_issue")
        def third(call, result):
            return result.text + " three"

        @hook.after("create_issue")
        def fourth(call, result):
            return result.text + " four"
        """,
    )

    async with running_daemon(config_dir) as daemon, daemon.client("/issues/mcp") as client:
        result = await client.call_tool("create_issue", {"title": "Bug"})

    assert received == [("create_issue", {"title": "Bug one two", "labels": None})]
    assert result.data == "created Bug one two None three four"


# --- resource and prompt Hooks -----------------------------------------------------------------


async def test_resource_hooks_rewrite_template_parameters_and_contents(
    config_dir: ConfigDir,
) -> None:
    server, received = tracker()
    await synced(config_dir, server)
    user_code(
        config_dir,
        """
        @hook.before.resource("issues://{id}")
        def pad(call):
            assert call.kind == "resource" and call.name == "issues://{id}"
            call.args["id"] = call.args["id"].zfill(4)

        @hook.after.resource("issues://open")
        async def trim(call, result):
            result.text = result.text.split(",")[0]

        @hook.before.resource("issues://open")
        def note(call):
            call.args["seen"] = True
        """,
    )

    async with running_daemon(config_dir) as daemon, daemon.client("/issues/mcp") as client:
        one = await client.read_resource("issues://7")
        opened = await client.read_resource("issues://open")

    assert received == [("issues://{id}", {"id": "0007"}), ("issues://open", {})]
    assert resource_text(one) == "issue 0007"
    assert resource_text(opened) == "open: #1"


async def test_a_resource_hook_short_circuits_or_fails_a_read(config_dir: ConfigDir) -> None:
    server, received = tracker()
    await synced(config_dir, server)
    user_code(
        config_dir,
        """
        @hook.before.resource("issues://open")
        def cached(call):
            return "open: cached"

        @hook.before.resource("issues://{id}")
        def refuse(call):
            raise LookupError("no such issue here")
        """,
    )

    async with running_daemon(config_dir) as daemon, daemon.client("/issues/mcp") as client:
        opened = await client.read_resource("issues://open")
        try:
            await client.read_resource("issues://1")
        except Exception as exc:  # noqa: BLE001  # whatever the Client raises, its text matters
            failure = str(exc)
        else:
            failure = ""

    assert received == []
    assert resource_text(opened) == "open: cached"
    assert "no such issue here" in failure


async def test_prompt_hooks_rewrite_arguments_and_messages(config_dir: ConfigDir) -> None:
    server, received = tracker()
    await synced(config_dir, server)
    user_code(
        config_dir,
        """
        @hook.before.prompt("triage")
        def rename(call):
            assert call.kind == "prompt" and call.name == "triage"
            call.args["name"] = call.args["name"].title()

        @hook.after.prompt("triage")
        async def remind(call, result):
            result.messages[0].text += " carefully"
            result.messages.append(Message("Understood.", role="assistant"))
        """,
    )

    async with running_daemon(config_dir) as daemon, daemon.client("/issues/mcp") as client:
        rendered = await client.get_prompt("triage", {"name": "bug report"})

    assert received == [("triage", {"name": "Bug Report"})]
    texts = [
        (m.role, m.content.text) for m in rendered.messages if isinstance(m.content, TextContent)
    ]
    assert texts == [("user", "Triage Bug Report carefully"), ("assistant", "Understood.")]


async def test_a_prompt_hook_short_circuits_with_a_string(config_dir: ConfigDir) -> None:
    server, received = tracker()
    await synced(config_dir, server)
    user_code(
        config_dir,
        """
        @hook.before.prompt("triage")
        def stub(call):
            return f"Skip triage of {call.args['name']}."
        """,
    )

    async with running_daemon(config_dir) as daemon, daemon.client("/issues/mcp") as client:
        rendered = await client.get_prompt("triage", {"name": "x"})

    assert received == []
    content = rendered.messages[0].content
    assert isinstance(content, TextContent)
    assert (rendered.messages[0].role, content.text) == ("user", "Skip triage of x.")


# --- Virtual Tools -----------------------------------------------------------------------------


async def test_a_virtual_tool_is_listed_with_its_schema_and_calls_the_upstream(
    config_dir: ConfigDir,
) -> None:
    server, received = tracker()
    await synced(config_dir, server)
    user_code(
        config_dir,
        """
        @tool
        async def close_all(ids: list[int], note: str = "") -> str:
            \"\"\"Close several issues at once.\"\"\"
            for id in ids:
                await upstream.call("close_issue", id=id)
            return f"closed {len(ids)}{note}"
        """,
    )

    async with running_daemon(config_dir) as daemon, daemon.client("/issues/mcp") as client:
        tools = {t.name: t for t in await client.list_tools()}
        assert "close_all" in tools
        assert tools["close_all"].description == "Close several issues at once."
        properties = tools["close_all"].input_schema["properties"]
        assert properties["ids"] == {"type": "array", "items": {"type": "integer"}}
        assert properties["note"] == {"type": "string", "default": ""}
        assert tools["close_all"].input_schema["required"] == ["ids"]
        result = await client.call_tool("close_all", {"ids": [1, 2], "note": "!"})

    assert received == [("close_issue", {"id": 1}), ("close_issue", {"id": 2})]
    assert result.data == "closed 2!"


async def test_the_upstream_handle_reads_gets_and_returns_upstream_results(
    config_dir: ConfigDir,
) -> None:
    server, received = tracker()
    await synced(config_dir, server)
    user_code(
        config_dir,
        """
        @tool(name="digest", description="Everything about an issue.")
        async def anything(id: str) -> str:
            issue = await upstream.read("issues://" + id)
            prompt = await upstream.get("triage", {"name": id})
            opened = await upstream.read("issues://open")
            return " | ".join([issue.text, prompt.messages[0].text, opened.text])

        @tool
        async def passthrough(title: str):
            \"\"\"Hand back the Upstream's own result.\"\"\"
            return await upstream.call("create_issue", {"title": title}, labels=["v"])
        """,
    )

    async with running_daemon(config_dir) as daemon, daemon.client("/issues/mcp") as client:
        tools = {t.name: t for t in await client.list_tools()}
        assert tools["digest"].description == "Everything about an issue."
        digest = await client.call_tool("digest", {"id": "9"})
        passed = await client.call_tool("passthrough", {"title": "T"})

    assert received == [
        ("issues://{id}", {"id": "9"}),
        ("triage", {"name": "9"}),
        ("issues://open", {}),
        ("create_issue", {"title": "T", "labels": ["v"]}),
    ]
    assert digest.data == "issue 9 | Triage 9 | open: #1, #2"
    assert text_of(passed) == "created T ['v']"
    assert passed.structured_content == {"result": "created T ['v']"}


async def test_a_virtual_tool_that_raises_answers_a_tool_error_carrying_its_message(
    config_dir: ConfigDir,
) -> None:
    server, _ = tracker()
    await synced(config_dir, server)
    user_code(
        config_dir,
        """
        @tool
        def broken(x: int) -> int:
            \"\"\"Sync, and failing.\"\"\"
            raise ValueError(f"cannot handle {x}")
        """,
    )

    async with running_daemon(config_dir) as daemon, daemon.client("/issues/mcp") as client:
        result = await client.call_tool("broken", {"x": 4}, raise_on_error=False)

    assert error_text(result) == "cannot handle 4"


async def test_an_upstream_error_reaches_user_code_as_an_exception_it_can_catch(
    config_dir: ConfigDir,
) -> None:
    server, _ = tracker()
    await synced(config_dir, server)
    user_code(
        config_dir,
        """
        @tool
        async def careful() -> str:
            \"\"\"Catches the Upstream's error.\"\"\"
            try:
                await upstream.call("explode")
            except UpstreamError as exc:
                return f"caught: {exc}"
            return "nothing happened"

        @tool
        async def careless() -> str:
            \"\"\"Lets it through.\"\"\"
            await upstream.call("no_such_tool")
            return "unreachable"
        """,
    )

    async with running_daemon(config_dir) as daemon, daemon.client("/issues/mcp") as client:
        caught = await client.call_tool("careful", {})
        missing = await client.call_tool("careless", {}, raise_on_error=False)

    assert caught.data.startswith("caught: ")
    assert "boom" in caught.data
    assert "no_such_tool" in error_text(missing)


async def test_user_code_reaches_only_its_own_upstream(config_dir: ConfigDir) -> None:
    server, received = tracker()
    await synced(config_dir, server)
    calc = FastMCP("calc")

    def add(a: int, b: int) -> int:
        return a + b

    calc.tool(add)
    config_dir.add_memory_upstream("calc", calc)
    user_code(
        config_dir,
        """
        @tool
        async def borrow() -> str:
            \"\"\"Tries a tool of another Upstream.\"\"\"
            return (await upstream.call("add", a=1, b=2)).text
        """,
    )

    async with running_daemon(config_dir) as daemon, daemon.client("/issues/mcp") as client:
        result = await client.call_tool("borrow", {}, raise_on_error=False)

    assert received == []
    assert "add" in error_text(result)


async def test_a_virtual_tool_colliding_with_an_exposed_tool_marks_the_proxy_unhealthy(
    config_dir: ConfigDir,
) -> None:
    server, _ = tracker()
    await synced(config_dir, server)
    user_code(
        config_dir,
        """
        @tool
        def create_issue(title: str) -> str:
            \"\"\"Shadows the Upstream's tool.\"\"\"
            return title
        """,
    )

    async with running_daemon(config_dir) as daemon:
        status = await daemon.status()

    proxy = status["upstreams"][0]["proxies"][0]
    assert proxy["health"] == "unhealthy"
    assert "create_issue" in proxy["detail"]


# --- load failures -----------------------------------------------------------------------------


async def test_a_broken_user_file_fails_every_call_naming_the_proxy_until_it_is_fixed(
    config_dir: ConfigDir,
) -> None:
    server, received = tracker()
    await synced(config_dir, server)
    user_code(config_dir, "")

    async with running_daemon(config_dir) as daemon, daemon.client("/issues/mcp") as client:
        before = sorted(t.name for t in await client.list_tools())
        assert text_of(await client.call_tool("close_issue", {"id": 1})) == "closed 1"

        user_code(
            config_dir,
            "@hook.before('close_issue')\ndef f(call):\n    return undefined_name\n\n"
            "import nonexistent_module\n",
        )
        await asyncio.sleep(0.01)  # a new mtime, on file systems that count in whole seconds
        assert sorted(t.name for t in await client.list_tools()) == before
        failed = await client.call_tool("close_issue", {"id": 2}, raise_on_error=False)
        assert re.search(r"Proxy issues/default .*nonexistent_module", error_text(failed))
        assert (await daemon.status())["upstreams"][0]["proxies"][0]["health"] == "unhealthy"

        user_code(config_dir, "")
        assert text_of(await client.call_tool("close_issue", {"id": 3})) == "closed 3"
        assert (await daemon.status())["upstreams"][0]["proxies"][0]["health"] == "ok"

    assert received == [("close_issue", {"id": 1}), ("close_issue", {"id": 3})]


def test_doctor_loads_user_files_and_reports_the_broken_and_the_orphaned(
    config_dir: ConfigDir,
) -> None:
    server, _ = tracker()
    asyncio.run(synced(config_dir, server))
    run_cli(config_dir, "proxy", "new", "issues/review")
    user_code(config_dir, "@hook.before('gone')\ndef f(call): ...\n")
    user_code(config_dir, "def broken(:\n    pass\n", proxy="review")

    result = run_cli(config_dir, "doctor")

    assert result.exit_code == 1, result.output
    assert "review.py" in result.output
    assert "SyntaxError" in result.output
    assert "orphaned Hook" in result.output
    assert "gone" in result.output
