"""
Remote (Streamable HTTP) transport for the ai·rete·rag MCP server.

The stdio server in `server.py` runs on the user's machine, one process per
user, holding that user's key in its environment. This module serves the same
tools over HTTP from one shared process, so a client can connect with a URL
instead of installing anything — which is also what Smithery, the Anthropic
connectors directory, and Glama connectors all require.

The tools are not redefined here. `server.py` remains the single definition of
what this server does; this module only changes how those definitions are
reached and where the credential comes from.

There are two endpoints, and the split is deliberate:

    /mcp        anonymous, or `Authorization: Bearer ik_...` for clients that
                can set a header. Advertises no OAuth, so a client connecting
                here reaches the demo domains immediately with nothing to fill
                in. This is the URL in every directory listing.

    /mcp/auth   OAuth-protected. Answers 401 with a `WWW-Authenticate` pointing
                at its protected-resource metadata, which is what makes
                claude.ai and Claude Desktop run the sign-in flow — they have
                no field for a static bearer header, so without this a
                connector user is stuck anonymous with no way to reach their
                own account from inside the client.

Keeping them apart is what lets both audiences work. Merging them would mean
choosing: advertise OAuth and every caller hits a sign-in wall, or don't and
claude.ai users can never authenticate.

The authorization server is the platform API, not this process — see
backend/app/api/oauth.py. This module verifies nothing itself; it asks the API
whether a token is good, so the property below still holds.

Run it:

    uvicorn ai_rete_rag_mcp.remote:app --host 127.0.0.1 --port 8002

Connect to it:

    https://ai-rete-rag.com/mcp        with  Authorization: Bearer ik_...
    https://ai-rete-rag.com/mcp/auth   with  Authorization: Bearer mk_...

The apex, not `api.`, and this is about TLS rather than routing. nginx serves
`api.ai-rete-rag.com` and terminates with a **Cloudflare Origin CA** cert,
which only Cloudflare trusts — that record is not proxied, so anything reaching
it directly fails certificate validation (`SEC_E_UNTRUSTED_ROOT`, or
`unable to get local issuer certificate`). The apex is proxied, so Cloudflare
presents a publicly trusted certificate there.

Reaching this box from the apex needs a Cloudflare Worker route for `/mcp/*`,
exactly as `/api/*` already has. See deploy/README.md.
"""
from __future__ import annotations

import hashlib
import os
import time

import httpx
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route

from mcp.server.transport_security import TransportSecuritySettings

from . import __version__
from .server import API_URL, _request_api_key, _request_client_ip, mcp

# Stateless: every call is independent, so a request's credential can never be
# read by a later one over a kept-alive session, and the process can be
# restarted or run behind more than one worker without losing client state.
mcp.settings.stateless_http = True

# DNS-rebinding protection is on by default in the SDK and rejects any Host it
# was not told about with a 421 — including the real one. Behind nginx the Host
# arrives as the public domain, so it has to be listed or every request fails.
# Set MCP_ALLOWED_HOSTS when the endpoint is served from somewhere else.
#
# Matching is exact against the Host header, which carries the port whenever it
# is not the scheme default. Production is portless on 443; the `:*` entries are
# what let a local run on an arbitrary port work without reconfiguring.
_ALLOWED_HOSTS = [
    h.strip() for h in os.environ.get(
        "MCP_ALLOWED_HOSTS",
        "api.ai-rete-rag.com,ai-rete-rag.com,"
        "localhost,localhost:*,127.0.0.1,127.0.0.1:*",
    ).split(",") if h.strip()
]
# Browser-based clients send an Origin and it is validated the same way. Note
# `["*"]` does NOT mean "any origin" here, however much it looks like it: the
# SDK's `_validate_origin` does an exact match and then checks only `:*` port
# patterns, so a bare `*` matches an Origin header whose literal value is `*`
# and nothing else. Setting it that way reads as permissive and behaves as an
# empty allowlist — every browser origin got a 403, while server-side callers
# (Anthropic's connector, Smithery's scanner, Claude Code, curl) sailed through
# because they send no Origin at all. Hence an explicit list.
_ALLOWED_ORIGINS = [
    o.strip() for o in os.environ.get(
        "MCP_ALLOWED_ORIGINS",
        # The browser surfaces that connect to us: Claude, and the two
        # directories that offer an in-page try-it (Glama's *Try in Browser*
        # is how their profile score picks up a usage signal).
        "https://claude.ai,https://glama.ai,https://smithery.ai,"
        "https://ai-rete-rag.com,"
        "http://localhost:*,http://127.0.0.1:*",
    ).split(",") if o.strip()
]

mcp.settings.transport_security = TransportSecuritySettings(
    allowed_hosts=_ALLOWED_HOSTS,
    allowed_origins=_ALLOWED_ORIGINS,
)

# The allowlist above only decides whether a request is *rejected*. A browser
# needs two more things before it will let a page use the response at all: a
# preflight answered on OPTIONS, which the MCP app does not route (it serves
# GET, POST and DELETE, so a preflight got a 405), and Access-Control-* headers
# on the reply. Without both, every browser client fails while server-side
# callers — Anthropic's connector, Smithery's scanner, Claude Code, curl — see
# nothing wrong, because they send no Origin.
#
# `:*` is the SDK's own port-wildcard syntax and means nothing to
# CORSMiddleware, so the local entries move to a regex and the rest stay exact.
_CORS_ORIGINS = [o for o in _ALLOWED_ORIGINS if not o.endswith(":*")]
_CORS_ORIGIN_REGEX = r"http://(localhost|127\.0\.0\.1)(:\d+)?"

# The URL this endpoint is reachable at from outside, which is what goes into
# directory listings — so it must be a host whose certificate the public trusts.
# That is the apex: `api.` is unproxied and serves a Cloudflare Origin CA cert,
# which fails validation for every client that is not Cloudflare.
PUBLIC_URL = os.environ.get("MCP_PUBLIC_URL", "https://ai-rete-rag.com/mcp")

# ── The OAuth-protected endpoint ───────────────────────────────────────────────

# Where the protected endpoint lives, and where a client is told to look for the
# metadata describing it. RFC 9728 builds the second from the first by inserting
# the resource path after the well-known segment, so these two must stay in step.
PROTECTED_PATH = "/mcp/auth"
PROTECTED_RESOURCE_METADATA_PATH = "/.well-known/oauth-protected-resource/mcp/auth"

# The authorization server. Scoped to the protected resource rather than sitting
# at the apex, so a client connecting to the anonymous /mcp finds no metadata
# above it and never tries to authenticate there.
OAUTH_ISSUER = os.environ.get("MCP_OAUTH_ISSUER", "https://ai-rete-rag.com/mcp/auth")
PROTECTED_PUBLIC_URL = os.environ.get("MCP_PROTECTED_PUBLIC_URL",
                                      "https://ai-rete-rag.com/mcp/auth")

# The server card is what Smithery reads when a tool scan doesn't succeed.
SERVER_CARD = {
    "name": "com.ai-rete-rag/ai-rete-rag-mcp",
    "title": "ai·rete·rag",
    "description": (
        "Author rules from policy docs, then decide: a Rete engine gives the "
        "verdict, an LLM explains why."
    ),
    "version": __version__,
    "websiteUrl": "https://ai-rete-rag.com",
    "repository": {
        "url": "https://github.com/zaharajabeen13-create/ai-rete-rag-mcp",
        "source": "github",
    },
    # Both doors, anonymous first: that is the URL directories scan, and the one
    # that works with nothing filled in.
    "remotes": [
        {"type": "streamable-http", "url": PUBLIC_URL},
        {"type": "streamable-http", "url": PROTECTED_PUBLIC_URL},
    ],
}


# Verified tokens, briefly. Every MCP request would otherwise cost an extra
# round trip to the API. Keyed by hash, never by the token itself — the same
# reasoning as the platform's own token cache.
# Only rejections are cached, and only briefly: long enough to blunt a guessing
# loop that would otherwise turn this endpoint into a free oracle against the
# API, short enough to be harmless. A cached rejection can never wrongly refuse
# a live token — the key is the token's own hash, and a refreshed connection
# carries a different token, so no entry here outlives what it describes.
#
# Successes are deliberately NOT cached. They were, for 60s, and it meant a
# revoked token kept passing this gate for up to a minute after the user pressed
# Disconnect — while Settings told them it took effect immediately. Revocation
# is the one answer that has to be fresh, and it is the button someone reaches
# for when they believe a connection is compromised. The saving was one loopback
# call per request, next to the API call the tool behind it already makes.
_TOKEN_CACHE: dict[str, tuple[bool, float]] = {}
_TOKEN_TTL_REJECTED = 10.0


async def _token_is_valid(token: str) -> bool:
    """Ask the API whether this connector token is good, every time.

    Deliberately not decided here. This process holds no signing key and no
    database, and asking keeps it that way — it stores no credentials, issues
    none, and still cannot be the thing that leaks them.
    """
    cache_key = hashlib.sha256(token.encode()).hexdigest()
    now = time.monotonic()
    hit = _TOKEN_CACHE.get(cache_key)
    if hit and hit[1] > now:
        return hit[0]   # only ever a cached rejection; see above
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"{API_URL}/api/v1/oauth/userinfo",
                headers={"Authorization": f"Bearer {token}"},
            )
    except httpx.RequestError:
        # The API is unreachable. Fail closed — answering as though the token
        # were good would hand out an anonymous session under an authenticated
        # URL, and every tool call behind it would fail anyway.
        return False
    valid = r.status_code == 200
    if not valid:
        _TOKEN_CACHE[cache_key] = (False, now + _TOKEN_TTL_REJECTED)
    return valid


def _challenge(description: str) -> Response:
    """401 in the shape that starts an OAuth flow.

    The `resource_metadata` parameter is the whole point: it is how a client
    that has never seen this server discovers where to register and sign in.
    Without it a 401 is just a closed door.
    """
    metadata_url = f"{PROTECTED_PUBLIC_URL.rsplit('/mcp/auth', 1)[0]}{PROTECTED_RESOURCE_METADATA_PATH}"
    return JSONResponse(
        {"error": "invalid_token", "error_description": description},
        status_code=401,
        headers={
            "WWW-Authenticate": (
                f'Bearer resource_metadata="{metadata_url}", '
                f'error="invalid_token", error_description="{description}"'
            )
        },
    )


class ProtectedEndpointMiddleware:
    """Serve the same MCP app at /mcp/auth, behind a bearer gate.

    `server.py` stays the single definition of what this server does — this
    reaches the existing app rather than declaring a second one, rewriting the
    path to the one FastMCP routes on. The gate runs first, so an
    unauthenticated request never reaches the session manager at all.

    Middleware rather than a Mount, because a Starlette Mount only matches
    *below* its prefix: `POST /mcp/auth` with no trailing slash fell straight
    past it into the catch-all mount and came back as a bare 404 — which a
    client reads as "no such endpoint" rather than "sign in", so the OAuth flow
    never starts. Intercepting before routing takes the trailing slash out of it.
    """

    def __init__(self, app, inner) -> None:
        self.app = app
        self.inner = inner
        self.inner_path = mcp.settings.streamable_http_path

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or scope["path"].rstrip("/") != PROTECTED_PATH:
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)
        auth = request.headers.get("Authorization", "")
        token = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""

        if not token:
            await _challenge("Sign in to use this endpoint.")(scope, receive, send)
            return
        if not await _token_is_valid(token):
            await _challenge("Token is invalid, expired or revoked.")(scope, receive, send)
            return

        scope = dict(scope)
        scope["path"] = self.inner_path
        scope["raw_path"] = self.inner_path.encode()
        await self.inner(scope, receive, send)


async def protected_resource_metadata(_request: Request) -> Response:
    """RFC 9728, for /mcp/auth only.

    The same path under /mcp must keep 404ing — that 404 is what tells a client
    the anonymous endpoint needs no sign-in, and it is the only thing standing
    between the demo and a sign-in wall.
    """
    return JSONResponse({
        "resource": PROTECTED_PUBLIC_URL,
        "authorization_servers": [OAUTH_ISSUER],
        "bearer_methods_supported": ["header"],
        "scopes_supported": ["mcp"],
        "resource_documentation": "https://ai-rete-rag.com/mcp",
    })


class CallerContextMiddleware(BaseHTTPMiddleware):
    """Bind the caller's identity to this request, and only this request.

    The key is passed straight through to the platform API, which already
    verifies `ik_` bearer tokens — so this transport stores no credentials,
    issues none, and can't be the thing that leaks them. A caller with no
    Authorization header is anonymous and reaches only the shared demo
    domains, exactly as the stdio server does without a key set. Anonymous
    LLM spend is bounded by the API's own global cap on unauthenticated
    explanations, not by anything here.

    The caller's IP is captured too. The API rate-limits per client IP and
    every call from here arrives over loopback, so without it all remote
    users would share one bucket and throttle one another.
    """

    async def dispatch(self, request: Request, call_next):
        auth = request.headers.get("Authorization", "")
        key = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""

        # request.client is the peer uvicorn accepted, resolved from the proxy
        # headers nginx sets (it runs with --proxy-headers). Deliberately not
        # read from the caller's own X-Forwarded-For, which they control.
        client_ip = request.client.host if request.client else None

        # Reset in a finally: worker tasks are reused across requests, and
        # anything left set would be inherited by whoever the worker serves next.
        key_token = _request_api_key.set(key or None)
        ip_token = _request_client_ip.set(client_ip)
        try:
            return await call_next(request)
        finally:
            _request_api_key.reset(key_token)
            _request_client_ip.reset(ip_token)


async def health(_request: Request) -> Response:
    return JSONResponse({"status": "ok", "version": __version__, "transport": "streamable-http"})


async def server_card(_request: Request) -> Response:
    return JSONResponse(SERVER_CARD)


def build_app() -> Starlette:
    """The ASGI app: the MCP endpoint, a health check, and the server card.

    Paths here are absolute, because nginx forwards the original URI unchanged.
    The MCP endpoint is at /mcp (FastMCP's own `streamable_http_path`), so the
    health check sits under that same prefix — anything outside it is not
    covered by the `/mcp*` route and would be unreachable from the internet.
    The server card is the exception: its path is fixed by convention, so it
    gets a route of its own at both the nginx and Cloudflare layers.
    """
    mcp_app = mcp.streamable_http_app()

    return Starlette(
        routes=[
            # Before the mount: Starlette matches in order, and the mount would
            # otherwise swallow this and hand it to the MCP app, which 404s it.
            Route("/mcp/health", health, methods=["GET"]),
            Route("/.well-known/mcp/server-card.json", server_card, methods=["GET"]),
            Route(PROTECTED_RESOURCE_METADATA_PATH, protected_resource_metadata,
                  methods=["GET"]),
            # /mcp/auth is not routed here — ProtectedEndpointMiddleware takes it
            # before routing happens. See the note in that class.
            # Mounted last: it owns everything beneath its own path.
            Mount("/", app=mcp_app),
        ],
        middleware=[
            # Outermost, deliberately: a preflight must be answered here rather
            # than travel further in, and the Access-Control-* headers have to
            # be on the way out no matter which layer produced the response.
            Middleware(
                CORSMiddleware,
                allow_origins=_CORS_ORIGINS,
                allow_origin_regex=_CORS_ORIGIN_REGEX,
                allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
                # Covers Authorization and Content-Type. Safe as a wildcard only
                # because credentials are off: the caller's key travels in a
                # header it sets explicitly, never in a cookie the browser
                # attaches on its own, so no ambient authority rides along.
                allow_headers=["*"],
                allow_credentials=False,
                # The session id is a response header, and a browser client
                # cannot read one that isn't exposed.
                expose_headers=["mcp-session-id", "mcp-protocol-version"],
            ),
            Middleware(CallerContextMiddleware),
            # Innermost, so the caller context above is already bound when a
            # gated request is handed to the MCP app.
            Middleware(ProtectedEndpointMiddleware, inner=mcp_app),
        ],
        # The MCP app runs a session manager that has to be started and stopped
        # with the process; without inheriting its lifespan the first call fails.
        lifespan=lambda _app: mcp_app.router.lifespan_context(_app),
    )


app = build_app()


def main() -> None:
    """Console-script entry point, for running without a separate uvicorn call."""
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("MCP_REMOTE_HOST", "127.0.0.1"),
        port=int(os.environ.get("MCP_REMOTE_PORT", "8002")),
    )


if __name__ == "__main__":
    main()
