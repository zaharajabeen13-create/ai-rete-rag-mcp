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

    https://api.ai-rete-rag.com/mcp    with  Authorization: Bearer ik_...

The host is `api.` and not the apex on purpose. nginx serves
`api.ai-rete-rag.com`; the apex is Cloudflare Pages, and the Worker in front of
it only re-routes `/api/*` to this box. A `/mcp` path on the apex would reach
the SPA and 404, so the endpoint is published under the host that actually
terminates it.
"""
from __future__ import annotations

import os

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route

from mcp.server.transport_security import TransportSecuritySettings

from . import __version__
from .server import _request_api_key, mcp

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
mcp.settings.transport_security = TransportSecuritySettings(
    allowed_hosts=_ALLOWED_HOSTS,
    # Any origin may call: this is an API for MCP clients, authenticated by
    # bearer token rather than by where the request came from. Host validation
    # is what stops rebinding, and it stays on.
    allowed_origins=["*"],
)

# The URL this endpoint is reachable at from outside, which is what goes into
# directory listings — so it must be the host nginx actually terminates.
PUBLIC_URL = os.environ.get("MCP_PUBLIC_URL", "https://api.ai-rete-rag.com/mcp")

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


class BearerKeyMiddleware(BaseHTTPMiddleware):
    """Bind the caller's API key to this request, and only this request.

    The key is passed straight through to the platform API, which already
    verifies `ik_` bearer tokens — so this transport stores no credentials,
    issues none, and can't be the thing that leaks them. A caller with no
    Authorization header is anonymous and reaches only the shared demo
    domains, exactly as the stdio server does without a key set.
    """

    async def dispatch(self, request: Request, call_next):
        auth = request.headers.get("Authorization", "")
        key = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""

        # Reset in a finally: worker tasks are reused across requests, and a
        # key left set would be inherited by whoever the worker serves next.
        token = _request_api_key.set(key or None)
        try:
            return await call_next(request)
        finally:
            _request_api_key.reset(token)


async def health(_request: Request) -> Response:
    return JSONResponse({"status": "ok", "version": __version__, "transport": "streamable-http"})


async def server_card(_request: Request) -> Response:
    return JSONResponse(SERVER_CARD)


def build_app() -> Starlette:
    """The ASGI app: the MCP endpoint, a health check, and the server card."""
    mcp_app = mcp.streamable_http_app()

    return Starlette(
        routes=[
            Route("/health", health, methods=["GET"]),
            Route("/.well-known/mcp/server-card.json", server_card, methods=["GET"]),
            # Mounted last: it owns everything beneath its own path.
            Mount("/", app=mcp_app),
        ],
        middleware=[Middleware(BearerKeyMiddleware)],
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
