# Using the tools

Contract `2.0.0`, published with `cloudfrog-knowledge` 0.6.0 and unchanged since. The catalog depends on the profile and on the `features` of the served vaults ([Profiles and `features`](../configuration.md#profiles-and-features)). The generated inventory of every tool, parameter and description is [`docs/tools/inventory.md`](inventory.md); the contract history is [`docs/contract/CHANGELOG.md`](../contract/CHANGELOG.md).

| Tool | Description |
|---|---|
| `get_document` | Reads a document: `chunk_id` (from a `search` row, without `file_path`) reads the block of one heading; also `section`, `start_line`/`end_line` and `max_chars` (3,000 by default; longer text comes back with `truncated: true` and `next_offset`). `metadata` carries `type`, `status`, `service`, `owner`, `tags` and `last_updated`; `full_metadata=True` returns the whole frontmatter. |
| `search` | Hybrid (default), `semantic` or `keyword` search, `limit` 1-50. Each row carries `file_path`, `score`, `chunk_id` and `section`, the pointer to the block to read; `excerpt` with `fields`, `snippet` with `snippet=True`. `rerank='on'\|'off'` with a vault judge. |
| `search_by_metadata` | Filters by frontmatter (`type`, `service`, `tag`, `status`). Answers `{total, offset, items}`, 20 rows per page (`limit` 1-200, `offset`). |
| `search_deep` | `search` plus multi-hop over the wiki-link graph from the candidates. |
| `find_path` | Shortest path between two documents (BFS, `max_depth=4`). |
| `get_related` | Direct neighbors in the graph, one row per document with `direction` `outgoing`, `incoming` or `both` and `weight` to 3 decimals; `limit` 1-200. |
| `get_impact` | Blast radius of a change through transitive backlinks; 20 paths per level (`max_items`, 0-1000) and `omitted` with the ones left out. |
| `reindex` | Reindexes the **vault**. Detects changes by SHA-256 and reprocesses only what changed. Runs `git pull` only with `pull=true` (the webhook pulls on every push). Only `target=vault` is accepted ([ADR-039](../adr/ADR-039.md) retired `code` and `all`). Admin scope over HTTP. |
| `context_pack` | Task context in one call (`budget_tokens`). Reads the memory server only with `include_memories=true` (default `false`), `MEMORY_HTTP_TOKEN` and a loopback `KNOWLEDGE_MEMORY_URL`; otherwise it answers with the vault and `degraded`. |
| `list_vaults` | The served vaults, the default and the contract version. Takes no `vault` and opens no index. |
| `get_flow` / `compare_flows` | Extension `flows`. Auth flow per provider; a deduplicated comparison with `shared_login_steps` and `files`. A provider without a flow document answers `E_FLOW_NOT_FOUND`, naming every missing one. |
| `get_stale_docs` | Extension `governance`. Review queue with its owner. Each row has a `reason`: `overdue` (with `age_days`) or `last_updated_invalid` (without `age_days`, since there is no age to compute). Filter with `owner="plat"`, or pass `include_unreadable=false` to see only overdue documents. Excludes what the `index.exclude` of the `vault.yaml` covers. |
| `suggest_links` | Extension `governance`. Proposes missing wiki-links between documents that share neighbors. |
| `sync_confluence` | Extension `confluence`. Reports which files under `scope_prefix` have a newer Confluence page. Read-only: `dry_run=False` answers `E_WRITE_FENCED` ([ADR-044](../adr/ADR-044.md)); without credentials `E_SECRET_MISSING`, and a scope with no tracked files `E_SCOPE_EMPTY`. Export happens with `cloudfrog-knowledge confluence export`, outside the vault, for a PR ([`docs/runbooks/promote-confluence.md`](../runbooks/promote-confluence.md)). Admin scope over HTTP. |
| `sync_jira` | Extension `jira`, over stdio only. Copies the Jira issues of the vault's projects as memory tasks (upsert by `jira_key`); `dry_run=True` by default (it only reads Jira). Requires the `admin` scope. See [`docs/runbooks/sync-jira.md`](../runbooks/sync-jira.md). |
| `search_code` | **Retired** from the `all` profile in 2.0.0 ([ADR-039](../adr/ADR-039.md), [ADR-046](../adr/ADR-046.md)): it answers `E_TOOL_RETIRED` with a redirect to `graphify explain <symbol>` / `graphify affected <file>` for relations and impact, `graphify query <symbol>` only with a real symbol name, `rg` for literal text and `search`/`search_deep` for intent. Only the `legacy` profile registers it, for one more release. |

Code understanding lives in `graphify`, through its own CLI against a local `graphify-out/`; this server never opens or parses that directory.

## Which tool to use

| I want to... | Use | Example |
|---|---|---|
| Find something by topic | `search` | `search "tenant provisioning"` |
| The same, but the topic is spread across linked documents | `search_deep` | `search_deep "billing a subscription" depth=2` |
| Read a document without burning tokens | `get_document` | `get_document "Runbooks/Troubleshooting.md" section="API calls fail"` |
| See how two things connect | `find_path` | `find_path "Modules/Billing/Billing.md" "Modules/Provisioning/Provisioning.md"` |
| Know what breaks if I change something | `get_impact` | `get_impact "Architecture/Data Model Overview.md"` |
| See a document's neighbors | `get_related` | `get_related "Modules/Billing/Billing.md"` |
| Filter by type or service | `search_by_metadata` | `search_by_metadata type="runbook"` |
| Find relations or impact of a real code symbol | `graphify explain` / `graphify affected` (`search_code`, retired in 2.0.0, redirects here) | `graphify explain "verifyStorageAccess"` |
| Find literal text in code, templates or QML | `rg` | `rg "verifyStorageAccess"` |
| Compare auth flows between providers | `compare_flows` | `compare_flows "<provider-a>" "<provider-b>"` |

Tips that save tokens and frustration:

- Read the `chunk_id` of a `search` row, or a `section="..."`, instead of raising `max_chars`: a block is a fraction of a large document.
- If `search` returns something that almost fits, do not repeat the search with other words: try `search_deep`, which follows wiki-links from the candidates.
- If the vault declares a judge (`rerank` in its `vault.yaml`), `search(query, rerank="on")` reorders the results with it: each row the judge read carries `rerank_score` (the probability that its block answers the query), and `chunk_id`/`section` point to that block. With `rerank.default: on` it happens without asking, and `rerank="off"` skips it. A judge that is down never breaks search: it answers without rerank and `_meta["knowledge/rerank"].reason` says why. Judge configuration: [`docs/install.md`](../install.md), "`search` judge (rerank)".
