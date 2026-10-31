# Sync Jira with memory tasks

Runbook for whoever wants to see a project's Jira issues in `mem_task_list` of their local memory. The `jira` extension reads the issues of the projects the vault declares and upserts one memory task per issue, with `jira_key` as the key. It does not write to Jira or to the vault: it reads Jira, writes to memory, and stores its cursor next to the vault's index.

It runs only over stdio. An HTTP deployment does not load it (`W_EXTENSION_UNAVAILABLE_HTTP`): the memory it writes to listens on the person's loopback.

## Scope

Only a vault that declares `integrations.jira` syncs Jira; `jira sync --auto` and `sync_jira` ignore the others. To enable it on a vault, add the block from step 1 in a PR of that vault and export `<SLUG>_JIRA_EMAIL` and `<SLUG>_JIRA_TOKEN` (with the vault's slug in uppercase as the prefix) in your session environment; no value is stored in the repo.

## 1. Declare it in the vault

Through a vault PR, with the owner as reviewer (D-11):

```yaml
features: [jira, memory-bridge]
memory:
  project: acme
integrations:
  jira:
    site: https://acme.atlassian.net
    projects: [ACME]
    jql_extra: ""                      # optional; appended as AND (...)
    kind_map: { default: feature }     # optional; see the table below
    status_map: {}                     # optional; rarely needed
    sync: { throttle_minutes: 60 }     # minutes between session-hook runs
    secrets: { email_env: ACME_JIRA_EMAIL, token_env: ACME_JIRA_TOKEN }
```

Rules checked when `vault.yaml` is loaded (`E_MANIFEST_INVALID` with the key path):

| Key | Rule |
|---|---|
| `site` | `https` on `*.atlassian.net` or on a host the operator lists in `KNOWLEDGE_INTEGRATION_SITES`, with no user or password in the URL; `http` only on `127.0.0.1`, `localhost` or `::1` |
| `projects` | List of project keys (`ACME`, `OPS`) |
| `secrets.email_env`, `secrets.token_env` | Uppercase variable names, with `JIRA` as a segment, ending in `_EMAIL` and `_TOKEN` and with the vault's `slug` as prefix (`ACME_JIRA_EMAIL`); without a prefix (`JIRA_API_TOKEN`) only the default vault, and never another vault's variable. Never a value: one shaped like a credential is `E_SECRET_IN_MANIFEST` |
| `kind_map` | Issue type → `feature`, `bugfix`, `refactor`, `incident`, `migration` or `spike`; the `default` key covers the types it does not name |
| `status_map` | Jira status → `open`, `analysis`, `in_progress`, `review`, `verified`, `done`, `blocked` or `cancelled` |
| `sync.throttle_minutes` | Integer, 0 or more |
| `target` | `memory` only |

`site`, `projects` and the two `secrets` names are required when the sync runs, not at load time: the `memory-bridge` card reads `projects` on its own. An HTTP server does not load `jira`, so an invalid value in `projects`, `jql_extra`, `kind_map`, `status_map`, `sync` or `target` only yields `W_EXTENSION_UNAVAILABLE_HTTP` there; `site` and `secrets` stop startup over any transport.

## 2. What travels to memory

One task per issue, in series, to `POST /projects/<memory.project>/tasks`:

| Field | Where it comes from |
|---|---|
| `jira_key` | The issue key (`ACME-1234`); a key of any other shape is skipped as `invalid_key` |
| `title` | The summary, up to 300 characters; if empty, the key |
| `kind` | `kind_map[type]`, otherwise the per-type default (`Bug` → `bugfix`, `Incident` → `incident`, `Spike` → `spike`), otherwise `kind_map.default`, otherwise `feature` |
| `jira_status` | The status name, as Jira gives it |
| `jira_status_category` | `new`, `indeterminate` or `done`, as Jira gives it. If Jira gives anything else, it does not travel and the status goes into `status_unmapped` |
| `assignee` | The assignee's display name, if any |
| `state` | Only if `status_map` names the status |

memory derives `state` from `jira_status` with its own map and, if it does not know the literal, from `jira_status_category`. That is why `status_map` is rarely needed: it serves for an organization-specific status that memory does not know.

## 3. Credentials and memory

```bash
# fish: export these in your shell configuration, never in a file of the repo
set -gx ACME_JIRA_EMAIL your.email@company.com
set -gx ACME_JIRA_TOKEN <API token from id.atlassian.com>
```

The write uses memory's bearer (`MEMORY_HTTP_TOKEN`). The local `memory serve` currently runs without a token, so `KNOWLEDGE_MEMORY_REQUIRE_TOKEN=0` applies to it (see [`docs/install.md`](../install.md), the memory-bridge section). A `--dry-run` needs neither.

Create the project card before the first write, so it carries the correct `jira_project`:

```bash
KNOWLEDGE_MEMORY_REQUIRE_TOKEN=0 uv run cloudfrog-knowledge memory-bridge sync --vault acme --card
```

## 4. Run it

```bash
uv run cloudfrog-knowledge doctor | jq '.vaults[] | select(.slug=="acme") | .jira'
uv run cloudfrog-knowledge jira sync --vault acme --dry-run --since -30d --json | jq '{fetched, skipped, status_unmapped}'
KNOWLEDGE_MEMORY_REQUIRE_TOKEN=0 uv run cloudfrog-knowledge jira sync --vault acme --json | jq '{fetched, upserted, created, skipped}'
curl -s "http://127.0.0.1:6366/projects/acme/tasks?limit=5" | jq '.total, [.items[].jira_key]'
```

A second run gives `created: 0`: the upsert is idempotent.

From an MCP client over stdio, `sync_jira(vault="acme")` does the same as `--dry-run` and `sync_jira(vault="acme", dry_run=false)` writes. The tool appears only when some served vault declares `jira`, at the end of the catalog.

With the `knowledge` plugin, the session hook launches `cloudfrog-knowledge jira sync --auto` in the background when `doctor` passes: it syncs every vault that declares `jira`, has its variables exported and has already met its `throttle_minutes`. It does not block the session or print anything. `KNOWLEDGE_JIRA_AUTOSYNC=0` turns it off.

## 5. Since when

- `--since` (or `since=` in the tool): an ISO date (`2026-09-01`), a date and time (`2026-09-01T12:00:00Z`) or `-Nd` (`-7d`). It always wins.
- Without it, after a complete run: the cursor minus one day of overlap; repeating an issue costs nothing.
- Without it, after a truncated run: the exact cursor, with no overlap, so that a tail of more than 2,000 issues always advances.
- Without a cursor: 30 days.

JQL reads dates in the Jira account's time zone, so the date that goes into `jql` is written in that zone (the one `GET /rest/api/3/myself` returns, for example `2026-10-05 14:45` for `17:45` UTC in Buenos Aires), while `since` and `next_since` stay in UTC. If Jira does not report the zone, the date is written in UTC-12: the query starts earlier than necessary and never later.

The cursor (`next_since`) is the `updated` of the last issue read, in UTC, and lives in `jira_sync.json` next to the vault's `index.sqlite`, with `truncated`, `retry`, `synced_at`, `last_attempt_at` and `last_result`. It changes only after a run that wrote and in which both Jira and memory responded. A run of more than 2,000 issues (or more than `--max-issues N` on the CLI) ends with `truncated: true` and the next one continues from its `next_since`; the search orders by `updated` and then by key, and JQL compares minutes, so the next run re-reads the issues of the minute where it was cut off (the upsert is idempotent). If all the issues that fit in the run share that minute, the cursor cannot advance and the run responds `E_JIRA_SYNC_STALLED` without writing anything.

The cursor never leaves behind an issue that did not reach memory:

- A task that memory rejects because of its content (`invalid_enum`, `task_key_conflict`) stays in `retry`, and the next run also reads it by key (`GET /rest/api/3/issue/<key>`); it leaves the list when memory accepts it or when Jira no longer has it (404). The list holds up to 50 keys; if memory rejects more, the cursor stays at the `updated` of the oldest one that did not fit.
- A `--since` later than the stored cursor reads from there but leaves the cursor where it was: the issues between the two remain pending for the next run without `--since`.

## 6. Report

```json
{
  "vault": "acme", "memory_project": "acme", "dry_run": false,
  "jql": "project in (ACME) AND updated >= \"2026-09-12 15:00\" ORDER BY updated ASC, key ASC",
  "since": "2026-09-12T15:00:00Z", "next_since": "2026-10-12T14:58:00Z", "truncated": false,
  "fetched": 126, "retried": 0, "upserted": 125, "created": 125,
  "skipped": [{"key": "ACME-77", "reason": "task_key_conflict"}],
  "status_unmapped": []
}
```

`retried` counts the issues the run re-read by key because memory rejected them in the last run. `skipped.reason` is `invalid_key`, memory's own code (`task_key_conflict`: the key already belongs to a task of another project; `invalid_enum`; `missing_field`) or `memory_unavailable`. If memory stops responding midway through the run, the tasks that were left go into `skipped`, the report carries `error` with its code, and the cursor does not advance.

## 7. Codes

| Code | What to do |
|---|---|
| `E_FEATURE_DISABLED` | The vault does not declare `jira`: add it to `features` through a vault PR |
| `E_MEMORY_PROJECT_MISSING` | The vault does not declare `memory.project` |
| `E_MANIFEST_INVALID` | `site`, `projects` or a `secrets` name is missing, or a value does not meet the table in section 1 |
| `E_SECRET_MISSING` | The variables named by `secrets` are empty; the message says which |
| `E_MEMORY_TOKEN_MISSING` | A write without `MEMORY_HTTP_TOKEN`: export it, or use `KNOWLEDGE_MEMORY_REQUIRE_TOKEN=0` for the local memory without a token |
| `E_MEMORY_URL_NOT_LOOPBACK` | `KNOWLEDGE_MEMORY_URL` is not `http` on loopback |
| `E_JIRA_AUTH_FAILED` | Jira rejected the email and token (401 or 403) |
| `E_JIRA_UNAVAILABLE` | Jira did not respond with issues: a 400 (check `projects` and `jql_extra`), a 5xx, more than 10 s, or a repeated 429 |
| `E_JIRA_SYNC_STALLED` | All the issues that fit in the run were updated in the minute where it starts, and the cursor cannot get past it: raise `--max-issues` (up to 2,000), narrow `jql_extra` or pass a `--since` later than that minute |
| `E_ARGUMENT_OUT_OF_RANGE` | `since` is not understood or is in the future |
| `E_SCOPE_REQUIRED` | The call arrived with a bearer that lacks `admin` |

`jira sync` exits with 0 if it handled all the issues, 1 with a Jira error (`E_JIRA_*`) or memory going down midway, and 2 if it could not attempt anything. `--auto` always exits with 0, whatever fails. No message, report or log line carries the token, the email or the body that Jira or memory responded with.
