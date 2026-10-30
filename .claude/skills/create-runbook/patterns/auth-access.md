---
pattern: auth-access
category: auth
service: middleware
severity: P2
evidence_required: [api-response-json, log-excerpt, manifest]
tools: [mcp__mcp-atlassian__jira_get_issue, mcp__plugin_memory_memory__mem_search, mcp__datadog-mcp__search_datadog_logs, curl]
knowledge_ref: "[[Auth Issue Template]]"
---

# Defaults — auth-access

Account blocked, login failures, password recovery errors, session errors, wrong
credentials. Technical category `auth`; default service `middleware` (ADR-032
point 3 normalizes this default from the category template below — override
`service` when the diagnosis points elsewhere, e.g. `portal` for a session-cookie
issue instead of a credential one).

`knowledge_ref` is a stable wikilink (`Auth Issue Template`, unchanged name and
folder per ADR-032 point 3, so it keeps resolving after the template PR merges).
Today, before that PR, the checklist and curated SQL for this pattern live in the
staged, normalized copy:

```
ai-architecture/ai-platform-plan/05-runbooks/migracion/plantillas/documentos/Templates/Auth Issue Template.md
```

`create-runbook` reads that checklist with `get_document` at execution time — one
source of truth (D-11) — it is not duplicated here.
