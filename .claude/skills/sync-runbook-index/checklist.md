# Checklist — sync-runbook-index

- [ ] `mem_project_card` run BEFORE the sync; `runbooks` and `runbooks_stale` noted.
- [ ] `search_by_metadata(type="runbook")` enumerated the vault reading `items` page by page, until `offset + len(items)` reached `total`.
- [ ] `get_document(..., full_metadata=true)` run for every result; no frontmatter field invented or inferred from the file name.
- [ ] `get_stale_docs(type="runbook")` joined by `file_path`, never by title.
- [ ] `prune_missing=true` only if the enumeration covered the whole vault.
- [ ] `skipped[]` read in full and classified: `template` and `not_runbook` are ignored; `invalid_id`, `invalid_status`, `unknown_service` and `missing_service` remain as vault defects with their `file_path`.
- [ ] No skip was "fixed" by editing the vault from the session; the ones that deserve it go out as a human PR on the vault's repository.
- [ ] `mem_runbook_find` verified with a known symptom; `id`, `stale` and `age_days` checked against the document's frontmatter.
- [ ] `mem_project_card` run AFTER; the count changed as expected, or the reason it did not is explained.
- [ ] `mem_save` (`type: discovery`, `topic_key: knowledge-mcp/runbooks/index`) if any skip revealed a vault defect.
