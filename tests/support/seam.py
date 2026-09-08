"""The one seam every test drives the system through.

A temp config directory, the Daemon app built in-process, in-memory Upstreams, and a FastMCP
Client that reaches the app over ASGI. Tests never import internal modules.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import socket
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

import httpx2
import pytest
import uvicorn
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from typer.testing import CliRunner, Result  # annotated at runtime

from mcpshape.api import STATUS_PATH
from mcpshape.cli import app
from mcpshape.config import memory_upstreams_allowed
from mcpshape.daemon import build_app, serve_all
from tests.support import child_upstream, upstreams
from tests.support.asgi import asgi_client_factory

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable, Generator, Mapping, Sequence
    from pathlib import Path

    from mcp.server.streamable_http import StreamableHTTPServerTransport
    from starlette.types import ASGIApp

    from mcpshape.connection import Clock

BASE_URL = "http://mcpshape.test"
# A wide, plain terminal so help text and tables render the same on every machine and CI.
CLI_ENV = {"COLUMNS": "200", "NO_COLOR": "1"}
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


@dataclass
class ConfigDir:
    """A temp config directory that tests populate before building the Daemon."""

    path: Path
    state: Path
    _registered: list[str] = field(default_factory=list[str])
    _servers: dict[str, FastMCP] = field(default_factory=dict[str, FastMCP])

    def add_upstream(
        self,
        name: str,
        body: str,
        lifecycle: dict[str, object] | None = None,
        proxies: Sequence[str] = ("default",),
    ) -> None:
        """Write the Upstream ``name``, whose ``upstream.toml`` says ``body``, and its Proxies."""
        upstream_dir = self.path / "upstreams" / name
        upstream_dir.mkdir(parents=True)
        (upstream_dir / "upstream.toml").write_text(
            f"version = 1\n{body}" + _lifecycle_table(lifecycle)
        )
        for proxy in proxies:
            (upstream_dir / f"{proxy}.toml").write_text("version = 1\n")

    def add_memory_upstream(
        self, name: str, server: FastMCP, lifecycle: dict[str, object] | None = None
    ) -> None:
        """Register ``server`` as the Upstream called ``name``, with the lifecycle it needs."""
        target = upstreams.register(name, server)
        self._registered.append(name)
        self._servers[name] = server
        self.add_upstream(name, f'transport = "memory"\ntarget = "{target}"\n', lifecycle)

    def add_stdio_upstream(
        self,
        name: str,
        command: str,
        args: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        lifecycle: dict[str, object] | None = None,
    ) -> None:
        """Register an Upstream the Daemon reaches by spawning ``command``."""
        body = (
            f'transport = "stdio"\ncommand = {json.dumps(command)}\n'
            f"args = {json.dumps(list(args))}\n"
        )
        if env:
            body += "\n[env]\n" + "".join(f"{key} = {json.dumps(v)}\n" for key, v in env.items())
        self.add_upstream(name, body, lifecycle)

    def add_url_upstream(
        self,
        name: str,
        url: str,
        kind: str = "http",
        lifecycle: dict[str, object] | None = None,
    ) -> None:
        """Register an Upstream the Daemon reaches by URL, over Streamable HTTP or legacy SSE."""
        self.add_upstream(name, f'transport = "{kind}"\nurl = {json.dumps(url)}\n', lifecycle)

    def add_proxy(self, upstream: str, proxy: str) -> None:
        """Give ``upstream`` one more Proxy, which shares the one connection its Upstream has."""
        (self.path / "upstreams" / upstream / f"{proxy}.toml").write_text("version = 1\n")

    def write_secrets(self, values: Mapping[str, str], mode: int = 0o600) -> Path:
        """Write the secrets file the ``${VAR}`` references resolve from, with ``mode``."""
        path = self.path / "secrets.toml"
        written = "".join(f"{key} = {json.dumps(value)}\n" for key, value in values.items())
        path.write_text(f"version = 1\n\n[secrets]\n{written}")
        path.chmod(mode)
        return path

    def break_upstream(self, name: str) -> None:
        """Take the Upstream's server away, so the next connect to it fails.

        What a Client sees is what matters: an Upstream that cannot be reached. A connection
        already open survives, exactly as a running server does when its host goes away.
        """
        upstreams.unregister(name)

    def restore_upstream(self, name: str, server: FastMCP | None = None) -> None:
        """Put the Upstream's server back, so the next connect to it succeeds."""
        upstreams.register(name, server or self._servers[name])

    def cleanup(self) -> None:
        for name in self._registered:
            upstreams.unregister(name)


@pytest.fixture
def config_dir(tmp_path: Path) -> Generator[ConfigDir]:
    """The temp config directory, with the seam's own ``memory`` transport enabled (#18)."""
    cfg = ConfigDir(tmp_path / "config", tmp_path / "state")
    cfg.path.mkdir()
    try:
        with memory_upstreams_allowed():
            yield cfg
    finally:
        cfg.cleanup()


@dataclass(frozen=True)
class RunningDaemon:
    """The Daemon app, running in-process behind an ASGI transport."""

    app: ASGIApp

    def client(
        self, path: str, headers: dict[str, str] | None = None
    ) -> Client[StreamableHttpTransport]:
        """A FastMCP Client for the Proxy served at ``path`` (for example ``/calc/mcp``)."""
        transport = StreamableHttpTransport(
            f"{BASE_URL}{path}",
            headers=headers,
            httpx_client_factory=asgi_client_factory(self.app, BASE_URL),
        )
        return Client(transport)

    async def status(self, headers: dict[str, str] | None = None) -> dict[str, Any]:
        """What the Daemon reports at ``/api/status``, as any HTTP caller would read it."""
        _, answer = await self.api("GET", STATUS_PATH, headers=headers)
        return answer

    async def api(
        self,
        method: str,
        path: str,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, Any]:
        """Call the management API at ``path``: the status code and the JSON it answered."""
        factory = asgi_client_factory(self.app, BASE_URL)
        async with factory() as http:
            answer = await http.request(method, f"{BASE_URL}{path}", params=params, headers=headers)
        return answer.status_code, json.loads(answer.text)

    async def request(
        self, method: str, path: str, headers: dict[str, str] | None = None
    ) -> httpx2.Response:
        """One raw HTTP request to the Daemon at ``path``, for what is not JSON: a redirect,
        a plain not found, a page."""
        factory = asgi_client_factory(self.app, BASE_URL)
        async with factory() as http:
            return await http.request(method, f"{BASE_URL}{path}", headers=headers)

    async def upstream(self, name: str) -> dict[str, Any]:
        """What the Daemon says about the one Upstream called ``name`` at ``/api/status``."""
        for upstream in (await self.status())["upstreams"]:
            if upstream["name"] == name:
                return upstream
        msg = f"the Daemon reports no Upstream named {name!r}"
        raise AssertionError(msg)

    async def upstream_state(self, name: str) -> str:
        return str((await self.upstream(name))["state"])

    async def awaiting_state(self, name: str, *states: str, patience: float = 5.0) -> str:
        """Wait until ``name`` reaches one of ``states``, and say which."""
        deadline = time.monotonic() + patience
        seen = await self.upstream_state(name)
        while seen not in states:
            if time.monotonic() > deadline:
                msg = f"Upstream {name} stayed {seen!r}, never reached {states}"
                raise AssertionError(msg)
            await asyncio.sleep(0.01)
            seen = await self.upstream_state(name)
        return seen


@contextlib.asynccontextmanager
async def running_daemon(
    cfg: ConfigDir, clock: Clock | None = None, token: str | None = None
) -> AsyncGenerator[RunningDaemon]:
    """Build the Daemon app from ``cfg`` and run its lifespan for the duration."""
    daemon_app = build_app(cfg.path, cfg.state, clock, token)
    app = daemon_app.main
    async with app.router.lifespan_context(app):
        yield RunningDaemon(app)


async def awaiting_state_at(url: str, name: str, *states: str, patience: float = 5.0) -> str:
    """``RunningDaemon.awaiting_state`` for a Daemon served on a socket at ``url``.

    Read from a thread, so the Daemon, which runs on this loop, keeps answering.
    """

    def state() -> str:
        answer = httpx2.get(f"{url}{STATUS_PATH}").json()
        return str(next(u["state"] for u in answer["upstreams"] if u["name"] == name))

    deadline = time.monotonic() + patience
    seen = await asyncio.to_thread(state)
    while seen not in states:
        if time.monotonic() > deadline:
            msg = f"Upstream {name} stayed {seen!r}, never reached {states}"
            raise AssertionError(msg)
        await asyncio.sleep(0.02)
        seen = await asyncio.to_thread(state)
    return seen


async def until(ready: Callable[[], bool], what: str, patience: float = 5.0) -> None:
    """Wait for something the file system or the CLI can see, or say it never happened."""
    deadline = time.monotonic() + patience
    while not ready():
        if time.monotonic() > deadline:
            msg = f"{what} never happened"
            raise AssertionError(msg)
        await asyncio.sleep(0.01)


def _lifecycle_table(lifecycle: dict[str, object] | None) -> str:
    if not lifecycle:
        return ""
    keys = "".join(f"{key} = {json.dumps(value)}\n" for key, value in lifecycle.items())
    return f"\n[lifecycle]\n{keys}"


@dataclass(frozen=True)
class CliResult:
    """What the user saw: exit code, the combined output, and stdout on its own."""

    exit_code: int
    output: str
    stdout: str
    """Only what went to stdout, so a test can parse what a command was asked to print."""


def run_cli(
    cfg: ConfigDir,
    *args: str,
    env: dict[str, str] | None = None,
    answers: str | None = None,
) -> CliResult:
    """Run the mcpshape CLI against ``cfg`` through Typer's runner.

    ``env`` goes on top of the plain terminal every test gets; ``answers`` is typed at
    whatever the command asks, one line per prompt.
    """
    result = CliRunner().invoke(
        app,
        ["--config-dir", str(cfg.path), "--state-dir", str(cfg.state), *args],
        env={**CLI_ENV, **(env or {})},
        input=answers,
    )
    return _result(result)


def run_cli_with_env(env: dict[str, str], *args: str, answers: str | None = None) -> CliResult:
    """Run the CLI without ``--config-dir``, letting ``env`` decide where config lives."""
    result = CliRunner().invoke(app, list(args), env={**CLI_ENV, **env}, input=answers)
    return _result(result)


def _result(result: Result) -> CliResult:
    return CliResult(
        exit_code=result.exit_code,
        output=ANSI.sub("", result.output),
        stdout=ANSI.sub("", result.stdout),
    )


def free_port() -> int:
    """A TCP port nothing listens on right now."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextlib.asynccontextmanager
async def serving_daemon(
    cfg: ConfigDir,
    clock: Clock | None = None,
    token: str | None = None,
    *,
    dashboard: bool = True,
) -> AsyncGenerator[str]:
    """Run the Daemon from ``cfg`` on a loopback port, as ``daemon up`` would, and yield its URL.

    For the tests that need a socket: a subprocess speaking to a Proxy, or the CLI reading
    live state. Everything else uses ``running_daemon``. The port is written into
    ``config.toml`` so the CLI computes the same URLs, with ``dashboard = false`` when asked.
    The Daemon's own ``/api/shutdown`` stop event is what is watched, so ``daemon down`` and
    this fixture's own cleanup agree.
    """
    port = free_port()
    settings = f"version = 1\n[daemon]\nport = {port}\n"
    if token:
        settings += f'token = "{token}"\n'
    if not dashboard:
        settings += "dashboard = false\n"
    (cfg.path / "config.toml").write_text(settings)
    daemon_app = build_app(cfg.path, cfg.state, clock, token)
    server = asyncio.create_task(serve_all(daemon_app, "127.0.0.1", port))
    try:
        await _wait_for_port(port)
        yield f"http://127.0.0.1:{port}"
    finally:
        daemon_app.stop.set()
        await server


@contextlib.asynccontextmanager
async def serving_upstream(server: FastMCP, transport: str = "http") -> AsyncGenerator[str]:
    """Serve ``server`` on a loopback port as a real Upstream, and yield the URL it answers on.

    In-process, like everything else, but over a socket: a Streamable HTTP or legacy SSE
    Upstream is reached by URL, which is the point of the test that uses it. An Upstream that
    has to die while the Daemon is connected to it needs ``restartable_upstream`` instead.
    """
    port = free_port()
    path = "/mcp" if transport == "http" else "/sse"
    app = server.http_app(path=path, transport=transport)  # pyright: ignore[reportArgumentType]  # the literal FastMCP takes
    running = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on")
    )
    serving = asyncio.create_task(running.serve())
    try:
        await _wait_for_port(port)
        yield f"http://127.0.0.1:{port}{path}"
    finally:
        await drain_sessions(app, expected=transport == "http")
        running.should_exit = True
        await serving


@dataclass
class ServedUpstream:
    """An Upstream served over Streamable HTTP by a child process, which a test can kill.

    A process of its own, not this one: killing a server that a legacy-era client still has
    a session on leaves every later FastMCP HTTP server in the same process unable to serve
    that era (checked 2026-09-08, FastMCP 4.0.3; see docs/clients.md). That would poison the
    reconnect the test is here to watch, and a child is what a dying Upstream is anyway.

    The port is held for the life of the context, so the Upstream that comes back answers on
    the URL the Upstream file already names.
    """

    port: int = field(default_factory=free_port)
    _child: asyncio.subprocess.Process | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"

    async def revive(self) -> None:
        """Start the child, and do not come back before it answers on its port."""
        self._child = await asyncio.create_subprocess_exec(
            child_upstream.command(),
            *child_upstream.http_args(self.port),
            env={**os.environ, **child_upstream.env()},
        )
        await _wait_for_port(self.port)

    async def kill(self) -> None:
        """End the child at once, as an Upstream whose host went away, and reap it."""
        child, self._child = self._child, None
        if child is None or child.returncode is not None:
            return
        child.kill()
        await child.wait()


@contextlib.asynccontextmanager
async def restartable_upstream() -> AsyncGenerator[ServedUpstream]:
    """An Upstream over Streamable HTTP that a test can kill and put back on the same URL."""
    served = ServedUpstream()
    await served.revive()
    try:
        yield served
    finally:
        await served.kill()


def session_managers(app: object) -> list[StreamableHTTPSessionManager]:
    """Every streamable HTTP session manager under ``app``: FastMCP sets one on the app it
    mounts at its MCP path, once its lifespan has run."""
    found: list[StreamableHTTPSessionManager] = []

    def walk(node: object) -> None:
        manager = getattr(node, "session_manager", None)
        if isinstance(manager, StreamableHTTPSessionManager):
            found.append(manager)
        routes = cast("list[object]", getattr(node, "routes", None) or [])
        for route in routes:
            walk(getattr(route, "app", None))

    walk(app)
    return found


def open_sessions(
    manager: StreamableHTTPSessionManager,
) -> dict[str, StreamableHTTPServerTransport]:
    """The sessions on ``manager`` that are not terminated: a closed client's stays listed,
    marked terminated, so the list alone says nothing (pinned in the contract tests)."""
    instances = manager._server_instances  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]  # what FastMCP's own shutdown reads
    return {
        session: transport
        for session, transport in instances.items()
        if not transport.is_terminated
    }


async def drain_sessions(app: object, patience: float = 5.0, *, expected: bool = True) -> None:
    """Let every streamable HTTP session on ``app`` end before its server stops (#76).

    A FastMCP HTTP server stopped in-process while a legacy-era session is still open leaves
    every later one in the process unable to serve that era (``docs/clients.md``). FastMCP
    itself terminates live transports when its lifespan exits, but uvicorn stops the server
    first, so the seam waits here for the sessions the Daemon just closed to be terminated,
    and terminates any that linger the way FastMCP would, saying so. Only a streamable HTTP
    app has a session manager to read; an SSE app has none, and ``expected=False`` says so,
    while a streamable HTTP app whose manager cannot be found is a wrapper hiding it, which
    would make this a silent no-op, so it fails instead.
    """
    managers = session_managers(app)
    if expected and not managers:
        msg = "no streamable HTTP session manager under the app to drain; is it wrapped?"
        raise AssertionError(msg)
    deadline = time.monotonic() + patience
    while any(open_sessions(manager) for manager in managers):
        if time.monotonic() > deadline:
            break
        await asyncio.sleep(0.02)
    for manager in managers:
        for session, transport in open_sessions(manager).items():
            print(f"a session was still open on a test server when it stopped: {session}")  # noqa: T201
            with contextlib.suppress(Exception):
                await transport.terminate()


async def _wait_for_port(port: int, attempts: int = 100) -> None:
    for _ in range(attempts):
        try:
            _, writer = await asyncio.open_connection("127.0.0.1", port)
        except OSError:
            await asyncio.sleep(0.05)
            continue
        writer.close()
        await writer.wait_closed()
        return
    msg = f"nothing listened on port {port} in time"
    raise TimeoutError(msg)
