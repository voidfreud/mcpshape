"""OAuth Upstreams: logging in from the CLI, and reaching one afterwards (stories 3, 4, 69).

Everything runs against the fake provider in ``tests.support.oauth_provider``, served on a
loopback port with the Upstream it protects behind it. The seam is the usual one: the CLI
through Typer's runner, the Daemon app in-process, a FastMCP Client over ASGI, and the file
system. The browser is the one thing stubbed, since no test may open one.
"""

from __future__ import annotations

import asyncio
import json
import stat
import threading
import time
from typing import TYPE_CHECKING

import httpx2
from fastmcp import Client
from mcp_types import TextContent

from tests.support import oauth_provider
from tests.support.clock import FakeClock
from tests.support.oauth_provider import Issuer, serving_provider
from tests.support.seam import run_cli, running_daemon, serving_daemon, until
from tests.test_catalog_drift import cli
from tests.test_proxy_seam import calculator

if TYPE_CHECKING:
    from pathlib import Path

    import pytest
    from fastmcp.client.client import CallToolResult

    from tests.support.oauth_provider import Provider
    from tests.support.seam import ConfigDir, RunningDaemon

CONNECTED = ("ready", "idle-pending")
LOGGED_IN = "Logged in."


def token_file(cfg: ConfigDir, upstream: str) -> Path:
    return cfg.state / "oauth" / f"{upstream}.json"


def key_file(cfg: ConfigDir) -> Path:
    return cfg.state / "oauth.key"


def access_token(provider: Provider) -> str:
    """The last token the provider handed out, which is the one in use."""
    return provider.issuer.issued[-1]


def visiting_browser(monkeypatch: pytest.MonkeyPatch, *, deny: bool = False) -> None:
    """Stub the browser: something visits the URL and follows the redirect to the callback.
    ``deny`` is the user refusing at the consent screen."""

    def open_url(url: str, *_args: object, **_kwargs: object) -> bool:
        def visit() -> None:
            with httpx2.Client(follow_redirects=True) as browser:
                browser.get(url + ("&deny=1" if deny else ""))

        threading.Thread(target=visit, daemon=True).start()
        return True

    monkeypatch.setattr("webbrowser.open", open_url)


def error_text(result: CallToolResult) -> str:
    content = result.content[0]
    assert isinstance(content, TextContent)
    return content.text


async def upstream_error(daemon_status: dict[str, object], name: str) -> str:
    upstreams = daemon_status["upstreams"]
    assert isinstance(upstreams, list)
    found = next(u for u in upstreams if u["name"] == name)  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
    return str(found["error"])  # pyright: ignore[reportUnknownArgumentType]


def add_oauth_upstream(cfg: ConfigDir, provider: Provider, name: str = "x") -> None:
    """Write the Upstream by hand, for the tests whose login is not what they are about."""
    cfg.add_upstream(
        name, f'transport = "http"\nurl = {json.dumps(provider.mcp_url)}\nauth = "oauth"\n'
    )


# --- logging in from the CLI -------------------------------------------------------------------


async def test_adding_an_oauth_upstream_logs_in_and_stores_the_token_encrypted(
    config_dir: ConfigDir, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Story 3: the browser flow, and story 69: what it leaves on disk."""
    visiting_browser(monkeypatch)
    async with serving_provider(calculator()) as provider:
        result = await cli(config_dir, "add", "x", "--url", provider.mcp_url, "--oauth")

    assert LOGGED_IN in result.output
    token = access_token(provider)
    assert token not in result.output, "the login printed what it received"
    stored = token_file(config_dir, "x")
    assert stored.is_file()
    assert token.encode() not in stored.read_bytes(), "the token file is not encrypted"
    assert stat.S_IMODE(key_file(config_dir).stat().st_mode) == 0o600
    assert stat.S_IMODE(stored.stat().st_mode) == 0o600


async def test_upstream_sync_logs_in_when_no_token_is_stored_and_then_scans(
    config_dir: ConfigDir, monkeypatch: pytest.MonkeyPatch
) -> None:
    visiting_browser(monkeypatch)
    async with serving_provider(calculator()) as provider:
        add_oauth_upstream(config_dir, provider)
        assert not token_file(config_dir, "x").is_file()

        result = await cli(config_dir, "upstream", "sync", "x")

    assert LOGGED_IN in result.output
    assert "1 tool" in result.output
    assert token_file(config_dir, "x").is_file()


async def test_upstream_show_says_the_auth_kind_and_whether_a_token_is_stored(
    config_dir: ConfigDir, monkeypatch: pytest.MonkeyPatch
) -> None:
    visiting_browser(monkeypatch)
    async with serving_provider(calculator()) as provider:
        add_oauth_upstream(config_dir, provider)
        before = run_cli(config_dir, "upstream", "show", "x")
        assert "OAuth, not logged in" in before.output

        await cli(config_dir, "upstream", "sync", "x")
        after = run_cli(config_dir, "upstream", "show", "x")

    assert "OAuth, logged in" in after.output
    assert access_token(provider) not in after.output


async def test_upstream_rm_forgets_the_stored_login(
    config_dir: ConfigDir, monkeypatch: pytest.MonkeyPatch
) -> None:
    visiting_browser(monkeypatch)
    async with serving_provider(calculator()) as provider:
        await cli(config_dir, "add", "x", "--url", provider.mcp_url, "--oauth")
        assert token_file(config_dir, "x").is_file()

        await cli(config_dir, "upstream", "rm", "x", "--yes")

    assert not token_file(config_dir, "x").is_file()


async def test_device_code_pairing_prints_the_uri_and_the_code_and_stores_the_token(
    config_dir: ConfigDir,
) -> None:
    """Story 4: the machine where no browser can open."""
    async with serving_provider(calculator()) as provider:
        approving = asyncio.create_task(_approve_when_asked(provider))
        result = await cli(config_dir, "add", "x", "--url", provider.mcp_url, "--oauth", "--device")
        await approving

        async with running_daemon(config_dir) as daemon, daemon.client("/x/mcp") as client:
            assert (await client.call_tool("add", {"a": 2, "b": 3})).data == 5

    assert "/activate" in result.output
    assert oauth_provider.USER_CODE in result.output
    assert access_token(provider) not in result.output
    assert token_file(config_dir, "x").is_file()


async def test_device_code_pairing_says_when_the_provider_offers_none(
    config_dir: ConfigDir,
) -> None:
    async with serving_provider(calculator(), Issuer(device_offered=False)) as provider:
        result = await asyncio.to_thread(
            run_cli, config_dir, "add", "x", "--url", provider.mcp_url, "--oauth", "--device"
        )

    assert result.exit_code == 1
    assert "no device-code pairing" in result.output


async def _approve_when_asked(provider: Provider) -> None:
    """Stand in for the user typing the code at the provider on another machine."""
    await until(provider.issuer.awaiting_device, "a device code to approve")
    provider.issuer.approve_device()


async def test_a_refused_login_is_not_a_stored_login_to_show_or_sync(
    config_dir: ConfigDir, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#56: the registration a refused login leaves behind is not a login."""
    visiting_browser(monkeypatch, deny=True)
    async with serving_provider(calculator()) as provider:
        refused = await asyncio.to_thread(
            run_cli, config_dir, "add", "x", "--url", provider.mcp_url, "--oauth"
        )
        assert refused.exit_code == 1
        assert token_file(config_dir, "x").is_file(), "the registration was not stored"

        shown = run_cli(config_dir, "upstream", "show", "x")
        assert "OAuth, not logged in" in shown.output

        visiting_browser(monkeypatch)
        synced = await cli(config_dir, "upstream", "sync", "x")

    assert LOGGED_IN in synced.output
    assert "1 tool" in synced.output
    assert "!" not in synced.output, "sync tried a scan before logging in"


async def test_a_login_from_the_cli_makes_a_running_daemon_connect_now(
    config_dir: ConfigDir, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#58: the clock never moves, so only the CLI telling the Daemon gets it out of the
    backoff its revoked token put it in."""
    clock = FakeClock()
    visiting_browser(monkeypatch)
    async with serving_provider(calculator(), Issuer(token_ttl=1)) as provider:
        add_oauth_upstream(config_dir, provider)
        await cli(config_dir, "upstream", "sync", "x")
        provider.issuer.refresh_accepted = False
        await asyncio.sleep(1.1)  # the stored token is dead and cannot be renewed
        provider.issuer.token_ttl = 3600  # what the new login gets lasts

        async with serving_daemon(config_dir, clock) as url, Client(f"{url}/x/mcp") as client:
            failed = await client.call_tool("add", {"a": 1, "b": 1}, raise_on_error=False)
            assert failed.is_error
            await _awaiting_state(url, "x", "unavailable")

            await cli(config_dir, "upstream", "sync", "x")

            await _awaiting_state(url, "x", *CONNECTED)
            assert (await client.call_tool("add", {"a": 2, "b": 3})).data == 5


async def _awaiting_state(url: str, upstream: str, *states: str, patience: float = 5.0) -> None:
    """Wait until ``/api/status`` at ``url``, read over the socket from a thread so the Daemon
    serving it keeps running, says ``upstream`` is in one of ``states``."""

    def state() -> str:
        answer = httpx2.get(f"{url}/api/status").json()
        return str(next(u["state"] for u in answer["upstreams"] if u["name"] == upstream))

    deadline = time.monotonic() + patience
    seen = await asyncio.to_thread(state)
    while seen not in states:
        if time.monotonic() > deadline:
            msg = f"Upstream {upstream} stayed {seen!r}, never reached {states}"
            raise AssertionError(msg)
        await asyncio.sleep(0.02)
        seen = await asyncio.to_thread(state)


# --- reaching the Upstream afterwards ----------------------------------------------------------


async def test_a_daemon_uses_the_stored_token_with_no_login_across_a_restart(
    config_dir: ConfigDir, monkeypatch: pytest.MonkeyPatch
) -> None:
    visiting_browser(monkeypatch)
    async with serving_provider(calculator()) as provider:
        await cli(config_dir, "add", "x", "--url", provider.mcp_url, "--oauth")
        monkeypatch.setattr("webbrowser.open", _no_browser)

        for _ in range(2):  # a Daemon started now, and one started after a restart
            async with running_daemon(config_dir) as daemon, daemon.client("/x/mcp") as client:
                assert (await client.call_tool("add", {"a": 2, "b": 3})).data == 5
                assert await daemon.upstream_state("x") in CONNECTED


async def test_an_expiring_token_is_refreshed_with_no_user_action(
    config_dir: ConfigDir, monkeypatch: pytest.MonkeyPatch
) -> None:
    visiting_browser(monkeypatch)
    async with serving_provider(calculator(), Issuer(token_ttl=1)) as provider:
        await cli(config_dir, "add", "x", "--url", provider.mcp_url, "--oauth")
        monkeypatch.setattr("webbrowser.open", _no_browser)
        refreshes = provider.issuer.refreshes

        async with running_daemon(config_dir) as daemon, daemon.client("/x/mcp") as client:
            assert (await client.call_tool("add", {"a": 2, "b": 3})).data == 5
            await asyncio.sleep(1.1)  # past the moment the provider said the token dies
            assert (await client.call_tool("add", {"a": 1, "b": 1})).data == 2
            assert await daemon.upstream_state("x") in CONNECTED

    assert provider.issuer.refreshes > refreshes, "the provider saw no refresh"


async def test_a_revoked_refresh_leaves_the_upstream_unavailable_naming_the_login(
    config_dir: ConfigDir, monkeypatch: pytest.MonkeyPatch
) -> None:
    message = "The Upstream is not up; nothing was written."
    visiting_browser(monkeypatch)
    async with serving_provider(calculator(), Issuer(token_ttl=1)) as provider:
        config_dir.add_upstream(
            "x",
            f'transport = "http"\nurl = {json.dumps(provider.mcp_url)}\nauth = "oauth"\n',
            {"unavailable_message": message},
        )
        await cli(config_dir, "upstream", "sync", "x")
        monkeypatch.setattr("webbrowser.open", _no_browser)
        provider.issuer.refresh_accepted = False
        await asyncio.sleep(1.1)  # the stored token is dead and cannot be renewed

        async with running_daemon(config_dir) as daemon, daemon.client("/x/mcp") as client:
            result = await client.call_tool("add", {"a": 1, "b": 1}, raise_on_error=False)
            assert result.is_error
            assert message in error_text(result)
            assert await daemon.awaiting_state("x", "unavailable") == "unavailable"
            reason = await upstream_error(await daemon.status(), "x")

    assert "mcpshape upstream sync x" in reason
    assert access_token(provider) not in reason


async def test_upstream_sync_logs_in_again_when_the_stored_login_stopped_working(
    config_dir: ConfigDir, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The command an unavailable OAuth Upstream names is the command that fixes it."""
    visiting_browser(monkeypatch)
    async with serving_provider(calculator(), Issuer(token_ttl=1)) as provider:
        add_oauth_upstream(config_dir, provider)
        await cli(config_dir, "upstream", "sync", "x")
        provider.issuer.refresh_accepted = False
        await asyncio.sleep(1.1)  # the stored token is dead and cannot be renewed
        provider.issuer.token_ttl = 3600  # what the new login gets lasts

        result = await cli(config_dir, "upstream", "sync", "x")

    assert LOGGED_IN in result.output
    assert access_token(provider) not in result.output


async def test_a_key_file_anyone_else_can_read_is_refused(
    config_dir: ConfigDir, monkeypatch: pytest.MonkeyPatch
) -> None:
    visiting_browser(monkeypatch)
    async with serving_provider(calculator()) as provider:
        await cli(config_dir, "add", "x", "--url", provider.mcp_url, "--oauth")
        key_file(config_dir).chmod(0o644)
        add_oauth_upstream(config_dir, provider, "y")

        result = await asyncio.to_thread(run_cli, config_dir, "upstream", "sync", "y")

    assert result.exit_code == 1
    assert "oauth.key" in result.output
    assert "0600" in result.output
    assert access_token(provider) not in result.output


async def test_an_oauth_upstream_with_no_stored_login_says_which_command_logs_it_in(
    config_dir: ConfigDir, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Daemon opens no browser: a login it has not got is one the user runs."""
    visiting_browser(monkeypatch)
    async with serving_provider(calculator()) as provider:
        add_oauth_upstream(config_dir, provider)
        await cli(config_dir, "upstream", "sync", "x")
        token_file(config_dir, "x").unlink()
        monkeypatch.setattr("webbrowser.open", _no_browser)

        async with running_daemon(config_dir) as daemon, daemon.client("/x/mcp") as client:
            result = await client.call_tool("add", {"a": 1, "b": 1}, raise_on_error=False)
            assert result.is_error
            assert await daemon.awaiting_state("x", "unavailable") == "unavailable"
            reason = await upstream_error(await daemon.status(), "x")

    assert "mcpshape upstream sync x" in reason


def _no_browser(url: str, *_args: object, **_kwargs: object) -> bool:
    msg = f"a browser was opened at {url} when a stored token should have been used"
    raise AssertionError(msg)


# --- the dashboard's flow, through the API -----------------------------------------------------


async def test_the_api_starts_a_login_without_a_browser_and_says_where_it_stands(
    config_dir: ConfigDir, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``/api`` flow (#16): the Daemon hands back the provider's page instead of opening
    it, takes the callback on loopback, and the Upstream is usable once the token is stored."""
    monkeypatch.setattr("webbrowser.open", _no_browser)
    async with serving_provider(calculator()) as provider:
        add_oauth_upstream(config_dir, provider)

        async with running_daemon(config_dir) as daemon:
            code, before = await daemon.api("GET", "/api/upstreams/x/oauth")
            assert (code, before) == (200, _login_state(stored=False))

            code, started = await daemon.api("POST", "/api/upstreams/x/oauth")
            assert code == 200, started
            assert started["pending"] is True
            assert started["stored"] is False
            url = started["url"]
            assert isinstance(url, str)
            assert url.startswith(provider.base)

            code, again = await daemon.api("POST", "/api/upstreams/x/oauth")
            assert (code, again["url"]) == (200, url), "a second start joined the pending login"

            await asyncio.to_thread(_visit, url)
            after = await _settled_login(daemon)
            assert after == _login_state(stored=True)
            assert access_token(provider) not in json.dumps(after)

            async with daemon.client("/x/mcp") as client:
                assert (await client.call_tool("add", {"a": 2, "b": 3})).data == 5
            assert await daemon.upstream_state("x") in CONNECTED


async def test_the_api_login_says_why_when_the_user_refuses_at_the_provider(
    config_dir: ConfigDir, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("webbrowser.open", _no_browser)
    async with serving_provider(calculator()) as provider:
        add_oauth_upstream(config_dir, provider)

        async with running_daemon(config_dir) as daemon:
            code, started = await daemon.api("POST", "/api/upstreams/x/oauth")
            assert code == 200, started

            await asyncio.to_thread(_visit, started["url"], deny=True)
            after = await _settled_login(daemon)

    assert after["stored"] is False, "a refused login left a token set behind"
    assert after["url"] is None
    assert "denied" in str(after["error"])


def _login_state(*, stored: bool) -> dict[str, object]:
    """What ``/api/upstreams/<name>/oauth`` says when no login is pending."""
    return {"stored": stored, "pending": False, "url": None, "error": None}


def _visit(url: str, *, deny: bool = False) -> None:
    """Stand in for the user's browser: open the provider's page and follow it to the
    callback. ``deny`` is the user refusing at the consent screen."""
    with httpx2.Client(follow_redirects=True) as browser:
        browser.get(url + ("&deny=1" if deny else ""))


async def _settled_login(daemon: RunningDaemon, patience: float = 5.0) -> dict[str, object]:
    """The login's state once it is no longer pending."""
    deadline = time.monotonic() + patience
    while True:
        _, state = await daemon.api("GET", "/api/upstreams/x/oauth")
        if not state["pending"]:
            return state
        if time.monotonic() > deadline:
            msg = f"the login never finished: {state}"
            raise AssertionError(msg)
        await asyncio.sleep(0.02)
