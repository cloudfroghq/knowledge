# Checklist — create-runbook

- [ ] `jira_get_issue(KEY, comment_limit=20)` run; symptoms, country, partner and `issue_type` extracted.
- [ ] `mem_search(incident/<KEY>)`, `search(query=<symptoms>)` and `search_by_metadata(type="runbook")` run without finding an existing runbook that fits; if there is one, verified that the procedure is still valid.
- [ ] `next-runbook-id.sh` run; `RB-NNN` noted and not duplicated in the vault.
- [ ] `instantiate-runbook.sh --id RB-NNN --pattern <pattern> --out-dir "$MEMORY_PROJECT_STAGING/create-runbook"` run; document at `RB-NNN <Title>.md` (OUTSIDE the vault); complete frontmatter with `status: draft`, symptoms and references, root cause verified against the ticket, resolution steps valid today, no PII.
- [ ] Human confirmation obtained with `AskUserQuestion`; adjustments incorporated if there were any.
- [ ] `validate-runbook.sh <staging-file>` exit 0; all 12 checks passed.
- [ ] Index (`Runbooks.md`) updated in staging with the new row if applicable.
- [ ] `Issue Patterns from Jira.md` updated in staging if the `pattern` is new.
- [ ] Delegation to `open-pr` completed; PR opened against `main` of `knowledge` on branch `docs/rb-nnn-<slug>`, with title `docs(runbooks): add RB-NNN <slug>` and a coherent description.
- [ ] `mem_save` (`type: architecture`, `topic_key: runbook/RB-NNN`) run with symptoms, root cause, Jira refs, service and owner.

**Manual step afterwards (NOT in this skill):**

- [ ] PR reviewed and merged into `main` of `knowledge` (human decision).
- [ ] Person runs `bash ai-architecture/ai-platform-plan/05-runbooks/migracion/scripts/aplicar-migracion.sh --apply` to copy the staging document into the vault.
- [ ] Changes in the vault confirmed and pushed (without opening another PR from the agent).
