"""A configured bearer token gates every request; a non-loopback bind needs one (#13)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.support.seam import run_cli, running_daemon
from tests.test_proxy_seam import calculator

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from tests.support.seam import ConfigDir

TOKEN = "s3cret"  # a test fixture, not a real credential


async def test_a_request_with_no_token_is_refused_when_one_is_configured(
    config_dir: ConfigDir,
) -> None:
    config_dir.add_memory_upstream("calc", calculator())

    async with running_daemon(config_dir, token=TOKEN) as daemon:
        status = await daemon.status()
        assert status == {"error": "a bearer token is required"}


async def test_a_request_with_the_wrong_token_is_refused(config_dir: ConfigDir) -> None:
    config_dir.add_memory_upstream("calc", calculator())

    async with running_daemon(config_dir, token=TOKEN) as daemon:
        status = await daemon.status(headers={"Authorization": "Bearer wrong"})
        assert status == {"error": "a bearer token is required"}


async def test_a_request_carrying_the_right_token_is_served(config_dir: ConfigDir) -> None:
    config_dir.add_memory_upstream("calc", calculator())

    async with running_daemon(config_dir, token=TOKEN) as daemon:
        status = await daemon.status(headers={"Authorization": f"Bearer {TOKEN}"})
        assert [u["name"] for u in status["upstreams"]] == ["calc"]

        async with daemon.client(
            "/calc/mcp", headers={"Authorization": f"Bearer {TOKEN}"}
        ) as client:
            assert (await client.call_tool("add", {"a": 2, "b": 3})).data == 5


def test_daemon_up_refuses_a_non_loopback_bind_with_no_token(config_dir: ConfigDir) -> None:
    (config_dir.path / "config.toml").write_text('version = 1\n[daemon]\nhost = "0.0.0.0"\n')

    result = run_cli(config_dir, "daemon", "up")

    assert result.exit_code == 1
    assert "loopback" in result.output
    assert "token" in result.output


def test_daemon_up_accepts_a_non_loopback_bind_with_a_token(
    config_dir: ConfigDir, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A configured token is enough to pass the refusal check; ``daemon.run`` itself is not
    exercised here (that would bind a real socket), only that the guard lets it through."""
    (config_dir.path / "config.toml").write_text(
        f'version = 1\n[daemon]\nhost = "0.0.0.0"\ntoken = "{TOKEN}"\n'
    )

    def no_run(_config_dir: Path, _state_dir: Path) -> None:
        return None

    monkeypatch.setattr("mcpshape.cli.daemon.daemon.run", no_run)

    result = run_cli(config_dir, "daemon", "up")

    assert result.exit_code == 0, result.output
    assert "loopback" not in result.output
