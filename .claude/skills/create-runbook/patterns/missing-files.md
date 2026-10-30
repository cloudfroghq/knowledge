---
pattern: missing-files
category: data-integrity
service: nextcloud
severity: P2
evidence_required: [api-response-json, log-excerpt, manifest]
tools: [mcp__mcp-atlassian__jira_get_issue, mcp__cloudfrog-knowledge__search_by_metadata, curl]
knowledge_ref: "[[Missing Files Issue Template]]"
---

# Defaults — missing-files

Files or folders the user expects to see are absent from a listing, a share, or a
sync client. Technical category `data-integrity`; default service `nextcloud`
(override to `metadata` or `metadata-extractor` when the diagnosis points at the
indexing pipeline instead of storage itself).

`knowledge_ref` is a stable wikilink (`Missing Files Issue Template`, unchanged
name and folder per ADR-032 point 3). Before its PR merges, the checklist and
curated SQL live in the staged, normalized copy:

```
ai-architecture/ai-platform-plan/05-runbooks/migracion/plantillas/documentos/Templates/Missing Files Issue Template.md
```

`create-runbook` reads that checklist with `get_document` at execution time — one
source of truth (D-11) — it is not duplicated here.
