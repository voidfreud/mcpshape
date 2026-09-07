"""A Proxy with a ``port`` override is served on that port too, besides its path (#13)."""

from __future__ import annotations

import socket
from typing import TYPE_CHECKING

from fastmcp import Client

from tests.support.seam import free_port, serving_daemon, until
from tests.test_proxy_seam import calculator

if TYPE_CHECKING:
    from tests.support.seam import ConfigDir


def _open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


async def test_a_proxy_with_a_port_override_is_served_on_that_port_besides_its_path(
    config_dir: ConfigDir,
) -> None:
    config_dir.add_memory_upstream("calc", calculator())
    extra_port = free_port()
    (config_dir.path / "upstreams" / "calc" / "default.toml").write_text(
        f"version = 1\nport = {extra_port}\n"
    )

    async with serving_daemon(config_dir) as url:
        await until(lambda: _open(extra_port), "the Proxy's port override listener")

        async with Client(f"{url}/calc/mcp") as client:
            assert (await client.call_tool("add", {"a": 2, "b": 3})).data == 5

        async with Client(f"http://127.0.0.1:{extra_port}/mcp") as client:
            assert (await client.call_tool("add", {"a": 1, "b": 1})).data == 2
