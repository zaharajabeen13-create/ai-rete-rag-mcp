"""
Tests for the remote (Streamable HTTP) transport.

The thing worth testing hardest is credential isolation: one process now serves
every caller, so a key that outlived its request would hand one user's account
to the next one through. The rest checks the endpoint really speaks MCP and
that anonymous discovery still works.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from ai_rete_rag_mcp import server as srv
from ai_rete_rag_mcp.remote import app

MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


@pytest.fixture(scope="module")
def anyio_backend():
    return "asyncio"


@pytest.fixture(scope="module")
async def client():
    """One app with its lifespan running, shared by the module.

    Two constraints force this shape. httpx's ASGITransport does not run
    lifespan events, and the MCP session manager starts its task group there —
    without it every call fails with "Task group is not initialized". And that
    manager refuses a second `.run()`, so the lifespan is entered once here
    rather than per test. Production runs the same way: one app, one lifespan,
    under uvicorn.
    """
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as c:
            yield c


def _parse(response: httpx.Response) -> dict:
    """Streamable HTTP replies as JSON or as a single SSE `data:` frame."""
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        for line in response.text.splitlines():
            if line.startswith("data:"):
                return json.loads(line.removeprefix("data:").strip())
        raise AssertionError(f"no data frame in SSE response: {response.text!r}")
    return response.json()


async def _initialize(client: httpx.AsyncClient, headers: dict | None = None) -> httpx.Response:
    return await client.post("/mcp", headers={**MCP_HEADERS, **(headers or {})}, json={
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"},
        },
    })


async def _call_tool(client: httpx.AsyncClient, name: str,
                     arguments: dict | None = None, headers: dict | None = None) -> dict:
    r = await client.post("/mcp", headers={**MCP_HEADERS, **(headers or {})}, json={
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": name, "arguments": arguments or {}},
    })
    return _parse(r)


class TestEndpointBasics:
    @pytest.mark.anyio
    async def test_health_reports_transport(self, client):
        r = await client.get("/mcp/health")
        assert r.status_code == 200
        assert r.json()["transport"] == "streamable-http"

    @pytest.mark.anyio
    async def test_health_lives_under_the_routed_prefix(self, client):
        # nginx and the Cloudflare Worker route `/mcp*`. A health check outside
        # that prefix is unreachable from the internet however well it works
        # locally — which is exactly how it shipped the first time.
        assert (await client.get("/mcp/health")).status_code == 200
        assert (await client.get("/health")).status_code == 404

    @pytest.mark.anyio
    async def test_server_card_is_served_for_smithery(self, client):
        # Smithery falls back to this when scanning for tools doesn't succeed.
        r = await client.get("/.well-known/mcp/server-card.json")
        assert r.status_code == 200
        card = r.json()
        assert card["name"] == "com.ai-rete-rag/ai-rete-rag-mcp"
        assert card["remotes"][0]["type"] == "streamable-http"

    @pytest.mark.anyio
    async def test_initialize_handshake_succeeds(self, client):
        r = await _initialize(client)
        assert r.status_code == 200
        assert _parse(r)["result"]["serverInfo"]["name"] == "ai-rete-rag"

    @pytest.mark.anyio
    async def test_all_eight_tools_are_exposed(self, client):
        # The remote transport must expose exactly what stdio does — same
        # definitions, different door.
        await _initialize(client)
        r = await client.post("/mcp", headers=MCP_HEADERS, json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {},
        })
        assert {t["name"] for t in _parse(r)["result"]["tools"]} == {
            "decide", "list_rules", "get_rule_source", "import_policy_rules",
            "put_rules", "ingest_text", "list_documents", "get_usage",
        }


class TestTransportSecurityMatchesProduction:
    """The DNS-rebinding settings, checked against the names traffic arrives on.

    Both of these shipped broken because the module was tested at a layer where
    the header under test is whatever the test client happened to send. Assert
    the real values instead.
    """

    @pytest.mark.anyio
    async def test_the_host_the_worker_forwards_is_allowed(self, client):
        # The Cloudflare Worker rewrites the hostname to api.ai-rete-rag.com
        # before forwarding, so that — not the public apex — is the Host this
        # app sees in production. Omitting it 421'd every request on the URL
        # that goes into three directory listings.
        r = await _initialize(client, {"Host": "api.ai-rete-rag.com"})
        assert r.status_code == 200

    @pytest.mark.anyio
    async def test_a_browser_origin_is_allowed(self, client):
        # `allowed_origins=["*"]` is not a wildcard in this SDK — it matches an
        # Origin header whose literal value is `*`. Every real browser origin
        # got a 403 while server-side callers, which send no Origin, passed.
        r = await _initialize(client, {"Origin": "https://claude.ai"})
        assert r.status_code == 200

    @pytest.mark.anyio
    async def test_an_unlisted_origin_is_still_rejected(self, client):
        r = await _initialize(client, {"Origin": "https://evil.example"})
        assert r.status_code == 403

    @pytest.mark.anyio
    async def test_preflight_is_answered(self, client):
        # The MCP app routes GET, POST and DELETE, so a preflight reached it and
        # got a 405 — and any request carrying Content-Type: application/json
        # triggers one. Allowing the origin was necessary and not sufficient.
        r = await client.options("/mcp", headers={
            "Origin": "https://glama.ai",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,authorization",
        })
        assert r.status_code == 200
        assert r.headers["access-control-allow-origin"] == "https://glama.ai"

    @pytest.mark.anyio
    async def test_the_response_a_browser_reads_carries_the_cors_headers(self, client):
        # Without this header the browser blocks the response after the fact,
        # even where no preflight was needed.
        r = await _initialize(client, {"Origin": "https://glama.ai"})
        assert r.headers["access-control-allow-origin"] == "https://glama.ai"
        assert "mcp-session-id" in r.headers.get("access-control-expose-headers", "")

    @pytest.mark.anyio
    async def test_an_unlisted_origin_gets_no_cors_headers(self, client):
        r = await client.options("/mcp", headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "POST",
        })
        assert "access-control-allow-origin" not in r.headers


class TestCredentialIsolation:
    """One process, many callers. A key must not outlive its request."""

    @pytest.fixture
    def seen_keys(self, monkeypatch) -> list[str]:
        """Record the key each tool invocation actually resolved to."""
        recorded: list[str] = []

        async def fake_request(method: str, path: str, **kwargs):
            recorded.append(srv.current_api_key())
            return "{}"

        monkeypatch.setattr(srv, "_request", fake_request)
        monkeypatch.setattr(srv, "API_KEY", "")
        return recorded

    @pytest.mark.anyio
    async def test_bearer_key_reaches_the_tool(self, client, seen_keys):
        await _initialize(client, {"Authorization": "Bearer ik_caller_one"})
        await _call_tool(client, "get_usage",
                         headers={"Authorization": "Bearer ik_caller_one"})
        assert seen_keys == ["ik_caller_one"]

    @pytest.mark.anyio
    async def test_key_does_not_leak_to_the_next_caller(self, client, seen_keys):
        # The failure this guards against is silent and severe: a caller with
        # no key of their own inheriting the previous caller's account.
        await _call_tool(client, "list_documents", {"domain": "loan"},
                         headers={"Authorization": "Bearer ik_secret_one"})
        await _call_tool(client, "list_documents", {"domain": "loan"})
        assert seen_keys == ["ik_secret_one", ""]

    @pytest.mark.anyio
    async def test_concurrent_callers_keep_separate_keys(self, client, monkeypatch):
        # ContextVars are the reason this holds. A module-level global would
        # pass this only by luck of scheduling, and fail under real load.
        observed: dict[str, str] = {}

        async def fake_request(method: str, path: str, **kwargs):
            key = srv.current_api_key()
            await asyncio.sleep(0.01)  # force interleaving between callers
            observed[key] = srv.current_api_key()
            return "{}"

        monkeypatch.setattr(srv, "_request", fake_request)
        monkeypatch.setattr(srv, "API_KEY", "")

        await asyncio.gather(*(
            _call_tool(client, "list_documents", {"domain": "loan"},
                       headers={"Authorization": f"Bearer ik_{tag}"})
            for tag in ("alpha", "beta", "gamma")
        ))
        # Each request saw its own key before and after the await point.
        assert observed == {f"ik_{t}": f"ik_{t}" for t in ("alpha", "beta", "gamma")}

    @pytest.mark.anyio
    async def test_unverified_identity_headers_cannot_be_set_remotely(
        self, client, monkeypatch
    ):
        # X-User-Id is an unverified claim the API honours in dev posture. It is
        # env-only by design; a remote caller must never be able to supply one.
        sent: list[dict] = []

        async def fake_request(method: str, path: str, **kwargs):
            sent.append(srv._headers())
            return "{}"

        monkeypatch.setattr(srv, "_request", fake_request)
        monkeypatch.setattr(srv, "API_KEY", "")
        monkeypatch.setattr(srv, "USER_ID", "")
        monkeypatch.setattr(srv, "USER_EMAIL", "")

        await _call_tool(client, "list_documents", {"domain": "loan"}, headers={
            "X-User-Id": "someone-elses-account",
            "X-User-Email": "victim@example.com",
        })
        # X-Forwarded-For is set from the accepted connection and belongs here;
        # the identity headers are the ones that must not survive the hop.
        assert "X-User-Id" not in sent[0]
        assert "X-User-Email" not in sent[0]
        assert "Authorization" not in sent[0]

    @pytest.mark.anyio
    async def test_non_bearer_authorization_is_ignored(self, client, seen_keys):
        await _call_tool(client, "list_documents", {"domain": "loan"},
                         headers={"Authorization": "Basic dXNlcjpwYXNz"})
        assert seen_keys == [""]


class TestRateLimitAttribution:
    """The API rate-limits per client IP, and every call from this endpoint
    reaches it over loopback. Without forwarding the real caller, all remote
    users share one bucket and throttle each other."""

    @pytest.fixture
    def sent_headers(self, monkeypatch) -> list[dict]:
        captured: list[dict] = []

        async def fake_request(method: str, path: str, **kwargs):
            captured.append(srv._headers())
            return "{}"

        monkeypatch.setattr(srv, "_request", fake_request)
        monkeypatch.setattr(srv, "API_KEY", "")
        return captured

    @pytest.mark.anyio
    async def test_caller_ip_is_forwarded(self, client, sent_headers):
        await _call_tool(client, "list_documents", {"domain": "loan"})
        assert sent_headers[0].get("X-Forwarded-For")

    @pytest.mark.anyio
    async def test_caller_supplied_forwarded_for_is_not_trusted(self, client, sent_headers):
        # Otherwise anyone could pick an IP to dodge the limit, or wear
        # someone else's and exhaust their bucket.
        await _call_tool(client, "list_documents", {"domain": "loan"},
                         headers={"X-Forwarded-For": "203.0.113.9"})
        assert sent_headers[0].get("X-Forwarded-For") != "203.0.113.9"

    @pytest.mark.anyio
    async def test_stdio_sends_no_forwarded_for(self, monkeypatch):
        # Over stdio the caller is the local machine; inventing an IP here
        # would attribute a local user's traffic to a made-up client.
        monkeypatch.setattr(srv, "API_KEY", "ik_local")
        assert "X-Forwarded-For" not in srv._headers()


class TestAnonymousAccess:
    @pytest.mark.anyio
    async def test_no_key_still_lists_tools(self, client):
        # Demo domains work without a key, and the directories scan the
        # endpoint unauthenticated — discovery must not require credentials.
        await _initialize(client)
        r = await client.post("/mcp", headers=MCP_HEADERS, json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {},
        })
        assert r.status_code == 200
        assert _parse(r)["result"]["tools"]

    @pytest.mark.anyio
    async def test_get_usage_without_a_key_explains_both_transports(self, monkeypatch):
        monkeypatch.setattr(srv, "API_KEY", "")
        monkeypatch.setattr(srv, "USER_ID", "")
        out = await srv.get_usage()
        assert "Authorization" in out and "AI_RETE_RAG_API_KEY" in out
