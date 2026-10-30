---
pattern: registration-subscription
category: registration
service: middleware
severity: P2
evidence_required: [api-response-json, sql-readonly-result, manifest]
tools: [mcp__mcp-atlassian__jira_get_issue, mysql-readonly, curl]
knowledge_ref: "[[Registration Issue Template]]"
---

# Defaults — registration-subscription

Sign-up, plan assignment, or BSS subscription sync fails or disagrees with what
the partner's billing system shows. Technical category `registration`; default
service `middleware` (this is the pattern behind RB-001 and RB-004 already in the
vault — check those first with `mem_runbook_find` before instantiating a new one).

`knowledge_ref` is a stable wikilink (`Registration Issue Template`, unchanged
name and folder per ADR-032 point 3). Before its PR merges, the checklist and
curated SQL live in the staged, normalized copy:

```
ai-architecture/ai-platform-plan/05-runbooks/migracion/plantillas/documentos/Templates/Registration Issue Template.md
```

`create-runbook` reads that checklist with `get_document` at execution time — one
source of truth (D-11) — it is not duplicated here.
