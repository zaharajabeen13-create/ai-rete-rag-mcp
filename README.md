# ai·rete·rag MCP Server

<!-- mcp-name: com.ai-rete-rag/ai-rete-rag-mcp -->

Use [ai·rete·rag](https://ai-rete-rag.com) — deterministic rule-based decisions with
RAG-powered explanations — from Claude Code, Claude Desktop, or any MCP client.

The verdict always comes from the Rete rule engine (auditable, reproducible);
the LLM only explains *why*, grounded in your ingested policy documents.

## Tools

| Tool | What it does |
|---|---|
| `decide` | Make a decision in a domain — structured facts and/or free text, with optional Pattern 01 (rules scope retrieval) and Pattern 02 (retrieval into working memory) |
| `list_rules` | Inspect a domain's rules — conditions, verdicts, salience, overlaps |
| `get_rule_source` | Fetch a domain's rule set as editable YAML |
| `import_policy_rules` | Turn a written policy document into draft rules, each citing the sentence it encodes (nothing is saved — review, then `put_rules`) |
| `put_rules` | Create or replace a domain's rule set from YAML (dry_run to validate) |
| `ingest_text` | Add policy text to a domain's knowledge base |
| `list_documents` | Browse a domain's ingested documents |
| `get_usage` | Check your plan and remaining monthly decision quota |

## Connect by URL (no install)

The server is also hosted, which is the only route for clients that connect to a
URL and have no field for a static header — claude.ai and Claude Desktop custom
connectors, in particular.

| URL | Auth | Reaches |
|---|---|---|
| `https://ai-rete-rag.com/mcp/auth` | Sign in with Google, once, in the browser | Your account — your domains, your plan quota |
| `https://ai-rete-rag.com/mcp` | None, or `Authorization: Bearer ik_...` | Shared demo domains anonymously; your account with a key |

Add `https://ai-rete-rag.com/mcp/auth` as a custom connector and approve the
prompt. Connections are listed under **Settings → Connected apps**, and
disconnecting one takes effect immediately.

## Install

No install needed with [uv](https://docs.astral.sh/uv/) — `uvx ai-rete-rag-mcp`
fetches and runs the server on demand (see the config snippets below).

Alternatively, install it as a package:

```bash
pip install ai-rete-rag-mcp         # from PyPI
pip install .                       # or from source, in this repo
```

## Configure

First create an API key: sign in at [ai-rete-rag.com](https://ai-rete-rag.com),
open **Settings → API Keys**, and create a key (`ik_...` — shown once).

### Claude Code

```bash
claude mcp add ai-rete-rag -e AI_RETE_RAG_API_KEY=ik_your-key-here -- uvx ai-rete-rag-mcp
```

(If you installed via pip, use `-- ai-rete-rag-mcp` instead of `-- uvx ai-rete-rag-mcp`.)

Or skip the install entirely and point it at the hosted endpoint with your key:

```bash
claude mcp add --transport http ai-rete-rag https://ai-rete-rag.com/mcp \
  --header "Authorization: Bearer ik_your-key-here"
```

### Claude Desktop / other clients (JSON)

```json
{
  "mcpServers": {
    "ai-rete-rag": {
      "command": "uvx",
      "args": ["ai-rete-rag-mcp"],
      "env": {
        "AI_RETE_RAG_API_KEY": "ik_your-key-here"
      }
    }
  }
}
```

(With a pip install, set `"command": "ai-rete-rag-mcp"` and drop `"args"`.)

Environment variables:

| Variable | Default | Purpose |
|---|---|---|
| `AI_RETE_RAG_API_KEY` | *(none)* | Your API key — authenticates calls and ties them to your plan quota |
| `AI_RETE_RAG_API_URL` | `https://ai-rete-rag.com` | API base URL — point at `http://localhost:8000` for local dev |

Without a key you can still explore the shared demo domains (`loan`, `fraud`,
`clinical`, …) subject to free-tier limits.

## Example

> "Use ai·rete·rag to decide whether this loan application should be approved:
> credit score 645, annual income $52k, requested amount $30k."

Claude calls `decide(domain="loan", facts={...})` and returns the rule-derived
verdict plus a plain-English explanation citing the underwriting policy.

<!-- mcp-name: com.ai-rete-rag/ai-rete-rag-mcp -->
