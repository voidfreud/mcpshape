"""A fake OAuth provider and the Upstream it protects, served from a child process (#76).

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

The provider runs in a process of its own, spawned by ``serving_provider``, since a legacy-era
session against an in-process FastMCP HTTP server can leave the test process unable to serve
that era for the rest of the run (``docs/clients.md``). The test steers its ``Issuer`` through
``ChildProvider.issuer``, which reads and writes over the ``/_control`` route.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import hashlib
import json
import secrets
import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx2
from starlette.applications import Starlette
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse
from starlette.routing import Mount, Route

from tests.support.child_server import build, factory_path, serve, spawned

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable

    from fastmcp import FastMCP
    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.types import ASGIApp, Receive, Scope, Send

MCP_PATH = "/mcp"
CONTROL_PATH = "/_control"
"""Where the test reaches the issuer from the parent process: read its state, set a field,
approve a device code. Not part of any protocol."""
SETTABLE = (
    "token_ttl",
    "refresh_accepted",
    "device_offered",
    "scopes_supported",
    "names_scope",
    "device_interval",
)
"""The issuer's settings, given to the child at start and settable from the test after."""
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

    def __init__(self, server: FastMCP[Any], issuer: Issuer) -> None:
        self.issuer = issuer
        self.base = ""
        """Where this provider is reached, learned from the first request's Host, since the
        child binds a port of the system's choosing before anything asks."""
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
                Route(CONTROL_PATH, self._control_read),
                Route(CONTROL_PATH, self._control_write, methods=["POST"]),
                Mount(MCP_PATH, app=_Guarded(self._mcp, issuer)),
            ],
        )

    @property
    def mcp_url(self) -> str:
        return f"{self.base}{MCP_PATH}"

    def _seen(self, request: Request) -> None:
        """Learn where this provider is reached from the first request, since the child bound
        a port of the system's choosing before anything asked."""
        if not self.base:
            self.base = f"{request.url.scheme}://{request.url.netloc}"

    def lifespan(self) -> Any:  # noqa: ANN401  # FastMCP's lifespan context is its own type
        """The mounted MCP app's own lifespan, which the parent app must run."""
        return self._mcp.router.lifespan_context(self._mcp)

    # --- the test's hand on the provider, from another process ---------------------------

    async def _control_read(self, _request: Request) -> Response:
        return JSONResponse(
            {
                **{name: getattr(self.issuer, name) for name in SETTABLE},
                "refreshes": self.issuer.refreshes,
                "registrations": self.issuer.registrations,
                "issued": self.issuer.issued,
                "awaiting_device": self.issuer.awaiting_device(),
            }
        )

    async def _control_write(self, request: Request) -> Response:
        asked: dict[str, Any] = await request.json()
        for name, value in asked.get("set", {}).items():
            if name not in SETTABLE:
                return JSONResponse({"error": f"not settable: {name}"}, status_code=400)
            setattr(self.issuer, name, value)
        if asked.get("approve_device"):
            self.issuer.approve_device()
        return JSONResponse({"ok": True})

    async def _resource(self, _request: Request) -> Response:
        self._seen(_request)
        return JSONResponse({"resource": self.mcp_url, "authorization_servers": [self.base]})

    async def _metadata(self, _request: Request) -> Response:
        self._seen(_request)
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
        self._seen(request)
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
        self._seen(request)
        asked = request.query_params
        if asked.get("deny"):
            return RedirectResponse(
                f"{asked['redirect_uri']}?error=access_denied&state={asked.get('state')}"
            )
        code = self.issuer.authorize(asked.get("code_challenge", ""), asked.get("scope", ""))
        return RedirectResponse(f"{asked['redirect_uri']}?code={code}&state={asked.get('state')}")

    async def _device(self, request: Request) -> Response:
        self._seen(request)
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
        self._seen(request)
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


class ChildIssuer:
    """The child's issuer as the test reaches it: the same names, over the control route.

    A read asks the child; a write tells it. Each is one short request to a process of its
    own, so it is done synchronously, as a test sets an attribute. The last state read is
    kept once the child is gone, since a test reads it after the provider stops, as it read
    the in-process issuer's memory before (#76).
    """

    def __init__(self, base: str) -> None:
        self._url = f"{base}{CONTROL_PATH}"
        self._last: dict[str, Any] = {}
        self._closed = False

    def _read(self) -> dict[str, Any]:
        if self._closed:
            return self._last
        with httpx2.Client() as http:
            answer = http.get(self._url)
            answer.raise_for_status()
            self._last = answer.json()
        return self._last

    def _write(self, body: dict[str, Any]) -> None:
        with httpx2.Client() as http:
            http.post(self._url, json=body).raise_for_status()

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._read()
        self._closed = True

    @property
    def refresh_accepted(self) -> bool:
        return bool(self._read()["refresh_accepted"])

    @refresh_accepted.setter
    def refresh_accepted(self, value: bool) -> None:
        self._write({"set": {"refresh_accepted": value}})

    @property
    def token_ttl(self) -> int:
        return int(self._read()["token_ttl"])

    @token_ttl.setter
    def token_ttl(self, value: int) -> None:
        self._write({"set": {"token_ttl": value}})

    @property
    def refreshes(self) -> int:
        return int(self._read()["refreshes"])

    @property
    def registrations(self) -> int:
        return int(self._read()["registrations"])

    @property
    def issued(self) -> list[str]:
        return list(self._read()["issued"])

    def awaiting_device(self) -> bool:
        return bool(self._read()["awaiting_device"])

    def approve_device(self) -> None:
        self._write({"approve_device": True})


@dataclass(frozen=True)
class ChildProvider:
    """The provider running in its child process, as the test sees it."""

    base: str
    issuer: ChildIssuer

    @property
    def mcp_url(self) -> str:
        return f"{self.base}{MCP_PATH}"


def _issuer_settings(issuer: Issuer) -> dict[str, Any]:
    """What the child starts its issuer with: every field a test may set, and only those.

    A field outside ``SETTABLE`` cannot cross to the child, so an ``Issuer`` that differs from
    the default in one is refused here rather than silently started on the default.
    """
    default = Issuer()
    unsettable = {
        name
        for name in vars(issuer)
        if not name.startswith("_")
        and name not in SETTABLE
        and getattr(issuer, name) != getattr(default, name)
    }
    if unsettable:
        msg = f"the child cannot be started with {sorted(unsettable)}; add them to SETTABLE"
        raise ValueError(msg)
    return {name: getattr(issuer, name) for name in SETTABLE}


@contextlib.asynccontextmanager
async def serving_provider(
    server: Callable[[], FastMCP[Any]], issuer: Issuer | None = None
) -> AsyncGenerator[ChildProvider]:
    """Serve the provider and the Upstream it protects from a child process, and yield what
    the test reaches it by (#76).

    ``server`` is the factory the child imports and calls; ``issuer`` is the state the child
    starts with, reachable after through ``ChildProvider.issuer``. A child, not this process:
    a legacy-era session against an in-process FastMCP HTTP server can leave the test process
    unable to serve that era for the rest of the run (``docs/clients.md``).
    """
    args = [
        sys.executable,
        "-m",
        "tests.support.oauth_provider",
        "--server",
        factory_path(server),
        "--issuer",
        json.dumps(_issuer_settings(issuer or Issuer())),
    ]
    async with spawned(args) as port:
        base = f"http://127.0.0.1:{port}"
        provider = ChildProvider(base=base, issuer=ChildIssuer(base))
        try:
            yield provider
        finally:
            await asyncio.to_thread(provider.issuer.close)


def main() -> None:
    """The child: build the provider from the command line and serve it until killed."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", required=True)
    parser.add_argument("--issuer", default="{}")
    given = parser.parse_args()
    provider = Provider(build(given.server), Issuer(**json.loads(given.issuer)))
    asyncio.run(serve(provider.app, provider.lifespan()))


if __name__ == "__main__":
    main()
