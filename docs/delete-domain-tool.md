# delete_domain tool

Calls `DELETE /api/v1/domains/{domain}?confirm={domain}` on the ai-rete-rag API. The tool removes the caller's own copy of a domain (rules and documents) and frees its slot in the plan's domain limit. Decision audits are kept, and a domain re-created under the same name starts with a fresh history. The tool is annotated `destructiveHint: true`, and `confirm` must repeat the domain name. The server instructions tell the assistant to delete only after the user confirms which domain.

Requires the API change in zaharajabeen13-create/ai-rete-rag (branch `fix/domain-delete-frees-quota`). Until that change is deployed, the tool returns a 404/405 error from the API.

## C4 — container view

```mermaid
C4Container
title Delete domain
Person(user, "Domain admin")
System_Ext(agent, "MCP client", "Copilot / Claude / ChatGPT")
Container_Boundary(arr, "ai-rete-rag") {
  Container(web, "Website", "React", "Rules page → Delete domain (type name to confirm)")
  Container(mcp, "MCP server", "Python", "delete_domain(domain, confirm)")
  Container(api, "API", "FastAPI", "DELETE /domains/{domain}?confirm=")
  ContainerDb(db, "SQLite", "domain_ownership, retired_storage_keys, domain_rule_sets, decision_audits")
  ContainerDb(vec, "ChromaDB", "One collection per storage key")
}
Rel(user, web, "Uses")
Rel(user, agent, "Asks")
Rel(agent, mcp, "tools/call")
Rel(web, api, "HTTPS")
Rel(mcp, api, "HTTPS")
Rel(api, vec, "delete_domain(key)")
Rel(api, db, "delete rule set; release_domain(key) → retire key")
```
