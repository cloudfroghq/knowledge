# knowledge

## What it is

A dynamic memory base reusable across several projects: an MCP server that exposes curated knowledge (architecture, flows, runbooks, API contracts) as a tool contract whose catalog depends on the profile and on the `features` of the served vaults (the 10 core tools always; `get_flow` and `compare_flows` with `flows`, `get_stale_docs` and `suggest_links` with `governance`, `sync_confluence` with `confluence`, and `sync_jira` with `jira`, over stdio; the `legacy` profile serves the 16 names of 1.4.0 for one release), backed by an Obsidian vault. The server is not tied to any project: the vaults it serves are chosen by `KNOWLEDGE_VAULTS` (a `vaults.yaml` with several vaults and one default) or, instead, by `VAULT_PATH` (a single vault, with no default value; with neither of the two, the user's `vaults.yaml` is served and, without it, startup fails with `E_VAULT_PATH_MISSING`), and the checkouts it can resolve are declared by each vault in `Contexts/config.json`, next to its context packages (`Contexts/<context>/`; ADR-047). It is the domain source for the packages that consume it: nothing is re-derived by reading another service's code when this server can answer it.

## Stack

- Python 3.12 (`requires-python = ">=3.12,<3.13"` in `pyproject.toml` and `.python-version` with 3.12 at the root), `uv` as environment manager and lockfile. The distribution is called `cloudfrog-knowledge`; the importable package is still `knowledge`. It is published on PyPI (`cloudfrog-knowledge`), on npm (`@cloudfrog/knowledge`, the wrapper in `npm/`) and as a git tag, with the same version (ADR-050), and it installs two console scripts that are the same `knowledge.cli:main`: `cloudfrog-knowledge` and `knowledge`.
- MCP server (FastMCP) over HTTP or stdio, in 35 flat modules inside `src/knowledge`:
  - `server`: the 10 core tools; the extension tools that some served vault declares; `KnowledgeMCP`, the FastMCP subclass that counts every `tools/call` in the default vault without touching the response and answers `E_TOOL_RETIRED` or `E_FEATURE_DISABLED` for a tool the catalog does not list; startup; and the package's only `importlib.import_module`, which loads each extension the first time it is needed.
  - `cli`: subcommands `serve`, `eval`, `vault-lint`, `doctor` (with `--shape`, the size distributions of the default vault), `init`, `migrate-index`, `confluence export`, `memory-bridge sync|promote` and `jira sync`; it imports `server` only inside the subcommands that serve or load an extension, so `--version`, `doctor`, `init` and `vault-lint` never load the MCP SDK.
  - `profiles`: tool profiles: `all`, the usual one, and `legacy`, selected with `serve --profile` or `KNOWLEDGE_PROFILE`; `core` and `ext` are internal.
  - `vault_lint`: review of frontmatter, wikilinks and vault rules in two sets, `1.2` (the one `reindex.validation` counts) and `1.3`; it only reads, except for `fix`, the only function that writes: it moves a date to `last_updated`.
  - `indexer`: frontmatter and index over injected ports; it writes each embedding batch with one `replace_files` call, and the keyword `idf` comes from a vocabulary of whitespace runs, built lazily on the first query after each reindex and memoized per term, exactly equal to the substring definition.
  - `chunker`: splits each body into chunks of 2,500 characters by `## ` sections with 10% overlap, with one `ChunkPlan` per provider bounded by its input limit.
  - `ports`: the protocols `EmbeddingProvider`, `VectorStore` (with `replace_files` and `documents(flt, with_body=...)`), `LexicalIndex`, `VaultSource`, `VaultSync`, `MemoryClient` and the `NullMemoryClient`, and `Judge` with `JudgeUnit`, `JudgeVerdict`, `JudgeUnavailable` and the `NullJudge`; the closed filter `ChunkFilter` and the records `DocRecord`, `ChunkRecord` and `ChunkHit`.
  - `embeddings`: the `openai`, `none` and `bag-of-words` providers, and the query cache.
  - `stores`: `SQLiteStore`: one `index.sqlite` per vault with FTS5 and `sqlite-vec`, with a standard-library fallback if the extension does not load; an embedding batch is one transaction (a store error rolls back the whole batch), vectors are deleted by id, every connection maps the file with `PRAGMA mmap_size` (256 MiB, `MMAP_SIZE`, whose comment records the SIGBUS and RSS trade-off), and `documents(with_body=False)` serves the listings that never read a body; `InMemoryVectorStore`, the same port in memory with FTS5-style BM25.
  - `vault_source`: `DirectoryVaultSource`, with injected sync; it walks the `*.md` files outside any path segment that starts with `.`, the same rule as `vault-lint`, so `Contexts/<app>/CLAUDE.md` is indexed and `Contexts/<app>/.claude/**` is not.
  - `graph`: Hebbian weight, decay, relations; `link_graph.json` is compact JSON (the loader also reads the old indented form), and the server saves learned weights on a single `graph-save` worker, coalesced per runtime, never on the request thread.
  - `usage`: usage per document, co-retrieval pairs and reads per search; a `tool_calls` section with the calls per UTC day, tool, client (`clientInfo.name`) and principal, kept for 120 days, summed under a cross-process lock, never arguments; a `tool_latency` section with a per-tool duration histogram per UTC day (fixed buckets from 1 ms to 5 s, the same 120 days; an older binary that rewrites the file drops it), whose p50/p95 `doctor` reports; one read of the file per write; the registry's trackers write on a `usage-flush` daemon thread every 2 s (and on close and at exit), so a hard crash can lose up to about 2 s of counts; a `rerank` section with the judge's aggregates, the day's spend that all of the vault's processes share, and the searches per day.
  - `settings`: explicit configuration: importing it does not read `.env` or `config.json` and does not create directories; `validate()` requires only what the provider and the transport use.
  - `errors`: stable error codes.
  - `config`: compatibility facade over `settings`.
  - `registry`: `VaultRegistry`: one runtime per registered vault, opened only when requested, which composes source, store, embedding provider, usage, memory and judge with its own sidecar, plus the judge's cache and circuit breaker.
  - `graph_targets`: graphify targets via the registry.
  - `extensions`: declarative registry of the extensions that a vault enables with `features`: tools and routes, module as text, credential variables, transports that load it.
  - `principals`: who each HTTP bearer is and with which scopes, `read` or `admin`, according to `MCP_API_KEY`, `MCP_ADMIN_KEY` or `bearers` in `vaults.yaml`; it compares each token against every entry in constant time.
  - `flows`: `get_flow` and `compare_flows`, loaded on first use.
  - `repos`: checkout registry, standard library only: what each checkout is comes from `<vault>/Contexts/config.json`; where it is comes from `~/.config/knowledge/<slug>/repos.local.json` or from `KNOWLEDGE_<ROOT>_ROOT`/`KNOWLEDGE_REPO_<ID>`, with `CD_*` as deprecated aliases; it never reads `vaults.yaml`.
  - `contexts`: `init`: locates each vault's roots, decides which checkouts receive a package on this machine (the `apply` list in `repos.local.json`, ADR-049), writes that file and installs the `Contexts/<context>/` packages into the selected checkouts that are present, by link or copy, never writing inside a vault.
  - `auth`: middleware that resolves each bearer to its principal and leaves its scopes on the request.
  - `gitsync`.
  - `webhook`: `/health` always; `/webhook/reindex` only if the default vault declares `repo` or `VAULT_REMOTE`, and with a manual bearer only for the `admin` scope.
  - `confluence_sync`: the read-only `sync_confluence` and `confluence export`, which writes outside every vault.
  - `review`.
  - `contract`: contract version and `TOOL_SINCE`.
  - `pointers`: `doc_id`, `chunk_id` and section selector.
  - `memory_bridge`: the writes of the `memory-bridge` extension to the local memory: card, tasks and rescan of `Runbooks/`, loopback only, with a bearer unless `KNOWLEDGE_MEMORY_REQUIRE_TOKEN=0`; and the wrapper for `memory project <slug> promote list|stamp`.
  - `jira_client`: read-only Jira Cloud JQL search, `GET /rest/api/3/search/jql` paginated with `nextPageToken`.
  - `jira_sync`: the `jira` extension: issues from `integrations.jira` as memory tasks keyed by `jira_key`, cursor in `jira_sync.json`; loaded on first use.
  - `memory_http`: read-only client for the memory server, standard library only.
  - `rerank`: the network-free reranking of `search`: per-document pointer blocks, token estimator, per-query and per-day budgets, LRU cache with TTL, circuit breaker and the bounded fusion `round(hybrid · boost · (0.8 + 0.5 · p), 2)`; it receives the already-built judge and the query's idf.
  - `judges`: the `rerank` extension: the System One client for `jev` on TypeSafe and `kev` on loopback, standard library, one persistent connection per thread and a total deadline; loaded only for a vault that declares a judge.
  - plus `__init__` and `__main__`.
- SQLite as the store: one `index.sqlite` per vault (documents, chunks, FTS5 `unicode61 remove_diacritics 2` and float32 vectors that `sqlite-vec==0.1.9` queries by KNN with `k = ?`). `chromadb` is not a dependency: `migrate-index` takes only `usage_stats.json` and the weights in `link_graph.json` from a Chroma directory, without opening its database. There are no Testcontainers: the suite uses a real `SQLiteStore` in a temporary directory and embeds with the `bag-of-words` provider (`knowledge.embeddings.BagOfWordsEmbeddings`, a deterministic bag of words over a fixed vocabulary), which `tests/conftest.py` and `tests/golden_stdio_server.py` select with `KNOWLEDGE_EMBEDDINGS_PROVIDER=bag-of-words`; nothing is patched.
- Where a vault's index lives: with `vaults.yaml`, `<data_dir>/<slug>`; with `VAULT_PATH` (the slug is `CHROMA_COLLECTION`, then the `slug` in the vault's `vault.yaml` and, with neither, `vault`), the first of `KNOWLEDGE_INDEX_DIR`, `CHROMA_PERSIST_DIR` (it remains a destination on purpose: the image sets it to `/data/chroma`, and a deployment whose compose file is not here may mount that directory), `<KNOWLEDGE_HOME>/<slug>` (only with an explicit `KNOWLEDGE_HOME`) and `<root>/.data/<slug>`. Next to `index.sqlite` live `link_graph.json`, `usage_stats.json`, `index_meta.json` and `skipped_files.json`.
- `sqlite-vec` needs a Python with loadable extensions (`uv`'s CPython has them; the python.org build for macOS does not). Without it, the server ranks the vectors with the standard library (`vector_backend: python`, about 110 ms per query with 1,480 vectors of 3,072 dimensions versus about 10 ms): same result except for exact ties at the `k` limit. `KNOWLEDGE_REQUIRE_SQLITE_VEC=1` turns that fallback into `E_SQLITE_VEC_UNAVAILABLE`; ring1 and the golden recording set it, and the plugin launches the server with `uvx --managed-python`.
- No vault lives in this repo: each one has its own repository, and its clones live wherever each machine keeps them (for example `~/vaults/<slug>`), declared in `~/.config/knowledge/vaults.yaml`. The `guard-vault-write.sh` hook protects the clones under `KNOWLEDGE_VAULTS_ROOT`, whose default is `~/Projects/Vaults`: export it if your clones live elsewhere. A vault is content, not code. It is edited by human PR, never by the agent (D-11). The server only knows directories (`VAULT_PATH` or the `path` entries in `vaults.yaml`); whether a directory lives in a git repo is a hosting decision, not a server requirement: git only runs for a vault that declares `repo` in `vaults.yaml` or, with `VAULT_PATH`, `VAULT_REMOTE`.

## Verified commands

```bash
uv sync                     # environment
uv run pytest                # full suite (unit + tool + contract)
KNOWLEDGE_REQUIRE_SQLITE_VEC=1 uv run pytest   # same, but fails if sqlite-vec does not load in this Python (this is how ring1 runs)
uv run ruff check             # lint
uv run lint-imports            # core/ext/http layer contracts from .importlinter (import-linter)
uv build --out-dir "$(mktemp -d)"   # packaging check: sdist + wheel of cloudfrog-knowledge (src/knowledge only) in a temporary directory outside the repo; never commit dist/ and never upload a local build
uv run cloudfrog-knowledge     # entry point ([project.scripts] -> knowledge.cli:main); with no subcommand it is equivalent to `serve`
uv run knowledge --version     # the `knowledge` alias: same entry point; usage, --version and hints name the command that ran
(cd npm && npm test)           # the npm wrapper (@cloudfrog/knowledge): node --test, no dependencies, no network
uv run cloudfrog-knowledge serve --transport http --vaults vaults.yaml [--profile all|legacy]   # server; configuration errors: `<code>: <message>`, rc 2 (`E_PROFILE_UNKNOWN` with any other profile)
uv run cloudfrog-knowledge vault-lint PATH [--rules 1.2|1.3] [--fix [--out DIR]]   # JSON on stdout; 1.3 rules by default; read-only except --fix (only with 1.3; never on a served vault, E_WRITE_FENCED; --out fixes a new copy); rc 0 without errors, 1 with errors, 2 if PATH is not a directory or the write is fenced
uv run cloudfrog-knowledge doctor [--brief] [--client]     # read-only; JSON (or one line of ≤ 200 bytes) with the rerank block of every vault that declares a judge (names, never values), the tool_calls block of the default vault (with latency_ms, calls and p50/p95 per tool, once some call was timed) and the contexts block of every vault with Contexts/config.json; --client checks only the client side (readable vault, Contexts/config.json, repos.local.json and each checkout per an init --dry-run without scanning) and skips embeddings, index, vector_backend, judge, memory and bearers; rc 0 healthy (also with init pending: warning W_CONTEXTS_INIT_PENDING —over stdio, or always with --client— and the short line "init pending … run: <init>"), 1 with problems (with --client, E_CONTEXTS_CONFLICT or E_CONTEXTS_CHECKOUT_INVALID per checkout; a git checkout without origin is the warning W_CONTEXTS_CHECKOUT_UNVERIFIED, rc 0; a present checkout outside `apply` is `not-selected`, informational; a linked one outside `apply` is W_CONTEXTS_LINKED_NOT_SELECTED and a repos.local.json without `apply` is W_CONTEXTS_SELECTION_MISSING, both rc 0), 2 if the configuration does not load; hints name KNOWLEDGE_COMMAND_LABEL ("Contexts/scripts/{command}.sh") or, without it, the command that ran (cloudfrog-knowledge or knowledge)
uv run cloudfrog-knowledge doctor --shape [--vaults FILE]   # read-only; JSON of aggregate numbers only for the default vault (shape_version, docs, body_chars, chunks/sections/links per doc, type_counts over the contract's types with the rest under other, query_term_df p50/p90/p99 or null), opened immutable; it feeds Shape.from_export of tests/synthetic_vault.py; rc 2 on E_ARGUMENT_CONFLICT (with --brief or --client) or E_INDEX_UNREADABLE
uv run cloudfrog-knowledge init [--vault SLUG] [--root [SLUG:]NAME=DIR] [--search-dir DIR] [--repo [SLUG:]ID] [--all] [--drop [SLUG:]ID] [--yes] [--dry-run] [--json] [--force|--refresh]   # client: for each vault with Contexts/config.json, writes ~/.config/knowledge/<slug>/repos.local.json and links Contexts/<context>/ into each present checkout in its `apply` list (ADR-049: --repo adds, --all adds the present ones that have a package, --drop removes the id and deletes only its links to the package; a file without `apply` adopts what is already installed; a new machine without flags asks and, with --yes or without a terminal, installs nothing and lists the present ones with the --repo hint; `apply` touches neither the graph nor the server) (link is the only recommended mode; copy remains for compatibility), skips the absent ones; a real .claude in the checkout receives one link per package entry (and per entry of a versioned .claude/<collection>/), and what the checkout versions in git is kept without conflict (ADR-048); never writes into a vault (E_WRITE_FENCED); idempotent; rc 0, 1 with a conflict or an unreadable vault, 2 on E_ROOT_UNKNOWN, E_CONTEXTS_REPO_UNKNOWN or configuration
uv run cloudfrog-knowledge migrate-index [--vaults FILE] [--vault SLUG] [--from-chroma DIR] [--build] [--prune-chroma]   # prepares index.sqlite and copies the Chroma sidecars; one JSON line per vault; rc 2 on E_INDEX_VERSION_NEWER, E_INDEX_UNREADABLE or E_PRUNE_REFUSED
uv run cloudfrog-knowledge memory-bridge sync [--vault SLUG] [--card]   # card and rescan of Runbooks/ in the local memory (loopback); JSON; rc 1 if memory rejects a step, 2 on E_FEATURE_DISABLED, E_MEMORY_PROJECT_MISSING, E_MEMORY_TOKEN_MISSING or E_MEMORY_URL_NOT_LOOPBACK
uv run cloudfrog-knowledge memory-bridge promote list|stamp [--vault SLUG] ...      # wraps `memory project <slug> promote list|stamp --json`; does not write to the vault
uv run cloudfrog-knowledge jira sync [--vault SLUG] [--since ISO|-Nd] [--dry-run] [--max-issues N] [--json] [--auto]   # Jira issues as tasks in the local memory (loopback); rc 1 on E_JIRA_* or memory going down midway, 2 if it could not be attempted; --auto always 0
uv run cloudfrog-knowledge confluence export [--vault SLUG] [--out DIR] [--dry-run]   # Confluence pages newer than the vault, written outside every vault; JSON; rc 1 with errors, 2 on E_WRITE_FENCED or E_SECRET_MISSING
uv run cloudfrog-knowledge eval --help          # retrieval evaluation (only in a checkout with scripts/)
uv run python -m knowledge.server               # current alias (knowledge.server:main)
uv run mypy src/knowledge      # types; it is in the dev group but is NOT a pipeline gate
uv run --with pyperf==2.10.0 python -m benchmarks run --docs 500,5000   # wall-clock harness over generated vaults (bag-of-words, no network); results in .data/bench/, never committed; pyperf optional, never in uv.lock
uv run python -m benchmarks compare BASE.json HEAD.json        # delta table of two runs; how to run the base from a git archive: docs/performance/benchmarks.md
```

Pipelines and gates:

- The pipeline gates (`.github/workflows/ring1.yml`) run on Linux on every pull request to `main` (lint and suite with coverage —`ruff`, `pytest` and the `node --test` of `npm/`—, `lint-imports`, gitleaks over the PR's commits, Semgrep CE and diff-coverage), on every push to `main` (the same ones except diff-coverage, plus the image built and tested on amd64) and by hand with `workflow_dispatch`; the details are in `CONTRIBUTING.md`, section "What CI runs", and operating CI and releases in `docs/runbooks/ci-ring1.md`.
- The weekly stale-documents report does not run here: each vault runs it in its own workflow, which fetches `scripts/weekly-stale-report.py` and `scripts/notify-stale-report.sh` from this repository.
- `release-check.yml` runs on every `v*` tag and by hand (`gh workflow run release-check.yml --ref main`): coherent versions (`scripts/check-version.sh`, with the tag only if the ref is a tag), frozen goldens, the plugin with `claude plugin validate --strict`, sdist and wheel; no secrets and no environment, and its green is what is expected before the marketplace pull request on every release. A local `uv build --out-dir <temporary dir outside the repo>` is fine as a packaging check, but `dist/` is never committed and a locally built artifact is never uploaded: every published sdist and wheel is built and published only by `publish.yml` on the runner, and `release-check.yml` checks them.
- `publish.yml` runs on every `v*` tag and by hand (`dry_run` by default): a read-only job verifies versions, the `uv` checksums of the npm wrapper, the wheel and the two console scripts, and only the job with the `release` environment and `id-token: write` publishes to PyPI and npm through trusted publishing, skipping a registry only if that version already exists there; the first npm version is published by hand (`docs/runbooks/ci-ring1.md`, section 7).
- Actions allows only GitHub-authored actions, pinned by SHA with their tag in a comment; ring1 reads no secret.
- The `protect-main` ruleset requires the five checks that run on a PR (all except the image); bypass, minutes, rotation of a read-only deploy key and rollback are in `docs/runbooks/ci-ring1.md`.
- `mypy` remains a development tool: no workflow runs it.

## Tool status (contract 2.0.0; catalog by profile and `features`)

Contract 2.0.0: `initialize` (the `instructions` start with `knowledge-contract: 2.0.0`), `tools/list` (no tool has an `outputSchema` by default), `search` (rows with `file_path`, `score`, `chunk_id` and `section`, no `excerpt`, no `structuredContent`, and with `_meta["knowledge/rerank"]` when the vault declares a judge), `get_document(chunk_id=…, max_chars=800)`, `search_code` (`E_TOOL_RETIRED`), `search_by_metadata(type="runbook", limit=3)` (`{total, offset, items}`) and `reindex` with a read-only bearer (`E_SCOPE_REQUIRED`). All of them are covered by the goldens in `tests/golden/v2.0.0/`, frozen with their `SHA256SUMS` and recorded in six sessions on the test vault: the main one (profile `all`, 10 tools), `structured` (the same catalog with `outputSchema`), `features` (`flows`, `governance` and `confluence`, 15), `legacy` (the 16 names of 1.4.0), `jira` (11) and the judge's session. None is documented as broken.

| Profile and served `features` | Tools in `tools/list` |
|---|---|
| `all` (default), no vault with `features` | the 10 core tools |
| `all`, `governance` | 12: the core, `get_stale_docs` and `suggest_links` |
| `all`, `flows` and `governance` | 14: the core, `get_flow`, `compare_flows`, `get_stale_docs` and `suggest_links` |
| `all`, `flows`, `governance` and `confluence` | 15: the above plus `sync_confluence` |
| `all` over stdio with a vault that declares `jira` | `sync_jira` at the end, in addition to whichever of the above apply |
| `legacy` (`serve --profile legacy` or `KNOWLEDGE_PROFILE=legacy`, for one release) | the 16 names of 1.4.0 in their order, `search_code` included, and the extension tools without looking at `features`; `sync_jira` remains conditional on `jira` |

| Tool | Status |
|---|---|
| `get_document`, `search`, `search_by_metadata`, `search_deep`, `find_path`, `get_related`, `get_impact`, `reindex`, `context_pack`, `list_vaults` | the core: registered in every process, in that order. Contract 2.0.0 (released with `cloudfrog-knowledge` 0.6.0, `docs/adr/ADR-046.md`): the `search` and `search_deep` row carries `chunk_id` and `section`, no `excerpt`; `get_document` reads 3,000 characters, with allowlisted metadata unless `full_metadata=true`, and accepts `chunk_id` without `file_path`; `search_by_metadata` answers `{total, offset, items}` 20 at a time; `get_related` merges both directions in `both`; every `limit` has a cap; every error is `isError` with an `E_` code |
| `get_flow`, `compare_flows`, `get_stale_docs`, `suggest_links`, `sync_confluence` | extension tools: registered only when some served vault declares `flows`, `governance` or `confluence` in `features`; without it, the call answers `E_FEATURE_DISABLED`, also on a vault that does not declare it in a process with several vaults, and the message names the vaults that do declare it. |
| `reindex`, `sync_confluence` | available, administrative: `reindex` mutates the index and runs `git pull` only with `pull=true` (the webhook pulls the vault on every push); `sync_confluence` is read-only, answers `E_WRITE_FENCED` with `dry_run=False` (ADR-044) and `E_SECRET_MISSING` without credentials; over HTTP they require the `admin` scope and answer `E_SCOPE_REQUIRED` to a read-only bearer; without `MCP_ADMIN_KEY` or `bearers`, `MCP_API_KEY` has both scopes, as in 1.2.0; over stdio no bearer is checked; `reindex` only accepts `target=vault` (ADR-039 retires `code` and `all`) |
| `search_by_metadata` | available: the `tag` filter returns results since 9bd99ff; an index built without that commit needs `reindex(force=true, pull=true)` once (the tool runs `git pull` only with `pull=true`; the webhook does it on every push) |
| `search_deep` | available: the coroutine is awaited since 506bc20 |
| `compare_flows` | available: fixed in 506bc20 (together with `search_deep`), coverage in ccae767; with a provider that has no flow file it answers `isError` with `E_FLOW_NOT_FOUND` and names every missing one |
| `search`, `search_deep`, `context_pack` | contract 1.2.0, released with `cloudfrog-knowledge` 0.3.0 (store change, `docs/adr/ADR-043.md`): `score`, row order and row membership come from per-chunk FTS5 BM25 and exact `sqlite-vec` KNN; keys and parameters do not change, and the rest of the tools return the same values as in 1.1.0 |
| `context_pack`, `list_vaults` | new in 1.1.0 (released with `cloudfrog-knowledge` 0.2.0), covered only by the golden and the suite. `list_vaults` takes no `vault` and opens no index. `context_pack` reads the memory server only with `include_memories=true`, `MEMORY_HTTP_TOKEN` and a loopback `KNOWLEDGE_MEMORY_URL`; if it cannot, it answers with the vault and `degraded` |
| `search(rerank=...)` | new in 1.4.0 (released with `cloudfrog-knowledge` 0.5.0): `rerank='on'` reranks with the judge that the `rerank` block of `vault.yaml` declares (`jev` or `kev`), `'off'` never does, and when omitted it follows `rerank.default`; rows carry `rerank_score`, with a summary in `_meta["knowledge/rerank"]`. Without `rerank` or `default: on`, the response is byte-for-byte that of 1.3.0. A vault enables it by default with `default: on` and `provider: jev`, and each vault brings its own key `<SLUG>_TYPESAFE_API_KEY`; without it the vault searches without a judge (`reason: key_missing`). Covered by `tests/test_rerank.py`, `tests/test_search_rerank.py` and the goldens session with a `kev` judge on loopback |
| `sync_jira` | new in 1.3.0 (released with `cloudfrog-knowledge` 0.4.0), administrative, conditional: it appears at the end of the catalog only when some served vault declares `jira`, and only over stdio (an HTTP deployment does not load it). It copies the issues from `integrations.jira` as memory tasks keyed by `jira_key`; `dry_run=True` by default. Covered by the golden of the session with `jira` (`tools_list__jira`, `sync_jira__unconfigured`) and by `tests/test_jira_sync.py` against a fake Jira and a fake memory on loopback. Procedure: `docs/runbooks/sync-jira.md` |
| `search_code` | **removed from the `all` profile in 2.0.0** (ADR-039, ADR-046): calling it answers `isError` with `E_TOOL_RETIRED` and the redirect to `graphify explain <symbol>`/`graphify affected <file>` for relations and impact, `graphify query <symbol>` only with a real symbol name, `rg` for literal text and `search`/`search_deep` for intent. Only the `legacy` profile registers it, for one more release |

If a tool is ever documented as broken, repairing it is the job of the `repair-mcp-tool` skill: the tool is not touched without its regression test failing (red) first, with the parameters documented as failing.

## Structure

- `src/knowledge/*.py`: 35 flat modules (no subpackages), in three groups that `.importlinter` enforces without moving them into directories:
  - **core** (`errors`, `ports`, `contract`, `settings`, `config`, `graph`, `usage`, `profiles`, `pointers`, `chunker`, `principals`, `extensions`, `embeddings`, `stores`, `vault_source`, `indexer`, `vault_lint`, `rerank`, `registry`): indexing and querying vaults, with no HTTP and no MCP;
  - **ext** (`repos`, `gitsync`, `memory_http`, `memory_bridge`, `review`, `confluence_sync`, `graph_targets`, `webhook`, `flows`, `jira_client`, `jira_sync`, `judges`, `contexts`): optional integrations;
  - **http** (`auth`, `server`, `cli`).
  - Four contracts:
    - `layers`, the full top-to-bottom order `cli`; `server`; `auth | webhook | graph_targets | jira_sync`; `confluence_sync | review | gitsync | memory_http | memory_bridge | jira_client | flows | judges | contexts`; `registry`; `indexer | vault_lint | rerank`; `embeddings | stores | vault_source | usage | graph | profiles | pointers | chunker | principals | extensions`; `config`; `settings`; `ports | errors | repos | contract` (a level imports neither upwards nor between siblings);
    - `core-isolated`, the core imports neither ext nor http;
    - `ext-below-http`, ext does not import http;
    - and `core-no-transport`, the core imports neither `mcp`, `starlette` nor `uvicorn`.
  - A name in parentheses is a module that the order reserves and that does not exist yet (import-linter treats it as optional); `tests/test_layered_architecture.py` requires removing the parentheses when the file appears, and `tests/test_dynamic_imports.py` forbids `importlib` in the core and `import_module` outside `server`.
  - `repos` is ext but sits at the bottom of the order because it imports nothing internal and runs as `python3 -m knowledge.repos` without the virtual environment.
- The content of each vault lives in its own repository, not here; `.gitignore` excludes `/vault/` so that no vault gets back in. In each vault repo, `90 - Memory/` is the only folder the automatic bridge writes to (`scripts/weekly-memory-bridge.sh` commits and pushes in the vault's repository, never in this one). The evaluation sets and their measurements live in each vault's `Eval/`; what stays here is the harness guide (`docs/eval/README.md`) and the fixture's set.
- `plugin/claude-code/`: the `knowledge` Claude Code plugin that the `cloudfroghq` marketplace publishes (manifest with `userConfig`, a `.mcp.json` that launches `uvx --managed-python --python 3.12 --from cloudfrog-knowledge==<version> cloudfrog-knowledge serve --transport stdio` from PyPI, so the marketplace pull request of a version waits until `publish.yml` has published it, a `SessionStart` hook that runs `doctor --brief` and stays silent if everything is fine, the skills `search-first`, `repair-mcp-tool` and `code-question`). Its version, the one in `pyproject.toml` and the `==<version>` are moved with `scripts/bump-version.sh` and checked with `scripts/check-version.sh`; `tests/test_plugin_manifest.py` covers it. Per-team installation: `docs/install.md`.
- `npm/`: the `@cloudfrog/knowledge` package, with no dependencies, Node ≥ 18: `knowledge` and `cloudfrog-knowledge` binaries that download a pinned version of `uv` (SHA-256 sums in `npm/lib/uv-release.json`, generated with `npm/scripts/uv-release.js`; they are never looked up at run time), verify it, store it in `~/.cache/cloudfrog-knowledge/uv/<version>/` (`%LOCALAPPDATA%` on Windows) and run `uv tool run --managed-python --python 3.12 --from cloudfrog-knowledge==<its version> <command>`; `CLOUDFROG_KNOWLEDGE_UV` uses your own `uv`. Its version is moved by `bump-version.sh` and `check-version.sh`; `tests/test_npm_package.py` requires the same license, the same `uv` as the workflows and the Python that `requires-python` declares.
- `benchmarks/`: the wall-clock harness (`python -m benchmarks run|compare|info`; generated vaults, in-process tools, index, usage and start-up scenarios), not packaged; how to run it, the result schema and the SLOs: `docs/performance/benchmarks.md`. Work is fenced without a clock by `tests/perf_counters.py` (SQL statements and rows through `SQLiteStore.observe`, file opens through an audit hook, calls through `sys.monitoring`, YAML parses) over `tests/synthetic_vault.py` (a byte-deterministic generated vault): `tests/test_perf_budgets.py` holds the baseline budgets with a `# TIGHTEN (<item>)` marker where a roadmap item will lower them, and `tests/test_perf_budget_{store,request,cli}.py` the budgets of each phase 1 change.
- `tests/fixtures/vault/`: test vault that reproduces the hard cases (hub without `depends_on`, runbook with an invalid `status`, wikilink with an old prefix); no test uses the real vault.
- `tests/fixtures/vaults/`: small vaults with their `vault.yaml`: `alpha` and `beta` for the multi-vault registry and its isolation; `links`, with wikilinks by path, alias, heading and repeated name, for the `graph.py` resolver; and `contexts`, with `Contexts/config.json` (two roots, three applications) and one package per application, for the registry, `init`, `doctor` and the indexer's dot-directory rule; the golden does not read them. `tests/fixtures/config.json` is the suite's static configuration (review thresholds, memory) and `tests/fixtures/eval/queries.yaml` the fixture's evaluation set.
- There is no `schema/` and no frontmatter validator in the indexer: frontmatter is read, not validated.
- Contributor files at the repo root: `CONTRIBUTING.md`, `SECURITY.md` and `CODE_OF_CONDUCT.md`; the performance improvement plan is `docs/performance/improvement-plan.md`.

## Conventions

- `file_path` is **always** returned without a prefix and without wikilink brackets.
- No response shape changes without declaring it as a contract change and without regenerating the server's tool inventory, `docs/tools/inventory.md` (`uv run python scripts/tool_inventory.py`).
- The contract declares its version in `src/knowledge/contract.py` (`CONTRACT_VERSION`, today `2.0.0`, released with `cloudfrog-knowledge` 0.6.0 and unchanged in 0.7.0, 0.8.0 and 0.9.0; `1.4.0` shipped with 0.5.0, `1.3.0` with 0.4.0 and `1.2.0` with 0.3.0; `TOOL_SINCE` says since which version each tool exists) and its history in `docs/contract/CHANGELOG.md`.
  - The golden of each version lives in `tests/golden/v<version>/`; a directory with `SHA256SUMS` is frozen (`v1.0.0`, `v1.1.0`, `v1.2.0`, `v1.3.0`, `v1.4.0` and `v2.0.0`) and `tests/test_contract_frozen.py` fails if a single byte changes, on any Python, whether `sqlite-vec` loads or not.
  - The server announces the version on the first line of the `initialize` `instructions` (`knowledge-contract: <version>`), in `list_vaults` (`contract`) and in `GET /health` (`contract_version`, next to `index_version` and `vector_backend`); `serverInfo.version` is still the `mcp` SDK's, and `serverInfo.icons` carries the CloudFrog mark (`src/knowledge/icon.svg`) as a `data:` URI, outside the contract and the goldens.
  - Every ordering a tool emits is contract: no golden depends on `PYTHONHASHSEED`.
  - The text blocks are the canonical response; `structuredContent` and `outputSchema` only travel with `KNOWLEDGE_STRUCTURED_OUTPUT=1`.
- Write fence: the server does not write to the vault outside `90 - Memory/` (ADR-044). `sync_confluence(dry_run=False)` and `confluence_sync.sync()` answer `E_WRITE_FENCED` without reading credentials; `cloudfrog-knowledge confluence export` writes only outside every served vault, and promotion is a human PR (`docs/runbooks/promote-confluence.md`). `tests/test_write_fence.py` calls every tool and the export on a copy of the fixture and requires the tree to be intact.
- `include_memories=false` is the default; the fence is tested, not assumed.
- The server only reads memory (`GET /search`, with `Authorization: Bearer` and `X-Memory-Client`, 1 s timeout) and never without `MEMORY_HTTP_TOKEN`. The suite sets `MEMORY_HTTP_TOKEN` and `KNOWLEDGE_MEMORY_URL` to empty and fails on any connection to port 6366: the memory tests use a fake server on an ephemeral port.
- Errors are returned with a stable code and an actionable message, never with a Python traceback, a bearer or an OpenAI key.

## Default methodology (D-05)

| Work type | Route | Gate |
|---|---|---|
| New tool or response-shape change | Full SDD + RDD High | Schema test + the RFC's contract suite |
| Repair of a broken tool | SDD-lite; regression test in red with the exact input that fails today | The input documented as failing returns a result |
| Indexing bugfix | Direct (≤3 files); reproduce with `tests/fixtures/vault/`, never with the real vault | Indexer suite |
| Vault change (content) | Human PR with the owner as reviewer (D-11) | Frontmatter validated + wikilinks resolved + Mermaid rendered |
| Layer refactor | SDD + characterization; golden master of every goldens session, with no diff | `import-linter` + empty diff of responses |
| Incident (MCP down or stale index) | Runbook + RDD High; `/health` first | Incident write-up on its tracking issue, with the `/health` output |

## Memory, knowledge and code graph

The `mem_*` tools arrive by two routes: the Claude Code plugin `memory@cloudfroghq` and the `memory` server that this repository's `.mcp.json` declares with the profile `agent,projects,workspace`. Different roles, with a band that both cover.

| Origin | Tool prefix | What it covers |
|---|---|---|
| Plugin `memory@cloudfroghq` | `mcp__plugin_memory_memory__` | Session memory: `mem_save`, `mem_search`, `mem_context`, `mem_get_observation`, `mem_session_summary`, `mem_current_project` and the rest of the `agent` profile. |
| `memory` server (`--tools=agent,projects,workspace`) | `mcp__memory__` | 35 tools: the ten of `projects` (`mem_project_card`, `mem_project_upsert`, `mem_task_upsert`, `mem_task_list`, `mem_task_link`, `mem_evidence_add`, `mem_evidence_list`, `mem_runbook_index_sync`, `mem_runbook_find`, `mem_context_pack`), the ten of `workspace` and the eighteen of `agent`. |

- Session decisions and findings → plugin. Project cards, tasks, evidence, runbook index and context packs → server.
- The profile pulls in `agent`, and that overlaps with the plugin on purpose.
  - Measured with one `tools/list` per profile: `agent` (18) and `admin` (4) are exactly the plugin's 22; `projects` (10) and `workspace` (10) repeat none of those 22, although they share three with each other (`mem_context_pack`, `mem_project_card`, `mem_task_list`), and that is why the declared union gives 35 and not 38.
  - Pulling in `agent` leaves **two routes to the same write**: `mem_save` answers both under the plugin's prefix and under `mcp__memory__`. It is a conscious decision, not an oversight: both routes write to the same project, so the history does not split and no observation is lost for having come in through one or the other.
  - What requires discipline is choosing: for a tool that exists on both sides, project cards, tasks and evidence go through the `mcp__memory__` prefix, and session memory through the plugin.
  - Enumerate the profile before changing it: `memory mcp --tools=PROFILE` answering a `tools/list` is the only list that counts.
- The plugin is installed from the `cloudfroghq` marketplace: `claude plugin marketplace add cloudfroghq/claude-plugins && claude plugin install memory@cloudfroghq --scope user`, followed by `memory setup claude-code` for the permissions; its tools' prefix is `mcp__plugin_memory_memory__`, the only one that this repo's `allowed-tools`, permissions and hooks list. A machine that still has the old `cloudfrog@cloudfrog` plugin (retired prefix) gets permission prompts when using those skills, not failures; migrating with `docs/install.md` is the recommended path.
- Both write to the same project: the plugin resolves it from the git remote and gets **`knowledge`**; the server receives it fixed in `MEMORY_PROJECT=knowledge`. If those two names diverge, server and plugin split the history into two projects and no search brings them back together. Check it with `mem_current_project` before changing either of them.
- This repo's code is indexed in graphify (`graphify-out/` already exists); `explain`/`affected` are reliable on Python.
- This repo's `.mcp.json` declares two stdio servers.
  - `memory`: `${MEMORY_BIN:-memory} mcp --tools=agent,projects,workspace` with `env` `MEMORY_PROJECT=knowledge`, `MEMORY_VAULT_ROOT=${HOME}/.cloudfrog` and `MEMORY_PROJECTS_SYNC=1`; it does not pass `MEMORY_CLOUD_AUTOSYNC`, because sync with memory cloud is done only by the `memory serve` service (enabled with `memory cloud config --autosync on`), `memory mcp` ignores that variable and `memory cloud status` flags it as superfluous.
  - `graphify`: `${GRAPHIFY_MCP_BIN:-graphify-mcp} ${CLAUDE_PROJECT_DIR:-.}/graphify-out/graph.json --transport stdio`.
- The knowledge server (this same server, deployed) is not in the `.mcp.json`: it is installed as a plugin from the `cloudfroghq` marketplace (`knowledge@cloudfroghq` over stdio, prefix `mcp__plugin_knowledge_knowledge__`, or `knowledge-remote@cloudfroghq` against an HTTP deployment, prefix `mcp__plugin_knowledge-remote_knowledge__`; see `docs/install.md`), or each person registers it in their Claude Code user configuration (`claude mcp get cloudfrog-knowledge`): user scope, http transport, URL `https://<deployment>/mcp` and an `Authorization: Bearer <token>` header that lives only in that local configuration, never in the repo.
- The vault is **not** indexed in graphify: it is content, not code, and navigating it is exactly what this server's tools solve.

### Binaries that `.mcp.json` needs

| Variable | What it points to | Default |
|---|---|---|
| `MEMORY_BIN` | The memory binary. | `memory`, resolved through `PATH` |
| `GRAPHIFY_MCP_BIN` | The `graphify-mcp` binary. | `graphify-mcp`, resolved through `PATH` |

How to check that they really resolve:

```bash
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"probe","version":"0"}}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' \
  | "${MEMORY_BIN:-memory}" mcp --tools=agent,projects,workspace

printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"probe","version":"0"}}}' \
  | "${GRAPHIFY_MCP_BIN:-graphify-mcp}" graphify-out/graph.json --transport stdio
```

The memory server answers the handshake and returns 35 tools, the union of the three profiles that `.mcp.json` declares; a shorter or longer list means the profile is not the declared one. A `--help`, a `--version`, a `test -e` or a `command -v` **do not work** as a check: all four exit before the server is built, and a dangling symlink still exists as a directory entry, so all four pass and the process dies later with an opaque `ENOENT` or `ImportError`. The only valid proof is that `initialize` and `tools/list` answer.

The defaults of `MEMORY_BIN` and `GRAPHIFY_MCP_BIN` are names that `PATH` resolves, not absolute paths. That is the difference from the dangling symlink: `PATH` either finds a binary that answers the handshake or finds nothing, whereas a fixed absolute path to a checkout that is no longer there turns "needs installing" into "the server is broken".

The graph path for `graphify-mcp` is **positional**, not a flag: `graphify-mcp <graph.json> --transport stdio`. `--graph` does not exist and the process dies while parsing arguments.

`${CLAUDE_PROJECT_DIR}` inside `command` or `args` of a project `.mcp.json` needs a default value: `${CLAUDE_PROJECT_DIR:-.}`. Claude Code defines that variable in the environment of the server it launches, not in its own, and it resolves the `${VAR}` expansion against its own environment; without a default value the reference stays unresolved and the binary receives a path that does not exist. The symptom is `CONNECTION_CLOSED` with a perfectly healthy binary: the handshake above answers in a terminal and the server still does not connect, so suspicion points at the binary while the defect is in the JSON. The `.` works because the working directory with which Claude Code launches a stdio server is the root of the project where the `.mcp.json` is declared.

The hooks in `.claude/settings.json` are the opposite case: there `CLAUDE_PROJECT_DIR` does arrive in the process environment, and `${CLAUDE_PROJECT_DIR}/.claude/hooks/...` resolves without a default value. The distinction is where the expansion happens: in Claude Code before launching the server, or in the environment that the hook receives.

`graphifyy` declares its `mcp` extra without an upper bound, so a clean install pulls in the `mcp` 2.x SDK, where `mcp.types.AnyUrl` no longer exists: the server dies with `ImportError: cannot import name 'AnyUrl' from 'mcp.types'` and reports it as `mcp not installed`. The interpreter that serves `graphify-mcp` needs `mcp<2`.

## Deployments

Each deployment is documented in its vault, under `Deploy/`: domain, application on the platform, keys by name, webhooks, verification and rollback. None lives here. `infra/compose.yml` is the generic compose file for every instance (`docs/runbooks/serve-multiple-vaults.md`).

## Hard rules

- VCS: this repository is hosted on GitHub; use `gh` for its pull requests, issues and API.
- The vault is edited by human PR with the owner as reviewer; the agent proposes, never writes (D-11).
- The ring 1 pipeline runs on Linux: macOS has already hidden a real case-insensitivity defect.
- Commits in Conventional Commits, with no ticket prefix and no AI attribution.
