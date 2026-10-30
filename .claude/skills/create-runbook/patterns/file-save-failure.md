---
pattern: file-save-failure
category: data-integrity
service: nextcloud
severity: P3
evidence_required: [screenshot, log-excerpt, manifest]
tools: [mcp__mcp-atlassian__jira_get_issue, curl, occ]
knowledge_ref: "[[File Save Failure Template]]"
---

# Defaults — file-save-failure

An upload or an edit reports success but the file is truncated, corrupted, or
never lands in storage. Technical category `data-integrity`; default service
`nextcloud` (override to `imaginary` when the failure is specific to preview or
thumbnail generation rather than the save path itself).

`knowledge_ref` is a stable wikilink (`File Save Failure Template`, unchanged name
and folder per ADR-032 point 3). Before its PR merges, the checklist and curated
SQL live in the staged, normalized copy:

```
ai-architecture/ai-platform-plan/05-runbooks/migracion/plantillas/documentos/Templates/File Save Failure Template.md
```

`create-runbook` reads that checklist with `get_document` at execution time — one
source of truth (D-11) — it is not duplicated here.
