---
name: sync-runbook-index
description: Rebuilds memory's runbook index by reading the vault through the knowledge tools, so that mem_runbook_find answers based on what the vault says today.
when_to_use: when mem_project_card reports runbooks_stale > 0, when the index's synced_at is more than 7 days old, when you have just merged a PR that adds or renames a runbook, or before a triage if mem_runbook_find returns no candidates for a symptom you know is documented.
allowed-tools: Read, Bash(memory project *), mcp__cloudfrog-knowledge__search_by_metadata, mcp__cloudfrog-knowledge__get_document, mcp__cloudfrog-knowledge__get_stale_docs, mcp__memory__mem_project_card, mcp__memory__mem_runbook_index_sync, mcp__memory__mem_runbook_find, mcp__plugin_memory_memory__mem_save
model: sonnet
effort: medium
disable-model-invocation: false
user-invocable: true
argument-hint: [service]
---

# sync-runbook-index

Rebuilds memory's `runbook_index` from the vault. The index is a derived cache: the vault document always wins, and this skill only copies it into memory. It never writes to the vault.

With no argument it syncs every service; with `$1` it narrows to the given service.

## When NOT to run this

- If `mem_project_card` says `runbooks_stale: 0` and the index was synced less than 7 days ago, there is nothing to do: a resync does not change a single row and spends 10 calls to knowledge.
- If what you want is to fix a runbook, this skill does not help: the vault is edited by human PR (D-11). Use `open-pr`.

## Steps

1. **Measure before touching.** `mem_project_card(project, include_counts=true)` and keep `counts.runbooks` and `counts.runbooks_stale`. Without that snapshot you cannot tell afterwards whether the sync was of any use.
2. **Enumerate.** `search_by_metadata(type="runbook", limit=200)`. It answers `{total, offset, items}`: the runbooks are in `items`, and while `offset + len(items) < total`, request the next page with `offset=offset + len(items)`. Without walking every page you did not enumerate the whole vault.
3. **Read each result's frontmatter.** `get_document(file_path, max_chars=1500, full_metadata=true)`. Without `full_metadata`, `metadata` carries only the 2.0.0 allowlist: `type`, `status`, `service`, `owner`, `tags` and `last_updated`. From the full metadata come `id`, `title`, `service`, `category`, `pattern`, `severity`, `status`, `symptoms`, `tags`, `owner`, `automation_level`, `last_updated` and `last_verified`. `max_chars=1500` is enough for the header and avoids fetching the whole runbook.
4. **Fetch freshness.** `get_stale_docs(limit=30, type="runbook")` returns `needs_review` and `age_days` per `file_path`; it exists only in a vault that declares `features: [governance]` (Cloud frog declares it; without it, it answers `E_FEATURE_DISABLED`). Join by `file_path`, not by title: two runbooks can share a title and none shares a path.
5. **Sync.** `mem_runbook_index_sync(source="knowledge-mcp", entries=[...])`, with `prune_missing=true` **only** if step 2 enumerated the whole vault, page by page up to `total`; with a `$1` that narrows the service, `prune_missing` would delete from the index runbooks that do exist.
6. **Read `skipped[]` — it is the deliverable, not a detail.** Each reason says something different and only two are normal.
7. **Verify.** `mem_runbook_find` with a symptom you know is documented (for example `"preview 503"` for `RB-003`) and confirm `id`, `stale` and `age_days`. A sync that reports `upserted` but whose `find` returns nothing left the FTS unpopulated; it must be investigated, not repeated.
8. **Save the finding if there was one.** `mem_save` (`type: discovery`, `topic_key: knowledge-mcp/runbooks/index`) when a skip reveals a vault defect, with the exact `file_path` and reason.

## What each `skipped[]` reason means

| Reason | What happened | What to do |
|---|---|---|
| `template` | The document is a template (tag `template` or `Runbooks/Templates/`) | Nothing. It is the filter working. |
| `not_runbook` | `type` other than `runbook`: playbooks and MOCs are not indexed in v1 | Nothing. They are reached from the runbook's `Related Notes`. |
| `invalid_id` | The `id` does not match `RB-NNN`, or the document has no `id` | Vault defect. PR with `open-pr` assigning the next free id (`create-runbook/scripts/next-runbook-id.sh`). |
| `invalid_status` | `status` outside `draft\|verified\|outdated` | Vault defect. PR fixing the frontmatter. |
| `unknown_service` | The `service` is not in the canonical list of 15 slugs | Almost always a template with the literal enum, or a new service. If the service really exists, the map lives in `internal/runbooks/service_map.go` in the memory fork and is extended there, not here. |
| `missing_service` | The document does not declare `service` | Vault defect. PR adding it. |

None of these reasons is fixed by writing to the vault from the session: the vault is edited by human PR with the service owner as reviewer.

## Alternative route without MCP

With a clone of the vault repository at hand, the same index can be rebuilt without the server:

```bash
memory project <slug> runbooks sync \
  --vault-dir "$HOME/Projects/Vaults/<slug>" --json
```

The vault does not live in this repository: `--vault-dir` is the vault's `path` in
`~/.config/knowledge/vaults.yaml` (or the process's `VAULT_PATH`), which on the
owner's machine is `~/Projects/Vaults/<slug>`. Do not derive it from
`git rev-parse --show-toplevel` of this checkout: there is no vault there anymore.

Two things change compared with the MCP route: `source` is `vault-fs`, and `stale` is derived from `age_days > 90` instead of coming from `needs_review`. The skips are the same, plus `not_runbook`, which does not appear over MCP because `search_by_metadata(type="runbook")` already filtered it out.

See `checklist.md` before accepting the index as good.
