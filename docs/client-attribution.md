# Client attribution logging

The hosted endpoint (`/mcp` and `/mcp/auth`) writes one structured log line for
each MCP request that matters for attribution. Each line says which MCP client
made the request (Claude, VS Code / Copilot, Cursor, a directory scanner, …), so
traffic from each directory listing can be measured instead of guessed.

The stdio server does not log this. It runs on the user's own machine, so a log
written there never reaches the operator.

## What is logged

One JSON object per line, on the `ai_rete_rag_mcp.clients` logger (stderr by
default):

```json
{"event": "mcp_client", "endpoint": "/mcp", "method": "tools/call",
 "tool": "decide", "client_name": "claude-ai", "client_version": "0.1.0",
 "client_source": "meta", "user_agent": "claude-user/1.0",
 "protocol_version": "2026-07-28", "authenticated": false, "status": 200}
```

| Field | Meaning |
|---|---|
| `endpoint` | `/mcp` (anonymous / API key) or `/mcp/auth` (OAuth), before the path is rewritten |
| `method` | JSON-RPC method: `initialize`, `server/discover` or `tools/call` |
| `tool` | Tool name, for `tools/call` only |
| `client_name`, `client_version` | The client's own `clientInfo` |
| `client_source` | Where `clientInfo` came from: `initialize` (legacy handshake), `meta` (per-request `io.modelcontextprotocol/clientInfo` envelope key), or `null` when the request carried none |
| `user_agent` | HTTP `User-Agent`. This is the only client signal on a legacy-protocol `tools/call`, because the endpoint is stateless and `clientInfo` arrived on an earlier `initialize` |
| `protocol_version` | From the request envelope or `initialize` params |
| `authenticated` | Whether an `Authorization` header was present. The token itself is never logged |
| `status` | HTTP status returned. `401` on `/mcp/auth` is a client being sent to sign in |

### What is never logged

API keys, OAuth tokens, caller IPs, tool arguments (facts, policy text, rule
YAML) and tool results. Client-supplied strings are stripped of control
characters and truncated, because a caller can put anything there.

## Configuration

| Variable | Default | Effect |
|---|---|---|
| `MCP_CLIENT_ATTRIBUTION` | `on` | `off` disables the middleware entirely |

The logger gets a stderr handler at `INFO` only if it has no handlers, so a
deployment that configures logging itself keeps full control.

## Counting clients

```bash
# initialize handshakes per client
journalctl -u ai-rete-rag-mcp -o cat | grep '"event": "mcp_client"' \
  | jq -r 'select(.method=="initialize") | .client_name' | sort | uniq -c | sort -rn

# decide calls per user agent
... | jq -r 'select(.tool=="decide") | .user_agent' | sort | uniq -c | sort -rn
```

## Design (C4)

### Level 1: System context

```mermaid
C4Context
    title Client attribution: system context
    Person(user, "MCP user", "Uses Claude, Copilot, Cursor, …")
    System_Ext(client, "MCP client", "claude.ai, VS Code/Copilot, Cursor, directory scanners")
    System(mcp, "ai·rete·rag MCP remote", "Streamable HTTP at /mcp and /mcp/auth")
    System_Ext(api, "ai·rete·rag API", "Rules engine + RAG")
    System_Ext(logs, "Log store", "journald / stderr")
    Person(op, "Operator", "Measures traffic per directory listing")

    Rel(user, client, "Asks")
    Rel(client, mcp, "JSON-RPC over HTTP, with clientInfo + User-Agent")
    Rel(mcp, api, "Tool calls")
    Rel(mcp, logs, "One JSON line per attributed request")
    Rel(op, logs, "Counts clients")
```

### Level 2: Containers

```mermaid
C4Container
    title Client attribution: containers
    System_Ext(client, "MCP client")
    Container_Boundary(box, "MCP host") {
        Container(nginx, "nginx", "Reverse proxy", "TLS behind Cloudflare, forwards /mcp*")
        Container(remote, "remote.py ASGI app", "Python, Starlette, uvicorn", "Serves the tools from server.py")
        ContainerDb(journal, "journald", "stderr capture")
    }
    System_Ext(api, "ai·rete·rag API")

    Rel(client, nginx, "HTTPS")
    Rel(nginx, remote, "HTTP, loopback")
    Rel(remote, api, "HTTPS")
    Rel(remote, journal, "ai_rete_rag_mcp.clients log lines")
```

### Level 3: Components (inside `remote.py`'s app)

```mermaid
C4Component
    title Client attribution: components
    Container_Boundary(app, "remote.py ASGI app") {
        Component(cors, "CORSMiddleware", "Starlette", "Preflight + CORS headers")
        Component(caller, "CallerContextMiddleware", "ASGI", "Binds key + IP to the request")
        Component(attr, "ClientAttributionMiddleware", "ASGI", "Buffers + replays the body, records the response status")
        Component(extract, "extract_attributions()", "Pure function", "JSON-RPC body + headers to AttributionEvent list")
        Component(sink, "AttributionSink", "Protocol", "record(event)")
        Component(logsink, "LoggingSink", "AttributionSink impl", "One JSON line on ai_rete_rag_mcp.clients")
        Component(gate, "ProtectedEndpointMiddleware", "ASGI", "OAuth gate for /mcp/auth")
        Component(mcpapp, "MCP streamable HTTP app", "mcp SDK", "Tools from server.py")
    }

    Rel(cors, caller, "next")
    Rel(caller, attr, "next")
    Rel(attr, extract, "parses with")
    Rel(attr, sink, "records to")
    Rel(logsink, sink, "implements")
    Rel(attr, gate, "next")
    Rel(gate, mcpapp, "next")
```

The attribution middleware sits **outside** the OAuth gate. That way it sees
the original `/mcp/auth` path before the gate rewrites it, and it records the
`401`s the gate returns.

### SOLID notes

- **Single responsibility.** Parsing (`extract_attributions`), transport
  plumbing (`ClientAttributionMiddleware`) and output (`LoggingSink`) are
  separate units, and each is tested on its own.
- **Open/closed.** The middleware takes the set of `methods` to record and the
  `paths` to watch as arguments. To add a method, pass a different set; the
  class doesn't change.
- **Liskov substitution / interface segregation.** `AttributionSink` has one
  method, `record`. Tests pass a list-backed sink. A metrics sink (Prometheus,
  forwarding to the API) can replace `LoggingSink` without touching the
  middleware.
- **Dependency inversion.** The middleware depends on the `AttributionSink`
  protocol, never on `logging`. `remote.build_app()` chooses the concrete sink.
