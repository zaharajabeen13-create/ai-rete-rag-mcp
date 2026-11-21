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
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

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


# Which surface a call came through, sent as X-Client-Channel so the API's
# day-level usage counts can tell MCP traffic from the website. Carries no
# identity. `remote.py` switches it: that process only serves the hosted endpoint.
CHANNEL = "mcp-stdio"


def current_api_key() -> str:
    """The key this call should authenticate with: the request's, else the
    process's. Empty means anonymous — demo domains only."""
    return _request_api_key.get() or API_KEY

mcp = MCPServer(
    "ai-rete-rag",
    instructions=(
        "ai·rete·rag combines a deterministic Rete rule engine with RAG so decisions are "
        "auditable (rules decide the verdict) and explainable (an LLM explains why, "
        "grounded in the domain's policy documents). Use `decide` for any decision in "
        "a known domain. To decide from a user's case, call `get_parameters` first to "
        "learn which facts the domain's rules decide on, collect those, then `decide`; "
        "if the result lists `missing_parameters`, the verdict could still change, so "
        "ask for those facts and decide again. When the case arrives as photos, voice "
        "notes or video, read them yourself, pass the values you found as `facts` "
        "under the `get_parameters` field names with `fact_sources` naming the file "
        "each came from, and pass any transcript or description as `unstructured_text`. "
        "If a `decide` result carries `next_step` (anonymous use), relay it to the user. "
        "To turn a policy into rules: `import_policy_rules` returns drafts and saves "
        "nothing; show the user the drafts, `uncovered_sentences` and "
        "`unverified_citations`, then save with `put_rules` (validate with dry_run "
        "first) and keep each rule's `citation` in the YAML. Use `get_rule_source` to "
        "see current rules before editing them. "
        "If a save or ingest is refused with a domain limit, `delete_domain` frees a "
        "slot, but only after the user confirms which domain to delete; copies of the "
        "demo domains never count toward the limit. "
        "Built-in demo domains: loan, fraud, clinical, blockchain, insurance, "
        "legal, operations, ecommerce."
    ),
)


def _headers() -> dict[str, str]:
    headers: dict[str, str] = {"X-Client-Channel": CHANNEL}

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
    """Call the ai·rete·rag API and return pretty-printed JSON.

    A failed call raises `ToolError` with a readable message. The SDK prefixes
    it with "Error executing tool <name>:" and flags the result `isError: true`,
    so clients such as Microsoft 365 Copilot can tell a refusal (sign-in
    required, domain limit) from data. A returned string would arrive flagged
    as success."""
    url = f"{API_URL}/api/v1{path}"
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.request(method, url, headers=_headers(), **kwargs)
    except httpx.RequestError as exc:
        raise ToolError(f"could not reach the ai·rete·rag API at {API_URL} ({exc})") from exc

    if r.status_code >= 400:
        try:
            detail = r.json().get("detail", r.text)
        except ValueError:
            detail = r.text
        raise ToolError(f"ai·rete·rag API returned {r.status_code}: {detail}")

    try:
        return json.dumps(r.json(), indent=2)
    except ValueError:
        return r.text


# Tool annotations, spelled out in full. Directory reviewers (OpenAI's in
# particular) require every hint to be an explicit boolean rather than left to
# the protocol default. openWorldHint is False throughout: every tool talks only
# to ai·rete·rag's own API, never to arbitrary external services.
READ_ONLY = {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False}
WRITE = {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False}
DESTRUCTIVE_WRITE = {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": False}


# Not read-only: each call persists an audit record and consumes plan quota.
@mcp.tool(title="Make a decision", annotations=WRITE)
async def decide(
    domain: str,
    query: str,
    facts: dict[str, Any] | None = None,
    unstructured_text: str | None = None,
    response_mode: Literal["verdict_only", "verdict_with_explanation", "full_audit"] = "verdict_with_explanation",
    filter_retrieval_with_rules: bool = False,
    extract_from_retrieval: bool = False,
    fact_sources: dict[str, str] | None = None,
    as_of: str | None = None,
) -> str:
    """Make a deterministic, auditable decision in a domain.

    The verdict comes from the domain's rule set (Rete engine, never the LLM),
    so it is reproducible and compliant. The explanation is generated from the
    domain's ingested policy documents.

    Without an API key only the shared demo domains are available, and the
    response carries `next_step`: how to get a free key for your own rules.

    Args:
        domain: Rule-set domain, e.g. "loan", "fraud", "clinical".
        query: Natural-language question or decision request.
        facts: Structured facts for working memory, e.g.
            {"credit_score": 710, "annual_income": 85000}, keyed by the
            field names the domain's rules test. Facts read from photos,
            document scans, voice notes or video belong here too; explicit
            facts always win over anything extracted server-side.
        fact_sources: Where each fact in `facts` came from, e.g.
            {"monthly_income": "payslip.jpg", "claim_usd": "voice note 1"}.
            Stored in the decision's audit record so a reviewer can tell
            typed facts from ones read off evidence. Facts without a source
            are recorded as "request".
        as_of: Read the domain's policy documents as of this day, as
            YYYY-MM-DD (default: today), so a case can be decided under the
            policy in force on the day it was filed; the rules themselves are
            always the current ones.
        unstructured_text: Optional free text (an application, a case note,
            a voice-note transcript, what a photo or video shows); facts are
            extracted from it automatically and merged.
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

    The result's `missing_parameters` lists rule inputs that were not
    supplied but could still change the verdict, most decisive first. When it
    is non-empty the verdict is provisional; when it is empty, more facts
    cannot change the verdict.
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
    if fact_sources:
        body["fact_sources"] = fact_sources
    if as_of:
        body["as_of"] = as_of
    return await _request("POST", "/decide", json=body)


@mcp.tool(title="List rules", annotations=READ_ONLY)
async def list_rules(domain: str | None = None) -> str:
    """List the decision rules for one domain (or all domains).

    Returns each rule's conditions — either a flat AND list (field / operator /
    value) or a `when` condition tree (nested all/any/not) — plus its verdict,
    salience, and any asserted facts (`action.assert`, the facts a rule
    produces for other rules to consume). `edges` lists the derived rule→rule
    dependencies: src asserts a fact type that dst's conditions test (forward
    chaining). Each rule may also carry `citation` — the policy sentence it
    encodes — which is what lets a decision be traced back to the source
    clause. Also includes overlap warnings and, through the conditions, the
    fact fields the domain's rules test.
    """
    params = {"domain": domain} if domain else None
    return await _request("GET", "/rules", params=params)


@mcp.tool(title="Get decision parameters", annotations=READ_ONLY)
async def get_parameters(domain: str) -> str:
    """List the input parameters a domain's rules decide on — the facts a
    decision in that domain uses. Derived from your account's own copy of
    the rules, so it covers your custom domains too.

    Each parameter has its `field` name (the key to use in `facts`), `kind`
    (number / boolean / string), the `tests` rules make on it (e.g. >= 720 —
    match these units and scales), the `rules` and `verdicts` it can lead to,
    and `max_salience`. Parameters come most decisive first: those feeding
    the highest-salience rules, which win when several rules fire. Also
    returns every verdict the domain can reach.
    """
    return await _request("GET", f"/domains/{domain}/parameters")


@mcp.prompt(title="Decide a case")
def decide_case(domain: str, case: str) -> str:
    """Agent workflow: pick the parameters a domain's rules need from a case —
    text, photos, voice notes or video — ask for what is missing, and reach a
    rule-backed verdict."""
    return (
        f"Decide this case in the ai·rete·rag domain `{domain}`.\n\n"
        f"Case:\n{case}\n\n"
        "1. Call `get_parameters` for the domain.\n"
        "2. Read the case evidence yourself — the text, plus any photos, document "
        "scans, voice notes or video attached or referenced (use whatever "
        "transcription or frame tools you have for audio and video). Map what it "
        "shows onto those exact field names, converting to the units and scale the "
        "rule tests use (e.g. 31% DTI tested against 0.36 → 0.31). Only use values "
        "you can actually see, hear or read; do not invent or estimate them.\n"
        "3. Call `decide` with those facts and `fact_sources` naming where each one "
        "came from (the file name, or \"user message\" for facts stated in text). "
        "Pass the case text, and any transcript or short description of what the "
        "media shows, as `unstructured_text`.\n"
        "4. If `missing_parameters` is non-empty, the verdict is provisional: ask "
        "me for those facts, most decisive first, then decide again with them.\n"
        "5. Report the verdict, the deciding rule and its reason, the facts used "
        "and the `fact_sources` recorded for them, "
        "and the decision_id for the audit trail. The verdict comes from the "
        "rules — never override or soften it."
    )


@mcp.tool(title="Ingest policy text", annotations=WRITE)
async def ingest_text(
    domain: str,
    text: str,
    source: str | None = None,
    effective_from: str | None = None,
    effective_until: str | None = None,
    supersedes: str | None = None,
) -> str:
    """Add policy/reference text to a domain's knowledge base.

    The text is chunked and embedded; explanations for future decisions in this
    domain will cite it. Creating a new domain claims it for your account
    (plan limits apply). Ingesting into a built-in demo domain gives your
    account its own copy of that demo (it counts as one of your domains); the
    shared demo others see is unchanged. On team plans, only the domain admin (the member
    who created the domain, or the subscription owner) can add documents.

    Documents can be dated. A decision only cites documents in force on its
    date, so a replaced policy is never quoted for a new decision. To publish a
    new version, ingest it under a new `source` with `supersedes` naming the old
    one: the old document stops applying on the new one's `effective_from`
    (today if not given), and the new one starts that day. A document can be
    superseded only once; to replace a successor, supersede the successor.

    Args:
        domain: Domain to ingest into (existing or new).
        text: The policy or reference text.
        source: Optional source name shown in the document list.
        effective_from: First day this document applies, as YYYY-MM-DD
            (default: always).
        effective_until: First day it no longer applies, as YYYY-MM-DD
            (default: no end).
        supersedes: Source name of the document this one replaces.
    """
    metadata = {"source": source} if source else {}
    body: dict[str, Any] = {"domain": domain, "text": text, "metadata": metadata}
    for key, value in (("effective_from", effective_from), ("effective_until", effective_until),
                       ("supersedes", supersedes)):
        if value:
            body[key] = value
    return await _request("POST", "/ingest/text", json=body)


@mcp.tool(title="List documents", annotations=READ_ONLY)
async def list_documents(domain: str) -> str:
    """List the documents ingested into a domain's knowledge base."""
    return await _request("GET", f"/domains/{domain}/documents")


@mcp.tool(title="Delete domain", annotations={**DESTRUCTIVE_WRITE, "idempotentHint": False})
async def delete_domain(domain: str, confirm: str) -> str:
    """Permanently delete your own copy of a domain: its rule set and every
    ingested document. This frees its slot in your plan's domain limit.

    Decision audit records are kept for the audit trail. A domain created later
    under the same name starts empty, with a fresh history. The shared demo
    domains are never affected: deleting `loan` removes only your own copy of it.

    Args:
        domain: The domain to delete.
        confirm: Must repeat the domain name exactly; any other value is refused.
    """
    return await _request("DELETE", f"/domains/{domain}", params={"confirm": confirm})


# structured_output=False: the reply is text meant to be shown as-is. With
# structured output on, the SDK would also send the whole reply a second time
# as one JSON-escaped string in `structuredContent`, which MCP clients are told
# to prefer. Copilot then had an escaped one-line copy of the YAML to work from
# as well, and sometimes cut the YAML short (ticket #5971696, item 6).
@mcp.tool(title="Get rule source (YAML)", annotations=READ_ONLY, structured_output=False)
async def get_rule_source(domain: str) -> str:
    """Fetch a domain's complete rule set as editable YAML, plus whether you may
    edit it. The whole YAML file is returned as one fenced block; show it to the
    user in full, never just its first lines. For a built-in demo domain you get
    your own copy's rules once you have edited it, otherwise the shared demo's.
    """
    return format_rule_source(await _request("GET", f"/domains/{domain}/rules"))


def format_rule_source(raw: str) -> str:
    """Render the API's rule-source response as the complete YAML in a fenced
    block under a one-line header.

    The API's JSON carries the YAML as one escaped string alongside the parsed
    rules, overlaps and edges, several times its size. Clients such as
    Microsoft 365 Copilot then summarise or cut the reply and the user sees
    only the first lines of the file. Returning the YAML itself, once and
    unescaped, keeps the whole file in view. Errors pass through unchanged."""
    try:
        data = json.loads(raw)
    except ValueError:
        return raw
    if not isinstance(data, dict) or "rules_yaml" not in data:
        return raw

    domain = data.get("domain", "")
    editable = "yes" if data.get("editable") else "no"
    if not data.get("exists", True):
        hint = " Save one with `put_rules` to create it." if data.get("editable") else ""
        return f"No rule set has been authored for domain `{domain}` yet (editable: {editable}).{hint}"

    yaml_text = str(data.get("rules_yaml", "")).rstrip("\n")
    n_rules = len(data.get("rules") or [])
    n_lines = yaml_text.count("\n") + 1 if yaml_text else 0
    demo = "yes" if data.get("is_demo") else "no"
    fence = "````" if "```" in yaml_text else "```"
    # The closing line names where the file ends. A bare closing fence as the
    # last line gave the model nothing to check its copy against, and Copilot
    # sometimes stopped a few lines in while saying it had shown everything.
    return (
        f"Complete rule set for domain `{domain}`: {n_rules} rules, {n_lines} lines of YAML "
        f"(editable: {editable}, built-in demo: {demo}). Show all {n_lines} lines.\n\n"
        f"{fence}yaml\n{yaml_text}\n{fence}\n\n"
        f"End of the `{domain}` rule set: {n_rules} rules, {n_lines} lines."
    )


@mcp.tool(title="Save rules", annotations=DESTRUCTIVE_WRITE)
async def put_rules(domain: str, rules_yaml: str, dry_run: bool = False) -> str:
    """Create or replace a domain's rule set from YAML (self-serve rule authoring).

    The first save to a new domain claims it for your account (plan limits
    apply). Saving to a built-in demo domain saves your own copy of it, seeded
    from the demo; the shared demo others see is unchanged. Rules are validated before
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

    A rule saved without its `citation` has no link from its decisions back
    to the policy clause that justifies it.

    Args:
        domain: Domain to author (an owned domain, or a new name to claim).
        rules_yaml: The full rule set as YAML text.
        dry_run: Validate only, without saving.
    """
    return await _request(
        "PUT", f"/domains/{domain}/rules",
        json={"rules_yaml": rules_yaml, "dry_run": dry_run},
    )


@mcp.tool(title="Draft rules from a policy", annotations=READ_ONLY)
async def import_policy_rules(domain: str, policy_text: str) -> str:
    """Convert a written policy document into DRAFT decision rules (LLM-assisted).

    Returns validated draft rules (when/action, including chained asserts where
    the policy stages its determinations), derived rule→rule edges, and overlap
    warnings. Each returned rule carries a `citation` field holding the policy
    sentence it encodes (also summarized in the top-level `citations` map).
    Coverage runs the other way too: `uncovered_sentences` lists policy
    sentences no rule's citation points back to (out of
    `policy_sentence_count`), and `unverified_citations` names rules whose
    quote is missing or not found verbatim in the policy. An uncovered
    sentence may need a rule; an unverified one may be a paraphrase that
    misstates the policy. `background_sentences` is the subset of uncovered
    sentences with no obligation language (definitions, preamble), split by a
    keyword heuristic.
    Nothing is saved: the drafts are returned for review only.

    Args:
        domain: Domain the rules are drafted for (an owned domain or a new name).
        policy_text: The policy document text (max ~50k characters).
    """
    return await _request(
        "POST", f"/domains/{domain}/rules/import",
        json={"policy_text": policy_text},
    )


@mcp.tool(title="Check usage and quota", annotations=READ_ONLY)
async def get_usage() -> str:
    """Show this account's decision usage, plan, and remaining monthly quota."""
    if not current_api_key() and not USER_ID:
        raise ToolError("no API key. Set AI_RETE_RAG_API_KEY when running locally, "
                        "or send it as an Authorization: Bearer header when connecting "
                        "to a remote endpoint.")
    return await _request("GET", "/usage")


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
