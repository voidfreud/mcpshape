"""A fake OAuth provider and the Upstream it protects, both served on one loopback port.

An OAuth Upstream cannot be driven in memory: the login is a browser visiting a URL and a
provider redirecting to a loopback callback, so both halves have to answer on a real socket.
This serves them the way ``seam.serving_upstream`` serves a plain Upstream, with uvicorn in
the test's own event loop.

One port carries everything a real deployment spreads over two: the protected resource's and
the authorization server's metadata, dynamic client registration, an authorize endpoint that
approves at once, a token endpoint speaking the authorization code (with PKCE), refresh, and
device code grants, a device authorization endpoint, and at ``/mcp`` the Upstream itself,
behind a check of the bearer token this provider issued.

``Issuer`` is what a test steers: how long a token lives, whether a refresh is still accepted,
whether device-code pairing is offered, and whether a device code has been approved. It also
counts what the provider saw, so a test can say that a refresh happened.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import secrets
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse
from starlette.routing import Mount, Route

from tests.support.seam import free_port

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from fastmcp import FastMCP
    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.types import ASGIApp, Receive, Scope, Send

MCP_PATH = "/mcp"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
USER_CODE = "WDJB-MJHT"
"""The code the user types at the provider. Fixed, so a test can look for it in the output."""

PENDING = "authorization_pending"
REFUSED = "invalid_grant"


@dataclass
class _Grant:
    """One authorization the provider handed out and has not yet turned into a token."""

    challenge: str
    scope: str
    approved: bool = True
    """False until the user approves it, which only device-code pairing waits for."""


@dataclass
class Issuer:
    """The provider's own state: what it will do next, and what it has seen.

    A test sets ``token_ttl`` short to make a token expire soon, clears ``refresh_accepted``
    to revoke a refresh, and calls ``approve_device`` to stand in for the user typing the code
    on another machine.
    """

    token_ttl: int = 3600
    refresh_accepted: bool = True
    device_offered: bool = True
    scopes_supported: list[str] = field(default_factory=list[str])
    """What the authorization server advertises; the SDK asks for these over the client's."""
    names_scope: bool = True
    """Whether a token answer carries ``scope``; RFC 6749 lets a provider leave it out when
    it granted what was asked."""
    device_interval: int = 0
    """Seconds between polls the provider asks for; zero keeps the test quick."""

    refreshes: int = 0
    registrations: int = 0
    issued: list[str] = field(default_factory=list[str])
    """Every access token handed out, newest last, so a test can look for one on disk."""

    _valid: set[str] = field(default_factory=set[str])
    _refreshable: set[str] = field(default_factory=set[str])
    _grants: dict[str, _Grant] = field(default_factory=dict[str, _Grant])

    # --- what a test does to it ---------------------------------------------------------------

    def approve_device(self) -> None:
        """Approve every device code asked for so far, as the user would at the provider."""
        for grant in self._grants.values():
            grant.approved = True

    def awaiting_device(self) -> bool:
        """Whether a device code is waiting for someone to approve it."""
        return any(not grant.approved for grant in self._grants.values())

    def accepts(self, token: str) -> bool:
        """Whether the resource server would let this bearer token through."""
        return token in self._valid

    # --- what the endpoints do to it ----------------------------------------------------------

    def authorize(self, challenge: str, scope: str) -> str:
        code = f"code-{secrets.token_urlsafe(8)}"
        self._grants[code] = _Grant(challenge=challenge, scope=scope)
        return code

    def start_device(self, scope: str) -> str:
        code = f"device-{secrets.token_urlsafe(8)}"
        self._grants[code] = _Grant(challenge="", scope=scope, approved=False)
        return code

    def redeem(self, code: str, verifier: str) -> dict[str, Any] | str:
        grant = self._grants.get(code)
        if grant is None or _challenge(verifier) != grant.challenge:
            return REFUSED
        del self._grants[code]
        return self._issue(grant.scope)

    def redeem_device(self, code: str) -> dict[str, Any] | str:
        grant = self._grants.get(code)
        if grant is None:
            return REFUSED
        if not grant.approved:
            return PENDING
        del self._grants[code]
        return self._issue(grant.scope)

    def refresh(self, refresh_token: str) -> dict[str, Any] | str:
        if refresh_token not in self._refreshable or not self.refresh_accepted:
            return REFUSED
        self.refreshes += 1
        return self._issue("")

    def register(self) -> str:
        self.registrations += 1
        return f"client-{self.registrations}"

    def _issue(self, scope: str) -> dict[str, Any]:
        """A fresh token set, which invalidates every token this provider issued before."""
        self._valid.clear()
        access = f"access-{secrets.token_urlsafe(16)}"
        refresh = f"refresh-{secrets.token_urlsafe(16)}"
        self.issued.append(access)
        self._valid.add(access)
        self._refreshable.add(refresh)
        return {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": self.token_ttl,
            "refresh_token": refresh,
            **({"scope": scope} if self.names_scope else {}),
        }


class Provider:
    """The fake provider serving ``server`` as the Upstream it protects."""

    def __init__(self, port: int, server: FastMCP[Any], issuer: Issuer) -> None:
        self.issuer = issuer
        self.base = f"http://127.0.0.1:{port}"
        self.mcp_url = f"{self.base}{MCP_PATH}"
        self._mcp = server.http_app(path="/")
        self.app = Starlette(
            routes=[
                Route(f"/.well-known/oauth-protected-resource{MCP_PATH}", self._resource),
                Route("/.well-known/oauth-protected-resource", self._resource),
                Route("/.well-known/oauth-authorization-server", self._metadata),
                Route("/register", self._register, methods=["POST"]),
                Route("/authorize", self._authorize),
                Route("/token", self._token, methods=["POST"]),
                Route("/device", self._device, methods=["POST"]),
                Mount(MCP_PATH, app=_Guarded(self._mcp, issuer)),
            ],
        )

    def lifespan(self) -> Any:  # noqa: ANN401  # FastMCP's lifespan context is its own type
        """The mounted MCP app's own lifespan, which the parent app must run."""
        return self._mcp.router.lifespan_context(self._mcp)

    async def _resource(self, _request: Request) -> Response:
        return JSONResponse({"resource": self.mcp_url, "authorization_servers": [self.base]})

    async def _metadata(self, _request: Request) -> Response:
        document: dict[str, Any] = {
            "issuer": self.base,
            "authorization_endpoint": f"{self.base}/authorize",
            "token_endpoint": f"{self.base}/token",
            "registration_endpoint": f"{self.base}/register",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token", DEVICE_GRANT],
            "code_challenge_methods_supported": ["S256"],
        }
        if self.issuer.device_offered:
            document["device_authorization_endpoint"] = f"{self.base}/device"
        if self.issuer.scopes_supported:
            document["scopes_supported"] = self.issuer.scopes_supported
        return JSONResponse(document)

    async def _register(self, request: Request) -> Response:
        asked: dict[str, Any] = await request.json()
        return JSONResponse(
            {
                "client_id": self.issuer.register(),
                "token_endpoint_auth_method": "none",
                "redirect_uris": asked.get("redirect_uris") or [],
                "grant_types": asked.get("grant_types") or ["authorization_code"],
                "response_types": asked.get("response_types") or ["code"],
                "scope": asked.get("scope") or "",
            },
            status_code=201,
        )

    async def _authorize(self, request: Request) -> Response:
        """The consent screen, already clicked through: ``deny=1`` is the user refusing."""
        asked = request.query_params
        if asked.get("deny"):
            return RedirectResponse(
                f"{asked['redirect_uri']}?error=access_denied&state={asked.get('state')}"
            )
        code = self.issuer.authorize(asked.get("code_challenge", ""), asked.get("scope", ""))
        return RedirectResponse(f"{asked['redirect_uri']}?code={code}&state={asked.get('state')}")

    async def _device(self, request: Request) -> Response:
        form = await request.form()
        return JSONResponse(
            {
                "device_code": self.issuer.start_device(str(form.get("scope") or "")),
                "user_code": USER_CODE,
                "verification_uri": f"{self.base}/activate",
                "expires_in": 600,
                "interval": self.issuer.device_interval,
            }
        )

    async def _token(self, request: Request) -> Response:
        form = await request.form()
        grant = str(form.get("grant_type"))
        if grant == "authorization_code":
            answer = self.issuer.redeem(str(form.get("code")), str(form.get("code_verifier") or ""))
        elif grant == "refresh_token":
            answer = self.issuer.refresh(str(form.get("refresh_token")))
        elif grant == DEVICE_GRANT:
            answer = self.issuer.redeem_device(str(form.get("device_code")))
        else:
            answer = "unsupported_grant_type"
        if isinstance(answer, str):
            return JSONResponse({"error": answer}, status_code=400)
        return JSONResponse(answer)


class _Guarded:
    """The Upstream, reachable only with a bearer token this provider issued.

    The unauthorized answer is what starts the login: the MCP SDK begins discovery from the
    401, not before it.
    """

    def __init__(self, app: ASGIApp, issuer: Issuer) -> None:
        self._app = app
        self._issuer = issuer

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self._authorized(scope):
            await self._app(scope, receive, send)
            return
        response = PlainTextResponse(
            "unauthorized", status_code=401, headers={"WWW-Authenticate": 'Bearer realm="mcp"'}
        )
        await response(scope, receive, send)

    def _authorized(self, scope: Scope) -> bool:
        headers: dict[bytes, bytes] = dict(scope.get("headers") or ())
        given = headers.get(b"authorization", b"").decode("latin-1")
        return given.startswith("Bearer ") and self._issuer.accepts(given.removeprefix("Bearer "))


def _challenge(verifier: str) -> str:
    """The S256 code challenge of ``verifier``, as RFC 7636 computes it."""
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


@contextlib.asynccontextmanager
async def serving_provider(
    server: FastMCP[Any], issuer: Issuer | None = None
) -> AsyncGenerator[Provider]:
    """Serve the provider and the Upstream it protects, and yield what it all runs on."""
    port = free_port()
    provider = Provider(port, server, issuer or Issuer())
    running = uvicorn.Server(
        uvicorn.Config(
            provider.app, host="127.0.0.1", port=port, log_level="warning", lifespan="off"
        )
    )
    async with provider.lifespan():
        serving = asyncio.create_task(running.serve())
        try:
            await _wait_for(port)
            yield provider
        finally:
            running.should_exit = True
            await serving


async def _wait_for(port: int, attempts: int = 100) -> None:
    for _ in range(attempts):
        try:
            _, writer = await asyncio.open_connection("127.0.0.1", port)
        except OSError:
            await asyncio.sleep(0.05)
            continue
        writer.close()
        await writer.wait_closed()
        return
    msg = f"the fake provider never listened on port {port}"
    raise TimeoutError(msg)
