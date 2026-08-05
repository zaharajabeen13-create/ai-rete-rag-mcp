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

Run it:

    uvicorn ai_rete_rag_mcp.remote:app --host 127.0.0.1 --port 8002

Connect to it:

    https://ai-rete-rag.com/mcp        with  Authorization: Bearer ik_...

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

import os

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route

from mcp.server.transport_security import TransportSecuritySettings

from . import __version__
from .server import _request_api_key, _request_client_ip, mcp

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
    "remotes": [{"type": "streamable-http", "url": PUBLIC_URL}],
}


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
