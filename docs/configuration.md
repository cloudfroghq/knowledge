# Configuration

Configuration is explicit: environment variables (a `.env` at the checkout root is loaded and never overrides variables already set), the `vaults.yaml` registry, each vault's own `vault.yaml`, and optionally a static `config.json` (the repository ships none). An empty value of `VAULT_PATH`, `KNOWLEDGE_INDEX_DIR`, `KNOWLEDGE_VAULTS` and similar path variables counts as absent: an empty `VAULT_PATH` never resolves to the checkout root.

## Which vaults are served

Decided once at startup, in this order:

1. `KNOWLEDGE_VAULTS` (or `serve --vaults FILE`) names a `vaults.yaml`: every vault it declares, with one default.
2. `VAULT_PATH` (or `vault_path` in `config.json`): a registry of one vault.
3. `$XDG_CONFIG_HOME/knowledge/vaults.yaml` or `~/.config/knowledge/vaults.yaml`, when it exists.
4. None of these: startup fails with `E_VAULT_PATH_MISSING`.

The vaults themselves never live in this repository: each one has its own repository (`/vault/` is in `.gitignore`). Each vault also declares, in `Contexts/config.json`, the checkouts it can resolve and its context packages (`Contexts/<context>/`, [ADR-047](adr/ADR-047.md)); see [Repository registry](repository-registry.md).

## `vaults.yaml`

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
- An entry can declare `repo` (the server then clones and pulls it), `git`, `webhook`, `features`, `memory` and `embeddings`; the top-level `bearers` list gives each HTTP client its own token and scopes. The full annotated example is [`vaults.yaml.example`](../vaults.yaml.example); procedures are in [`docs/runbooks/serve-multiple-vaults.md`](runbooks/serve-multiple-vaults.md).
- Try it with the fixtures: copy `tests/fixtures/vaults/alpha` and `tests/fixtures/vaults/beta` next to the file, then `uv run cloudfrog-knowledge serve --transport stdio --vaults path/to/vaults.yaml`; `search` with `"vault":"beta"` queries the second one.

## Where the index lives

With `vaults.yaml`: `<data_dir>/<slug>` (`KNOWLEDGE_HOME` overrides `data_dir`). With `VAULT_PATH`, the slug is `CHROMA_COLLECTION`, else the `slug` of the vault's `vault.yaml`, else `vault`, and the index directory is the first of: `KNOWLEDGE_INDEX_DIR`, `CHROMA_PERSIST_DIR`, `<KNOWLEDGE_HOME>/<slug>` (only with an explicit `KNOWLEDGE_HOME`) and `<checkout>/.data/<slug>`. When you run an installed package with `VAULT_PATH`, set `KNOWLEDGE_INDEX_DIR` or `KNOWLEDGE_HOME` so the index lands where you expect. Next to `index.sqlite` live `link_graph.json`, `usage_stats.json`, `index_meta.json` and `skipped_files.json`.

## Embeddings providers

| `KNOWLEDGE_EMBEDDINGS_PROVIDER` | What it does |
|---|---|
| `openai` (default) | OpenAI embeddings. Needs `OPENAI_API_KEY` (or the variable named by `embeddings.key_env`); without it startup fails with `E_EMBEDDINGS_KEY_MISSING`. Model: `EMBEDDING_MODEL` or `embeddings.model`, default `text-embedding-3-large` (3,072 dimensions). A one-file commit costs about $0.0003 in embeddings. |
| `none` | No embeddings: lexical (FTS5) search only. No key needed. |
| `bag-of-words` | A deterministic bag of words over a fixed vocabulary. Used by the test suite and the goldens; good for trying the server offline, not for real retrieval. |

A vault can declare its provider in the `embeddings` block of `vaults.yaml` or its `vault.yaml`; the environment variable wins.

## Profiles and `features`

| Profile and served `features` | Tools in `tools/list` |
|---|---|
| `all` (default), no vault with `features` | the 10 core tools |
| `all`, `governance` | 12: the core, `get_stale_docs` and `suggest_links` |
| `all`, `flows` and `governance` | 14: the core, `get_flow`, `compare_flows`, `get_stale_docs` and `suggest_links` |
| `all`, `flows`, `governance` and `confluence` | 15: the above plus `sync_confluence` |
| `all` over stdio with a vault that declares `jira` | `sync_jira` at the end, besides whichever of the above apply |
| `legacy` (`serve --profile legacy` or `KNOWLEDGE_PROFILE=legacy`, for one release) | the 16 tool names of 1.4.0 in their order, `search_code` included and the extension tools without looking at `features`; `sync_jira` still requires `jira` |

Other `features` a vault can declare: `git-webhook` (the push webhook) and `memory-bridge` (publishing the vault to the local memory server). Calling an extension tool that no served vault declares returns `E_FEATURE_DISABLED`, also for a vault that does not declare it in a process that serves several, and the message names the vaults that do. Another profile value stops startup with `E_PROFILE_UNKNOWN`.

## Environment variables

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
| `KNOWLEDGE_STRUCTURED_OUTPUT` | `0` | Text blocks are the canonical response. `1` adds `outputSchema` to every tool and `structuredContent` to every response, the same JSON as the text; the catalog order does not change. See [ADR-046](adr/ADR-046.md). |
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
| `KNOWLEDGE_COMMAND_LABEL` | | The command that hints name; see [Command label in hints](cli.md#command-label-in-hints). |
| `KNOWLEDGE_<ROOT>_ROOT` | from `repos.local.json` | Pins a whole root, for example `KNOWLEDGE_WORK_ROOT`. `CD_<ROOT>_ROOT` and the root's declared `env` still work as deprecated aliases, and `python3 -m knowledge.repos --json` reports them in `warnings`. See [Repository registry](repository-registry.md). |
| `KNOWLEDGE_REPO_<ID>` | from the matching root | Pins one checkout, for example `KNOWLEDGE_REPO_LEDGER_API`. `CD_REPO_<ID>` still works as a deprecated alias. |

## `sqlite-vec` and the Python fallback

`sqlite-vec` needs a Python with loadable SQLite extensions. `uv`'s CPython (`uv python install 3.12`, or `--managed-python`) has them; the python.org build for macOS does not. Without the extension the server sorts the vectors with the standard library (`vector_backend: python`, about 110 ms per query over 1,480 vectors of 3,072 dimensions instead of about 10 ms) with the same results, except exact ties at the `k` boundary. `doctor` and `/health` report the backend, and `KNOWLEDGE_REQUIRE_SQLITE_VEC=1` turns the fallback into an error.
