# Using a team deployment

## Connect

Plugin installation for Claude Code (the `cloudfroghq` marketplace, one line per profile) is in [`docs/install.md`](install.md). What follows is the manual registration.

Ask whoever administers the stack for two credentials, over a secure channel and never through git: the knowledge server's bearer (`MCP_API_KEY`) and the memory cloud token (`MEMORY_CLOUD_TOKEN`).

They are two pieces with different roles:

- **cloudfrog-knowledge** (remote MCP, HTTP): the project's curated knowledge. You connect to the central server; you install nothing.
- **memory** (local binary plus stdio MCP): your agent's episodic memory (decisions, bugs, session context). It is local-first: it lives on your machine and is **replicated** to the shared cloud for backup and common knowledge. The remote container is not an MCP server you connect to.

Register the knowledge server (one line):

```bash
claude mcp add -s user -t http cloudfrog-knowledge https://<deployment>/mcp -H "Authorization: Bearer <token>"
```

`<token>` is the deployment's read-only `MCP_API_KEY`; a deployment that serves HTTPS encrypts the bearer. `reindex` and `sync_confluence` require the `admin` scope, which `MCP_ADMIN_KEY` grants. Each deployment's URL and how to request its bearer are documented in its vault, under `Deploy/`.

Install the `memory` binary and its Claude Code setup as described in [`docs/install.md`](install.md) (Homebrew or a checksummed release download); `memory setup claude-code` registers the stdio MCP server and the session hooks, and `memory doctor` verifies them. Do not use `gentleman-programming/tap/engram`: it is the tap of the **original** project this memory fork comes from, and it silently installs that project without the backfill fix or this team's cloud configuration.

Connect your memory to the team cloud:

```bash
memory cloud config --server https://<team-memory-cloud>
memory cloud enroll knowledge
MEMORY_CLOUD_TOKEN=<token> memory sync --cloud --project knowledge
```

The sync is bidirectional: it pushes your memories and pulls your teammates' (check `Pending import` in `memory sync --cloud --status --project knowledge`). Repeat it whenever you want to share or receive, or automate it with a weekly cron. Shared-memory conventions (see `docs/TEAM-USAGE.md` in the memory repository, not in this one):

- `scope: project` is knowledge useful to the team (decisions, bugfixes, gotchas). `scope: personal` is **not** excluded from the sync: if something is truly personal, save it under another project name that is not enrolled.
- Save project memories in one common language: memory search is lexical (FTS5) and does not cross languages.

Restart Claude Code and you are done.

## Edit and publish the vault

The vault does not live in this repository: each organization has its own, in its own repository. Clone the one you are going to edit:

```bash
git clone git@github.com:<org>/vault.git ~/vaults/<slug>
```

Open it in Obsidian, edit, and propose the change by PR with the vault owner as reviewer (D-11). When the PR lands on `main`, that repository's push webhook notifies the server, which pulls the changes and reindexes only what changed. A one-file commit costs about $0.0003 in embeddings, so edit without fear.

Vault maintenance tools, if you curate content: `get_stale_docs` (review queue), `suggest_links` (missing wiki-links), `sync_confluence` (which files have a newer Confluence page; it needs `CONFLUENCE_EMAIL` and `CONFLUENCE_TOKEN` or the names in `integrations.confluence.secrets`), `reindex` (forces a reindex) and `sync_jira` (over stdio, for a vault that declares `jira`). See [Tools](tools/usage.md).

## If something fails

- **The tools do not appear in Claude Code.** Restart Claude Code after registering the server. Check the registration with `claude mcp get cloudfrog-knowledge`.
- **You get 401.** The bearer you registered does not match the server's `MCP_API_KEY`. Check the header with `claude mcp get cloudfrog-knowledge` and ask the administrator for the right key.
- **Responses carry too much text.** Use `get_document(chunk_id=...)`, `section="..."` or `max_chars=500`.
- **You edited the vault and it does not show up in searches.** Check that the push arrived, then the index state with `curl -s https://<deployment>/health`: `last_indexed_at` must be later than your push. If `indexing` is `true`, it is working: wait and look again.
