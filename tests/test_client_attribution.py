"""
Tests for client attribution logging on the hosted endpoint.

Two properties matter most. Attribution must never change what a caller gets:
the body reaches the app byte-for-byte, and a broken sink fails nothing. And
it must never log anything sensitive: no tokens, IPs or tool arguments.
"""
from __future__ import annotations

import json
import logging

import httpx
import pytest

from ai_rete_rag_mcp import remote as rm
from ai_rete_rag_mcp.client_attribution import (
    CLIENT_INFO_META_KEY,
    LOGGER_NAME,
    MAX_BODY_BYTES,
    PROTOCOL_VERSION_META_KEY,
    AttributionEvent,
    ClientAttributionMiddleware,
    LoggingSink,
    configure_default_logger,
    extract_attributions,
)

MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}

LEGACY_INIT = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "Visual Studio Code", "version": "1.107.0"},
    },
}


@pytest.fixture(scope="module")
def anyio_backend():
    return "asyncio"


class ListSink:
    def __init__(self) -> None:
        self.events: list[AttributionEvent] = []

    def record(self, event: AttributionEvent) -> None:
        self.events.append(event)


def _extract(body, **kw):
    return extract_attributions(
        body,
        endpoint=kw.pop("endpoint", "/mcp"),
        user_agent=kw.pop("user_agent", "ua/1"),
        authenticated=kw.pop("authenticated", False),
        **kw,
    )


# ── extract_attributions: pure parsing ────────────────────────────────────────

class TestExtract:
    def test_legacy_initialize_reads_client_info(self):
        [e] = _extract(LEGACY_INIT)
        assert (e.method, e.client_name, e.client_version) == (
            "initialize", "Visual Studio Code", "1.107.0")
        assert e.client_source == "initialize"
        assert e.protocol_version == "2025-06-18"
        assert e.user_agent == "ua/1"

    def test_modern_meta_envelope_on_tools_call(self):
        [e] = _extract({
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {
                "name": "decide", "arguments": {"domain": "loan"},
                "_meta": {
                    PROTOCOL_VERSION_META_KEY: "2026-07-28",
                    CLIENT_INFO_META_KEY: {"name": "claude-ai", "version": "0.1.0"},
                },
            },
        })
        assert (e.tool, e.client_name, e.client_source, e.protocol_version) == (
            "decide", "claude-ai", "meta", "2026-07-28")

    def test_legacy_tools_call_falls_back_to_user_agent(self):
        # Stateless endpoint: clientInfo arrived on an earlier initialize.
        [e] = _extract({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                        "params": {"name": "decide"}}, user_agent="claude-code/2.1")
        assert e.client_name is None and e.client_source is None
        assert e.user_agent == "claude-code/2.1"
        assert e.tool == "decide"

    def test_tool_arguments_are_never_captured(self):
        [e] = _extract({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                        "params": {"name": "decide",
                                   "arguments": {"ssn": "123-45-6789"}}})
        assert "123-45-6789" not in json.dumps(e.__dict__)

    def test_uninteresting_methods_and_notifications_skipped(self):
        assert _extract({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) == []
        assert _extract({"jsonrpc": "2.0", "method": "notifications/initialized"}) == []
        assert _extract({"jsonrpc": "2.0", "id": 1, "result": {}}) == []

    def test_batch_yields_one_event_per_attributable_message(self):
        events = _extract([LEGACY_INIT,
                           {"jsonrpc": "2.0", "id": 2, "method": "ping"},
                           {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                            "params": {"name": "get_parameters"}}])
        assert [e.method for e in events] == ["initialize", "tools/call"]

    @pytest.mark.parametrize("body", [None, 7, "x", [1, None], {"method": 5},
                                      {"method": "initialize", "params": "nope"},
                                      {"method": "initialize",
                                       "params": {"clientInfo": "nope"}}])
    def test_malformed_bodies_never_raise(self, body):
        for e in _extract(body):
            assert e.client_name is None

    def test_caller_strings_are_cleaned_and_capped(self):
        [e] = _extract({"method": "initialize", "params": {
            "clientInfo": {"name": "evil\n{\"event\":\"forged\"}" + "x" * 500,
                           "version": 3}}}, user_agent="a\r\nb" + "y" * 500)
        assert "\n" not in e.client_name and len(e.client_name) <= 64
        assert e.client_version is None     # not a string
        assert "\r" not in e.user_agent and len(e.user_agent) <= 160

    def test_methods_are_configurable(self):
        assert [e.method for e in _extract({"id": 1, "method": "tools/list"},
                                           methods={"tools/list"})] == ["tools/list"]


# ── LoggingSink ───────────────────────────────────────────────────────────────

class TestLoggingSink:
    def test_writes_one_json_line(self, caplog):
        [e] = _extract(LEGACY_INIT)
        with caplog.at_level(logging.INFO, logger="attr-test"):
            LoggingSink(logging.getLogger("attr-test")).record(e.with_status(200))
        [rec] = caplog.records
        line = json.loads(rec.getMessage())
        assert line["event"] == "mcp_client"
        assert line["client_name"] == "Visual Studio Code"
        assert line["status"] == 200

    def test_default_logger_is_configured_once(self):
        logger = configure_default_logger()
        handlers = list(logger.handlers)
        assert configure_default_logger() is logger
        assert logger.handlers == handlers and logger.name == LOGGER_NAME


# ── ClientAttributionMiddleware: ASGI plumbing ────────────────────────────────

def _echo_app(status: int = 200):
    """Returns the exact body it received, so replay can be checked."""
    async def app(scope, receive, send):
        body = b""
        while True:
            m = await receive()
            body += m.get("body", b"")
            if not m.get("more_body"):
                break
        await send({"type": "http.response.start", "status": status, "headers": []})
        await send({"type": "http.response.body", "body": body})
    return app


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://localhost")


class TestMiddleware:
    @pytest.mark.anyio
    async def test_body_reaches_app_unchanged_and_status_recorded(self):
        sink = ListSink()
        app = ClientAttributionMiddleware(_echo_app(202), sink=sink, paths=["/mcp"])
        raw = json.dumps(LEGACY_INIT).encode()
        async with _client(app) as c:
            r = await c.post("/mcp", content=raw,
                             headers={"User-Agent": "vscode/1", "Authorization": "Bearer ik_x"})
        assert r.content == raw
        [e] = sink.events
        assert (e.status, e.authenticated, e.user_agent, e.endpoint) == (
            202, True, "vscode/1", "/mcp")
        assert "ik_x" not in json.dumps(e.__dict__)

    @pytest.mark.anyio
    async def test_other_paths_and_methods_pass_through(self):
        sink = ListSink()
        app = ClientAttributionMiddleware(_echo_app(), sink=sink, paths=["/mcp"])
        async with _client(app) as c:
            await c.post("/other", json=LEGACY_INIT)
            await c.get("/mcp")
        assert sink.events == []

    @pytest.mark.anyio
    async def test_broken_sink_never_fails_the_request(self):
        class Boom:
            def record(self, event):
                raise RuntimeError("sink down")
        app = ClientAttributionMiddleware(_echo_app(), sink=Boom(), paths=["/mcp"])
        async with _client(app) as c:
            r = await c.post("/mcp", json=LEGACY_INIT)
        assert r.status_code == 200

    @pytest.mark.anyio
    async def test_non_json_and_oversized_bodies_pass_through_unrecorded(self):
        sink = ListSink()
        app = ClientAttributionMiddleware(_echo_app(), sink=sink, paths=["/mcp"])
        big = b'{"method":"initialize","pad":"' + b"x" * MAX_BODY_BYTES + b'"}'
        async with _client(app) as c:
            assert (await c.post("/mcp", content=b"not json")).content == b"not json"
            assert (await c.post("/mcp", content=big)).content == big
        assert sink.events == []


# ── Wired into the real remote app ────────────────────────────────────────────

@pytest.fixture(scope="module")
async def wired():
    sink = ListSink()
    app = rm.build_app(attribution_sink=sink)
    async with app.router.lifespan_context(app):
        async with _client(app) as c:
            yield c, sink


class TestRemoteWiring:
    @pytest.mark.anyio
    async def test_initialize_on_mcp_is_attributed(self, wired):
        c, sink = wired
        sink.events.clear()
        r = await c.post("/mcp", headers={**MCP_HEADERS, "User-Agent": "vscode/1"},
                         json=LEGACY_INIT)
        assert r.status_code == 200
        [e] = sink.events
        assert (e.endpoint, e.client_name, e.status) == ("/mcp", "Visual Studio Code", 200)

    @pytest.mark.anyio
    async def test_unauthenticated_mcp_auth_records_original_path_and_401(self, wired):
        c, sink = wired
        sink.events.clear()
        r = await c.post("/mcp/auth", headers=MCP_HEADERS, json=LEGACY_INIT)
        assert r.status_code == 401
        [e] = sink.events
        assert (e.endpoint, e.status, e.authenticated) == ("/mcp/auth", 401, False)

    @pytest.mark.anyio
    async def test_health_is_not_attributed(self, wired):
        c, sink = wired
        sink.events.clear()
        await c.get("/mcp/health")
        assert sink.events == []

    def test_can_be_switched_off(self, monkeypatch):
        monkeypatch.setattr(rm, "CLIENT_ATTRIBUTION", False)
        app = rm.build_app()
        assert not any(m.cls is ClientAttributionMiddleware for m in app.user_middleware)
