---
name: create-runbook
description: Drafts a new runbook from the symptoms and known executions of a ticket, with validation before opening a PR.
when_to_use: when triage raises a ticket that is a known procedure case, when an operator needs to document a solution after resolving an incident, or when `mem_runbook_find` returns a runbook whose procedure went stale after infrastructure changes.
allowed-tools: Read, Edit, Bash(memory project *), Bash(.claude/skills/create-runbook/scripts/next-runbook-id.sh), Bash(.claude/skills/create-runbook/scripts/instantiate-runbook.sh), Bash(.claude/skills/create-runbook/scripts/validate-runbook.sh), mcp__mcp-atlassian__jira_get_issue, mcp__cloudfrog-knowledge__search, mcp__cloudfrog-knowledge__search_by_metadata, mcp__plugin_memory_memory__mem_search, mcp__plugin_memory_memory__mem_save, AskUserQuestion, open-pr
disable-model-invocation: false
user-invocable: true
argument-hint: "<TICKET-KEY[,TICKET-KEY…]> <pattern>"
---

# create-runbook

Not to be confused with `design-runbook`, the skill that some service context
packages ship: that one documents directly in `cloudfrog-knowledge`, with no
staging or PR, the diagnosis that `resolve-ticket` already resolved. This one drafts
the vault's curated runbook and does require staging, `validate-runbook.sh` and a
human PR (D-11).

Draft a new runbook in ≤ 15 minutes with human confirmation of the diagnosis.

## When NOT to run this

- If the symptom is already documented in `mem_runbook_find`, this skill is not the way to renew it: the vault is edited through a human PR (D-11). Use `open-pr` with the specific vault file if you need changes.
- If the ticket is a one-off incident with no repeatable pattern, it is not a runbook: it is a historical record of the resolution in memory (`mem_save` with `type: incident`).
- If the pattern is not yet in the template's enum (`missing-files`, `auth-access`, `file-save-failure`, `sync-upload`, `registration-subscription`, `other`), pick the closest one or declare `other` and document which pattern is needed.

## Steps

### 1. Input and literal symptoms (minute 0–2)

1. Read the ticket with `jira_get_issue(KEY, comment_limit=20)` and extract: reported symptoms, affected country, partner (if applicable), `issue_type`.
2. Note the KEY for later use (`jira_refs`).

### 2. Known root cause and previous executions (minute 2–4)

1. Search memory: `mem_search(incident/<KEY>)` to see whether there is a previous incident with the same symptom.
2. Search the knowledge server: `search(query=<ticket symptoms>)` to find runbooks by symptom, and `search_by_metadata(type="runbook")` for the full list (it answers `{total, offset, items}`: read `items` and ask for the next page with `offset` while `offset + len(items) < total`).
3. If you find a candidate runbook, cross-check it: does the procedure still work? Are the resolution steps still valid? If not, delegate to `open-pr` to update the existing one instead of creating a new one.

### 3. Reserve an id (minute 4–5)

1. Run `.claude/skills/create-runbook/scripts/next-runbook-id.sh` (no arguments).
2. Keep the `RB-NNN` it returns.

### 4. Drafting in staging (minute 5–9) — BOUNDARY WITH THE VAULT

**HERE IS WHERE THE CONFLICT WITH `guard-vault-write.sh` HAPPENS:**

The `guard-vault-write.sh` hook (PreToolUse on Write|Edit) blocks any write under the vault clones (`~/Projects/Vaults/<slug>/`, or `KNOWLEDGE_VAULTS_ROOT`) except `90 - Memory/`. The reason: the vault is content curated by people, not by agents (ADR D-11), and it lives in its own repository, not in this one. Section 6 of plantilla-runbook.md says to write directly under the vault's `Runbooks/`, but that destination is forbidden by the hook.

**Resolution:** The document is drafted in a **staging path**, OUTSIDE the vault:

```
$MEMORY_PROJECT_STAGING/create-runbook/RB-NNN <Title>.md
```

(By default: `$HOME/.local/share/knowledge/staging/create-runbook/`)

1. Run `.claude/skills/create-runbook/scripts/instantiate-runbook.sh --id <RB-NNN> --pattern <pattern> --out-dir "$MEMORY_PROJECT_STAGING/create-runbook" --title "<title>" --jira-refs <KEY> [--fixture <ticket.json>]`. The script reads the `pattern` defaults from `create-runbook/patterns/<pattern>.md` (category, service, `tools[]`, `evidence_required[]` and the `knowledge_ref` to the per-category template — ADR-032 point 3), extracts the literal skeleton from `ai-platform-plan/05-runbooks/plantilla-runbook.md` section 3 and replaces every frontmatter variable with a real value (it never leaves an unresolved `{{...}}` there: it would not be valid YAML). The symptoms, country and Jira references come from the ticket read in step 1 (or from the fixture, when rehearsing).
2. Add by hand the diagnosis steps (`{{quick_step}}`, `{{mutating_step}}`, `{{validation_command}}`, ...) and the confirmed root cause: those BODY variables are left unresolved on purpose — they need the human diagnosis of the next step, and `validate-runbook.sh` check 9 expects them there.
3. The file is born in staging with `status: draft`. It does not touch the vault.
4. Note: The document will go to the vault when a person runs the apply scripts (next step in the operational flow, not in this skill).

### 5. Human confirmation of the diagnosis and the steps (minute 9–11)

1. Present the draft document to the person with `AskUserQuestion`:
   - "Do the diagnosis and the steps match what resolved the incident?"
   - "Is any step missing? Is any incorrect or too specific?"
   - "Is the procedure still valid in the current code/infra?"
2. Incorporate adjustments if there are any. Save to staging again.

### 6. Validation (minute 11–12)

1. Run `.claude/skills/create-runbook/scripts/validate-runbook.sh <staging-file>`.
2. If exit 0, continue. If not, report the errors to the person and repeat the adjustments.
3. The validator's checks include: complete YAML frontmatter with enums, unique id in the vault, symptoms and references, known tools, sections in order, mermaid blocks, [GATE] steps with rollback, no PII, language, etc.

### 7. Index and PR preparation (minute 12–14)

1. Add a row to the table of the vault's `Runbooks.md` (the index). Do not edit the vault yet; prepare the row in staging.
2. If the `pattern` is new (it does not appear in the vault's `Issue Patterns from Jira.md`), update that document as well in staging.
3. Write the PR body with the title `docs(runbooks): add RB-NNN <slug>` and the changelog.
4. **Delegate the rest to `open-pr`**: that skill handles the PR against `main` of `knowledge` on the branch `docs/rb-nnn-<slug>`. It opens it, assigns a reviewer and notifies.
5. Note: Merging the PR is a manual, human step.

### 8. Applying the document to the vault (manual step, after the PR)

**This skill does NOT run this part.** It is described here for completeness:

When the PR is approved and merged, a person runs (outside the agent):

```bash
cd ai-architecture/ai-platform-plan/05-runbooks/migracion/
bash scripts/aplicar-migracion.sh --apply
```

(Or using the specific pattern in `create-runbook/staging/` if there are new documents ready.)

This script:
- Validates that the staging document passes all the checks.
- Copies the staging document to `Runbooks/<Category>/RB-NNN <Title>.md` in the clone of the vault repository (`MEMORY_VAULT_ROOT`).
- Updates the index in the vault.
- Does not commit or open another PR: it leaves the changes in the working tree.

Then the person runs `git add`, `git commit`, `git push` manually.

### 9. Memory (minute 14–15)

1. Run `mem_save` with:
   - `type: architecture`
   - `topic_key: runbook/RB-NNN`
   - `project: <service>` (the `service` from the frontmatter)
   - Content: symptoms, root cause, Jira refs, creation date, name of whoever drafted it.

## Tools and how they are used

| Tool | Step | Notes |
|---|---|---|
| `jira_get_issue` | 1 | Reads the ticket with `comment_limit: 20` for context and previous decisions |
| `mem_search`, `search`, `search_by_metadata` | 2 | Searches existing runbooks; does not write |
| `next-runbook-id.sh` | 3 | Local script; scans the vault and returns a free `RB-NNN` (bash 3.2 compatible) |
| `instantiate-runbook.sh` | 4 | Local script; instantiates the template with the defaults of `patterns/<pattern>.md` in `$MEMORY_PROJECT_STAGING/create-runbook/`, **never in the vault** (it rejects with exit 3 if `--out-dir` resolves inside it) |
| `Edit` (staging) | 4 | Fills in by hand the diagnosis variables that the script leaves pending in the body |
| `AskUserQuestion` | 5 | Obtains confirmation of the diagnosis |
| `validate-runbook.sh` | 6 | Local script; 12 checks against the template; exit 0 or 1 |
| `open-pr` | 7 | Delegates; opens the PR in the vault repository, with the owner as reviewer |
| `mem_save` | 9 | Records in memory under `runbook/RB-NNN` |

## The `guard-vault-write.sh` hook and why it exists

```bash
case "$path" in
  "$root"/*/90\ -\ Memory/*) exit 0 ;;
  "$root"/*)
    echo "guard-vault-write: ... curated vault content; ... only '90 - Memory/' ... (D-11)" >&2
    exit 2 ;;
esac
```

**Why:** Each vault (a repository of its own, cloned under `~/Projects/Vaults/<slug>/`) is a corpus curated by people, not by agents. The only exception is `90 - Memory/`, where the automatic memory bridge writes. Any other path requires a reviewed human PR. See ADR-011 (D-11).

**Implication for this skill:** All runbooks, indexes and patterns go to staging and are applied through a human PR. The hook is not a problem to "get around" or "bypass": it is a curation quality policy. This skill respects it.

## Staging path

By default, `$HOME/.local/share/knowledge/staging/create-runbook/`. It is controlled with `MEMORY_PROJECT_STAGING`.

```bash
mkdir -p "$MEMORY_PROJECT_STAGING/create-runbook"
echo "# RB-001: ..." > "$MEMORY_PROJECT_STAGING/create-runbook/RB-001 Title.md"
```

## Chosen pattern for Runbooks (see migracion/README.md)

This skill follows the pattern consolidated in `ai-platform-plan/05-runbooks/migracion/`:

1. **Drafting in staging.** The agent prepares documents outside the vault.
2. **Full validation.** The scripts (`next-runbook-id.sh`, `validate-runbook.sh`) run before any application.
3. **Human confirmation.** A person reviews and runs `aplicar-migracion.sh --apply`.
4. **Audited PR.** The final chain (push + PR) remains a human change.

There is no shortcut: it is the price of a curated vault.
