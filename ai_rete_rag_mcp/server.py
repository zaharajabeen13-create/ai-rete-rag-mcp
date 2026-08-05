"""
ai·rete·rag MCP server.

Exposes the ai·rete·rag decision platform (https://ai-rete-rag.com) as MCP tools,
so any MCP client — Claude Code, Claude Desktop, Cursor, etc. — can make
deterministic, auditable rule-based decisions with natural-language
explanations.

Configuration (environment variables):
    AI_RETE_RAG_API_KEY     Your API key (create one at ai-rete-rag.com Settings) — recommended
    AI_RETE_RAG_API_URL     Base URL of the ai·rete·rag API (default: https://ai-rete-rag.com)
    AI_RETE_RAG_USER_ID     Legacy fallback identity when no API key is set
    AI_RETE_RAG_USER_EMAIL  Legacy fallback email when no API key is set
"""
from __future__ import annotations

import json
import os
from contextvars import ContextVar
from typing import Any, Literal

import httpx
from mcp.server.fastmcp import FastMCP

API_URL = os.environ.get("AI_RETE_RAG_API_URL", "https://ai-rete-rag.com").rstrip("/")
API_KEY = os.environ.get("AI_RETE_RAG_API_KEY", "")
USER_ID = os.environ.get("AI_RETE_RAG_USER_ID", "")
USER_EMAIL = os.environ.get("AI_RETE_RAG_USER_EMAIL", "")

# Over stdio the key is this process's own, from the environment: one user, one
# key, set once at startup. Served over HTTP the process is shared, so the key
# belongs to the request rather than the process and `remote.py` sets this per
# call. A ContextVar keeps those two concurrent requests from seeing each
# other's credential — a module global would leak one caller's key to the next.
_request_api_key: ContextVar[str | None] = ContextVar("_request_api_key", default=None)

# The remote caller's own IP, when serving over HTTP. The API rate-limits per
# client IP, and every call from the hosted endpoint reaches it over loopback —
# so without passing this along, all remote users share one bucket and throttle
# each other. Unset over stdio, where the caller is the local machine.
_request_client_ip: ContextVar[str | None] = ContextVar("_request_client_ip", default=None)


def current_api_key() -> str:
    """The key this call should authenticate with: the request's, else the
    process's. Empty means anonymous — demo domains only."""
    return _request_api_key.get() or API_KEY

mcp = FastMCP(
    "ai-rete-rag",
    instructions=(
        "ai·rete·rag combines a deterministic Rete rule engine with RAG so decisions are "
        "auditable (rules decide the verdict) and explainable (an LLM explains why, "
        "grounded in the domain's policy documents). Use `decide` for any decision in "
        "a known domain; use `list_rules` first if you are unsure which facts a domain "
        "expects. Author your own rules with `put_rules` (YAML; use dry_run to validate). "
        "Built-in demo domains: loan, fraud, clinical, blockchain, insurance, "
        "legal, operations, ecommerce."
    ),
)


def _headers() -> dict[str, str]:
    headers: dict[str, str] = {}

    # Carries the real caller to the API's per-IP rate limiter. Taken from the
    # connection the endpoint actually accepted, never from a header the caller
    # sent — otherwise anyone could pick an IP and dodge the limit, or wear
    # someone else's.
    client_ip = _request_client_ip.get()
    if client_ip:
        headers["X-Forwarded-For"] = client_ip

    key = current_api_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
        return headers
    # Legacy fallback: bare identity headers (no key verification server-side).
    # Deliberately env-only — these are unverified claims of identity, so they
    # must never be settable by a remote caller.
    if USER_ID:
        headers["X-User-Id"] = USER_ID
    if USER_EMAIL:
        headers["X-User-Email"] = USER_EMAIL
    return headers


async def _request(method: str, path: str, **kwargs: Any) -> str:
    """Call the ai·rete·rag API and return pretty-printed JSON, or a readable error."""
    url = f"{API_URL}/api/v1{path}"
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.request(method, url, headers=_headers(), **kwargs)
    except httpx.RequestError as exc:
        return f"Error: could not reach ai·rete·rag API at {API_URL} ({exc})"

    if r.status_code >= 400:
        try:
            detail = r.json().get("detail", r.text)
        except ValueError:
            detail = r.text
        return f"Error {r.status_code}: {detail}"

    try:
        return json.dumps(r.json(), indent=2)
    except ValueError:
        return r.text


# Not read-only: each call persists an audit record and consumes plan quota.
@mcp.tool(title="Make a decision", annotations={"readOnlyHint": False, "destructiveHint": False})
async def decide(
    domain: str,
    query: str,
    facts: dict[str, Any] | None = None,
    unstructured_text: str | None = None,
    response_mode: Literal["verdict_only", "verdict_with_explanation", "full_audit"] = "verdict_with_explanation",
    filter_retrieval_with_rules: bool = False,
    extract_from_retrieval: bool = False,
) -> str:
    """Make a deterministic, auditable decision in a domain.

    The verdict comes from the domain's rule set (Rete engine, never the LLM),
    so it is reproducible and compliant. The explanation is generated from the
    domain's ingested policy documents.

    Args:
        domain: Rule-set domain, e.g. "loan", "fraud", "clinical".
        query: Natural-language question or decision request.
        facts: Structured facts for working memory, e.g.
            {"credit_score": 710, "annual_income": 85000}. Use `list_rules`
            to see which fields a domain's rules test.
        unstructured_text: Optional free text (an application, a case note);
            facts are extracted from it automatically and merged.
        response_mode: "verdict_only" (fastest), "verdict_with_explanation",
            or "full_audit" (every rule evaluation + retrieved chunks, available
            on every plan including the free tier).
            rule_firings come back in causal order: a rule that matched a fact
            asserted by an earlier firing appears after it, with the derived
            facts listed under `asserted_facts`.
        filter_retrieval_with_rules: Pattern 01 — run the rules first and let a
            fired rule's `retrieval_scope` action narrow which documents the
            retrieval searches before it runs.
        extract_from_retrieval: Pattern 02 — parse the retrieved documents into
            facts and assert them into working memory, so rules fire on what was
            actually read (not just the facts you passed).
    """
    body: dict[str, Any] = {
        "domain": domain,
        "query": query,
        "facts": facts or {},
        "response_mode": response_mode,
    }
    if unstructured_text:
        body["unstructured_text"] = unstructured_text
    if filter_retrieval_with_rules:
        body["filter_retrieval_with_rules"] = True
    if extract_from_retrieval:
        body["extract_from_retrieval"] = True
    return await _request("POST", "/decide", json=body)


@mcp.tool(title="List rules", annotations={"readOnlyHint": True})
async def list_rules(domain: str | None = None) -> str:
    """List the decision rules for one domain (or all domains).

    Returns each rule's conditions — either a flat AND list (field / operator /
    value) or a `when` condition tree (nested all/any/not) — plus its verdict,
    salience, and any asserted facts (`action.assert`, the facts a rule
    produces for other rules to consume). `edges` lists the derived rule→rule
    dependencies: src asserts a fact type that dst's conditions test (forward
    chaining). Each rule may also carry `citation` — the policy sentence it
    encodes — which is what lets a decision be traced back to the source
    clause. Also includes overlap warnings. Use this to learn which fact
    fields a domain expects before calling `decide`.
    """
    params = {"domain": domain} if domain else None
    return await _request("GET", "/rules", params=params)


@mcp.tool(title="Ingest policy text", annotations={"readOnlyHint": False, "destructiveHint": False})
async def ingest_text(domain: str, text: str, source: str | None = None) -> str:
    """Add policy/reference text to a domain's knowledge base.

    The text is chunked and embedded; explanations for future decisions in this
    domain will cite it. Creating a new domain claims it for your account
    (plan limits apply). The built-in demo domains are read-only — ingest into
    your own domain instead. On team plans, only the domain admin (the member
    who created the domain, or the subscription owner) can add documents.

    Args:
        domain: Domain to ingest into (existing or new).
        text: The policy or reference text.
        source: Optional source name shown in the document list.
    """
    metadata = {"source": source} if source else {}
    return await _request(
        "POST", "/ingest/text",
        json={"domain": domain, "text": text, "metadata": metadata},
    )


@mcp.tool(title="List documents", annotations={"readOnlyHint": True})
async def list_documents(domain: str) -> str:
    """List the documents ingested into a domain's knowledge base."""
    return await _request("GET", f"/domains/{domain}/documents")


@mcp.tool(title="Get rule source (YAML)", annotations={"readOnlyHint": True})
async def get_rule_source(domain: str) -> str:
    """Fetch a domain's rule set as editable YAML (plus the parsed rules and
    whether you may edit it). Use this before `put_rules` to see the current
    rules; the built-in demo domains are read-only.
    """
    return await _request("GET", f"/domains/{domain}/rules")


@mcp.tool(title="Save rules", annotations={"readOnlyHint": False, "destructiveHint": True})
async def put_rules(domain: str, rules_yaml: str, dry_run: bool = False) -> str:
    """Create or replace a domain's rule set from YAML (self-serve rule authoring).

    The first save to a new domain claims it for your account (plan limits
    apply); the built-in demo domains are read-only. Rules are validated before
    saving — set dry_run=true to validate without persisting. The response
    reports ok/errors, the parsed rules, and any overlap warnings.

    YAML format — a list of rules. Flat form (conditions are AND-ed):
        - name: "Approve"
          salience: 10
          conditions:
            - type: loan
              field: credit_score
              op: ">="
              value: 700
          action:
            verdict: "APPROVED"
            reason: "Credit score meets threshold"

    Tree form — `when:` holds nested all/any/not condition groups, and an
    action may assert derived facts that other rules consume (forward
    chaining; the rule graph derives from these automatically):
        - name: "Sepsis Screen"
          salience: 30
          when:
            all:
              - {type: clinical, field: temperature_f, op: ">=", value: 101.5}
              - any:
                  - {type: clinical, field: wbc_count, op: ">", value: 12.0}
                  - {type: clinical, field: bands_pct, op: ">", value: 10}
          action:
            verdict: "URGENT_ALERT"
            assert:
              - {type: sepsis_flag, fields: {severity: high}}
        - name: "Escalate"
          salience: 40
          when:
            all:
              - {type: sepsis_flag, field: severity, op: "==", value: high}
              - {type: clinical, field: age, op: ">=", value: 65}
          action:
            verdict: "ADMIT_ICU"

    Use either `conditions:` or `when:` per rule, never both. `not` passes
    when the inner condition does not hold (including when the field is
    absent). Produce/consume cycles between rules are rejected at validation.
    An action may also carry `retrieval_scope: { <key>: <value> }` to narrow
    which documents retrieval searches (Pattern 01).

    A rule may also carry `citation:` — the policy sentence it encodes. It is
    stored with the rule and shown beside it in decision audits, so a verdict
    can be defended with the source language, not just the rule name:
        - name: "Decline Late Returns"
          salience: 20
          citation: "Returns are accepted within 30 days of delivery."
          when:
            all:
              - {type: retail, field: days_since_delivery, op: ">", value: 30}
          action:
            verdict: "DENIED"

    IMPORTANT: when persisting drafts returned by `import_policy_rules`, copy
    each rule's `citation` through into this YAML. Dropping it silently loses
    the link from the decision back to the policy clause that justifies it.

    Args:
        domain: Domain to author (an owned domain, or a new name to claim).
        rules_yaml: The full rule set as YAML text.
        dry_run: Validate only, without saving.
    """
    return await _request(
        "PUT", f"/domains/{domain}/rules",
        json={"rules_yaml": rules_yaml, "dry_run": dry_run},
    )


@mcp.tool(title="Draft rules from a policy", annotations={"readOnlyHint": True})
async def import_policy_rules(domain: str, policy_text: str) -> str:
    """Convert a written policy document into DRAFT decision rules (LLM-assisted).

    Returns validated draft rules (when/action, including chained asserts where
    the policy stages its determinations), derived rule→rule edges, and overlap
    warnings. Each returned rule carries a `citation` field holding the policy
    sentence it encodes (also summarized in the top-level `citations` map).
    NOTHING IS SAVED: review the drafts (and show them to the user), then
    persist explicitly with `put_rules` — validate first with dry_run=true,
    and keep each rule's `citation` in the YAML you save so the audit trail
    back to the policy survives.

    Args:
        domain: Domain the rules are drafted for (an owned domain or a new name).
        policy_text: The policy document text (max ~50k characters).
    """
    return await _request(
        "POST", f"/domains/{domain}/rules/import",
        json={"policy_text": policy_text},
    )


@mcp.tool(title="Check usage and quota", annotations={"readOnlyHint": True})
async def get_usage() -> str:
    """Show this account's decision usage, plan, and remaining monthly quota."""
    if not current_api_key() and not USER_ID:
        return ("Error: no API key. Set AI_RETE_RAG_API_KEY when running locally, "
                "or send it as an Authorization: Bearer header when connecting "
                "to a remote endpoint.")
    return await _request("GET", "/usage")


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
