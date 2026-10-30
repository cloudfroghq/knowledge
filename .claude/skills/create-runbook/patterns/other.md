---
pattern: other
category: ""
service: ""
severity: P3
evidence_required: [manifest]
tools: [mcp__mcp-atlassian__jira_get_issue]
knowledge_ref: "none"
---

# Defaults — other

The escape hatch named in `create-runbook`'s own "Cuándo NO correr esto": the
ticket's symptom does not fit any of the five named patterns yet. There is no
category template to fall back on, so `category` and `service` have **no**
default here — `instantiate-runbook.sh` requires both explicitly (`--category`,
`--service`) when `--pattern other` is used, and refuses to guess.

If this pattern keeps recurring for the same symptom, that is a signal to
propose a sixth named pattern (ADR-032 point 2, taxonomy of two axes) instead of
leaving every such runbook under `other` indefinitely.
