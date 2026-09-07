"""Reaching an ASGI app from a FastMCP Client with no sockets."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx2
from fastmcp.utilities.asgi_transport import StreamingASGITransport

if TYPE_CHECKING:
    from mcp.shared._httpx_utils import McpHttpClientFactory
    from starlette.types import ASGIApp


def asgi_client_factory(app: ASGIApp, base_url: str) -> McpHttpClientFactory:
    """An ``httpx_client_factory`` for FastMCP's HTTP transports that drives ``app`` in-process."""

    def factory(
        headers: dict[str, str] | None = None,
        timeout: httpx2.Timeout | None = None,
        auth: httpx2.Auth | None = None,
        **kwargs: Any,  # noqa: ANN401  # FastMCP passes more than its factory type declares
    ) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(
            transport=StreamingASGITransport(app),
            base_url=base_url,
            headers=headers,
            timeout=timeout,
            auth=auth,
            **kwargs,
        )

    return factory
