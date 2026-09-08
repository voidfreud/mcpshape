"""Hooks and Virtual Tools: a Python file next to the Proxy file changes what a Client sees.

Every test writes a user file into the temp config directory, then asserts on the arguments
the in-memory Upstream received and on the results the Client got. The user files import
only ``mcpshape``.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
import textwrap
import time
from typing import TYPE_CHECKING, Any

import pytest
from fastmcp import Client, FastMCP
from mcp_types import TextContent, TextResourceContents

from tests.support.seam import run_cli, running_daemon, serving_daemon
from tests.test_overrides import curate, synced

if TYPE_CHECKING:
    from collections.abc import Generator

    from fastmcp.client.client import CallToolResult

    from tests.support.seam import ConfigDir

Received = list[tuple[str, dict[str, Any]]]


@pytest.fixture(autouse=True)
def _no_stale_helper_modules() -> Generator[None]:  # pyright: ignore[reportUnusedFunction]  # pytest autouse
    """Undo Python's own module cache between tests.

    A Proxy's ``import helpers`` caches by the bare name ``helpers``, which is only ever
    safe within one Daemon's lifetime: its config directory, and so its Upstreams'
    directories, never change while it runs, which is what ``load_user_code`` relies on to
    know a cached ``helpers`` is stale. Tests run many temp config directories in one
    process, so without this a later test's ``import helpers`` could be handed an earlier
    test's now-unrelated module of the same name.
    """
    before = set(sys.modules)
    yield
    for name in set(sys.modules) - before:
        sys.modules.pop(name, None)


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

    def stats() -> dict[str, int]:
        """How many issues are open and closed."""
        received.append(("stats", {}))
        return {"open": 2, "closed": 1}

    server.tool(create_issue)
    server.tool(close_issue)
    server.tool(explode)
    server.tool(stats)
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


def helper_code(config_dir: ConfigDir, text: str, upstream: str = "issues") -> None:
    """Write ``helpers.py`` next to the Upstream's Proxy files, as a user would."""
    path = config_dir.path / "upstreams" / upstream / "helpers.py"
    path.write_text(textwrap.dedent(text))


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


async def test_a_hook_that_raises_is_a_log_line_and_leaves_the_proxy_healthy(
    config_dir: ConfigDir, caplog: pytest.LogCaptureFixture
) -> None:
    """Story 48: an exception inside a Hook is a tool error and a log line, nothing more."""
    server, _ = tracker()
    await synced(config_dir, server)
    user_code(
        config_dir,
        """
        @hook.before("create_issue")
        def block(call):
            raise PermissionError("no new issues from here")
        """,
    )

    with caplog.at_level(logging.WARNING, logger="mcpshape"):
        async with running_daemon(config_dir) as daemon, daemon.client("/issues/mcp") as client:
            failed = await client.call_tool("create_issue", {"title": "Bug"}, raise_on_error=False)
            status = await daemon.status()

    assert error_text(failed) == "no new issues from here"
    assert status["upstreams"][0]["proxies"][0]["health"] == "ok"
    lines = [record.getMessage() for record in caplog.records if "block" in record.getMessage()]
    assert lines == ["Hook block on tool create_issue raised"]
    assert any("no new issues from here" in (record.exc_text or "") for record in caplog.records)


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


async def test_an_after_hook_rewrites_an_upstream_error_into_a_readable_message(
    config_dir: ConfigDir,
) -> None:
    server, _ = tracker()
    await synced(config_dir, server)
    user_code(
        config_dir,
        """
        @hook.after("explode")
        def readable(call, result):
            assert result.is_error
            result.text = "issues could not create that: try again later"
            return result
        """,
    )

    async with running_daemon(config_dir) as daemon, daemon.client("/issues/mcp") as client:
        result = await client.call_tool("explode", {}, raise_on_error=False)

    assert error_text(result) == "issues could not create that: try again later"


async def test_an_after_hook_turns_an_upstream_error_into_a_success(config_dir: ConfigDir) -> None:
    server, _ = tracker()
    await synced(config_dir, server)
    user_code(
        config_dir,
        """
        @hook.after("explode")
        def recovered(call, result):
            result.is_error = False
            result.text = "recovered"
            return result
        """,
    )

    async with running_daemon(config_dir) as daemon, daemon.client("/issues/mcp") as client:
        result = await client.call_tool("explode", {})

    assert not result.is_error
    assert result.data == "recovered"


async def test_an_after_hook_returning_none_leaves_an_upstream_error_as_is(
    config_dir: ConfigDir,
) -> None:
    server, _ = tracker()
    await synced(config_dir, server)
    user_code(
        config_dir,
        """
        @hook.after("explode")
        def look_only(call, result):
            assert result.is_error
            return None
        """,
    )

    async with running_daemon(config_dir) as daemon, daemon.client("/issues/mcp") as client:
        result = await client.call_tool("explode", {}, raise_on_error=False)

    assert "boom" in error_text(result)


async def test_a_virtual_tool_returning_an_error_result_reaches_its_after_hook_as_one(
    config_dir: ConfigDir,
) -> None:
    server, _ = tracker()
    await synced(config_dir, server)
    user_code(
        config_dir,
        """
        from mcpshape import ToolResult

        @tool
        def refuse(reason: str) -> str:
            \"\"\"Always refuses.\"\"\"
            return ToolResult(content=[{"type": "text", "text": reason}], is_error=True)

        @hook.after("refuse")
        def soften(call, result):
            result.text = f"is_error={result.is_error}: {result.text}"
            return result
        """,
    )

    async with running_daemon(config_dir) as daemon, daemon.client("/issues/mcp") as client:
        result = await client.call_tool("refuse", {"reason": "no"}, raise_on_error=False)

    assert error_text(result) == "is_error=True: no"


async def test_a_short_circuited_result_reaches_the_after_hook_as_a_success(
    config_dir: ConfigDir,
) -> None:
    server, _ = tracker()
    await synced(config_dir, server)
    user_code(
        config_dir,
        """
        @hook.before("explode")
        def refuse(call):
            return "refused before reaching the upstream"

        @hook.after("explode")
        def note(call, result):
            result.text = f"is_error={result.is_error}: {result.text}"
            return result
        """,
    )

    async with running_daemon(config_dir) as daemon, daemon.client("/issues/mcp") as client:
        result = await client.call_tool("explode", {})

    assert result.data == "is_error=False: refused before reaching the upstream"


async def test_an_after_hook_result_that_does_not_fit_the_schema_fails_at_the_proxy(
    config_dir: ConfigDir, caplog: pytest.LogCaptureFixture
) -> None:
    """Settled in #25: the Proxy's own error, naming the Hook and the tool, not the Client's."""
    server, _ = tracker()
    await synced(config_dir, server)
    user_code(
        config_dir,
        """
        @hook.after("create_issue")
        def reshape(call, result):
            return {"oops": "not a string"}
        """,
    )

    with caplog.at_level(logging.WARNING, logger="mcpshape"):
        async with running_daemon(config_dir) as daemon, daemon.client("/issues/mcp") as client:
            result = await client.call_tool("create_issue", {"title": "Bug"}, raise_on_error=False)

    text = error_text(result)
    assert "reshape" in text
    assert "create_issue" in text
    assert "string" in text
    lines = [record.getMessage() for record in caplog.records if "reshape" in record.getMessage()]
    assert len(lines) == 1
    assert "create_issue" in lines[0]


async def test_a_before_hook_short_circuit_that_does_not_fit_the_schema_fails_at_the_proxy(
    config_dir: ConfigDir,
) -> None:
    server, _ = tracker()
    await synced(config_dir, server)
    user_code(
        config_dir,
        """
        @hook.before("create_issue")
        def refuse(call):
            return {"oops": "not a string"}
        """,
    )

    async with running_daemon(config_dir) as daemon, daemon.client("/issues/mcp") as client:
        result = await client.call_tool("create_issue", {"title": "Bug"}, raise_on_error=False)

    text = error_text(result)
    assert "refuse" in text
    assert "create_issue" in text
    assert "string" in text


async def test_an_after_hook_result_that_fits_an_object_schema_reaches_the_client(
    config_dir: ConfigDir,
) -> None:
    server, _ = tracker()
    await synced(config_dir, server)
    user_code(
        config_dir,
        """
        @hook.after("stats")
        def relabel(call, result):
            return {"open": 5, "closed": 9}
        """,
    )

    async with running_daemon(config_dir) as daemon, daemon.client("/issues/mcp") as client:
        result = await client.call_tool("stats", {})

    assert result.structured_content == {"open": 5, "closed": 9}


# An error result is never checked against the schema: covered already by
# test_an_after_hook_rewrites_an_upstream_error_into_a_readable_message (#24), which sets
# ``result.text`` to plain, non-JSON text on an ``is_error`` result and gets that exact text
# back at the Client. Nothing added here.


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


async def test_hooks_keyed_by_a_virtual_tools_name_run_around_it(config_dir: ConfigDir) -> None:
    server, received = tracker()
    await synced(config_dir, server)
    user_code(
        config_dir,
        """
        @tool
        async def close_one(id: int) -> str:
            \"\"\"Close one issue.\"\"\"
            await upstream.call("close_issue", id=id)
            return f"closed #{id}"

        @hook.before("close_one")
        def bump(call):
            call.args["id"] += 1

        @hook.after("close_one")
        def sign(call, result):
            return result.text + " by proxy"
        """,
    )

    async with running_daemon(config_dir) as daemon, daemon.client("/issues/mcp") as client:
        result = await client.call_tool("close_one", {"id": 1})

    assert received == [("close_issue", {"id": 2})]
    assert result.data == "closed #2 by proxy"


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


# --- sync user code ----------------------------------------------------------------------------


async def test_a_sync_before_hook_calls_the_upstream_and_short_circuits_with_what_it_said(
    config_dir: ConfigDir,
) -> None:
    """A plain function reaches the Upstream: no ``await``, the call blocks until it answers."""
    server, received = tracker()
    await synced(config_dir, server)
    user_code(
        config_dir,
        """
        @hook.before("close_issue")
        def instead(call):
            made = upstream.call("create_issue", title=f"closing {call.args['id']}")
            return f"logged: {made.text}"
        """,
    )

    async with running_daemon(config_dir) as daemon, daemon.client("/issues/mcp") as client:
        result = await client.call_tool("close_issue", {"id": 7})

    assert received == [("create_issue", {"title": "closing 7", "labels": None})]
    assert text_of(result) == "logged: created closing 7 None"


async def test_a_sync_virtual_tool_calls_the_upstream_twice_and_builds_its_answer(
    config_dir: ConfigDir,
) -> None:
    server, received = tracker()
    await synced(config_dir, server)
    user_code(
        config_dir,
        """
        @tool
        def pair(first: int, second: int) -> str:
            \"\"\"Close two issues.\"\"\"
            one = upstream.call("close_issue", id=first)
            two = upstream.call("close_issue", id=second)
            return f"{one.text} and {two.text}"
        """,
    )

    async with running_daemon(config_dir) as daemon, daemon.client("/issues/mcp") as client:
        result = await client.call_tool("pair", {"first": 1, "second": 2})

    assert received == [("close_issue", {"id": 1}), ("close_issue", {"id": 2})]
    assert result.data == "closed 1 and closed 2"


async def test_a_sync_hook_catches_the_upstreams_error_like_an_async_one(
    config_dir: ConfigDir,
) -> None:
    server, _ = tracker()
    await synced(config_dir, server)
    user_code(
        config_dir,
        """
        @hook.before("close_issue")
        def careful(call):
            try:
                upstream.call("explode")
            except UpstreamError as exc:
                return f"caught: {exc}"
            return "nothing happened"
        """,
    )

    async with running_daemon(config_dir) as daemon, daemon.client("/issues/mcp") as client:
        result = await client.call_tool("close_issue", {"id": 1})

    assert text_of(result).startswith("caught: ")
    assert "boom" in text_of(result)


async def test_a_sync_hook_that_blocks_stalls_only_its_own_call(config_dir: ConfigDir) -> None:
    """The worker thread holds the sleep; the Daemon's loop keeps serving everything else."""
    server, _ = tracker()
    await synced(config_dir, server)
    user_code(
        config_dir,
        """
        import time

        @hook.before("close_issue")
        def slowly(call):
            time.sleep(0.5)
        """,
    )
    finished: list[str] = []

    async def call(client: Client[Any], name: str, args: dict[str, Any]) -> None:
        await client.call_tool(name, args)
        finished.append(name)

    async with running_daemon(config_dir) as daemon, daemon.client("/issues/mcp") as client:
        started = time.perf_counter()
        await asyncio.gather(
            call(client, "close_issue", {"id": 1}),
            call(client, "create_issue", {"title": "T"}),
        )
        elapsed = time.perf_counter() - started

    assert finished == ["create_issue", "close_issue"]
    assert elapsed >= 0.5, "the sleeping Hook still ran to the end"


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


async def test_a_user_file_that_raises_while_loading_leaves_the_daemon_up(
    config_dir: ConfigDir,
) -> None:
    """Not an import error: user code that runs at module level and fails. The Daemon keeps
    answering, the Proxy says why it is unhealthy, and nothing reached the Upstream."""
    server, received = tracker()
    await synced(config_dir, server)
    user_code(config_dir, "")

    async with running_daemon(config_dir) as daemon, daemon.client("/issues/mcp") as client:
        before = sorted(t.name for t in await client.list_tools())

        user_code(config_dir, 'settings = {}\nlimit = settings["limit"]\n')
        await asyncio.sleep(0.01)  # a new mtime, on file systems that count in whole seconds
        failed = await client.call_tool("close_issue", {"id": 2}, raise_on_error=False)
        status = await daemon.status()
        assert sorted(t.name for t in await client.list_tools()) == before

    proxy = status["upstreams"][0]["proxies"][0]
    assert proxy["health"] == "unhealthy"
    assert proxy["detail"] == "default.py, line 3: KeyError: 'limit'"
    assert error_text(failed) == "Proxy issues/default is unhealthy: " + proxy["detail"]
    assert received == []


async def test_daemon_reload_re_reads_every_proxy_and_reports_each_ones_health(
    config_dir: ConfigDir,
) -> None:
    """``daemon reload`` (story 44): every Proxy re-reads its files now, and the command says
    what came of it, unhealthy ones with their reason."""
    server, _ = tracker()
    await synced(config_dir, server)
    run_cli(config_dir, "proxy", "new", "issues/review")
    user_code(config_dir, "")

    async with serving_daemon(config_dir):
        user_code(config_dir, "import nonexistent_module\n", proxy="review")
        reloaded = await asyncio.to_thread(run_cli, config_dir, "daemon", "reload")
        assert reloaded.exit_code == 0, reloaded.output
        assert "Reloaded every Proxy" in reloaded.stdout
        assert re.search(r"review\s+unhealthy", reloaded.stdout), reloaded.stdout
        assert re.search(r"default\s+ok", reloaded.stdout), reloaded.stdout
        assert "issues/review: review.py, line 2: ModuleNotFoundError" in reloaded.stdout

        user_code(config_dir, "", proxy="review")
        recovered = await asyncio.to_thread(run_cli, config_dir, "daemon", "reload")
        assert re.search(r"review\s+ok", recovered.stdout), recovered.stdout
        assert "unhealthy" not in recovered.stdout


def test_daemon_reload_says_so_when_nothing_is_running(config_dir: ConfigDir) -> None:
    result = run_cli(config_dir, "daemon", "reload")

    assert result.exit_code == 0, result.output
    assert "Daemon not running" in result.stdout
    assert "reads every file when it starts" in result.stdout


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


# --- a Proxy file imports a helper next to it (#27) ---------------------------------------------


async def test_two_proxies_of_one_upstream_both_import_the_same_helper(
    config_dir: ConfigDir,
) -> None:
    server, received = tracker()
    await synced(config_dir, server)
    run_cli(config_dir, "proxy", "new", "issues/review")
    helper_code(config_dir, "TAG = 'proxied'\n")
    hook_body = """
        import helpers

        @hook.before("create_issue")
        def label(call):
            call.args["labels"] = [*(call.args.get("labels") or []), helpers.TAG]
        """
    user_code(config_dir, hook_body)
    user_code(config_dir, hook_body, proxy="review")

    async with running_daemon(config_dir) as daemon:
        async with daemon.client("/issues/mcp") as client:
            await client.call_tool("create_issue", {"title": "Bug", "labels": ["p1"]})
        async with daemon.client("/issues/review/mcp") as client:
            await client.call_tool("create_issue", {"title": "Bug2", "labels": ["p2"]})

    assert received == [
        ("create_issue", {"title": "Bug", "labels": ["p1", "proxied"]}),
        ("create_issue", {"title": "Bug2", "labels": ["p2", "proxied"]}),
    ]


async def test_editing_a_helper_alone_changes_both_proxies_on_the_next_request(
    config_dir: ConfigDir,
) -> None:
    server, received = tracker()
    await synced(config_dir, server)
    run_cli(config_dir, "proxy", "new", "issues/review")
    helper_code(config_dir, "TAG = 'v1'\n")
    hook_body = """
        import helpers

        @hook.before("create_issue")
        def label(call):
            call.args["labels"] = [helpers.TAG]
        """
    user_code(config_dir, hook_body)
    user_code(config_dir, hook_body, proxy="review")

    async with running_daemon(config_dir) as daemon:
        async with daemon.client("/issues/mcp") as client:
            await client.call_tool("create_issue", {"title": "A"})
        async with daemon.client("/issues/review/mcp") as client:
            await client.call_tool("create_issue", {"title": "B"})

        await asyncio.sleep(0.01)  # a new mtime, on file systems that count in whole seconds
        helper_code(config_dir, "TAG = 'v2'\n")

        async with daemon.client("/issues/mcp") as client:
            await client.call_tool("create_issue", {"title": "C"})
        async with daemon.client("/issues/review/mcp") as client:
            await client.call_tool("create_issue", {"title": "D"})

        (config_dir.path / "upstreams" / "issues" / "helpers.py").unlink()
        async with daemon.client("/issues/mcp") as client:
            gone = await client.call_tool("create_issue", {"title": "E"}, raise_on_error=False)
        assert re.search(r"Proxy issues/default is unhealthy: .*helpers", error_text(gone))

    assert received == [
        ("create_issue", {"title": "A", "labels": ["v1"]}),
        ("create_issue", {"title": "B", "labels": ["v1"]}),
        ("create_issue", {"title": "C", "labels": ["v2"]}),
        ("create_issue", {"title": "D", "labels": ["v2"]}),
    ]


async def test_a_helper_of_one_upstream_is_never_seen_by_anothers_proxy(
    config_dir: ConfigDir,
) -> None:
    issues_server, issues_received = tracker()
    await synced(config_dir, issues_server)

    calc_server = FastMCP[Any]("calc")
    calc_received: Received = []

    def add(a: int, b: int) -> int:
        calc_received.append(("add", {"a": a, "b": b}))
        return a + b

    calc_server.tool(add)
    config_dir.add_memory_upstream("calc", calc_server)
    synced_calc = await asyncio.to_thread(run_cli, config_dir, "upstream", "sync", "calc")
    assert synced_calc.exit_code == 0, synced_calc.output

    helper_code(config_dir, "TAG = 'issues-helper'\n", upstream="issues")
    helper_code(config_dir, "OFFSET = 1000\n", upstream="calc")
    user_code(
        config_dir,
        """
        import helpers

        @hook.before("create_issue")
        def label(call):
            call.args["labels"] = [helpers.TAG]
        """,
        upstream="issues",
    )
    user_code(
        config_dir,
        """
        import helpers

        @hook.before("add")
        def offset(call):
            call.args["a"] += helpers.OFFSET
        """,
        upstream="calc",
    )

    async with running_daemon(config_dir) as daemon:
        async with daemon.client("/issues/mcp") as client:
            await client.call_tool("create_issue", {"title": "Bug"})
        async with daemon.client("/calc/mcp") as client:
            await client.call_tool("add", {"a": 1, "b": 2})

    assert issues_received == [("create_issue", {"title": "Bug", "labels": ["issues-helper"]})]
    assert calc_received == [("add", {"a": 1001, "b": 2})]


def test_doctor_loads_a_helper_import_without_a_module_not_found_error(
    config_dir: ConfigDir,
) -> None:
    server, _ = tracker()
    asyncio.run(synced(config_dir, server))
    helper_code(config_dir, "TAG = 'ok'\n")
    user_code(
        config_dir,
        """
        import helpers

        @hook.before("create_issue")
        def label(call):
            call.args["labels"] = [helpers.TAG]
        """,
    )

    result = run_cli(config_dir, "doctor")

    assert result.exit_code == 0, result.output
    assert "ModuleNotFoundError" not in result.output


def test_doctor_reports_a_helper_that_raises_while_loading(config_dir: ConfigDir) -> None:
    server, _ = tracker()
    asyncio.run(synced(config_dir, server))
    helper_code(config_dir, "raise ValueError('boom')\n")
    user_code(config_dir, "import helpers\n")

    result = run_cli(config_dir, "doctor")

    assert result.exit_code == 1, result.output
    assert "ValueError" in result.output
    assert "boom" in result.output
