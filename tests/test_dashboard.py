"""The dashboard: static files the Daemon serves at ``/``, read-only, fed by ``/api`` (#17).

No build step: plain files shipped in the package. The page polls ``/api/status`` and
``/api/calls`` and asks for a Catalog, its Drift, or a Proxy's exposed set on request. One
route the page needs is new: a Proxy's exposed set, the Catalog with its Overrides applied, as
the Daemon already derives it. ``[daemon] dashboard = false`` leaves ``/`` unmounted, and
``mcpshape ui`` opens the page, or says why it cannot.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from mcpshape.api import STATUS_PATH, UPSTREAMS_PATH
from tests.support.seam import free_port, run_cli, running_daemon, serving_daemon
from tests.test_catalog_drift import cli, notes
from tests.test_overrides import issues
from tests.test_proxy_seam import calculator

if TYPE_CHECKING:
    import pytest

    from tests.support.seam import ConfigDir

TOKEN = "s3cret"


def settings(config_dir: ConfigDir, **daemon: object) -> None:
    lines = "".join(
        f"{key} = {str(value).lower() if isinstance(value, bool) else value!r}\n"
        for key, value in daemon.items()
    )
    (config_dir.path / "config.toml").write_text(f"version = 1\n[daemon]\n{lines}")


def by_name(items: list[dict[str, Any]], kind: str) -> dict[str, dict[str, Any]]:
    return {item["name"]: item for item in items if item["kind"] == kind}


def browser(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Stub the browser: what ``ui`` asks it to open lands here instead."""
    opened: list[str] = []

    def open_url(url: str, *_args: object, **_kwargs: object) -> bool:
        opened.append(url)
        return True

    monkeypatch.setattr("webbrowser.open", open_url)
    return opened


# --- the page ------------------------------------------------------------------------------


async def test_the_dashboard_is_served_at_the_root_and_reads_only_the_api(
    config_dir: ConfigDir,
) -> None:
    config_dir.add_memory_upstream("calc", calculator())

    async with running_daemon(config_dir) as daemon:
        page = await daemon.request("GET", "/")
        assert page.status_code == 200
        assert "text/html" in page.headers["content-type"]
        assert "mcpshape" in page.text
        assert "read-only" in page.text

        script = await daemon.request("GET", "/app.js")
        assert script.status_code == 200
        for path in (STATUS_PATH, "/api/calls", "/catalog", "/drift", "/exposed"):
            assert path in script.text, f"the page never reads {path}"

        style = await daemon.request("GET", "/style.css")
        assert style.status_code == 200

        assert (await daemon.request("GET", "/nothing.css")).status_code == 404
        assert (await daemon.request("GET", "/__init__.py")).status_code == 404, (
            "only the page's files are served, never the package's"
        )
        assert (await daemon.request("GET", "/api/nope")).status_code == 404

        async with daemon.client("/calc/mcp") as client:  # the Proxies still answer beside it
            assert (await client.call_tool("add", {"a": 2, "b": 3})).data == 5


async def test_the_dashboard_is_behind_the_bearer_token_like_every_route(
    config_dir: ConfigDir,
) -> None:
    config_dir.add_memory_upstream("calc", calculator())

    async with running_daemon(config_dir, token=TOKEN) as daemon:
        assert (await daemon.request("GET", "/")).status_code == 401
        page = await daemon.request("GET", "/", headers={"Authorization": f"Bearer {TOKEN}"})
        assert page.status_code == 200
        assert "read-only" in page.text


async def test_the_dashboard_can_be_switched_off_in_the_global_settings(
    config_dir: ConfigDir,
) -> None:
    config_dir.add_memory_upstream("calc", calculator())
    settings(config_dir, dashboard=False)

    async with running_daemon(config_dir) as daemon:
        assert (await daemon.request("GET", "/")).status_code == 404
        assert (await daemon.request("GET", "/app.js")).status_code == 404
        status, live = await daemon.api("GET", STATUS_PATH)
        assert status == 200
        assert [upstream["name"] for upstream in live["upstreams"]] == ["calc"]

        async with daemon.client("/calc/mcp") as client:
            assert (await client.call_tool("add", {"a": 2, "b": 3})).data == 5


# --- a Proxy's exposed set -------------------------------------------------------------------


async def test_the_exposed_set_route_answers_the_catalog_with_its_overrides_applied(
    config_dir: ConfigDir,
) -> None:
    config_dir.add_memory_upstream("issues", issues())
    await cli(config_dir, "upstream", "sync", "issues")
    hidden = run_cli(config_dir, "tool", "hide", "issues/default", "list_issues")
    assert hidden.exit_code == 0, hidden.output
    renamed = run_cli(config_dir, "tool", "rename", "issues/default", "create_issue", "new_issue")
    assert renamed.exit_code == 0, renamed.output

    async with running_daemon(config_dir) as daemon:
        status, answer = await daemon.api("GET", f"{UPSTREAMS_PATH}/issues/proxies/default/exposed")
        assert status == 200
        tools = by_name(answer["items"], "tool")
        assert tools["new_issue"]["origin"] == "create_issue"
        assert tools["new_issue"]["hidden"] is False
        assert tools["new_issue"]["description"]
        assert tools["list_issues"]["hidden"] is True
        assert tools["list_issues"]["origin"] == "list_issues"
        assert "create_issue" not in tools, "a renamed tool is listed under its exposed name"
        assert answer["name"] == "issues/default"

        status, _ = await daemon.api("GET", f"{UPSTREAMS_PATH}/issues/proxies/nothing/exposed")
        assert status == 404
        status, _ = await daemon.api("GET", f"{UPSTREAMS_PATH}/nothing/proxies/default/exposed")
        assert status == 404


async def test_the_exposed_set_covers_every_kind_of_item(config_dir: ConfigDir) -> None:
    config_dir.add_memory_upstream("notes", notes())
    await cli(config_dir, "upstream", "sync", "notes")

    async with running_daemon(config_dir) as daemon:
        status, answer = await daemon.api("GET", f"{UPSTREAMS_PATH}/notes/proxies/default/exposed")
        assert status == 200
        assert answer["health"] == "ok"
        assert answer["scanned"] is True
        assert set(by_name(answer["items"], "tool")) == {"add_note"}
        assert set(by_name(answer["items"], "resource")) == {"notes://all"}
        assert set(by_name(answer["items"], "resource_template")) == {"notes://{id}"}
        assert set(by_name(answer["items"], "prompt")) == {"greeting"}
        assert all(item["hidden"] is False for item in answer["items"])


async def test_the_exposed_set_says_when_the_proxy_is_unhealthy_or_the_upstream_unscanned(
    config_dir: ConfigDir,
) -> None:
    """An unhealthy Proxy keeps advertising its last exposed set; the route says so rather
    than reporting that set as the Proxy's choices."""
    config_dir.add_memory_upstream("calc", calculator())
    config_dir.break_upstream("calc")  # so the Daemon's start-up scan reaches nothing

    async with running_daemon(config_dir) as daemon:
        status, answer = await daemon.api("GET", f"{UPSTREAMS_PATH}/calc/proxies/default/exposed")
        assert status == 200
        assert answer["scanned"] is False
        assert answer["items"] == []

        config_dir.restore_upstream("calc")
        await cli(config_dir, "upstream", "sync", "calc")
        (config_dir.path / "upstreams" / "calc" / "default.py").write_text("this is not python (\n")

        status, answer = await daemon.api("GET", f"{UPSTREAMS_PATH}/calc/proxies/default/exposed")
        assert status == 200
        assert answer["health"] == "unhealthy"
        assert "default.py" in answer["detail"]
        assert answer["scanned"] is True


async def test_the_exposed_set_lists_a_virtual_tool_beside_the_catalogs(
    config_dir: ConfigDir,
) -> None:
    config_dir.add_memory_upstream("calc", calculator())
    await cli(config_dir, "upstream", "sync", "calc")
    (config_dir.path / "upstreams" / "calc" / "default.py").write_text(
        "from mcpshape import tool\n\n\n"
        "@tool\n"
        "def double(x: int) -> int:\n"
        '    """Twice x."""\n'
        "    return x * 2\n"
    )

    async with running_daemon(config_dir) as daemon:
        status, answer = await daemon.api("GET", f"{UPSTREAMS_PATH}/calc/proxies/default/exposed")
        assert status == 200
        tools = by_name(answer["items"], "tool")
        assert tools["add"]["virtual"] is False
        assert tools["double"]["virtual"] is True
        assert tools["double"]["origin"] is None
        assert tools["double"]["description"] == "Twice x."


# --- mcpshape ui -----------------------------------------------------------------------------


async def test_ui_opens_the_dashboard_of_a_running_daemon(
    config_dir: ConfigDir, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir.add_memory_upstream("calc", calculator())
    opened = browser(monkeypatch)

    async with serving_daemon(config_dir) as url:
        result = await asyncio.to_thread(run_cli, config_dir, "ui")

    assert result.exit_code == 0, result.output
    assert opened == [f"{url}/"]
    assert f"{url}/" in result.stdout


def test_ui_says_so_when_the_daemon_is_not_running(
    config_dir: ConfigDir, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir.add_memory_upstream("calc", calculator())
    settings(config_dir, port=free_port())
    opened = browser(monkeypatch)

    result = run_cli(config_dir, "ui")

    assert result.exit_code == 0, result.output
    assert "Daemon not running" in result.output
    assert "mcpshape daemon up" in result.output
    assert opened == []


async def test_ui_says_so_when_the_daemon_serves_no_dashboard(
    config_dir: ConfigDir, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What the running Daemon answers decides, not the file: the Daemon read the setting
    at its start, and the file may have changed since."""
    config_dir.add_memory_upstream("calc", calculator())
    opened = browser(monkeypatch)

    async with serving_daemon(config_dir, dashboard=False) as url:
        config = config_dir.path / "config.toml"
        config.write_text(config.read_text().replace("dashboard = false", "dashboard = true"))
        # edited since the Daemon started: the Daemon still serves no page
        result = await asyncio.to_thread(run_cli, config_dir, "ui")

    assert result.exit_code != 0, result.output
    assert "dashboard = false" in result.output
    assert url not in "".join(opened)
    assert opened == []


async def test_ui_says_a_token_guarded_page_needs_the_users_routing(
    config_dir: ConfigDir, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir.add_memory_upstream("calc", calculator())
    opened = browser(monkeypatch)

    async with serving_daemon(config_dir, token=TOKEN) as url:
        result = await asyncio.to_thread(run_cli, config_dir, "ui")

    assert result.exit_code == 0, result.output
    assert "token" in result.output
    assert f"{url}/" in result.output
    assert opened == []


async def test_ui_says_so_when_no_browser_opens(
    config_dir: ConfigDir, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A headless machine has no browser to open; the URL is printed to open by hand."""
    config_dir.add_memory_upstream("calc", calculator())

    def no_browser(_url: str, *_args: object, **_kwargs: object) -> bool:
        return False

    monkeypatch.setattr("webbrowser.open", no_browser)

    async with serving_daemon(config_dir) as url:
        result = await asyncio.to_thread(run_cli, config_dir, "ui")

    assert result.exit_code == 0, result.output
    assert "No browser" in result.output
    assert f"{url}/" in result.output
