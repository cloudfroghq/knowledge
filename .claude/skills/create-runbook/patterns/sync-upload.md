---
pattern: sync-upload
category: network
service: nextcloud
severity: P2
evidence_required: [api-response-json, log-excerpt, manifest]
tools: [mcp__mcp-atlassian__jira_get_issue, mcp__datadog-mcp__search_datadog_logs, curl]
knowledge_ref: "[[Sync Issue Template]]"
---

# Defaults — sync-upload

The desktop or mobile client stalls, retries forever, or reports a conflict while
syncing or uploading. Technical category `network`; default service `nextcloud`
(override to `notifications` when the failure is in the push confirmation rather
than the transfer itself).

`knowledge_ref` is a stable wikilink (`Sync Issue Template`, unchanged name and
folder per ADR-032 point 3). Before its PR merges, the checklist and curated SQL
live in the staged, normalized copy:

```
ai-architecture/ai-platform-plan/05-runbooks/migracion/plantillas/documentos/Templates/Sync Issue Template.md
```

`create-runbook` reads that checklist with `get_document` at execution time — one
source of truth (D-11) — it is not duplicated here.
