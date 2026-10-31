# cloudfrog-knowledge

[![ring 1](https://img.shields.io/github/actions/workflow/status/cloudfroghq/knowledge/ring1.yml?branch=main&label=ring%201)](https://github.com/cloudfroghq/knowledge/actions/workflows/ring1.yml)
[![PyPI](https://img.shields.io/pypi/v/cloudfrog-knowledge)](https://pypi.org/project/cloudfrog-knowledge/)
[![npm](https://img.shields.io/npm/v/@cloudfrog/knowledge)](https://www.npmjs.com/package/@cloudfrog/knowledge)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue)](.python-version)
[![License: PolyForm Shield 1.0.0](https://img.shields.io/badge/license-PolyForm%20Shield%201.0.0-orange)](LICENSE)

> A project's curated knowledge (flows, runbooks, endpoints, business rules) answerable from Claude Code or any MCP client.
> You edit the Obsidian vault, push, and the server reindexes itself.

`cloudfrog-knowledge` is an [MCP](https://modelcontextprotocol.io) server that serves one or more Obsidian vaults through hybrid search, wiki-link graph navigation and structured metadata queries. It is a reusable knowledge base: no project is wired into the code, and the vaults it serves are decided by configuration (`KNOWLEDGE_VAULTS` or `VAULT_PATH`). It runs locally over stdio, or on a server where it is exposed to a team over HTTP with bearer authentication. It is meant for teams that keep architecture notes, flows, runbooks and API contracts in a vault, and want their coding agents to read the right section instead of re-deriving it from code.

The source is available under the [PolyForm Shield License 1.0.0](#license). That is a source-available license, not an OSI-approved open-source license.

**What do you need?**

| If you are... | Go to |
|---|---|
| Trying it out on your machine | [Quick start](#quick-start-offline-about-5-minutes) |
| On a team that queries and edits a deployed vault | [Using a team deployment](#using-a-team-deployment) |
| Administering a deployment | [Operating the server](#operating-the-server) |
| Changing the code of this repository | [CONTRIBUTING.md](CONTRIBUTING.md) and [Tests and merge gates (ring 1)](#tests-and-merge-gates-ring-1) |

## Contents

- [Features](#features)
- [How it works](#how-it-works)
- [Quick start (offline, about 5 minutes)](#quick-start-offline-about-5-minutes)
- [Run over HTTP](#run-over-http)
- [Installation](#installation)
- [Configuration](#configuration)
- [Tools](#tools)
- [CLI reference](#cli-reference)
- [Using a team deployment](#using-a-team-deployment)
- [Operating the server](#operating-the-server)
- [Call telemetry](#call-telemetry)
- [Tests and merge gates (ring 1)](#tests-and-merge-gates-ring-1)
- [Scripts](#scripts)
- [Documentation](#documentation)
- [Contributing, security and support](#contributing-security-and-support)
- [License](#license)

## Features

- **Several vaults, one process.** A `vaults.yaml` registry serves any number of vaults with one default; every tool takes `vault=<slug>`, and each vault keeps its own index. A single directory works too (`VAULT_PATH`).
- **Hybrid search on SQLite.** One `index.sqlite` per vault: BM25 over FTS5 (`unicode61 remove_diacritics 2`) per chunk, plus exact KNN over float32 vectors with `sqlite-vec`, fused into one score. Without the extension the server falls back to the standard library with the same results.
- **Chunk pointers, not whole files.** Each `search` row carries `file_path`, `score`, `chunk_id` and `section`; `get_document(chunk_id=...)` reads only that block. Documents are cut into chunks of about 2,500 characters along `## ` sections with 10% overlap.
- **Graph tools.** A bidirectional wiki-link graph with learned (Hebbian) weights: `find_path`, `get_related`, `get_impact`, and `search_deep`, which follows wiki-links from the candidates.
- **Profiles and `features`.** The 10 core tools are always served; extension tools appear only when a served vault declares them (`flows`, `governance`, `confluence`, `jira`). The `legacy` profile keeps the 16 tool names of contract 1.4.0 for one release.
- **Write fence.** The server never writes inside a vault outside `90 - Memory/` ([ADR-044](docs/adr/ADR-044.md)); every tool is tested against a copy of the fixture that must stay byte-identical.
- **A frozen, versioned tool contract.** Contract `2.0.0` ([ADR-046](docs/adr/ADR-046.md)) is announced in `initialize`, `list_vaults` and `GET /health`. Each contract version has golden responses under `tests/golden/v<version>/`, frozen by a `SHA256SUMS` file. Every error is `isError` with a stable `E_` code, never a Python traceback.
- **Optional rerank judge.** A vault can declare a judge (`jev` from TypeSafe, or a local `kev`) that reorders `search` results, with per-query and per-day budgets, a cache and a circuit breaker. A judge that is down never breaks search.
- **Team operations.** HTTP with `read` and `admin` bearer scopes, a signed push webhook that pulls the vault and reindexes incrementally, a `/health` endpoint, per-tool call telemetry, and a `doctor` command.

## How it works

![Architecture of the knowledge MCP server](docs/architecture.png)

The diagram is a generated image so that it also renders where Mermaid does not (PyPI, mirrors, offline viewers); GitHub itself renders Mermaid. Its source is [`docs/architecture.mmd`](docs/architecture.mmd). If you change the diagram, edit that file and regenerate the PNG:

```bash
npx -y @mermaid-js/mermaid-cli -i docs/architecture.mmd -o docs/architecture.png -b white -s 2
```

The flow: the vault (a directory, usually a clone of its own git repository) is parsed into documents and chunks. Frontmatter becomes metadata, wiki-links become graph edges, and each chunk is written to the vault's `index.sqlite` with its FTS5 row and its embedding. Indexing is automatic: the first tool call that needs the index builds it, and later reindexes reprocess only files whose SHA-256 changed. A push to the vault's repository reaches `/webhook/reindex`, which brings the clone to the tip of the served branch and reindexes what changed.

Main components (35 flat modules in `src/knowledge`, in three layers that [`.importlinter`](.importlinter) enforces: core, ext and http; see [CONTRIBUTING.md](CONTRIBUTING.md#layered-architecture)):

- **`server.py`**: the FastMCP server, stdio or HTTP (`MCP_TRANSPORT`). Tools run off the event loop (the `_offloaded` decorator), so a slow search does not block the rest. It is the only module that imports extensions, the first time they are needed.
- **`auth.py`**: bearer middleware with constant-time comparison. `/health` stays open; `/webhook/reindex` validates the webhook HMAC or a manual bearer.
- **`webhook.py`**: validates the signature, updates the vault clone (`gitsync`) and triggers the reindex in the background. It also serves `/health`. The server registers `/webhook/reindex` only when the default vault declares a repository (`repo` or `VAULT_REMOTE`); `/health` is always there.
- **`extensions.py`**: the declarative registry of the extensions a vault turns on with `features` (`flows`, `governance`, `confluence`, `git-webhook`, `jira`, `memory-bridge`): the tools and routes each one adds, the module that implements it, its credential variables and the transports that load it. It names modules as text; only `server.py` imports them.
- **`flows.py`**: `get_flow` and `compare_flows` over the vault's flow documents, loaded on first use.
- **`jira_client.py`**: a read-only client for Jira Cloud's JQL search (`GET /rest/api/3/search/jql`, paginated with `nextPageToken`), with basic auth and no proxies or redirects.
- **`jira_sync.py`**: the `jira` extension: builds the JQL, turns each issue into a memory task and writes it with `memory_bridge.py`; keeps its cursor in `jira_sync.json`. Loaded the first time `sync_jira` or `jira sync` runs.
- **`indexer.py`**: frontmatter parsing, chunking by level-2 headings (`chunker.py`), embeddings, and an atomic per-file write to the vault's `index.sqlite` (`stores.py`: FTS5 and `sqlite-vec` vectors). Change detection by SHA-256.
- **`graph.py`**: the bidirectional wiki-link graph: `related`, `has_node`, `neighbors`, and BFS for `find_path` and `get_impact`.
- **`config.py`**: a compatibility facade over `settings.py`, the explicit configuration: environment variables, `.env`, each vault's `vault.yaml` and, if present, the project's `config.json` or the one `KNOWLEDGE_CONFIG_JSON` names.

## Quick start (offline, about 5 minutes)

This runs the server from a clone against a copy of the test vault in `tests/fixtures/vault/`. It needs no network after `uv sync` and no API key: the `bag-of-words` embeddings provider is deterministic and local.

### Prerequisites

- [`uv`](https://docs.astral.sh/uv/) (the only hard requirement). The project pins Python 3.12 (`.python-version`, and `requires-python = ">=3.12,<3.13"`). Python 3.13 is not supported; if your system Python is 3.13, let `uv` provision 3.12 (it does so automatically, or with `uv python install 3.12`).
- `git`, and `curl` plus `openssl` for the HTTP section.
- If your shell exports an empty `UV_NATIVE_TLS=`, unset it: every `uv` call fails with "expected a boolish value".

```bash
git clone https://github.com/cloudfroghq/knowledge.git
cd knowledge
uv sync    # creates .venv with Python 3.12 and the dev tools
```

### Serve the fixture vault over stdio

```bash
DEMO="$(mktemp -d)"
cp -r tests/fixtures/vault "$DEMO/vault"     # never point the server at a vault you care about to try it
export VAULT_PATH="$DEMO/vault" KNOWLEDGE_INDEX_DIR="$DEMO/index" KNOWLEDGE_EMBEDDINGS_PROVIDER=bag-of-words
{
  printf '%s\n' \
    '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"quickstart","version":"0"}}}' \
    '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
    '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' \
    '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"search","arguments":{"query":"middleware outage","limit":3}}}' \
    '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"get_document","arguments":{"chunk_id":"d9bc623bc4:1","max_chars":300}}}' \
    '{"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"list_vaults","arguments":{}}}'
  sleep 5
} | uv run cloudfrog-knowledge serve --transport stdio 2>/dev/null
```

The `sleep` keeps stdin open while the server answers. Without it the pipe closes right after the last request, and the server can shut down before it writes the replies. `2>/dev/null` hides the log; drop it to see the indexing lines.

### What you should see

Five JSON-RPC responses, one per line (the notification gets none), not necessarily in request order:

- `initialize`: `instructions` starts with `knowledge-contract: 2.0.0`. `serverInfo.version` is the version of the `mcp` SDK, not of this package.
- `tools/list`: the 10 core tools, in this order: `get_document`, `search`, `search_by_metadata`, `search_deep`, `find_path`, `get_related`, `get_impact`, `reindex`, `context_pack`, `list_vaults`.
- `search`: one text block per row, for example `{"file_path":"Runbooks/Runbook - Middleware Outage.md","score":1.06,"chunk_id":"d9bc623bc4:1","section":"Runbook - Middleware Outage"}`, then `Architecture/Auth Architecture.md` (with `needs_review` and `age_days`) and `Services/Middleware Service.md`. Scores can vary slightly between runs.
- `get_document`: that chunk only: `file_path`, `metadata` (`type: runbook`, `service: middleware`, ...), `content` starting with `# Runbook - Middleware Outage`, `chunk_id` and `section`. Chunk ids are stable for unchanged content, which is why the recipe can hard-code the one `search` returns.
- `list_vaults`: `{"contract":"2.0.0","default":"vault","vaults":[{"slug":"vault","name":"vault","default":true,"features":[]}]}`.

The first `tools/call` builds the index: the log says that no existing index was found and runs the initial indexing, then `Wrote 14 documents (14 chunks)`. `tools/list` and `list_vaults` do not trigger it. `$DEMO/index` then holds `index.sqlite`, `index_meta.json`, `link_graph.json` and `usage_stats.json`. The warning `W_REVIEW_THRESHOLDS_FALLBACK` in the log is harmless: the fixture declares no review thresholds.

Two more things to try:

```bash
uv run cloudfrog-knowledge doctor --brief                     # knowledge doctor: ok, contract 2.0.0, 1 vault(s), default vault
uv run cloudfrog-knowledge vault-lint "$DEMO/vault" --rules 1.2   # ok: true, rc 0
```

`vault-lint` with its default `1.3` rules exits with 1 on the fixture on purpose: the fixture reproduces hard cases (an invalid runbook `status`, an old-style `last_updated`, and so on).

Without `VAULT_PATH`, `KNOWLEDGE_VAULTS` or a user `vaults.yaml`, startup fails with `E_VAULT_PATH_MISSING` (exit code 2). `KNOWLEDGE_EMBEDDINGS_PROVIDER=none` also works, with lexical search only.

### Connect it to an MCP client

**Claude Code, with the plugin (recommended).** The `cloudfroghq` marketplace publishes `knowledge@cloudfroghq` (stdio over your local vaults) and `knowledge-remote@cloudfroghq` (a deployed HTTP server). Install steps, prerequisites (including the minimum Claude Code version) and the vault registry the plugin reads are in [`docs/install.md`](docs/install.md):

```bash
claude plugin marketplace add cloudfroghq/claude-plugins && claude plugin install knowledge@cloudfroghq --scope user
```

**Claude Code, by hand.** Register the published package over stdio:

```bash
claude mcp add -s user knowledge -e VAULT_PATH=/path/to/your/vault -- \
  uvx --managed-python --python 3.12 cloudfrog-knowledge serve --transport stdio
```

**Any MCP client (stdio).** The same server as a JSON entry; add `OPENAI_API_KEY` to `env` if you keep the default `openai` provider, or set `KNOWLEDGE_EMBEDDINGS_PROVIDER` to `none`:

```json
{
  "mcpServers": {
    "knowledge": {
      "type": "stdio",
      "command": "uvx",
      "args": ["--managed-python", "--python", "3.12", "cloudfrog-knowledge", "serve", "--transport", "stdio"],
      "env": {
        "VAULT_PATH": "/path/to/your/vault",
        "KNOWLEDGE_EMBEDDINGS_PROVIDER": "none"
      }
    }
  }
}
```

To run a local checkout instead of the published package, use `"command": "uv"` with `"args": ["run", "--directory", "/path/to/knowledge", "cloudfrog-knowledge", "serve", "--transport", "stdio"]`. `python -m knowledge.server` is still a valid entry point, but on its own it just waits for JSON-RPC on stdin.

The first start can take longer than the 30 s an MCP client waits by default (it downloads the package and, if needed, a managed Python). Run the command once by hand to warm the cache, or raise `MCP_TIMEOUT` in Claude Code.

## Run over HTTP

```bash
export VAULT_PATH="$DEMO/vault" KNOWLEDGE_INDEX_DIR="$DEMO/index" KNOWLEDGE_EMBEDDINGS_PROVIDER=bag-of-words
export MCP_HTTP_HOST=127.0.0.1 MCP_HTTP_PORT=8765
export MCP_API_KEY="$(openssl rand -hex 32)"
uv run cloudfrog-knowledge serve --transport http &      # about 5 s to come up

curl -sS http://127.0.0.1:8765/health                    # 200, no bearer needed

INIT='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"quickstart","version":"0"}}}'
curl -i -X POST http://127.0.0.1:8765/mcp -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' -d "$INIT"                       # 401 {"error":"unauthorized"}
curl -i -X POST http://127.0.0.1:8765/mcp -H "Authorization: Bearer $MCP_API_KEY" \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -d "$INIT"   # 200, mcp-session-id header
kill %1
```

- `MCP_HTTP_HOST` defaults to `0.0.0.0`, which binds every interface. Set `127.0.0.1` for anything local.
- `--transport http` is the same as `MCP_TRANSPORT=http` (the default is `stdio`). Port and path default to `8000` and `/mcp`.
- Over HTTP the server refuses to start without a bearer: `E_HTTP_API_KEY_MISSING` (exit 2) unless `MCP_API_KEY`, `MCP_ADMIN_KEY` or `bearers` in `vaults.yaml` is set.
- Responses are server-sent events (`event: message` / `data: {...}`). Every later request carries the `mcp-session-id` header from the `initialize` response, and the session starts with a `notifications/initialized` POST (202).
- `/health` before the first tool call shows `indexed: 0` and `index_loaded: false`; after a search, `indexed: 14` and `index_loaded: true`. `embedding_model` shows `text-embedding-3-large` even with `bag-of-words` or `none` (cosmetic).
- `/webhook/reindex` answers 404 here because the vault declares no repository.

## Installation

The package is published on PyPI (`cloudfrog-knowledge`), on npm (`@cloudfrog/knowledge`) and as a git tag, always with the same version ([ADR-050](docs/adr/ADR-050.md)). Every installation provides two commands that are the same program, `knowledge` and `cloudfrog-knowledge`.

| Channel | Command |
|---|---|
| PyPI, run without installing | `uvx --managed-python --python 3.12 cloudfrog-knowledge doctor` |
| PyPI, install the tool | `uv tool install --managed-python --python 3.12 cloudfrog-knowledge`, then `knowledge doctor` |
| npm | `npx @cloudfrog/knowledge doctor` (or `pnpm dlx @cloudfrog/knowledge doctor`, or `npm install -g @cloudfrog/knowledge`) |
| Git tag | `uvx --managed-python --python 3.12 --from git+https://github.com/cloudfroghq/knowledge.git@vX.Y.Z cloudfrog-knowledge doctor` |
| Claude Code plugin | `knowledge@cloudfroghq` or `knowledge-remote@cloudfroghq`, see [`docs/install.md`](docs/install.md) |
| Docker / Compose | `infra/compose.yml`, see below |

- `vX.Y.Z` is a release tag; take the latest one from the [releases](https://github.com/cloudfroghq/knowledge/releases) or the PyPI badge above. Pin an exact version in scripts and `.mcp.json` files: `cloudfrog-knowledge==X.Y.Z`, `npx @cloudfrog/knowledge@X.Y.Z` or the tag `@vX.Y.Z`.
- `--managed-python --python 3.12` matters: `uv` ignores the upper bound of `requires-python` when it picks an interpreter, and a `uv`-managed CPython loads the `sqlite-vec` extension.
- The npm package ships no Python: it downloads a pinned `uv`, checks its SHA-256 against a checksum bundled in the package, and runs the PyPI release of the same version with it. Details in [`npm/README.md`](npm/README.md) and [`docs/install.md`](docs/install.md), "Install the CLI".
- **Docker.** The `Dockerfile` builds a two-stage image on `python:3.12-slim-trixie` that serves HTTP on port 8000 (with a `HEALTHCHECK` on `/health`) and starts with `knowledge-bootstrap`, which clones the vaults, runs `migrate-index` and then `serve`. `infra/compose.yml` is the generic Compose file for every instance; it publishes no ports (add a `ports:` override such as `127.0.0.1:8000:8000`). How to declare vaults and entries for it: [`docs/runbooks/serve-multiple-vaults.md`](docs/runbooks/serve-multiple-vaults.md), "Generic Compose file". [`lab/README.md`](lab/README.md) runs several fake instances and clients with it.

## Configuration

Configuration is explicit: environment variables (a `.env` at the checkout root is loaded and never overrides variables already set), the `vaults.yaml` registry, each vault's own `vault.yaml`, and optionally a static `config.json` (the repository ships none). An empty value of `VAULT_PATH`, `KNOWLEDGE_INDEX_DIR`, `KNOWLEDGE_VAULTS` and similar path variables counts as absent: an empty `VAULT_PATH` never resolves to the checkout root.

### Which vaults are served

Decided once at startup, in this order:

1. `KNOWLEDGE_VAULTS` (or `serve --vaults FILE`) names a `vaults.yaml`: every vault it declares, with one default.
2. `VAULT_PATH` (or `vault_path` in `config.json`): a registry of one vault.
3. `$XDG_CONFIG_HOME/knowledge/vaults.yaml` or `~/.config/knowledge/vaults.yaml`, when it exists.
4. None of these: startup fails with `E_VAULT_PATH_MISSING`.

The vaults themselves never live in this repository: each one has its own repository (`/vault/` is in `.gitignore`). Each vault also declares, in `Contexts/config.json`, the checkouts it can resolve and its context packages (`Contexts/<context>/`, [ADR-047](docs/adr/ADR-047.md)); see [Repository registry](#repository-registry).

### `vaults.yaml`

A minimal registry with two vaults (relative paths are anchored at the file; each vault's index lives in `<data_dir>/<slug>/`):

```yaml
version: 1
data_dir: ~/.local/share/knowledge
vaults:
  - slug: alpha
    path: ./alpha
    default: true
  - slug: beta
    path: ./beta
    features: [flows, governance]
```

- A slug matches `^[a-z0-9][a-z0-9-]{1,30}[a-z0-9]$`. Each vault directory must contain its `vault.yaml`.
- Nothing secret goes in this file. Deploy keys, webhook secrets and bearer tokens are named by the environment variable that holds them (keys ending in `_env`), never written; a value that looks like a credential is rejected with `E_SECRET_IN_MANIFEST`.
- An entry can declare `repo` (the server then clones and pulls it), `git`, `webhook`, `features`, `memory` and `embeddings`; the top-level `bearers` list gives each HTTP client its own token and scopes. The full annotated example is [`vaults.yaml.example`](vaults.yaml.example); procedures are in [`docs/runbooks/serve-multiple-vaults.md`](docs/runbooks/serve-multiple-vaults.md).
- Try it with the fixtures: copy `tests/fixtures/vaults/alpha` and `tests/fixtures/vaults/beta` next to the file, then `uv run cloudfrog-knowledge serve --transport stdio --vaults path/to/vaults.yaml`; `search` with `"vault":"beta"` queries the second one.

### Where the index lives

With `vaults.yaml`: `<data_dir>/<slug>` (`KNOWLEDGE_HOME` overrides `data_dir`). With `VAULT_PATH`, the slug is `CHROMA_COLLECTION`, else the `slug` of the vault's `vault.yaml`, else `vault`, and the index directory is the first of: `KNOWLEDGE_INDEX_DIR`, `CHROMA_PERSIST_DIR`, `<KNOWLEDGE_HOME>/<slug>` (only with an explicit `KNOWLEDGE_HOME`) and `<checkout>/.data/<slug>`. When you run an installed package with `VAULT_PATH`, set `KNOWLEDGE_INDEX_DIR` or `KNOWLEDGE_HOME` so the index lands where you expect. Next to `index.sqlite` live `link_graph.json`, `usage_stats.json`, `index_meta.json` and `skipped_files.json`.

### Embeddings providers

| `KNOWLEDGE_EMBEDDINGS_PROVIDER` | What it does |
|---|---|
| `openai` (default) | OpenAI embeddings. Needs `OPENAI_API_KEY` (or the variable named by `embeddings.key_env`); without it startup fails with `E_EMBEDDINGS_KEY_MISSING`. Model: `EMBEDDING_MODEL` or `embeddings.model`, default `text-embedding-3-large` (3,072 dimensions). A one-file commit costs about $0.0003 in embeddings. |
| `none` | No embeddings: lexical (FTS5) search only. No key needed. |
| `bag-of-words` | A deterministic bag of words over a fixed vocabulary. Used by the test suite and the goldens; good for trying the server offline, not for real retrieval. |

A vault can declare its provider in the `embeddings` block of `vaults.yaml` or its `vault.yaml`; the environment variable wins.

### Profiles and `features`

| Profile and served `features` | Tools in `tools/list` |
|---|---|
| `all` (default), no vault with `features` | the 10 core tools |
| `all`, `governance` | 12: the core, `get_stale_docs` and `suggest_links` |
| `all`, `flows` and `governance` | 14: the core, `get_flow`, `compare_flows`, `get_stale_docs` and `suggest_links` |
| `all`, `flows`, `governance` and `confluence` | 15: the above plus `sync_confluence` |
| `all` over stdio with a vault that declares `jira` | `sync_jira` at the end, besides whichever of the above apply |
| `legacy` (`serve --profile legacy` or `KNOWLEDGE_PROFILE=legacy`, for one release) | the 16 tool names of 1.4.0 in their order, `search_code` included and the extension tools without looking at `features`; `sync_jira` still requires `jira` |

Other `features` a vault can declare: `git-webhook` (the push webhook) and `memory-bridge` (publishing the vault to the local memory server). Calling an extension tool that no served vault declares returns `E_FEATURE_DISABLED`, also for a vault that does not declare it in a process that serves several, and the message names the vaults that do. Another profile value stops startup with `E_PROFILE_UNKNOWN`.

### Environment variables

Nothing is required over stdio except a vault (`VAULT_PATH`, `KNOWLEDGE_VAULTS` or a user `vaults.yaml`) and, with the default `openai` provider, `OPENAI_API_KEY`. Over HTTP a bearer is also required. The HTTP variables apply only with `MCP_TRANSPORT=http`.

| Variable | Default | Notes |
|---|---|---|
| `VAULT_PATH` | `vault_path` of `config.json`, if any | Directory of a single vault (a clone of its repository). Without it and without `KNOWLEDGE_VAULTS`, the user's `vaults.yaml` is served; without that, startup fails with `E_VAULT_PATH_MISSING`. Empty means absent. |
| `KNOWLEDGE_VAULTS` | | Path of a `vaults.yaml`. `serve --vaults` overrides it. |
| `KNOWLEDGE_INDEX_DIR` | | Index directory (`index.sqlite` and its sidecars) of a vault served with `VAULT_PATH`. Wins over the next two. |
| `CHROMA_PERSIST_DIR` | | Directory of an old Chroma index: `migrate-index` copies usage statistics and graph weights from it. Without `KNOWLEDGE_INDEX_DIR`, it is also the index directory (`index.sqlite` next to `chroma.sqlite3`); the image sets `/data/chroma`. |
| `CHROMA_COLLECTION` | | Slug of a `VAULT_PATH` vault, before the `slug` of its `vault.yaml`. |
| `KNOWLEDGE_HOME` | | Data root: without `KNOWLEDGE_INDEX_DIR` or `CHROMA_PERSIST_DIR`, the index lives in `<KNOWLEDGE_HOME>/<slug>`; without any of them, in `<checkout>/.data/<slug>`. |
| `OPENAI_API_KEY` | | Required on the server when the embeddings provider is `openai`. HTTP clients never need it. |
| `KNOWLEDGE_EMBEDDINGS_PROVIDER` | `openai` | `openai`, `none` or `bag-of-words`. With `none` the server starts without `OPENAI_API_KEY`. |
| `EMBEDDING_MODEL` | `text-embedding-3-large` | OpenAI embeddings model. |
| `KNOWLEDGE_STRUCTURED_OUTPUT` | `0` | Text blocks are the canonical response. `1` adds `outputSchema` to every tool and `structuredContent` to every response, the same JSON as the text; the catalog order does not change. See [ADR-046](docs/adr/ADR-046.md). |
| `KNOWLEDGE_PROFILE` | `all` | Tool profile: `all` or `legacy`. `serve --profile` wins over the variable; any other value stops startup with `E_PROFILE_UNKNOWN`. |
| `KNOWLEDGE_TEXT_INDENT` | | Unset, the text of each response is compact JSON; an integer from 1 to 8 indents it (`2` reproduces the text of contract 1.0.0). |
| `KNOWLEDGE_REQUIRE_SQLITE_VEC` | | With `1`, if `sqlite-vec` does not load in this Python, the server and the suite fail with `E_SQLITE_VEC_UNAVAILABLE` instead of sorting vectors with the standard-library fallback (`vector_backend: python`, about ten times slower). Ring 1 and golden recording set it. |
| `KNOWLEDGE_CONFIG_JSON` | `<checkout>/config.json`, if it exists | Static configuration file (the repository ships none). |
| `MCP_TRANSPORT` | `stdio` | `http` for a remote deployment. |
| `MCP_API_KEY` | | Bearer required on every `/mcp` request over HTTP. Without `MCP_ADMIN_KEY` it holds the `read` and `admin` scopes; with it, only `read`. Ignored if `vaults.yaml` declares `bearers`. |
| `MCP_ADMIN_KEY` | | Bearer with the `admin` scope (`read` and `admin`): the only one that can call `reindex`, `sync_confluence` and `POST /webhook/reindex` with a manual bearer. Must differ from `MCP_API_KEY` (if they match, startup exits with `E_MANIFEST_INVALID`). Ignored if `vaults.yaml` declares `bearers`, and unused over stdio. |
| `MCP_WEBHOOK_SECRET` | | Webhook HMAC secret. If unset, the webhook accepts only a manual bearer. The `/webhook/reindex` route exists only with `VAULT_REMOTE` (or `repo` in `vaults.yaml`). |
| `MCP_HTTP_HOST` | `0.0.0.0` | Bind address. Use `127.0.0.1` locally. |
| `MCP_HTTP_PORT` | `8000` | |
| `MCP_HTTP_PATH` | `/mcp` | |
| `VAULT_REMOTE` / `VAULT_BRANCH` | / `main` | Repository and branch of a `VAULT_PATH` vault; with `VAULT_REMOTE` the server runs git for it and registers the webhook route. |
| `CONFLUENCE_URL` | `confluence.url` of `config.json`, if any | Root of the Atlassian site (`https://<org>.atlassian.net`), **without** `/wiki`: the client adds `/wiki/api/v2` itself, and with `/wiki` at the end every page answers 404. A vault replaces it with `integrations.confluence.site`. |
| `CONFLUENCE_EMAIL` / `CONFLUENCE_TOKEN` | | Only for `sync_confluence` and `confluence export`. A vault can name other variables in `integrations.confluence.secrets` (`email_env`, `token_env`), always Confluence ones with their own prefix (`<SLUG>_CONFLUENCE_EMAIL`, `<SLUG>_CONFLUENCE_TOKEN`; unprefixed only for the default vault), and the site in `integrations.confluence.site` (`https` on `*.atlassian.net` or on `KNOWLEDGE_INTEGRATION_SITES`); any other name or site is `E_MANIFEST_INVALID`. |
| `KNOWLEDGE_INTEGRATION_SITES` | | Hosts that `integrations.<confluence\|jira>.site` may name besides `*.atlassian.net`, comma-separated: an exact host, or `.domain` for the hosts under it. Set by the operator; a vault never widens it. |
| `KNOWLEDGE_MEMORY_URL` | `http://127.0.0.1:6366` | Local memory server. Used only if it is `http` on `127.0.0.1`, `localhost` or `::1`: any other URL turns off reads (`E_MEMORY_URL_NOT_LOOPBACK`) and writes. |
| `MEMORY_HTTP_TOKEN` | | Memory bearer. Without it, `context_pack(include_memories=true)` does not read memory and `memory-bridge` does not write. |
| `KNOWLEDGE_MEMORY_REQUIRE_TOKEN` | `1` | Only for the writes of `memory-bridge` (and `jira`): with exactly `0`, they write without `MEMORY_HTTP_TOKEN` to a local memory that runs without a token. Any other value requires it. Reads always require it. |
| `MEMORY_BIN` | `memory` on the `PATH` | Memory binary that `memory-bridge promote` runs. |
| `<PREFIX>_JIRA_EMAIL` / `<PREFIX>_JIRA_TOKEN` | | Only for `sync_jira` and `jira sync`: the Jira account email and its API token, with the names declared by `integrations.jira.secrets` (`email_env`, `token_env`) of the `vault.yaml`, prefixed with the vault slug, for example `ACME_JIRA_EMAIL` and `ACME_JIRA_TOKEN` for `acme` (unprefixed, like `JIRA_API_TOKEN`, only for the default vault). They live in the person's environment (stdio), never in a deployment. |
| `KNOWLEDGE_JIRA_AUTOSYNC` | `1` | With `0`, the plugin's session hook does not launch `jira sync --auto` in the background. |
| `TYPESAFE_API_KEY` / `<PREFIX>_TYPESAFE_API_KEY` | | Key of the TypeSafe `jev` judge, with the name declared by `rerank.key_env` of the `vault.yaml` (unprefixed only for the default vault). Without it the server starts anyway, over stdio and HTTP, with `W_RERANK_KEY_MISSING`, and that vault searches without a judge (`reason: key_missing`). Measured cost: 0.042 USD per million input tokens, about 11,700 tokens per reranked search, so about 0.0005 USD per search; `rerank.max_usd_per_day` (1.00 by default) is the cap per vault and per day, shared by every process of the vault. If the platform stores the key under a short name, the Compose file copies it to the name the vault declares. |
| `KEV_API_KEY` / `<PREFIX>_KEV_API_KEY` | | Key of the local `kev` judge (only `http` on loopback), when the Kev server asks for one. Kev costs nothing. |
| `KNOWLEDGE_RERANK` | | `off` turns off the judge of every vault in the process: `search` answers without rerank with `reason: disabled`. Empty or unset, each vault decides. Any other value also turns it off and warns with `W_RERANK_INVALID`. |
| `KNOWLEDGE_RERANK_PROVIDERS` | `jev,kev` | Providers the operator allows; a vault that declares another searches without a judge. |
| `KNOWLEDGE_RERANK_BASE_URL` | | Replaces the URL of every judge: `https://api.typesafe.ai` or an `http` URL on loopback (the suite uses it with its System One double). No vault can change it. |
| `KNOWLEDGE_RERANK_TIMEOUT_MS` | `rerank.timeout_ms` of the vault (1,500) | Total deadline of each judge call, 200 to 60,000 ms: DNS resolution, connection, TLS and read. When it expires, `search` answers without rerank with `reason: timeout`. |
| `KNOWLEDGE_COMMAND_LABEL` | | The command that hints name; see [Command label in hints](#command-label-in-hints). |
| `KNOWLEDGE_<ROOT>_ROOT` | from `repos.local.json` | Pins a whole root, for example `KNOWLEDGE_WORK_ROOT`. `CD_<ROOT>_ROOT` and the root's declared `env` still work as deprecated aliases, and `python3 -m knowledge.repos --json` reports them in `warnings`. See [Repository registry](#repository-registry). |
| `KNOWLEDGE_REPO_<ID>` | from the matching root | Pins one checkout, for example `KNOWLEDGE_REPO_LEDGER_API`. `CD_REPO_<ID>` still works as a deprecated alias. |

### `sqlite-vec` and the Python fallback

`sqlite-vec` needs a Python with loadable SQLite extensions. `uv`'s CPython (`uv python install 3.12`, or `--managed-python`) has them; the python.org build for macOS does not. Without the extension the server sorts the vectors with the standard library (`vector_backend: python`, about 110 ms per query over 1,480 vectors of 3,072 dimensions instead of about 10 ms) with the same results, except exact ties at the `k` boundary. `doctor` and `/health` report the backend, and `KNOWLEDGE_REQUIRE_SQLITE_VEC=1` turns the fallback into an error.

## Tools

Contract `2.0.0`, published with `cloudfrog-knowledge` 0.6.0 and unchanged since. The catalog depends on the profile and on the `features` of the served vaults ([Profiles and `features`](#profiles-and-features)). The generated inventory of every tool, parameter and description is [`docs/tools/inventory.md`](docs/tools/inventory.md); the contract history is [`docs/contract/CHANGELOG.md`](docs/contract/CHANGELOG.md).

| Tool | Description |
|---|---|
| `get_document` | Reads a document: `chunk_id` (from a `search` row, without `file_path`) reads the block of one heading; also `section`, `start_line`/`end_line` and `max_chars` (3,000 by default; longer text comes back with `truncated: true` and `next_offset`). `metadata` carries `type`, `status`, `service`, `owner`, `tags` and `last_updated`; `full_metadata=True` returns the whole frontmatter. |
| `search` | Hybrid (default), `semantic` or `keyword` search, `limit` 1-50. Each row carries `file_path`, `score`, `chunk_id` and `section`, the pointer to the block to read; `excerpt` with `fields`, `snippet` with `snippet=True`. `rerank='on'\|'off'` with a vault judge. |
| `search_by_metadata` | Filters by frontmatter (`type`, `service`, `tag`, `status`). Answers `{total, offset, items}`, 20 rows per page (`limit` 1-200, `offset`). |
| `search_deep` | `search` plus multi-hop over the wiki-link graph from the candidates. |
| `find_path` | Shortest path between two documents (BFS, `max_depth=4`). |
| `get_related` | Direct neighbors in the graph, one row per document with `direction` `outgoing`, `incoming` or `both` and `weight` to 3 decimals; `limit` 1-200. |
| `get_impact` | Blast radius of a change through transitive backlinks; 20 paths per level (`max_items`, 0-1000) and `omitted` with the ones left out. |
| `reindex` | Reindexes the **vault**. Detects changes by SHA-256 and reprocesses only what changed. Runs `git pull` only with `pull=true` (the webhook pulls on every push). Only `target=vault` is accepted ([ADR-039](docs/adr/ADR-039.md) retired `code` and `all`). Admin scope over HTTP. |
| `context_pack` | Task context in one call (`budget_tokens`). Reads the memory server only with `include_memories=true` (default `false`), `MEMORY_HTTP_TOKEN` and a loopback `KNOWLEDGE_MEMORY_URL`; otherwise it answers with the vault and `degraded`. |
| `list_vaults` | The served vaults, the default and the contract version. Takes no `vault` and opens no index. |
| `get_flow` / `compare_flows` | Extension `flows`. Auth flow per provider; a deduplicated comparison with `shared_login_steps` and `files`. A provider without a flow document answers `E_FLOW_NOT_FOUND`, naming every missing one. |
| `get_stale_docs` | Extension `governance`. Review queue with its owner. Each row has a `reason`: `overdue` (with `age_days`) or `last_updated_invalid` (without `age_days`, since there is no age to compute). Filter with `owner="plat"`, or pass `include_unreadable=false` to see only overdue documents. Excludes what the `index.exclude` of the `vault.yaml` covers. |
| `suggest_links` | Extension `governance`. Proposes missing wiki-links between documents that share neighbors. |
| `sync_confluence` | Extension `confluence`. Reports which files under `scope_prefix` have a newer Confluence page. Read-only: `dry_run=False` answers `E_WRITE_FENCED` ([ADR-044](docs/adr/ADR-044.md)); without credentials `E_SECRET_MISSING`, and a scope with no tracked files `E_SCOPE_EMPTY`. Export happens with `cloudfrog-knowledge confluence export`, outside the vault, for a PR ([`docs/runbooks/promote-confluence.md`](docs/runbooks/promote-confluence.md)). Admin scope over HTTP. |
| `sync_jira` | Extension `jira`, over stdio only. Copies the Jira issues of the vault's projects as memory tasks (upsert by `jira_key`); `dry_run=True` by default (it only reads Jira). Requires the `admin` scope. See [`docs/runbooks/sync-jira.md`](docs/runbooks/sync-jira.md). |
| `search_code` | **Retired** from the `all` profile in 2.0.0 ([ADR-039](docs/adr/ADR-039.md), [ADR-046](docs/adr/ADR-046.md)): it answers `E_TOOL_RETIRED` with a redirect to `graphify explain <symbol>` / `graphify affected <file>` for relations and impact, `graphify query <symbol>` only with a real symbol name, `rg` for literal text and `search`/`search_deep` for intent. Only the `legacy` profile registers it, for one more release. |

Code understanding lives in `graphify`, through its own CLI against a local `graphify-out/`; this server never opens or parses that directory.

### Which tool to use

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
- If the vault declares a judge (`rerank` in its `vault.yaml`), `search(query, rerank="on")` reorders the results with it: each row the judge read carries `rerank_score` (the probability that its block answers the query), and `chunk_id`/`section` point to that block. With `rerank.default: on` it happens without asking, and `rerank="off"` skips it. A judge that is down never breaks search: it answers without rerank and `_meta["knowledge/rerank"].reason` says why. Judge configuration: [`docs/install.md`](docs/install.md), "`search` judge (rerank)".

## CLI reference

The `cloudfrog-knowledge` entry point (`knowledge.cli:main`) has nine subcommands; with none it is `serve`. `knowledge` is a second name for the same entry point: the usage (`--help`), `--version` (`knowledge X.Y.Z` or `cloudfrog-knowledge X.Y.Z`) and hints name the command you ran. Configuration errors print `<code>: <message>` on stderr, without a traceback, and exit with 2.

| Command | What it does | Exit codes |
|---|---|---|
| `serve [--transport stdio\|http] [--vaults FILE] [--profile all\|legacy]` | Starts the MCP server. The flags override `MCP_TRANSPORT`, `KNOWLEDGE_VAULTS` and `KNOWLEDGE_PROFILE`. | 2 on a configuration error (`E_PROFILE_UNKNOWN` with another profile) |
| `eval <args...>` | Runs `scripts/eval_retrieval.py` with those arguments (`--stdio\|--http`, `--queries`, `--vault`, `--provider`, `--rerank`, `--json`, `--markdown`, ...). Needs a source checkout. Guide: [`docs/eval/README.md`](docs/eval/README.md). | the script's; 2 with `E_EVAL_UNAVAILABLE` if there is no `scripts/` |
| `vault-lint PATH [--format json] [--rules 1.2\|1.3] [--fix [--out DIR]]` | Checks a vault and prints JSON on stdout (`vault`, `files`, `rules`, `ok`, `errors`, `warnings`). | 0 without errors, 1 with errors, 2 with `E_LINT_PATH_NOT_DIR`, or with `--fix` `E_WRITE_FENCED` and `E_ARGUMENT_CONFLICT` |
| `doctor [--vaults FILE] [--brief] [--client]` | Read-only diagnosis of whether this installation can serve; JSON, or one line of up to 200 bytes with `--brief`. | 0 healthy (also with a pending `init`), 1 with problems, 2 if the configuration does not load |
| `init [--vaults FILE] [--vault SLUG] [--root [SLUG:]NAME=DIR] [--search-dir DIR] [--max-depth N] [--repo [SLUG:]ID] [--all] [--drop [SLUG:]ID] [--yes] [--dry-run] [--json] [--force\|--refresh] [--exclude local\|gitignore]` | Client side: installs each vault's context packages into the checkouts selected on this machine. | 0; 1 on an unreadable vault or a conflict; 2 with `E_ROOT_UNKNOWN`, `E_CONTEXTS_REPO_UNKNOWN` or a configuration error |
| `migrate-index [--vaults FILE] [--vault SLUG] [--from-chroma DIR] [--build] [--prune-chroma]` | Prepares each vault's `index.sqlite` and copies the Chroma sidecars. | 0; 2 with `E_INDEX_VERSION_NEWER`, `E_INDEX_UNREADABLE` or `E_PRUNE_REFUSED` |
| `confluence export [--vaults FILE] [--vault SLUG] [--out DIR] [--space KEY] [--scope-prefix PREFIX] [--dry-run]` | Writes Confluence pages newer than the vault, outside every vault. | 0; 1 if the report has an error; 2 with `E_WRITE_FENCED`, `E_SECRET_MISSING` or an unknown vault |
| `memory-bridge sync [--vaults FILE] [--vault SLUG] [--card]` / `memory-bridge promote list\|stamp` | Publishes the vault to the local memory server; wraps `memory project <slug> promote`. | see below |
| `jira sync [--vaults FILE] [--vault SLUG] [--since ISO\|-Nd] [--dry-run] [--max-issues N] [--json] [--auto]` | Jira issues as tasks of the local memory server. | 0; 1 on `E_JIRA_*` or memory down midway; 2 if it could not be attempted; `--auto` always 0 |

### Command details

**`vault-lint`.** Without `--fix` it writes nothing. `--rules 1.3` (the default) applies the vault rules: ISO `last_updated`, a known `type`, the vault's own `scope`, NFC paths without case collisions, runbooks that memory can index, and wikilinks that ignore code. `--rules 1.2` is the set that `reindex.validation` counts. Without `--rules` the result can differ from 1.2.0 (exit 1 where it was 0, for example from `E_FRONTMATTER_LAST_UPDATED`); `--rules 1.2` reproduces it. `--fix` (only with `1.3`) moves `updated`, `last_review` or `last_verified.date` to `last_updated`, only in the frontmatter, atomically and idempotently, and adds `fixed` and `remaining` to the JSON. It never writes in a served vault (`E_WRITE_FENCED`): run it on a checkout that is not served, or with `--out DIR` to fix a new copy outside every served vault, and deliver the change by PR. Error codes: `E_FRONTMATTER_UNPARSEABLE`, `E_FRONTMATTER_NOT_MAPPING`, `E_WIKILINK_UNRESOLVED` and, with `1.3`, `E_FRONTMATTER_LAST_UPDATED`, `E_SCOPE_FOREIGN`, `E_PATH_CASE_COLLISION`, `E_PATH_NOT_NFC`, `E_RUNBOOK_ID_INVALID`, `E_RUNBOOK_STATUS_INVALID`, `E_RUNBOOK_SERVICE_UNKNOWN`, `E_SERVICES_MAP_INVALID`. `E_ARGUMENT_CONFLICT` means `--out` is inside `PATH` or not empty. More in [`docs/install.md`](docs/install.md), "Check a vault for its PR".

**`doctor`.** Checks, without writing anything, whether this installation can serve: the discovery rule that won and the registry file; per vault, its directory, `vault.yaml`, embeddings provider, whether its key is defined (never the value) and whether there is an index; whether `KNOWLEDGE_HOME` is writable; the package and contract versions; the `rerank` block of each vault that declares a judge (names, never values); and the `tool_calls` block of the default vault (see [Call telemetry](#call-telemetry)). For each vault that declares `Contexts/config.json`, a `contexts` block says whether this machine has its `repos.local.json`; the ones that do not go in `init.pending` and, over stdio, in the `W_CONTEXTS_INIT_PENDING` warning. With `--brief`, one line of up to 200 bytes (the one the Claude Code plugin hook prints), which with a pending `init` is `knowledge doctor: init pending for vault(s) <slugs>; run: cloudfrog-knowledge init`. With `--client`, for a machine that consumes a remote server, it checks only the client side: that each vault exists and can be read, that its `Contexts/config.json` loads, its `repos.local.json`, and the state of each checkout according to an `init --dry-run` that neither scans nor prompts (`contexts.checkouts`); it skips the embeddings provider and key, the index, `vector_backend`, the judge, memory and bearers (`skipped`). A present checkout that is not in `apply` is `not-selected` (information); a selected one still to be installed counts as `init` pending; one with links to its package that is not in `apply` is the warning `W_CONTEXTS_LINKED_NOT_SELECTED`, and a `repos.local.json` without `apply` the warning `W_CONTEXTS_SELECTION_MISSING` (both exit 0; the healthy brief line adds `, <n> checkout(s) linked and not selected` or `, selection missing in <n> vault(s)`). A checkout that is a git repository without an `origin` remote is the warning `W_CONTEXTS_CHECKOUT_UNVERIFIED` (it does not fail: nothing proves it is another repository); a conflict is `E_CONTEXTS_CONFLICT`, and a checkout whose `origin` is another repository or that is not a git repository is `E_CONTEXTS_CHECKOUT_INVALID`. The healthy `--client` brief line is `knowledge doctor: ok, client, <n> vault(s), default <slug>`, plus `, <n> checkout(s) unverified` when there are such warnings. Exit 1 codes: `E_VAULT_PATH_MISSING`, `E_VAULT_PATH_NOT_DIR`, `E_VAULT_MANIFEST_MISSING`, `E_EMBEDDINGS_PROVIDER_UNKNOWN`, `E_EMBEDDINGS_KEY_MISSING`, `E_KNOWLEDGE_HOME_NOT_WRITABLE`; with `--client`, `E_VAULT_PATH_*`, `E_VAULT_MANIFEST_MISSING`, `E_CONTEXTS_INVALID`, `E_CONTEXTS_CONFLICT` or `E_CONTEXTS_CHECKOUT_INVALID`.

**`init`.** For each registered vault that declares `Contexts/config.json`, it locates each root (flag, environment, `repos.local.json`, the vault's `default`, git checkouts whose `origin` matches under `--search-dir` (by default `~/Projects` and the parents of known paths), or a prompt, except with `--yes`). It decides which checkouts receive a package on this machine: the `apply` list of `repos.local.json` ([ADR-049](docs/adr/ADR-049.md)). `--repo ID` adds, `--all` adds every present checkout with a package, and `--drop ID` removes the id and deletes only that checkout's links to `Contexts/<package>/`. A file without `apply` adopts the checkouts that already have their package installed; a machine with nothing to adopt and no flags asks in a terminal and, with `--yes` or without a terminal, installs nothing and lists the present checkouts with the `--repo ID` hint; an id the registry no longer declares leaves with a warning. It writes `<config home>/knowledge/<slug>/repos.local.json` (honoring `$XDG_CONFIG_HOME`) and installs the `Contexts/<context>/` package of each selected, present checkout (present but unselected ones stay `not-selected`): relative links (`link`, the only recommended mode) or a copy (`copy`, kept for compatibility), plus ignore entries in `.git/info/exclude`. It skips checkouts that are not there. A `.claude` that is a real directory in the checkout stays real and receives one link per entry of the package's `.claude` (and per entry of a versioned `.claude/<collection>/`); what the checkout versions in git is its own and is kept with a warning, without conflict ([ADR-048](docs/adr/ADR-048.md)). It never replaces unversioned content of its own without `--force` (which sets it aside as `<name>.pre-ai-context.bak-<epoch>`), resyncs a copy with `--refresh`, never writes inside a vault (`E_WRITE_FENCED`) and is idempotent. It ends by saying where each file lives; `--dry-run` also shows selection changes. Exit 1 also covers `E_CONTEXTS_INVALID` (including an `apply` that is not a list of ids), a `--root` that is not a directory, and an `origin` that is not the declared remote; `E_CONTEXTS_REPO_UNKNOWN` is a `--repo` or `--drop` that no vault declares. See [`docs/install.md`](docs/install.md), "Initialize the client", and [ADR-047](docs/adr/ADR-047.md).

**`migrate-index`.** Prepares each vault's `index.sqlite` without embedding anything, and copies `usage_stats.json` and the learned weights of `link_graph.json` from a Chroma directory (it never opens the Chroma database; `chromadb` is not a dependency). It prints one JSON line per vault (`{vault, outcome, index_version, from}`, with `outcome` `created`, `migrated`, `up_to_date` or `upgraded`). A second run writes nothing. `--build` indexes right away; `--prune-chroma` deletes only `chroma.sqlite3` and Chroma's segment directories. The image runs it before `serve`.

**`confluence export`.** Writes under `--out` (by default `<data_dir>/<slug>/confluence-export/`) the Confluence pages newer than their vault file, with the vault's frontmatter and the `last_updated` of the day the page changed. It never writes in a vault: an `--out` inside any served vault, or a file that resolves there through a symlink, is `E_WRITE_FENCED` and no file is written. JSON on stdout (`{out_dir, written, unchanged, errors, ...}`). Exit 1 means nothing was tracked in the scope or a page could not be read. Promotion to the vault is a human PR ([`docs/runbooks/promote-confluence.md`](docs/runbooks/promote-confluence.md)).

**`memory-bridge sync`.** Publishes the vault to its project (`memory.project`) in the local memory server: with `--card`, it creates or updates the card (`display_name` from `name`, `jira_project` from the first `integrations.jira.projects`, `knowledge_hub_path` from `memory.hub`; what the vault does not declare is not sent), then has memory rescan `Runbooks/` (`source: vault-fs`). It writes only to memory, on loopback, and requires the `memory-bridge` feature. JSON on stdout (`{vault, memory_project, ok, card, runbooks}`). Exit 1 if memory rejected a step (`E_MEMORY_WRITE_FAILED` with `http_status` and `reason`); 2 with `E_FEATURE_DISABLED`, `E_MEMORY_PROJECT_MISSING`, `E_MEMORY_TOKEN_MISSING` or `E_MEMORY_URL_NOT_LOOPBACK`.

**`memory-bridge promote list [--types LIST] [--limit N]` / `memory-bridge promote stamp OBS --knowledge-ref PATH`.** Wrap `memory project <slug> promote list|stamp --json` (`MEMORY_BIN` or `memory` on the `PATH`): the pinned observations that can become a document, and the record, on the observation, of the document the PR merged. Nothing is written in the vault; `stamp` requires `--knowledge-ref` to be an existing `.md` in it. Prints memory's JSON. Exit 1 if memory answers an error (its JSON envelope); 2 with `E_KNOWLEDGE_REF_INVALID`, `E_ARGUMENT_OUT_OF_RANGE` (a `--types` value that is not a lowercase observation type), `E_MEMORY_UNAVAILABLE` (missing binary, more than 30 s, or output without JSON) or a configuration error.

**`jira sync`.** Copies the issues of the vault's `integrations.jira` projects updated since `--since` (by default the cursor minus one day after a complete run, the exact cursor after a truncated one, or 30 days; tasks that memory rejected are read again by key on the next run) as tasks of its memory project, one per issue and per `jira_key`. Unlike the tool, it writes unless `--dry-run`. One summary line, or the full report with `--json`. `--auto` runs every vault that declares `jira`, has its variables and has met `sync.throttle_minutes`; it is what the session hook launches. Over stdio only, since it writes to the local memory. Exit 1 with `E_JIRA_UNAVAILABLE`, `E_JIRA_AUTH_FAILED`, `E_JIRA_SYNC_STALLED` or memory down midway (the report has `error`); 2 with `E_FEATURE_DISABLED`, `E_SECRET_MISSING`, `E_MANIFEST_INVALID`, `E_MEMORY_TOKEN_MISSING` or another configuration error. See [`docs/runbooks/sync-jira.md`](docs/runbooks/sync-jira.md).

### Command label in hints

The hints that say what to run (the `doctor --brief` line, `init.command` in the `doctor` JSON, the `W_CONTEXTS_INIT_PENDING` warning, the `_README` of each `repos.local.json`, the hint of an `init` conflict and the header of its report) name `cloudfrog-knowledge init` or `cloudfrog-knowledge doctor`, or `knowledge init` and `knowledge doctor` when you ran the CLI as `knowledge`. A vault whose clients call `init` and `doctor` through their own scripts sets `KNOWLEDGE_COMMAND_LABEL` in them, and the hints name those scripts instead of this CLI, under either name:

| `KNOWLEDGE_COMMAND_LABEL` | Hint for `init` | Hint for `doctor --client` |
|---|---|---|
| unset or blank | `cloudfrog-knowledge init` (`knowledge init` if you ran `knowledge`) | `cloudfrog-knowledge doctor --client` (`knowledge doctor --client`) |
| `Contexts/scripts/{command}.sh` | `Contexts/scripts/init.sh` | `Contexts/scripts/doctor.sh` |
| `acme-knowledge` | `acme-knowledge init` | `acme-knowledge doctor` |

It is one variable for both commands: `{command}` is replaced by the subcommand and, without it, the subcommand goes after the label. With a label defined, the hint adds no flags: a script carries its own. No other environment value appears in a hint.

## Using a team deployment

### Connect

Plugin installation for Claude Code (the `cloudfroghq` marketplace, one line per profile) is in [`docs/install.md`](docs/install.md). What follows is the manual registration.

Ask whoever administers the stack for two credentials, over a secure channel and never through git: the knowledge server's bearer (`MCP_API_KEY`) and the memory cloud token (`MEMORY_CLOUD_TOKEN`).

They are two pieces with different roles:

- **cloudfrog-knowledge** (remote MCP, HTTP): the project's curated knowledge. You connect to the central server; you install nothing.
- **memory** (local binary plus stdio MCP): your agent's episodic memory (decisions, bugs, session context). It is local-first: it lives on your machine and is **replicated** to the shared cloud for backup and common knowledge. The remote container is not an MCP server you connect to.

Register the knowledge server (one line):

```bash
claude mcp add -s user -t http cloudfrog-knowledge https://<deployment>/mcp -H "Authorization: Bearer <token>"
```

`<token>` is the deployment's read-only `MCP_API_KEY`; a deployment that serves HTTPS encrypts the bearer. `reindex` and `sync_confluence` require the `admin` scope, which `MCP_ADMIN_KEY` grants. Each deployment's URL and how to request its bearer are documented in its vault, under `Deploy/`.

Install the `memory` binary and its Claude Code setup as described in [`docs/install.md`](docs/install.md) (Homebrew or a checksummed release download); `memory setup claude-code` registers the stdio MCP server and the session hooks, and `memory doctor` verifies them. Do not use `gentleman-programming/tap/engram`: it is the tap of the **original** project this memory fork comes from, and it silently installs that project without the backfill fix or this team's cloud configuration.

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

### Edit and publish the vault

The vault does not live in this repository: each organization has its own, in its own repository. Clone the one you are going to edit:

```bash
git clone git@github.com:<org>/vault.git ~/Projects/Vaults/<slug>
```

Open it in Obsidian, edit, and propose the change by PR with the vault owner as reviewer (D-11). When the PR lands on `main`, that repository's push webhook notifies the server, which pulls the changes and reindexes only what changed. A one-file commit costs about $0.0003 in embeddings, so edit without fear.

Vault maintenance tools, if you curate content: `get_stale_docs` (review queue), `suggest_links` (missing wiki-links), `sync_confluence` (which files have a newer Confluence page; it needs `CONFLUENCE_EMAIL` and `CONFLUENCE_TOKEN` or the names in `integrations.confluence.secrets`), `reindex` (forces a reindex) and `sync_jira` (over stdio, for a vault that declares `jira`). See [Tools](#tools).

### If something fails

- **The tools do not appear in Claude Code.** Restart Claude Code after registering the server. Check the registration with `claude mcp get cloudfrog-knowledge`.
- **You get 401.** The bearer you registered does not match the server's `MCP_API_KEY`. Check the header with `claude mcp get cloudfrog-knowledge` and ask the administrator for the right key.
- **Responses carry too much text.** Use `get_document(chunk_id=...)`, `section="..."` or `max_chars=500`.
- **You edited the vault and it does not show up in searches.** Check that the push arrived, then the index state with `curl -s https://<deployment>/health`: `last_indexed_at` must be later than your push. If `indexing` is `true`, it is working: wait and look again.

## Operating the server

Each deployment is an instance of the same server with its own environment: its vaults, index, keys and domain. What is specific to each one (domain, platform application, keys by name, webhooks, verification, rollback) is documented in its vault, under `Deploy/`; only what they share is here.

### What runs

`infra/compose.yml` is the Compose file of every instance (the procedure for vaults and entries is in [`docs/runbooks/serve-multiple-vaults.md`](docs/runbooks/serve-multiple-vaults.md), "Generic Compose file"):

- **knowledge-mcp**: built from this repository, with no published ports. A platform routes to the container's port 8000, and whoever runs `docker compose` by hand publishes it with an override.
- **memory-cloud** (profile `memory`): image `ghcr.io/horacioespinosa/memory`, tag in `MEMORY_IMAGE_TAG` (default `latest`) with `pull_policy: always`, no published ports. The GHCR package is private: pulling it requires a registry credential loaded in the platform.
- **Postgres** (profile `memory`): internal, no exposed port.

`infra/dokploy-compose.yml` is the Compose file of a Dokploy deployment that has not migrated to `infra/compose.yml` yet; it is documented in that deployment's vault.

### How it is wired

In case it has to be recreated:

1. **Source.** The platform builds this repository with `infra/compose.yml` (a GitHub connection to `cloudfroghq/knowledge`, branch `main`). Automatic deployment **does not deploy anything on its own** in Dokploy: the server does not register the webhook, so it must be created by hand in GitHub (step 4). If it is missing, pushes land in the repository and the server keeps running the old image, with no visible error.
2. **Vaults at runtime.** No vault lives in this repository. `KNOWLEDGE_VAULTS_YAML` carries a `vaults.yaml` with each instance's entries (a `repo` the entrypoint clones into `/data/vaults/<slug>`, or a folder mounted with `path`, for example on `/data/local`), and in single-vault mode `VAULT_REMOTE`, `VAULT_BRANCH` and `VAULT_PATH` (default `/data/vaults/default`). The clone needs a read-only deploy key (`GIT_DEPLOY_KEY_B64`, or one per vault, `<SLUG_UPPER>_GIT_DEPLOY_KEY_B64`). Review thresholds come from the vault's `vault.yaml`, so no `config.json` is mounted (`KNOWLEDGE_CONFIG_JSON` is not defined). If the volume already has a clone, startup repoints its remote, fetches and force-checks out the branch, so an old volume adopts the new remote with its content. After the clone, startup checks that `VAULT_PATH` exists and aborts naming the path, the remote and the branch if it is missing: a remote whose tree lacks that directory would otherwise die in the validation of `config.py` and, with `restart: unless-stopped`, crashloop behind a deploy that reports success.

   **Index.** It lives in the `knowledge-index` volume, in `KNOWLEDGE_INDEX_DIR=/data/index/<slug>` (`index.sqlite` with its sidecars `link_graph.json`, `usage_stats.json`, `index_meta.json` and `skipped_files.json`). Startup runs `migrate-index` and only then `serve`; `migrate-index` embeds nothing: it creates `index.sqlite` and, if it finds a Chroma index in `/data/chroma` (a volume mounted only as a source), copies its usage statistics and the graph's learned weights. The index fills on the first tool call or with the webhook. After a deploy, `docker volume ls` must list the `knowledge-index` volume and `/health` must report `index_version`.
3. **Compose environment** (in the platform's panel, never in git): `MCP_API_KEY` (the clients' read bearer), `MCP_ADMIN_KEY` (the `admin` bearer, different from the previous one: the only one that can call `reindex`, `sync_confluence` or `POST /webhook/reindex` with a manual bearer), `MCP_WEBHOOK_SECRET`, `OPENAI_API_KEY` (only here; clients never touch it; or `KNOWLEDGE_EMBEDDINGS_PROVIDER=none`), `<SLUG>_TYPESAFE_API_KEY` (optional: the `search` judge key, with the name declared by the vault's `vault.yaml`), `GIT_DEPLOY_KEY_B64` and, with the `memory` profile, `POSTGRES_PASSWORD`, `MEMORY_JWT_SECRET`, `MEMORY_CLOUD_TOKEN`, `MEMORY_CLOUD_ADMIN` and `MEMORY_CLOUD_TOKEN_PEPPER`. Also, with no secret value: `MEMORY_IMAGE_TAG`, which pins the cloud image tag (without it Compose uses `latest`, the moving tag, and a rollback means giving it an exact published version, for example `v1.0.3`), and `MEMORY_CLOUD_IDENTITY_MODE`, memory-cloud's identity mode (memory 1.7.0 or later): `grace` (the default) accepts the shared `MEMORY_CLOUD_TOKEN` alongside personal tokens; `required` rejects it on `/sync/*` with `401 identity_required` and leaves the dashboard to administrators only. To change it, set it in the panel and redeploy; an image older than 1.7.0 ignores it. Plus a registry credential for `ghcr.io`, because the package is private. The optional env file `infra/.env.vault-keys` is gitignored.

   Two `memory-cloud` variables are **not** loaded in the panel, because the Compose file pins them inside the service: `MEMORY_CLOUD_ALLOWED_PROJECTS: "*"` (a single-user server behind the bearer replicates every enrolled project instead of filtering by name) and `MEMORY_PROJECTS_SYNC: "1"` (enables the replication of project cards, tasks and evidence). A value set in the panel for either one never reaches the container.
4. **Two GitHub webhooks** (Settings, Webhooks), both triggered on push, each in its own repository:
   - **reindex**, in the vault's repository: `https://<domain>/webhook/reindex?target=vault`, content type `application/json`, with the webhook's Secret field set to the value of `MCP_WEBHOOK_SECRET`. A push to the vault pulls the clone and reindexes incrementally.
   - **deploy**, in `cloudfroghq/knowledge`: the deploy URL the platform exposes for this Compose file, without a secret (the token in the URL authenticates). A push to the code redeploys.

   **Check each one's delivery history.** Without it, a webhook that delivers fine looks exactly like one that never fires.
5. **Health check.** See [Health endpoint](#health-endpoint).
6. **Domains and TLS.** The Compose file publishes no ports; the platform routes the domain to the container's port 8000 and issues the certificate. Each deployment's domain, DNS and certificate are documented in its vault, under `Deploy/`.

### Health endpoint

`GET /health` (open, no bearer) reports `status`, `contract_version`, `profile`, `transport` and these signals, which are worth not confusing:

| Field | Meaning |
|---|---|
| `indexed` | Documents on disk. Always available. |
| `index_loaded` | Whether this process has loaded the index yet. It starts `false` after every deploy; it does not mean data loss. |
| `indexing` | Whether a reindex is running right now. |
| `stale` / `last_indexed_at` | Whether the vault changed after the last indexing. |
| `index_version` / `vector_backend` | Format of `index.sqlite`, and who sorts the vectors: `sqlite-vec` or `python` (the slower standard-library fallback, when the extension does not load). |
| `vault_commit` | Short commit (12 characters) of the vault clone right now; read on every call, so it is correct from startup. `null` if the vault declares no repository. |
| `last_sync_at` / `last_sync_result` / `last_sync_error` | When and how the last update of the clone in this process ended: `fast_forward`, `up_to_date`, `realigned` (rewritten or divergent history: the clone was aligned with the remote) or `failed`, with the reason in `last_sync_error` (one line, no credentials). `null` before the first update after startup and for a vault without a repository. A `failed` does not change `status`: the vault keeps being served from disk. |
| `last_change_at` / `last_change_result` | When and how the last update that moved the clone in this process ended: `fast_forward` or `realigned`. A later `up_to_date` or `failed` does not replace it, so a merge that was pulled stays visible even after a pass with nothing new. `null` until the first change after startup and for a vault without a repository. |

### Generate the keys

```bash
openssl rand -hex 32   # MCP_API_KEY (clients)
openssl rand -hex 32   # MCP_ADMIN_KEY (admin bearer)
openssl rand -hex 32   # MCP_WEBHOOK_SECRET (the repository webhook)
```

### Bearer scopes

Over HTTP every request presents a bearer, and each bearer is a principal with scopes: `read` uses the server; `admin` can also call `reindex`, `sync_confluence` and `POST /webhook/reindex` with a manual bearer. A bearer without `admin` gets `E_SCOPE_REQUIRED` from those tools (403 on the webhook). Without `MCP_ADMIN_KEY` and without `bearers`, `MCP_API_KEY` holds both scopes, as in 1.2.0. `bearers` in `vaults.yaml` gives one bearer per person (`id`, the variable that holds the token, `scopes`) and replaces both keys. Every token is compared against every entry in constant time. Over stdio no bearer is checked. Details: [`docs/install.md`](docs/install.md), "Bearers and scopes of an HTTP deployment".

### Vault sync

The server keeps its own clone of each vault that has a repository (in `/data/vaults/<slug>` with the generic Compose file) and reindexes in response to the webhooks.

1. **On your machine**: you edit the vault and push (or the Obsidian Git plugin commits every N minutes).
2. **GitHub** delivers the push webhook.
3. **Container**: `/webhook/reindex` validates the HMAC (`X-Hub-Signature-256`, or `X-Hub-Signature` when it carries a `sha256=` digest, like Bitbucket's), brings the vault clone to the tip of the served branch on `origin`, and triggers the incremental reindex. Cost per one-file commit: about $0.0003 in OpenAI.
   - The clone converges with the remote whatever happened upstream: `git fetch --prune origin`; if `HEAD` is an ancestor of `origin/<branch>` (the normal push), `git merge --ff-only`; if not (the branch was rewritten with a force push, the clone diverged, or the fast-forward is rejected), `git checkout -f -B <branch> origin/<branch>`, the same command `infra/bootstrap-vault.sh` uses to adopt a clone at startup, with a `WARNING` in the log naming the old and the new commit. Untracked files stay, as in the bootstrap. If the `fetch` fails (key, network), the clone is not touched, the log carries an `ERROR`, what is on disk is indexed, and `/health` reports `last_sync_result: failed`.
   - Only a push to the served branch (`git.branch` in `vaults.yaml`, `VAULT_BRANCH` with `VAULT_PATH`; `main` by default) brings anything into the clone. A signed push to another branch or to a tag, or one that deletes a branch (the one `--delete-branch` sends on merge, or "Close source branch" on merge in Bitbucket), gets 200 with `"status": "ignored"` and runs neither git nor reindex. A Bitbucket Cloud `repo:push` carries no `ref`: it is judged by `push.changes` and runs only if some change leaves the served branch on a commit (`new` of type `branch` with that name).
   - Consecutive deliveries do not trample each other: while a pull and reindex of a vault runs, the ones that arrive add a single extra pass at the end, which brings the latest from the remote. The `reindex` tool takes the same lock, so two git updates never run on the same clone.

#### Manual reindex

To force a reindex without a commit. The route exists only when the default vault declares a repository (`repo` in `vaults.yaml`, or `VAULT_REMOTE`); without one it answers 404, and the reindex is requested with the `reindex` tool. The bearer needs the `admin` scope: `MCP_ADMIN_KEY` when it is defined, or `MCP_API_KEY` when no scopes are configured; a read-only bearer gets 403 with `E_SCOPE_REQUIRED`.

```bash
curl -X POST -H "Authorization: Bearer $MCP_ADMIN_KEY" \
  "https://<domain>/webhook/reindex?target=vault"
```

> [!important]
> **A full reindex is needed only once.** Chunk metadata includes `owner` and `last_verified`. The incremental reindex skips files whose hash did not change, so an index built before those fields existed lacks them, and `get_stale_docs` would return `unassigned` for everything. The same applies to the `tag` filter of `search_by_metadata` (fixed in 9bd99ff): an index built before that commit needs one `reindex(force=true, pull=true)`.
>
> Two ways to force it:
>
> - the `reindex` MCP tool with `force=true`;
> - `POST /webhook/reindex?force=true`, **only** with the manual `admin` bearer. A repository push authenticates by HMAC signature and cannot ask for `force`: a full embeddings run over the whole vault never fires from a commit on its own.
>
> The weekly report (`scripts/weekly-stale-report.py`) does not depend on the index and works from the first minute.

### Repository registry

No repository path is written in the code. The repository-to-path mapping lives in one place, and every consumer reads it from there.

| File | What it declares | Versioned? |
|---|---|---|
| `<vault>/Contexts/config.json`, keys `repos` (and `graph`) | What each repository **is**: its identifier, the group it belongs to, the remote that proves it, the relative path it takes under a root, and the context package (`Contexts/<context>/`) that belongs to it. The same on every machine. | Yes, with the vault |
| `~/.config/knowledge/<slug>/repos.local.json` | Where the roots **are** on this machine, and the vault directory. Absolute paths. Also `apply`, the list of identifiers whose context packages `init` installs on this machine (`--repo`, `--all` and `--drop` change it; neither the graph targets nor the server read it). Written by `cloudfrog-knowledge init` (under `$XDG_CONFIG_HOME` if defined). | No: it lives outside every repository |

The server is not tied to any project: the checkouts it can reach are declared by each vault in its `Contexts/config.json`, one entry per checkout. The fixture `tests/fixtures/vaults/contexts/Contexts/config.json` shows the shape, and [ADR-047](docs/adr/ADR-047.md) explains each field the parser requires.

**The key is the repository's identity**, never the name of the local directory. The two can differ (a renamed clone, a worktree, a second copy) and only the identifier survives that difference. The folder name is a value of the registry (`path`), never a key.

**Roots and their groups.** A root is the directory the declared paths hang from, and the difference between groups is not cosmetic: checkouts that come down together as submodules of the same parent arrive with a single `git submodule update --init`; `standalone` ones are independent clones, and cloning the parent brings none of them; `vendored` ones appear only when the host checkout's package manager installs them in its gitignored dependency tree. A tool that treats them as one set reports a whole platform from half of it.

**Precedence**, highest first: `KNOWLEDGE_REPO_<ID>` (or the deprecated `CD_REPO_<ID>`), then `repos.<id>` in `repos.local.json`, then `KNOWLEDGE_<ROOT>_ROOT` (or the deprecated `CD_<ROOT>_ROOT` and the root's `env`), then `roots.<id>` in `repos.local.json`, then the `default` the root declares in `Contexts/config.json`, only if that directory exists; otherwise nothing, and the checkout is *unconfigured*.

**Which declaration is read.** The one of the vault you name (`--vault SLUG` or `--vault-dir DIR` in `python3 -m knowledge.repos`); if you name none, the one of the only vault initialized on this machine; with several, you must name one; with none, the registry answers that you must run `cloudfrog-knowledge init`. The module does not read `vaults.yaml` (it does not need PyYAML): each `repos.local.json` that `init` writes stores the vault directory and its slug.

**Three answers, never two.** `unconfigured` means nobody said where to look and nothing was inspected; `missing` means this machine did say where to look and the directory is not there. Neither one is a pass, and confusing them turns "I could not look" into "I looked and there was nothing".

```bash
# Resolution table, verifying each checkout's remote
PYTHONPATH=src python3 -m knowledge.repos --report --verify-remotes

# From bash: repo_path / repo_status / repo_require
. scripts/lib/repo-registry.sh
repo_path ledger-api
repo_status ledger-api
```

Consumer: `graph.targets` of the default vault's `Contexts/config.json` (each entry names a registry `repo` and is the roster of repositories that `graphify` onboards). The `Makefile` (`make help`) wraps `init` and the graph builds over that registry.

> A `CD_*` variable set in `.env` reaches the Python side, because `load_dotenv()` runs before the registry is read. The bash side reads the real environment, so there they must be exported. For stable configuration, `repos.local.json` serves both.

### Server troubleshooting

**`E_EMBEDDINGS_KEY_MISSING: ... needs OPENAI_API_KEY`**: the embeddings provider is `openai` and neither the environment nor `.env` carries the key. On the server, add it in the environment panel; to start without embeddings, `KNOWLEDGE_EMBEDDINGS_PROVIDER=none`.

**The repository webhook answers 401**: the secret does not match. Check that `MCP_WEBHOOK_SECRET` on the server is exactly the webhook's "Secret" in GitHub.

**Pushes do not deploy**: check that the deploy webhook exists in GitHub and that its delivery history shows calls. `autoDeploy` in the panel does not create the webhook by itself.

**`/health` reports `status: degraded`**: empty vault or index without entries. On the first start, make sure the vault directory (`/data/vaults/<slug>` with the generic Compose file) has the clone and that the server ran the initial reindex.

**The deploy reports success and `https://<domain>/health` does not answer**: the container starts, does not find `VAULT_PATH` and restarts in a loop. The service logs carry the line `fatal: VAULT_PATH ... is missing from the clone of ...` with the remote and branch it cloned: either `VAULT_REMOTE`/`VAULT_BRANCH` point to the wrong repository, or the deploy key in `GIT_DEPLOY_KEY_B64` is not the remote's and the clone never happened.

**`/health` reports `indexed: 0`**: a problem only if search also returns nothing. An `index_loaded: false` with `indexed` above zero is normal after a deploy.

**The domain does not answer, or answers something that is not ours**: the server and `memory-cloud` publish no ports; all traffic enters through the platform's proxy. Check the domain's DNS record, the service's domain in the panel (service, internal port 8000 and certificate type) and the proxy logs. Ports of other deployments on the same host do not belong to this Compose file, which publishes none.

**`memory-cloud` cannot resolve `postgres`**: a different failure from the previous one, and not a consequence of it: if a port bind had failed, the process would not have started and there would be no log. The service logs carry:

```
memory: cloudstore: ping postgres: failed to connect to `user=memory database=memory_cloud`:
hostname resolving error: lookup postgres on 127.0.0.53:53:
read udp 127.0.0.1:32991->127.0.0.53:53: read: connection refused
```

`127.0.0.53` is the **host's** systemd-resolved stub, not Docker's embedded DNS (`127.0.0.11`). A container that is in no user-defined network falls into the default `bridge`, which has no service discovery and copies the host's `/etc/resolv.conf`: the name `postgres` is never asked of Docker, it goes to the host resolver, and that address is unreachable from inside the container. The symptom is not "postgres is down", it is "the name `postgres` does not exist in this container's network".

What to check, in order:

1. That the three services declare the same network in `infra/compose.yml`. The render confirms it: `docker compose -f infra/compose.yml --profile memory config` (with no secret values in that terminal's environment) must show `memory-net` under `networks:` in `knowledge-mcp`, `memory-cloud` and `postgres`, and a top-level `memory-net` entry.
2. That the network exists on the host and the three containers are in it: `docker network inspect <project>_memory-net`.
3. From the failing container, that the resolver is Docker's: `cat /etc/resolv.conf` must say `nameserver 127.0.0.11`. Any other address means the container ended up outside the declared network, and there no service name resolves.
4. If the network is fine, only then is it a postgres problem: `docker logs` of the service and the `pg_isready` health check.

`memory cloud serve` does not retry: it opens the pool, pings with a five-second limit and, if that fails, prints that line and exits with code 1. The container comes back through the restart policy, so a log that repeats every few seconds is the same failure over and over, not different failures. The startup safety net is `depends_on` with `condition: service_healthy`.

Why several Compose constructs behave differently on Dokploy: **Dokploy orchestrates with Docker Swarm**, which is why its troubleshooting docs talk about `docker service update --endpoint-mode dnsrr` and the `tasks.<app>` prefix, which are Swarm syntax. This stack, however, is deployed with `docker compose`, not `docker stack deploy`: the stack schema rejects `pull_policy` and the long form of `depends_on`, and ignores `build:`, so `infra/compose.yml` does not even parse that way. Two practical consequences: `restart`, `depends_on` with `condition: service_healthy` and `pull_policy` apply exactly as written; and the network, declared without `driver:`, is created as `bridge` (the same declaration under `docker stack deploy` would be `overlay`, which is precisely the reason not to pin the driver).

### Local memory stack (`infra/`)

`infra/compose.yml` with the `memory` profile starts, besides this MCP server (HTTP plus bearer), the authenticated replica of [memory](https://github.com/HoracioEspinosa/memory) (the fork with this team's cloud configuration, not the original project): `memory cloud serve` plus Postgres.

```bash
cd infra
docker compose --profile memory up -d --build
```

- The secrets live in the Compose environment or in an `infra/.env` file you create (gitignored): the Postgres password, `MEMORY_JWT_SECRET`, `MEMORY_CLOUD_TOKEN` / `MEMORY_CLOUD_ADMIN`, `MEMORY_CLOUD_TOKEN_PEPPER`, `MCP_API_KEY` and `MCP_ADMIN_KEY`. Without them the `memory` profile does not start; generate values with `openssl rand -hex 32`.
- The Compose file publishes no ports. To see the `memory-cloud` dashboard at `http://127.0.0.1:18080/dashboard`, publish that port (`127.0.0.1:18080:18080`) in an override.
- Postgres publishes no port to the host: it is reachable only inside the Compose network.
- The local memory binary remains the source of truth for memory (stdio MCP plus hooks); the container is its backup replica through `memory sync --cloud --project <p>`.
- The cloud image is `ghcr.io/horacioespinosa/memory:${MEMORY_IMAGE_TAG:-latest}` (a private package: pulling it requires a registry credential).

## Call telemetry

The server counts every `tools/call` it receives, including those that end in error (`isError`), in the `tool_calls` section of the default vault's `usage_stats.json`. Each count carries the UTC day, the tool name, the client (the `clientInfo.name` that the session's `initialize` sent, the name each client picks for itself) and the principal: the bearer id over HTTP (`service`, `service-read`, `admin` or the `id` of a `bearers` entry) and `stdio` over stdio. It never stores arguments, queries, paths or the bearer. Names are reduced to ASCII letters, digits, `.`, `_` and `-` (anything else becomes `_`); an empty name is stored as `unknown`, and beyond 64 tools, 32 clients per tool or 16 principals per client on the same day, new names count under `other`. Counts are kept for 120 days.

Counting changes no response and not the contract: if counting fails, the server logs it and the call goes on. Each process adds its own counts to what is already in the file, under the same lock as the judge's spending, so several processes on one vault do not trample each other. Counts are written every 20 usage events and when the process closes; a process that dies without closing loses the ones it had not written yet.

`doctor` reads them without writing anything, in `vaults[i].tool_calls` of the vault with `"default": true`:

| Key | What it carries |
|---|---|
| `last_7_days`, `last_30_days`, `kept` | `calls`, `errors`, and calls `by_tool`, `by_client` and `by_principal` over the last 7 and 30 UTC days and over everything kept |
| `first_day`, `last_day`, `days_kept` | First and last day with calls, and the retention (120) |
| `deprecated` | Calls, over everything kept, to a tool the contract marks `knowledge/deprecated` (`search_code`, retired in 2.0.0: under `all` its `E_TOOL_RETIRED` counts, under `legacy` its redirect) |
| `unregistered` | Calls to a name the contract does not define (a client with an old catalog, or a probe) |

The procedure to read them on a deployment is in [`docs/runbooks/serve-multiple-vaults.md`](docs/runbooks/serve-multiple-vaults.md), section "Reading per-tool calls".

## Tests and merge gates (ring 1)

Run locally (details, expected results and environment caveats in [CONTRIBUTING.md](CONTRIBUTING.md#gates)):

```bash
uv sync
uv run ruff check
uv run lint-imports
uv run pytest -q                                   # against tests/fixtures/, no network, no API key
KNOWLEDGE_REQUIRE_SQLITE_VEC=1 uv run pytest -q    # how ring 1 runs it
(cd npm && npm test)                               # the npm wrapper: node --test, no dependencies, no network
```

The suite never touches a real vault or the OpenAI API: it runs a real `SQLiteStore` in a temporary directory against the versioned fixtures, and embeds with the deterministic `bag-of-words` provider, which `tests/conftest.py` selects with `KNOWLEDGE_EMBEDDINGS_PROVIDER=bag-of-words`; nothing is patched. That is why no `OPENAI_API_KEY` is needed, and none must be added as a repository variable.

`.github/workflows/ring1.yml` is the ring 1 gate. It runs on Linux (`ubuntu-latest`) with three triggers:

| Event | Jobs |
|---|---|
| `pull_request` to `main` | lint and suite with coverage (plus the npm package's `node --test`, in the same job), import-linter, gitleaks over the PR's commits, Semgrep CE and patch coverage |
| `push` to `main` | the same except patch coverage (the diff against `origin/main` is empty), plus the Docker image built and tested on amd64 |
| `workflow_dispatch` | the same as on `push`, on the branch you choose: `gh workflow run ring1.yml --ref <branch>` |

The patch-coverage threshold lives in `pyproject.toml`, not in the YAML, so that the gate and your machine cannot disagree. The image does not run on pull requests: it is the slowest job, and a broken image shows up on the `push` to `main` and on any manual dispatch of the branch. There is no path filter: a required check that does not run leaves the PR waiting forever.

The `protect-main` ruleset requires on every PR the five checks that run on a `pull_request` (all but the image). Bypass, Actions minutes, rotating a read-only deploy key and rollback: [`docs/runbooks/ci-ring1.md`](docs/runbooks/ci-ring1.md).

The workflow has `permissions: contents: read`, reads no secret and pins each action by SHA with its tag in a comment; the repository admits only GitHub actions and requires the SHA pin. `tests/test_pipeline_contract.py` pins all of that: triggers, which jobs run on a PR, pins, permissions, and that ring 1 names no secret.

The weekly stale-documents report does not run here: each vault runs it in its own workflow, which fetches `scripts/weekly-stale-report.py` and `scripts/notify-stale-report.sh` from this repository, pinned by commit or tag. It is never a gate and never writes in the vault.

`.github/workflows/release-check.yml` verifies a release: it runs on every pushed `v*` tag and by hand. It reads no secrets or environments, and its token is read-only. It checks, in this order: that `pyproject.toml`, `plugin.json`, the `@v<version>` of `.mcp.json`, `npm/package.json` and `uv.lock` agree (`scripts/check-version.sh`, which also compares the tag when the ref is a tag; from a branch there is no tag and it compares only the files); that each `tests/golden/v*/SHA256SUMS` holds and that `test_contract_frozen.py`, `test_contract_golden.py` and `test_plugin_manifest.py` pass with `sqlite-vec` loaded; that `claude plugin validate --strict plugin/claude-code` passes (Node 22 and Claude Code `2.1.296`, pinned exactly); and that the sdist and the wheel build and carry only the package (`tests/test_sdist_contents.py`). The build runs on the runner; the prohibition of `uv build` is for the local machine. A dispatch uses the workflow of the ref you choose: `gh workflow run release-check.yml --ref main` works, a tag older than `release-check.yml` does not (it lacks the file). Whoever cuts the release waits for the tag run to go green before opening the marketplace pull request (`cloudfroghq/claude-plugins`).

`.github/workflows/publish.yml` publishes each `v*` tag to PyPI (`cloudfrog-knowledge`) and npm (`@cloudfrog/knowledge`). A first job, with no permission beyond reading the repository, compares the tag with every version, checks the `uv` checksums the npm package carries against the `uv` release and installs it with the package's own code, runs `node --test` and `npm pack --dry-run` in `npm/`, builds the sdist and the wheel, installs the wheel in a clean Python 3.12 environment, and requires `cloudfrog-knowledge --version` and `knowledge --version` to print the version. Only then does the second job, with the `release` environment and `id-token: write`, publish by trusted publishing (OIDC, no secrets): PyPI with `uv publish --trusted-publishing always` and npm with `npm publish --provenance --access public`. Each registry is skipped, with a warning, only if that exact version already exists; any other answer fails it. By hand, `gh workflow run publish.yml --ref main` builds and verifies without publishing. Operating it, the first manual `npm publish` and the trusted-publisher setup are in [`docs/runbooks/ci-ring1.md`](docs/runbooks/ci-ring1.md), section 7.

Two things to read carefully before trusting a green run:

- **Semgrep's zero does not mean "nothing to report".** One rule (`dynamic-urllib-use-detected`, on the Confluence client) still fires and is silenced with an audited comment. What removes the risk is the scheme allowlist of `confluence_sync._api_get`, pinned by its own tests. Every suppression in the code is declared in `tests/test_suppressions.py` with its reason and the test that backs it; a new undeclared one breaks the suite.
- **Semgrep scans only files tracked by git.** In a workflow run that is the whole tree; on your machine, with unadded work, it is not, and the count of scanned files will be lower.

The gates have their own canary battery, which breaks each gate on purpose and requires the exact failure; see [CONTRIBUTING.md](CONTRIBUTING.md#gate-canaries).

## Scripts

| Script | Description |
|---|---|
| `scripts/setup.sh` | Local setup: dependencies, `.env` and `migrate-index` (prepares the index without embedding; it fills on the first tool call). |
| `scripts/weekly-memory-bridge.sh` | Weekly bridge (launchd): exports memories from memory to `90 - Memory/`, replicates them to the cloud, and commits and pushes that folder in the vault's repository; the push webhook reindexes the server. `BRIDGE_DRY_RUN=1` rehearses without committing. |
| `scripts/weekly-stale-report.py` | Stale-documents report grouped by owner. No index and no OpenAI: it runs in CI. Honors the `index.exclude` of the `vault.yaml` of the vault it checks. |
| `scripts/notify-stale-report.sh` | Posts that report to the webhook in `STALE_REPORT_WEBHOOK_URL`. Exit `3` if a binary is missing: it never disguises that as a network failure. |
| `scripts/tests/notify-stale-report-canaries.sh` | Canary battery for the notification. Pins the four exit codes with exact equality. |
| `scripts/tests/ring1-gates-canaries.sh` | Canary battery for the ring 1 gates. Breaks each one on purpose and requires the exact failure. `--with-semgrep` adds the cases that use the pinned image. |
| `scripts/ci-install-uv.sh DIR` | Installs into `DIR` the `uv` pinned by `npm/lib/uv-release.json`, from its GitHub release and verified by SHA-256; `publish.yml` uses it. |
| `scripts/bump-version.sh X.Y.Z` / `scripts/check-version.sh [vX.Y.Z]` | Move and check the version in `pyproject.toml`, `plugin.json`, `.mcp.json`, `npm/package.json` and `uv.lock`. |
| `npm/scripts/uv-release.js <version>\|--check\|--smoke` | Regenerates the `uv` SHA-256 checksums the npm package carries from the release's `.sha256` files, compares them with the release, or installs `uv` with the package's code and runs `uv --version`. |

These scripts are for local stdio mode. On a server the index lives in the `knowledge-index` volume and is maintained through the webhook; there is no snapshot to export or import, because the index is rebuilt from the vault. The full guide is [`scripts/README.md`](scripts/README.md).

## Documentation

| Document | What it covers |
|---|---|
| [`docs/install.md`](docs/install.md) | Team installation: plugins, CLI channels, `init`, bearers, memory bridge, Jira, rerank judge, diagnostics |
| [`docs/mcp-user-scope-setup.md`](docs/mcp-user-scope-setup.md) | Configuring MCP servers at user scope |
| [`docs/runbooks/serve-multiple-vaults.md`](docs/runbooks/serve-multiple-vaults.md) | Serving several vaults, secrets per instance, the generic Compose file, errors |
| [`docs/runbooks/sync-jira.md`](docs/runbooks/sync-jira.md) | Syncing Jira issues as memory tasks |
| [`docs/runbooks/promote-confluence.md`](docs/runbooks/promote-confluence.md) | Promoting Confluence pages to the vault by PR |
| [`docs/runbooks/ci-ring1.md`](docs/runbooks/ci-ring1.md) | Operating ring 1, required checks, rollback, releases |
| [`docs/tools/inventory.md`](docs/tools/inventory.md) | Generated inventory of every tool (`uv run python scripts/tool_inventory.py`) |
| [`docs/contract/CHANGELOG.md`](docs/contract/CHANGELOG.md) | Tool contract history, version by version |
| [`docs/adr/`](docs/adr/) | Architecture decision records (ADR-039 to ADR-050) |
| [`docs/eval/README.md`](docs/eval/README.md) | Retrieval evaluation harness |
| [`docs/performance/improvement-plan.md`](docs/performance/improvement-plan.md) | Performance improvement plan |
| [`docs/architecture.mmd`](docs/architecture.mmd) | Architecture diagram source |
| [`scripts/README.md`](scripts/README.md) | Scripts guide |
| [`lab/README.md`](lab/README.md) | Dockerized lab with fake tenants and clients |
| [`plugin/claude-code/README.md`](plugin/claude-code/README.md) | The `knowledge` plugin for Claude Code |
| [`npm/README.md`](npm/README.md) | The `@cloudfrog/knowledge` npm wrapper |
| [`CHANGELOG.md`](CHANGELOG.md) | Package release history |

## Contributing, security and support

- [CONTRIBUTING.md](CONTRIBUTING.md): development setup, gates, contract rules, commit and PR flow, releases.
- [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)
- [SECURITY.md](SECURITY.md): how to report a vulnerability. Never open a public issue for one.
- [SUPPORT.md](SUPPORT.md): where to ask questions.
- Issues and pull requests use the templates in [`.github/ISSUE_TEMPLATE/`](.github/ISSUE_TEMPLATE/) and [`.github/PULL_REQUEST_TEMPLATE.md`](.github/PULL_REQUEST_TEMPLATE.md).

## License

The source is available under the [PolyForm Shield License 1.0.0](https://polyformproject.org/licenses/shield/1.0.0) ([`LICENSE`](LICENSE)). You may use, modify and redistribute it for any purpose except providing a product that competes with the software, or with any product the licensor provides using it. Anyone who receives a copy also receives the terms and the `Required Notice:` line of `LICENSE`. PolyForm Shield is a source-available license; it is not an OSI-approved open-source license.
