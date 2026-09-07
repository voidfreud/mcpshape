"""``mcpshape serve``: the stdio shim, for the Clients that accept stdio only.

Hidden, because ``proxy install`` writes it into a Client's config and nobody types it. It
speaks MCP over stdio to the Client and forwards everything to the Proxy's URL over
Streamable HTTP, starting the Daemon when nothing answers on the configured address.

stdout is the protocol channel: nothing but MCP is ever written to it, so every diagnostic,
including the pid of a Daemon this shim started, goes to stderr.
"""

from __future__ import annotations

import os
import subprocess  # the Daemon is started by path, from sys.executable
import sys
import time
from pathlib import Path  # noqa: TC003  # typer resolves annotations at runtime
from typing import Annotated

import typer

from mcpshape.adapters.fastmcp import run_shim
from mcpshape.cli.common import (
    answering,
    errors,
    example,
    fail,
    parse_proxy_ref,
    reporting_errors,
    state,
)
from mcpshape.cli.listing import proxy_url
from mcpshape.config import load_proxy, load_settings
from mcpshape.model import DEFAULT_PROXY_NAME
from mcpshape.paths import CONFIG_DIR_ENV, STATE_DIR_ENV

DAEMON_START_TIMEOUT = 15.0
"""Seconds to wait for a Daemon this shim started, or another shim's, to answer.

Codex CLI gives an MCP server 10 s to start and no other Client documents a startup budget
(``docs/clients.md``), so waiting a little longer beats giving up while the Daemon is still
binding its port: the next Client to start finds it up.
"""

PROBE_INTERVAL = 0.05

EPILOG = example("serve github/default")

RefArg = Annotated[
    str,
    typer.Argument(
        metavar="UPSTREAM[/PROXY]", help="The Proxy to serve; its Upstream alone means default."
    ),
]
ProxyArg = Annotated[
    str | None,
    typer.Argument(metavar="[PROXY]", show_default=False, help="The Proxy, given separately."),
]


def serve(ctx: typer.Context, ref: RefArg, proxy_name: ProxyArg = None) -> None:
    """Speak stdio to a Client and forward to a Proxy, starting the Daemon if needed."""
    config_dir, state_dir = state(ctx).config_dir, state(ctx).state_dir
    upstream, proxy = resolve(ref, proxy_name)
    with reporting_errors():
        load_proxy(config_dir, upstream, proxy)
        daemon = load_settings(config_dir).daemon
    ensure_daemon(config_dir, state_dir, daemon.host, daemon.port)
    url = proxy_url(config_dir, upstream, proxy)
    errors.print(f"Serving [bold]{upstream}/{proxy}[/bold] from {url} over stdio.")
    run_shim(url, daemon.token)


def resolve(ref: str, given: str | None) -> tuple[str, str]:
    """``<upstream>/<proxy>``, ``<upstream> <proxy>``, or ``<upstream>`` for the default Proxy."""
    if "/" in ref:
        if given is not None:
            fail(f"name the Proxy once: either {ref!r} or {ref.partition('/')[0]!r} {given!r}")
        return parse_proxy_ref(ref)
    if not ref:
        fail("name the Upstream to serve")
    return ref, given or DEFAULT_PROXY_NAME


def ensure_daemon(config_dir: Path, state_dir: Path, host: str, port: int) -> None:
    """Start the Daemon unless something already answers on ``host``:``port``, and wait for it.

    Two Clients starting shims at once both spawn a Daemon; ``daemon up`` takes a lock for as
    long as it runs, so the loser's own ``daemon up`` notices the winner and exits at once
    instead of failing to bind. That exit (status 0, no port ever bound by it) is not this
    shim's failure: it is only a failure once the port never answers either way (#13).
    """
    if answering(host, port):
        return
    daemon = start_daemon(config_dir, state_dir)
    errors.print(f"Started the Daemon (pid {daemon.pid}) on http://{host}:{port}")
    deadline = time.monotonic() + DAEMON_START_TIMEOUT
    while time.monotonic() < deadline:
        if answering(host, port):
            return
        if (code := daemon.poll()) is not None and code != 0:
            fail(f"the Daemon exited with status {code} instead of listening on {host}:{port}")
        time.sleep(PROBE_INTERVAL)
    fail(f"the Daemon did not answer on {host}:{port} within {DAEMON_START_TIMEOUT:.0f} seconds")


def start_daemon(config_dir: Path, state_dir: Path) -> subprocess.Popen[bytes]:
    """Spawn a Daemon that outlives this shim and every Client that spawns one.

    Its own session, so a Client killing the shim's process group leaves it running, and no
    pipes, so nothing it writes can ever reach the Client's stdout.
    """
    return subprocess.Popen(  # a fixed command line, run from sys.executable
        [sys.executable, "-m", "mcpshape", "daemon", "up"],
        env={**os.environ, CONFIG_DIR_ENV: str(config_dir), STATE_DIR_ENV: str(state_dir)},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
