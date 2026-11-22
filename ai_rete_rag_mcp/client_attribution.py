"""
Which MCP client made each request to the hosted endpoint.

The point is measurement: when the server is listed in a directory (Claude's
connectors, GitHub's MCP registry for Copilot, Glama, Smithery), the only way
to know what that listing brought in is to see who connects. Clients say who
they are in `clientInfo`, and this module records it, one structured log line
per attributed request.

Where `clientInfo` lives depends on the protocol generation:

    legacy  (<= 2025-11-25)  once, in `initialize` params. The endpoint is
                             stateless, so a later `tools/call` carries no
                             clientInfo, only the HTTP User-Agent.
    modern  (2026-07-28)     on every request, in `params._meta` under
                             `io.modelcontextprotocol/clientInfo`.

Both are read, and `User-Agent` is always recorded as the fallback signal.

Three units, kept apart so each can change alone:

    extract_attributions()       pure: JSON-RPC body + headers -> events
    AttributionSink / LoggingSink  where events go
    ClientAttributionMiddleware  ASGI plumbing: buffer, replay, record status

Nothing here logs credentials, IPs, tool arguments or results. Every string
taken from the caller is cleaned and capped, because the caller chooses it.
"""
from __future__ import annotations

import json
import logging
import re
import sys
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from typing import Any, Protocol

CLIENT_INFO_META_KEY = "io.modelcontextprotocol/clientInfo"
PROTOCOL_VERSION_META_KEY = "io.modelcontextprotocol/protocolVersion"

# What is worth a log line. `initialize` / `server/discover` is "a client
# connected", and `tools/call` is "a client used it". `tools/list` and `ping`
# would only add noise.
DEFAULT_METHODS: frozenset[str] = frozenset({"initialize", "server/discover", "tools/call"})

LOGGER_NAME = "ai_rete_rag_mcp.clients"

_MAX_NAME = 64
_MAX_VERSION = 32
_MAX_USER_AGENT = 160
_MAX_TOOL = 64
# Don't parse bodies above this size for attribution. They are still passed
# through untouched; the event is just not recorded. Bodies this large are
# policy documents, never handshakes.
MAX_BODY_BYTES = 2 * 1024 * 1024

_UNSAFE = re.compile(r"[\x00-\x1f\x7f]")


def _clean(value: Any, limit: int) -> str | None:
    """A caller-chosen value made safe to log: a string, with no control
    characters (so no forged log lines), and capped in length."""
    if not isinstance(value, str):
        return None
    value = _UNSAFE.sub("", value).strip()
    return value[:limit] or None


@dataclass(frozen=True)
class AttributionEvent:
    """One attributed MCP request. Every field is safe to log as-is."""

    endpoint: str
    method: str
    tool: str | None
    client_name: str | None
    client_version: str | None
    client_source: str | None      # "initialize" | "meta" | None
    user_agent: str | None
    protocol_version: str | None
    authenticated: bool
    status: int | None = None

    def with_status(self, status: int | None) -> AttributionEvent:
        return AttributionEvent(**{**asdict(self), "status": status})


def _client_info(params: Mapping[str, Any], method: str) -> tuple[Any, str | None]:
    """The raw clientInfo and where it came from. The modern envelope wins,
    because it is the one the client sends on purpose with every request."""
    meta = params.get("_meta")
    if isinstance(meta, Mapping) and isinstance(meta.get(CLIENT_INFO_META_KEY), Mapping):
        return meta[CLIENT_INFO_META_KEY], "meta"
    if method == "initialize" and isinstance(params.get("clientInfo"), Mapping):
        return params["clientInfo"], "initialize"
    return None, None


def _protocol_version(params: Mapping[str, Any]) -> Any:
    meta = params.get("_meta")
    if isinstance(meta, Mapping) and PROTOCOL_VERSION_META_KEY in meta:
        return meta[PROTOCOL_VERSION_META_KEY]
    return params.get("protocolVersion")


def extract_attributions(
    body: Any,
    *,
    endpoint: str,
    user_agent: str | None,
    authenticated: bool,
    methods: Iterable[str] = DEFAULT_METHODS,
) -> list[AttributionEvent]:
    """Events for the attributable messages in a parsed JSON-RPC body.

    A body is a single message or a batch. Anything that isn't shaped like a
    request is skipped rather than rejected. Validating the body is the SDK's
    job, and attribution must never be the reason a request fails.
    """
    wanted = frozenset(methods)
    messages = body if isinstance(body, list) else [body]
    agent = _clean(user_agent, _MAX_USER_AGENT)
    events: list[AttributionEvent] = []
    for message in messages:
        if not isinstance(message, Mapping):
            continue
        method = message.get("method")
        if not isinstance(method, str) or method not in wanted:
            continue
        params = message.get("params")
        params = params if isinstance(params, Mapping) else {}
        info, source = _client_info(params, method)
        info = info or {}
        events.append(AttributionEvent(
            endpoint=endpoint,
            method=method,
            tool=_clean(params.get("name"), _MAX_TOOL) if method == "tools/call" else None,
            client_name=_clean(info.get("name"), _MAX_NAME),
            client_version=_clean(info.get("version"), _MAX_VERSION),
            client_source=source,
            user_agent=agent,
            protocol_version=_clean(_protocol_version(params), _MAX_VERSION),
            authenticated=authenticated,
        ))
    return events


class AttributionSink(Protocol):
    """Where attribution events go. One method, so any backend fits."""

    def record(self, event: AttributionEvent) -> None: ...


class LoggingSink:
    """Writes each event as one JSON line, so `jq` can count them."""

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger or logging.getLogger(LOGGER_NAME)

    def record(self, event: AttributionEvent) -> None:
        self._logger.info(json.dumps({"event": "mcp_client", **asdict(event)},
                                     ensure_ascii=False))


def configure_default_logger() -> logging.Logger:
    """Make the attribution logger visible under a bare `uvicorn` run.

    uvicorn configures only its own loggers, and Python drops INFO records
    from unconfigured ones, so without this every line would vanish. A
    deployment that already attached handlers keeps its own setup.
    """
    logger = logging.getLogger(LOGGER_NAME)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


class ClientAttributionMiddleware:
    """Record who is calling, without changing what they get.

    Raw ASGI rather than BaseHTTPMiddleware, because the body must be read here
    and then handed on intact. The body is buffered, parsed, and replayed to
    the next app as the same chunks it arrived in. The response status is
    captured from `http.response.start` and attached to each event after the
    app has answered, so a 401 from the OAuth gate is recorded as one.
    """

    def __init__(
        self,
        app,
        *,
        sink: AttributionSink,
        paths: Iterable[str],
        methods: Iterable[str] = DEFAULT_METHODS,
    ) -> None:
        self.app = app
        self.sink = sink
        self.paths = frozenset(p.rstrip("/") for p in paths)
        self.methods = frozenset(methods)

    async def __call__(self, scope, receive, send) -> None:
        if (
            scope["type"] != "http"
            or scope.get("method") != "POST"
            or scope["path"].rstrip("/") not in self.paths
        ):
            await self.app(scope, receive, send)
            return

        chunks: list[dict] = []
        size = 0
        while True:
            message = await receive()
            chunks.append(message)
            if message["type"] != "http.request":
                break          # http.disconnect: let the app see it
            size += len(message.get("body", b""))
            if not message.get("more_body", False):
                break

        events = self._events(scope, chunks, size)

        pending = iter(chunks)

        async def replay():
            try:
                return next(pending)
            except StopIteration:
                return await receive()

        status: list[int] = []

        async def capture(message):
            if message["type"] == "http.response.start":
                status.append(message["status"])
            await send(message)

        try:
            await self.app(scope, replay, capture)
        finally:
            for event in events:
                self._record(event.with_status(status[0] if status else None))

    def _events(self, scope, chunks: list[dict], size: int) -> list[AttributionEvent]:
        if size == 0 or size > MAX_BODY_BYTES:
            return []
        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in scope.get("headers", [])}
        try:
            body = json.loads(b"".join(c.get("body", b"") for c in chunks
                                       if c["type"] == "http.request"))
        except (ValueError, UnicodeDecodeError):
            return []
        return extract_attributions(
            body,
            endpoint=scope["path"].rstrip("/"),
            user_agent=headers.get("user-agent"),
            authenticated=headers.get("authorization", "").startswith("Bearer "),
            methods=self.methods,
        )

    def _record(self, event: AttributionEvent) -> None:
        # A broken sink must never fail the caller's request.
        try:
            self.sink.record(event)
        except Exception:  # noqa: BLE001
            logging.getLogger(__name__).exception("client attribution sink failed")
