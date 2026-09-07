"""The one seam every test drives the system through.

A temp config directory, the Daemon app built in-process, in-memory Upstreams, and a FastMCP
Client that reaches the app over ASGI. Tests never import internal modules.
"""

from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from typer.testing import CliRunner

from mcpshape.cli import app
from mcpshape.daemon import build_app
from tests.support import upstreams
from tests.support.asgi import asgi_client_factory

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Generator
    from pathlib import Path

    from starlette.types import ASGIApp

BASE_URL = "http://mcpshape.test"
# A wide, plain terminal so help text and tables render the same on every machine and CI.
CLI_ENV = {"COLUMNS": "200", "NO_COLOR": "1"}
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


@dataclass
class ConfigDir:
    """A temp config directory that tests populate before building the Daemon."""

    path: Path
    _registered: list[str] = field(default_factory=list[str])

    def add_memory_upstream(self, name: str, server: FastMCP) -> None:
        """Register ``server`` as the Upstream called ``name``."""
        target = upstreams.register(name, server)
        self._registered.append(name)
        upstream_dir = self.path / "upstreams" / name
        upstream_dir.mkdir(parents=True)
        (upstream_dir / "upstream.toml").write_text(
            f'version = 1\ntransport = "memory"\ntarget = "{target}"\n',
        )
        (upstream_dir / "default.toml").write_text("version = 1\n")

    def cleanup(self) -> None:
        for name in self._registered:
            upstreams.unregister(name)


@pytest.fixture
def config_dir(tmp_path: Path) -> Generator[ConfigDir]:
    cfg = ConfigDir(tmp_path / "config")
    cfg.path.mkdir()
    try:
        yield cfg
    finally:
        cfg.cleanup()


@dataclass(frozen=True)
class RunningDaemon:
    """The Daemon app, running in-process behind an ASGI transport."""

    app: ASGIApp

    def client(self, path: str) -> Client[StreamableHttpTransport]:
        """A FastMCP Client for the Proxy served at ``path`` (for example ``/calc/mcp``)."""
        transport = StreamableHttpTransport(
            f"{BASE_URL}{path}", httpx_client_factory=asgi_client_factory(self.app, BASE_URL)
        )
        return Client(transport)


@contextlib.asynccontextmanager
async def running_daemon(cfg: ConfigDir) -> AsyncGenerator[RunningDaemon]:
    """Build the Daemon app from ``cfg`` and run its lifespan for the duration."""
    app = build_app(cfg.path)
    async with app.router.lifespan_context(app):
        yield RunningDaemon(app)


@dataclass(frozen=True)
class CliResult:
    """What the user saw: exit code and the combined output."""

    exit_code: int
    output: str


def run_cli(cfg: ConfigDir, *args: str) -> CliResult:
    """Run the mcpshape CLI against ``cfg`` through Typer's runner."""
    result = CliRunner().invoke(app, ["--config-dir", str(cfg.path), *args], env=CLI_ENV)
    return CliResult(exit_code=result.exit_code, output=ANSI.sub("", result.output))


def run_cli_with_env(env: dict[str, str], *args: str) -> CliResult:
    """Run the CLI without ``--config-dir``, letting ``env`` decide where config lives."""
    result = CliRunner().invoke(app, list(args), env={**CLI_ENV, **env})
    return CliResult(exit_code=result.exit_code, output=ANSI.sub("", result.output))
