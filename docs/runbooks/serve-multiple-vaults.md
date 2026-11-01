# Serve several vaults with a single server

Runbook for whoever deploys or moves `cloudfrog-knowledge` instances. A process can serve one vault (compatibility mode) or several (the `vaults.yaml` registry). In a deployment, each instance runs `infra/compose.yml` with its own environment: its vaults, its index, its deploy keys and its webhook secret. What is specific to each deployment (domain, application on the platform, keys by name, rollback) is documented in its vault, under `Deploy/`.

## 1. Discovery modes

The server decides what it serves in this order; the first one that exists wins:

1. `KNOWLEDGE_VAULTS`: path to a `vaults.yaml` (relative to the checkout). If the file does not exist, startup fails with `E_VAULTS_FILE_MISSING`.
2. `VAULT_PATH`: compatibility mode, a single vault. If `KNOWLEDGE_VAULTS` is also set and `VAULT_PATH` points somewhere else, a `W_VAULT_PATH_IGNORED` warning is emitted.
3. `vault_path` in the `config.json` named by `KNOWLEDGE_CONFIG_JSON`: the same compatibility mode. This repository ships no `config.json`, because no vault lives here: in a checkout with neither `VAULT_PATH` nor `KNOWLEDGE_VAULTS`, the next rule wins.
4. `$XDG_CONFIG_HOME/knowledge/vaults.yaml` or `~/.config/knowledge/vaults.yaml`: only if none of the above is defined. Without this file either, startup fails with `E_VAULT_PATH_MISSING`: there is no default vault.

`KNOWLEDGE_VAULTS`, `VAULT_PATH`, `KNOWLEDGE_INDEX_DIR`, `CHROMA_PERSIST_DIR`, `KNOWLEDGE_HOME`, `KNOWLEDGE_CONFIG_JSON` and `KNOWLEDGE_EMBEDDINGS_PROVIDER` are read as absent when empty.

## 2. Where the index lives

Each vault has an index directory with `index.sqlite` (documents, chunks, FTS5 and vectors) and its sidecars: `link_graph.json`, `usage_stats.json`, `index_meta.json` and `skipped_files.json`.

- Compatibility mode: `KNOWLEDGE_INDEX_DIR` > `CHROMA_PERSIST_DIR` > `KNOWLEDGE_HOME/<slug>` (only if `KNOWLEDGE_HOME` is defined and non-empty) > `<root>/.data/<slug>`. The slug comes from `CHROMA_COLLECTION` (blank is read as absent); without it, from `chroma_collection` in `config.json`; without that, from the `slug` in the directory's `vault.yaml` (only if it is a valid slug, because it builds paths); and with none of those it is `vault`, a name that belongs to no tenant. With the slug from `vault.yaml`, the credential variables the vault names (`ACME_TYPESAFE_API_KEY` in a vault `acme`) match the vault being served. With `CHROMA_PERSIST_DIR`, `index.sqlite` lives next to `chroma.sqlite3`, on the same volume.
- Registry: `<data_dir>/<slug>`, with `data_dir` from `vaults.yaml` or `KNOWLEDGE_HOME`; with neither, `$XDG_DATA_HOME/knowledge`.
- The image sets `KNOWLEDGE_HOME=/data/index` and `CHROMA_PERSIST_DIR=/data/chroma`. Without `KNOWLEDGE_INDEX_DIR` the second wins: the index ends up at `/data/chroma/index.sqlite`, next to `chroma.sqlite3` and inside the volume the compose mounts there. That default exists for deployments whose compose is not in this repo (see section 7).
- The composes in `infra/` set `KNOWLEDGE_INDEX_DIR=/data/index/<slug>` on the `knowledge-index` volume. If your compose mounts the Chroma volume at `/data/chroma`, it is only a migration source: the server does not write there.
- `cloudfrog-knowledge migrate-index` prepares each vault's index without embedding anything: it copies `usage_stats.json` and the learned weights of `link_graph.json` from the directory where Chroma used to live (`<data_dir>/<slug>/chroma`, `CHROMA_PERSIST_DIR` or `--from-chroma`) and leaves the index empty; the first tool call (or the webhook) fills it. With `--prune-chroma` it deletes only `chroma.sqlite3` and Chroma's segment directories.
- The image entrypoint (`infra/bootstrap-vault.sh`) runs `migrate-index` without `--build` and then `serve`, with the migration report on stderr. An `index.sqlite` from a newer package stops startup with `E_INDEX_VERSION_NEWER`. Embedding is left for the first tool call or the webhook: with `openai` it takes one or two minutes, and the `HEALTHCHECK` allows 20 s of startup.
- `vector_backend` (in `doctor` and in `/health`) says what sorts the vectors: `sqlite-vec` or `python`. `python` means the extension does not load in that interpreter (a Python without loadable extensions, such as the python.org one for macOS): the result is the same and each query costs about 110 ms instead of about 10. The image runs on `python:3.12-slim-trixie` and loads the extension; over stdio, `uvx --managed-python` uses a `uv` CPython, which also loads it.
- There is no index snapshot to export or import: the index is rebuilt from the vault. The repository ships neither `scripts/export-data.sh` nor `import-data.sh`.

## 3. Add a vault

1. Choose a `slug` that matches `^[a-z0-9][a-z0-9-]{1,30}[a-z0-9]$`. It names the index directory and the collection, so do not change it afterwards.
2. Add an entry to `vaults.yaml` (start from `vaults.yaml.example`). `path` is required. `deploy_key_env`, `git` and `webhook` require `repo`.
3. Name secrets by environment variable (`deploy_key_env`, `webhook.secret_env`). Never write them in the file.
4. If the vault ships a `vault.yaml`, its `slug` must match the registry's.
5. Validate without starting anything:

```bash
uv run cloudfrog-knowledge vault-lint <path-to-vault>              # rules 1.3: the vault's rules
uv run cloudfrog-knowledge vault-lint <path-to-vault> --rules 1.2  # the ones reindex.validation counts
uv run python -c "from pathlib import Path; from knowledge.settings import parse_vaults_file; print(parse_vaults_file(Path('vaults.yaml'), check_paths=False))"
```

## 4. When git runs

Git runs only if the vault declares a remote. In the container that means a non-empty `VAULT_REMOTE`; in `vaults.yaml`, `repo` with `git.auto_pull` other than `false`.

- `VAULT_REMOTE` set: `infra/bootstrap-vault.sh` creates or adopts the clone at `/data/vault-repo`, forces it to `VAULT_BRANCH` and requires `GIT_DEPLOY_KEY_B64`. Without a key it exits with `E_DEPLOY_KEY_MISSING` (code 64) without touching git.
- `VAULT_REMOTE` empty: zero git calls. The vault is whatever the volume already contains.
- The generic compose keeps the clones in the `knowledge-vaults` volume (`/data/vaults/<slug>`), read-write. `90 - Memory` is the only folder where the server writes. The variant without git, with the vault on a read-only volume, is in "Vault delivered as a volume".
- If `VAULT_PATH` is not a directory, startup exits with `E_VAULT_PATH_NOT_DIR` and names the path, the remote and the branch.

## 5. Secrets per instance

Each instance defines its own in its environment; none is shared between instances of different organizations:

| Variable | What it is |
|---|---|
| `GIT_DEPLOY_KEY_B64` | Read-only deploy key registered in that organization's vault repo, in base64. Needed only with `VAULT_REMOTE`. |
| `MCP_WEBHOOK_SECRET` | HMAC secret of that repo's `push` webhook. Without it, the webhook is rejected and a `W_WEBHOOK_SECRET_MISSING` warning is emitted. |
| `MCP_API_KEY` | The server's bearer. Without `MCP_ADMIN_KEY` it has the `read` and `admin` scopes; with it, only `read`. With neither of the two (nor `bearers`), `E_HTTP_API_KEY_MISSING`. |
| `MCP_ADMIN_KEY` | Optional. Bearer with the `admin` scope, the only one that can call `reindex`, `sync_confluence` and `POST /webhook/reindex` with a manual bearer. Different from `MCP_API_KEY`: if they coincide, `E_MANIFEST_INVALID`. |
| `OPENAI_API_KEY` | Embeddings with the `openai` provider. Without it, `E_EMBEDDINGS_KEY_MISSING`. |
| `<SLUG>_TYPESAFE_API_KEY` | Optional. Key of the `search` judge (rerank), with the name declared by the vault's `vault.yaml` `rerank.key_env` (`ACME_TYPESAFE_API_KEY` for the slug `acme`); `TYPESAFE_API_KEY` is valid only for the default vault. If the platform stores the key under a short name, the compose copies it to the vault's name. Without a value, `W_RERANK_KEY_MISSING` and that vault searches without a judge. |
| `CONFLUENCE_URL`, `CONFLUENCE_EMAIL`, `CONFLUENCE_TOKEN` | Only with the `confluence` extension. `CONFLUENCE_URL` is the site root (`https://<org>.atlassian.net`), **without** `/wiki`: the client appends `/wiki/api/v2` on its own, and with a trailing `/wiki` every page responds 404 and ends up in `errors` of `sync_confluence`. A vault can set its own site and variables in `integrations.confluence` ([`docs/runbooks/promote-confluence.md`](promote-confluence.md)). |

The webhook is configured in the vault repo with the URL `http://<host>:<port>/webhook/reindex?target=vault`. With a secret, GitHub signs each delivery in `X-Hub-Signature-256` (`sha256=`) and in `X-Hub-Signature` (`sha1=`); the server tries the first one first and accepts only `sha256`, also under `X-Hub-Signature`, which is where Bitbucket sends it. The route exists only when the default vault declares a repository (`repo` in `vaults.yaml` or `VAULT_REMOTE`): without a repository there is nothing to update with git and the server responds 404 on that route; the index is rebuilt with the `reindex` tool.

A vault's clone follows only its served branch (`git.branch` in `vaults.yaml`; `VAULT_BRANCH` with `VAULT_PATH`; `main` by default). A signed delivery whose `ref` is another branch or a tag, or that deletes the ref (`deleted: true`, or an all-zeros `after`), receives 200 and runs nothing. A Bitbucket Cloud `repo:push` carries no top-level `ref`: it lists each reference the push updated in `push.changes`, with its state before (`old`) and after (`new`, `null` if the push deleted it), and the delivery runs only if some change has `new` of type `branch` with the served branch's name; otherwise it receives the same response:

```json
{"status": "ignored", "mode": "signature", "reason": "not_served_branch", "ref": "refs/heads/fix/typo", "branch": "main", "target": "vault"}
```

`reason` is `not_served_branch` or `ref_deleted`. A merge with `--delete-branch` (GitHub) or with "Close source branch" (Bitbucket) sends two deliveries almost together: the served branch's gets 202 and the deletion's is `ignored`. In a Bitbucket `repo:push`, `ref` is the first change's reference as a git ref (`refs/heads/<branch>`, `refs/tags/<tag>`), and `ref_deleted` with `refs/heads/<served branch>` when a change deleted the served branch. Two shapes are judged: a payload with a top-level `ref` (GitHub, Gitea, Forgejo) and one with `push.changes` (Bitbucket Cloud); a `ping`, a body that is not JSON, any other shape, an empty `push.changes` list or a change that cannot be read still trigger the pull. A manual bearer with the `admin` scope always enqueues the reindex, whatever body it brings. A vault's runs never overlap: a delivery that arrives while one is running adds a single pass at the end (which fetches the latest from the remote), and the `reindex` tool waits on the same lock. In the delivery history, a 200 is a delivery deliberately ignored and a 202 is one that was enqueued.

Each pass brings the clone to the tip of `origin/<served branch>`, also when the branch was rewritten (force push) or the clone diverged: `git fetch --prune origin`, then `git merge --ff-only` if `HEAD` is an ancestor of the remote tip and, if not, `git checkout -f -B <branch> origin/<branch>`, the same command with which the bootstrap adopts an existing clone (no `git clean`: untracked files stay). The realignment leaves a `WARNING` with the old and new commit, and then runs the reindex. If the `fetch` fails, the clone is not touched and the log carries an `ERROR`. `/health` shows it without reading logs:

| Key | Value |
|---|---|
| `vault_commit` | Abbreviated commit the clone holds now (`null` without a repository). |
| `last_sync_at` | End of the last update in this process, ISO 8601 in UTC. |
| `last_sync_result` | `fast_forward`, `up_to_date`, `realigned` or `failed`. |
| `last_sync_error` | Reason for a `failed`, on one line and without credentials; `null` otherwise. |
| `last_change_at` | End of the last update that moved the clone in this process, ISO 8601 in UTC. |
| `last_change_result` | `fast_forward` or `realigned`. A later `up_to_date` or `failed` does not replace it. |

The three `last_sync_*` are `null` until the first update after a startup (the bootstrap already brought the clone) and in a vault without a repository; the two `last_change_*`, until the first update that moves the clone. The `last_sync_*` keys describe the last attempt and the `last_change_*` keys the last change: two merges in a row leave one extra pass that no longer brings anything (`last_sync_result: up_to_date`), and `last_change_result` still says how the merge arrived. A `failed` does not change `status`. If `last_sync_result` stays at `failed` delivery after delivery, the clone is serving the commit in `vault_commit`: check the deploy key and the remote.

### Scopes per bearer

Each HTTP bearer is a principal with scopes: `read` uses the server and `admin` also calls the administration tools (`reindex`, which changes the index, and `sync_confluence`, which only reads) and `POST /webhook/reindex` with a manual bearer. None of them writes to the vault. A bearer without `admin` receives `isError` with `E_SCOPE_REQUIRED` on those tools and 403 on the webhook; the rest of the tools do not change.

| Configuration | Principals |
|---|---|
| `MCP_API_KEY` only | `service`, with `read` and `admin`: same as in 1.2.0 |
| `MCP_ADMIN_KEY` (and optionally `MCP_API_KEY`) | `admin` (`MCP_ADMIN_KEY`), with `read` and `admin`; `service-read` (`MCP_API_KEY`), `read` only |
| `bearers` in `vaults.yaml` | One per entry, and no other: `MCP_API_KEY` and `MCP_ADMIN_KEY` are ignored |

`bearers` lives only in `vaults.yaml` (a `VAULT_PATH` deployment does not read it) and names each token's variable, never the token:

```yaml
bearers:
  - id: ana            # ^[a-z0-9][a-z0-9-]{0,31}$, unique; it is what appears in the log
    token_env: KNOWLEDGE_TOKEN_ANA
    scopes: [read]     # read by default; admin requires read
  - id: ops
    token_env: KNOWLEDGE_TOKEN_OPS
    scopes: [read, admin]
```

Over HTTP, an entry whose variable is empty stops startup with `E_SECRET_MISSING`, and two entries with the same token stop it with `E_MANIFEST_INVALID`. Over stdio there are no bearers: they are ignored with `W_BEARERS_IGNORED_STDIO`. `cloudfrog-knowledge doctor` shows the `auth` block (ids, scopes, variable names and whether they are defined, never a value).

## 6. Threshold gate before the cutover

The image ships no `config.json`. The `get_stale_docs` thresholds (`review.thresholds_days`) come, in this order, from `vault.yaml` at the vault root, from a file pointed to by `KNOWLEDGE_CONFIG_JSON`, or from the built-in values, with `W_REVIEW_THRESHOLDS_FALLBACK` in the startup log.

**Do not cut an instance over to its vault repo until that repo has a `vault.yaml` with `review.thresholds_days`** (or until you mount a `config.json` and point to it with `KNOWLEDGE_CONFIG_JSON`). Otherwise `get_stale_docs` changes its results without anything failing. A vault that declares its thresholds in `vault.yaml` does not need the compose to define `KNOWLEDGE_CONFIG_JSON`.

## 7. Cutting an instance over to its vault repo

This is the common procedure; the domain, the application on the platform, the keys by name and the rollback of each deployment are in its vault, under `Deploy/`.

Order matters. If the new compose is deployed with the old deploy key, the bootstrap fails at `git clone` under `set -eu` and the container ends up in a restart loop; if the key is changed before the compose, the `fetch` from the old remote fails just the same on the next restart.

1. Before: note down `curl -s https://<domain>/health` and save `indexed`.
2. Register a read-only deploy key on the vault repo: `gh repo deploy-key list -R <org>/<vault>` shows it with `read-only`.
3. Turn off the platform's automatic deployment of the compose, if it has one.
4. Merge the compose change to `main` (no deployment, because the automatic one is off).
5. In the compose's environment: `GIT_DEPLOY_KEY_B64` with the new key, `MCP_ADMIN_KEY` (different from `MCP_API_KEY`) and `MCP_WEBHOOK_SECRET` (new; it is the vault repo's webhook secret). The rest stays the same.
6. Deploy and wait for `/health` with `status: ok` and the published `contract_version`.
7. Create the `push` webhook on the vault repo (`https://<domain>/webhook/reindex?target=vault`, `application/json`, secret = `MCP_WEBHOOK_SECRET`) and trigger the first reindex with a test delivery or a push. The delivery is HMAC-signed and carries no `admin` bearer. With `openai`, the first reindex embeds the whole vault.
8. Afterwards: the webhook's delivery history shows `202`, and `/health` returns `index_version: 1`, `vector_backend: sqlite-vec`, an `indexed` greater than zero, a new `last_indexed_at`, the `vault_commit` of the served branch's tip and a `last_sync_result` other than `failed`. The `indexed` number counts chunks.
9. Turn the automatic deployment back on.

Rollback: restore the compose's previous environment, revert the compose change through a PR and deploy. Before changing anything, back up the container's volume (`docker run --rm -v <volume>:/v -v "$PWD":/b alpine tar czf /b/vol.tgz -C /v .`): the new compose replaces the old index with the vault repo's content. The bootstrap does a `checkout -f` of `VAULT_BRANCH` on a clone that already exists, so a rollback to the old compose starts only if that branch keeps what the old compose expects.

#### Reading per-tool calls

The server counts each call by tool, client and principal in the index's `usage_stats.json` (`/data/index/<slug>`, `knowledge-index` volume); the format is in [Call telemetry](../operations.md#call-telemetry). To read it, open the `knowledge-mcp` service's terminal on your platform (or `docker exec -it <container> sh` on the host) and run `doctor`, which only reads:

```bash
python -m knowledge doctor | python3 -c 'import json,sys; v=[r for r in json.load(sys.stdin)["vaults"] if r["default"]][0]; print(json.dumps(v["tool_calls"], indent=1))'
```

The container already carries `VAULT_PATH` and `KNOWLEDGE_INDEX_DIR`, so `doctor` reads the deployment's file. `last_7_days.by_client` says which clients call, under the name each one declares in its `initialize` (a new client shows up here under its own name), `by_principal` with which bearer (`service-read` is `MCP_API_KEY`, `admin` is `MCP_ADMIN_KEY`), `deprecated` how many calls `search_code` received and `unregistered` how many went to a name the contract does not define. No key carries arguments or the bearer. A redeploy does not delete the file (it lives on the volume), but it does lose the counts the process had not yet written if the container dies without shutting down cleanly.

A process running a version without this telemetry rewrites the file without the `tool_calls` section: to roll back without losing it, revert to a commit that includes it.

#### Deploying an image to a target whose compose is not here

Before deploying an image of this package to a target with its own compose, check what its container mounts: `docker inspect <container> --format '{{json .Mounts}}'`. If `/data/chroma` is a volume and its compose does not set `KNOWLEDGE_INDEX_DIR`, the index is written to `/data/chroma/index.sqlite`, inside that volume. If `/data/chroma` is not a volume, the index is lost on every deployment and the vault is re-embedded on every startup: mount a volume there, or set `KNOWLEDGE_INDEX_DIR` on one, and only then deploy.

#### Vault delivered as a volume

When whoever owns the content publishes the vault on a volume and the container must not run git, the vault is mounted read-only and `90 - Memory`, the only folder where the server writes, goes on a separate volume. It is an override of `infra/compose.yml`:

```yaml
services:
  knowledge-mcp:
    environment:
      VAULT_PATH: /data/vault
      VAULT_REMOTE: ""
      KNOWLEDGE_INDEX_DIR: /data/index/<slug>
    volumes:
      - vault-data:/data/vault:ro
      - "memory-export:/data/vault/90 - Memory"
volumes:
  vault-data:
    external: true
  memory-export:
```

Precondition: the `vault-data` volume already contains the `90 - Memory` folder. The runtime cannot create a mount point inside a read-only mount, and without it `up` fails with "read-only file system" even though `docker compose config` accepts the file. Without `VAULT_REMOTE` git does not run and no deploy key is needed. `sync_confluence` only reports and `cloudfrog-knowledge confluence export` writes outside every vault (ADR-044), so nothing else needs the vault to be writable.

### Generic Compose file (`infra/compose.yml`)

This is the organization-agnostic compose: the same file serves N instances, and what tells them apart is only their environment. It carries no vault URLs, domains or slugs; it publishes no ports (a platform routes to the container's port 8000, and whoever uses `docker compose` by hand adds `ports:` in an override). `memory-cloud` and its `postgres` live under the `memory` profile: without `--profile memory` (or `COMPOSE_PROFILES=memory`) only the knowledge server runs. `MEMORY_CLOUD_IDENTITY_MODE` reaches `memory-cloud` with `grace` by default (it accepts the shared `MEMORY_CLOUD_TOKEN` alongside personal tokens); with `required`, memory 1.7.0 or later responds `401 identity_required` to that token on `/sync/*` and leaves the dashboard to administrators only. Each instance changes it in its environment and with a new deployment.

Vaults come in through the environment. `KNOWLEDGE_VAULTS_YAML` carries the contents of a `vaults.yaml` (multiline) and each entry is of one of two kinds:

| Entry | What the entrypoint does |
|---|---|
| `repo` (and `git.branch`, `main` by default) | Clones or updates the vault at `/data/vaults/<slug>` (or at its `path`, if it carries one). It runs on every startup and the server updates it afterwards with `reindex(pull=true)`. |
| `path` only | Uses the mounted folder as is; git does not run. Mount the folder in the container (`/data/local` is an empty volume for that; `KNOWLEDGE_LOCAL_DIR` can name a host directory). |

`infra/resolve-vaults.py` (`knowledge-resolve-vaults` in the image) validates each entry's slug and origin, writes `/data/vaults.resolved.yaml` with all `path` values made absolute and leaves the other keys intact: the server validates it at startup, with its own codes. Then the entrypoint exports `KNOWLEDGE_VAULTS` pointing at that file and discards `VAULT_PATH` and `VAULT_REMOTE`, which the image sets by default and which would otherwise be reported as ignored.

A vault's deploy key over ssh (base64 of the private key), in this order: the variable named by the entry's `deploy_key_env`, `<SLUG_IN_UPPERCASE>_GIT_DEPLOY_KEY_B64` (`team-docs` → `TEAM_DOCS_GIT_DEPLOY_KEY_B64`) and `GIT_DEPLOY_KEY_B64`. A `file://`, `http(s)://` or local-path remote needs no key. Each vault writes its own to `~/.ssh/knowledge_<slug>` (0600) and its clone remembers it in `core.sshCommand`, so the server's `git pull` reads with the same key. The compose lists only `GIT_DEPLOY_KEY_B64`: per-vault keys go in the optional file named by `KNOWLEDGE_ENV_FILE` (`infra/.env.vault-keys` by default, ignored by git) or in an override's `environment`.

- Without `KNOWLEDGE_VAULTS_YAML` the entrypoint is the usual one: `VAULT_PATH`, `VAULT_REMOTE`, `VAULT_BRANCH` and `GIT_DEPLOY_KEY_B64`, a single vault.
- If an existing clone cannot be updated (the remote does not respond), startup warns with `W_VAULT_REFRESH_FAILED` and serves what is on disk; a clone that cannot be created exits with `E_VAULT_CLONE_FAILED` and serves nothing. A missing key for an ssh remote exits with `E_DEPLOY_KEY_MISSING` (code 64), before calling git.
- `/webhook/reindex` and `/health` serve only the default vault. The other vaults with `repo` are updated on restart or with `reindex(vault=<slug>, pull=true)` (`admin` bearer).

`lab/` tests all of this with fake tenants: two instances of this compose, three vaults (two over `file://`, one folder), separate bearers and a PASS/FAIL table (`lab/run.sh`, see [`lab/README.md`](../../lab/README.md)).

## 8. Errors and what to do

| Code | Action |
|---|---|
| `E_VAULT_PATH_MISSING` | Set `VAULT_PATH` to a clone of the vault's repository (or `vault_path` in `config.json`, or a `vaults.yaml`). This repository ships no vault. |
| `E_VAULT_PATH_NOT_DIR` | The path does not exist: mount the volume there, or check that the `VAULT_REMOTE` clone at `VAULT_BRANCH` contains it. |
| `E_DEPLOY_KEY_MISSING` | `VAULT_REMOTE` is set and `GIT_DEPLOY_KEY_B64` is empty: load it, or empty `VAULT_REMOTE`. |
| `E_EMBEDDINGS_KEY_MISSING` | Set the variable named by `key_env` (`OPENAI_API_KEY` by default) or use the `none` provider. |
| `E_EMBEDDINGS_PROVIDER_UNKNOWN` | Use `openai`, `none` or `bag-of-words`. |
| `E_HTTP_API_KEY_MISSING` | Set `MCP_API_KEY` (or `MCP_ADMIN_KEY`, or `bearers` in `vaults.yaml`) for the HTTP transport. |
| `E_SCOPE_REQUIRED` | The call's bearer lacks the `admin` scope: `reindex` and `sync_confluence` respond `isError` with this code, and `POST /webhook/reindex` with a manual bearer, 403. Use the `MCP_ADMIN_KEY` bearer or a `bearers` entry with `scopes: [read, admin]`. |
| `E_HTTP_PORT_INVALID` | `MCP_HTTP_PORT` must be a valid integer. |
| `E_CONFIG_JSON_INVALID` | The `config.json` exists but cannot be read or is not a JSON object: fix it or remove it. |
| `E_INDEX_PROVIDER_MISMATCH` | Warning in the log: the index was built with another provider, model or chunk plan, and the server empties it and re-embeds the whole vault before responding (with `openai`, the cost of a full reindex). |
| `E_INDEX_VERSION_NEWER` | The `index.sqlite` was written by a newer package than the installed one: upgrade `cloudfrog-knowledge` or point the vault to another index directory. `migrate-index` exits with code 2. |
| `E_INDEX_UNREADABLE` | The `index.sqlite` is not a SQLite database or SQLite cannot read it (truncated or foreign file). `migrate-index` exits with code 2 and startup stops: move the file elsewhere and the index is rebuilt empty on the next run. An unreadable `link_graph.json` in the Chroma directory stops nothing: the learned weights are skipped with a warning in the log. |
| `E_SQLITE_VEC_UNAVAILABLE` | `KNOWLEDGE_REQUIRE_SQLITE_VEC=1` and the `sqlite-vec` extension does not load in this Python: use a Python with loadable extensions (`uvx --managed-python`, the image's) or remove the variable to accept the slower `python` fallback. `doctor` and `/health` say which `vector_backend` is serving. |
| `E_PRUNE_REFUSED` | `migrate-index --prune-chroma` deletes nothing in a directory that is or contains a registered vault: check `--from-chroma`. |
| `E_VAULTS_FILE_MISSING` | `KNOWLEDGE_VAULTS` points to a file that does not exist. |
| `E_MANIFEST_INVALID` | The message carries the key path: fix that key in `vaults.yaml` or `vault.yaml`. In `integrations.<confluence|jira>`, `site` must be `https` on `*.atlassian.net` or on a host in `KNOWLEDGE_INTEGRATION_SITES` (`http` only on loopback) and `secrets.email_env` and `secrets.token_env` must name a variable of that integration with the vault's prefix (`ACME_JIRA_EMAIL` for `acme`; without a prefix, such as `CONFLUENCE_TOKEN`, only the default vault), never a server secret or another vault's variable. It also appears when `MCP_ADMIN_KEY` equals `MCP_API_KEY` or two `bearers` entries resolve to the same token: generate a different one with `openssl rand -hex 32`. |
| `E_SECRET_IN_MANIFEST` | Something shaped like a credential is in the file: delete it, rotate it and reference an environment variable. |
| `E_VAULT_SLUG_INVALID` | The slug does not match the pattern in section 3. |
| `E_VAULT_SLUG_DUPLICATE` | Two entries with the same slug. |
| `E_VAULT_SLUG_MISMATCH` | The `vault.yaml` declares a slug different from the registry's: make them match. |
| `E_VAULT_DEFAULT_AMBIGUOUS` | More than one entry with `default: true`. |
| `E_VAULT_DEFAULT_MISSING` | There are several vaults and none is `default: true`. |
| `E_VAULT_MANIFEST_MISSING` | The registry requires a `vault.yaml` in the vault and it is not there. |
| `E_VAULT_UNKNOWN` | The requested slug is not registered: at startup, or in the `vault=` of a tool call (the message lists the registered slugs). |
| `E_EVAL_UNAVAILABLE` | `eval` needs `scripts/`, which does not ship in the image or the package. |
| `E_LINT_PATH_NOT_DIR` | `vault-lint` received a path that is not a directory. |
| `E_KNOWLEDGE_HOME_NOT_WRITABLE` | `doctor` found that the data directory (`KNOWLEDGE_HOME` or `data_dir` of `vaults.yaml`) cannot be written: point it to a writable directory. |
| `E_FRONTMATTER_UNPARSEABLE` | A frontmatter is not valid YAML: fix it in a vault PR. |
| `E_FRONTMATTER_NOT_MAPPING` | The frontmatter must be a key-value map. |
| `E_WIKILINK_UNRESOLVED` | A wikilink does not point to any file in the vault. |
| `E_LINT_RULES_UNKNOWN` | `vault-lint --rules` accepts only `1.2` (the rules `reindex.validation` counts) and `1.3` (those of `1.2` plus the vault's). |
| `E_FRONTMATTER_LAST_UPDATED` | `vault-lint` (rules 1.3): the note has no `last_updated` or it is not an ISO date, and `get_stale_docs` cannot measure its age. `vault-lint --fix` takes it from `updated`, `last_review` or `last_verified.date` if any of them is a date; the rest is done by hand in a vault PR. |
| `E_SCOPE_FOREIGN` | `vault-lint` (rules 1.3): the note's `scope` is not the vault's `slug`, `global`, or one of the `scopes` its `vault.yaml` declares; it is another vault's convention. Move the note there, or declare the scope in `scopes:` if the vault uses it on purpose. |
| `E_PATH_CASE_COLLISION` | `vault-lint` (rules 1.3): two paths, or a directory, differ only by case or by the Unicode form of the name, and collide on a disk that is case-insensitive or that normalizes. Rename one. |
| `E_PATH_NOT_NFC` | `vault-lint` (rules 1.3): the path is not in Unicode NFC form. Rename it with the NFC form of the same name. |
| `E_RUNBOOK_ID_INVALID` | `vault-lint` (rules 1.3): a runbook (`type: runbook` under `runbooks.dir`, not a template) without an `id` of the form `RB-NNN`. The memory server skips it on rescan. |
| `E_RUNBOOK_STATUS_INVALID` | `vault-lint` (rules 1.3): a runbook's `status` is not `draft`, `verified` or `outdated`. |
| `E_RUNBOOK_SERVICE_UNKNOWN` | `vault-lint` (rules 1.3): a runbook's `service` is not a `slug` or alias in `Runbooks/services.json` (or in the manifest's `runbooks.services_file`). |
| `E_SERVICES_MAP_INVALID` | `vault-lint` (rules 1.3): `services.json` is not `{"services": [{"slug", "aliases"}]}`, or declares no service. With the file broken, runbooks' `service` is not checked. |
| `E_PROFILE_UNKNOWN` | The requested tool profile does not exist. |
| `E_OUTPUT_SETTING_INVALID` | `KNOWLEDGE_STRUCTURED_OUTPUT` accepts only `0` (default: text blocks only) or `1` (catalog with `outputSchema` and responses with `structuredContent`); `KNOWLEDGE_TEXT_INDENT` accepts an integer from 1 to 8 (indentation of each response's text; if undefined, compact JSON). |
| `E_FIELD_UNKNOWN` | A call's `fields` names a key the tool does not have, or is empty: the message carries the allowed list. |
| `E_ARGUMENT_CONFLICT` | Two arguments are mutually exclusive: in `get_document`, `chunk_id` with `section`, `start_line` or `end_line`, or a `chunk_id` from another document (the message names which); in `search_by_metadata`, `fields` with `facets=True`. |
| `E_ARGUMENT_OUT_OF_RANGE` | An argument is out of its range: `limit` outside 1-50 in `search`, 1-20 in `search_deep`, 1-200 in `search_by_metadata`, `get_related` and `suggest_links` or 1-500 in `get_stale_docs`, negative `offset` in `search_by_metadata`, `max_items` outside 0-1000 in `get_impact`, `budget_tokens` outside [500, 12000] in `context_pack`, or `max_chars` less than 1 in `get_document`. |
| `E_ARGUMENT_RETIRED` | A parameter the contract retired and the schema still declares so as to warn instead of ignoring it: `include_metadata` of `get_document`, retired in 2.0.0. Use `full_metadata=true` for the whole frontmatter, or `fields` without `metadata` to leave it out. |
| `E_ARGUMENT_MISSING` | The call does not name what to act on: `get_document` without `file_path` or `chunk_id`. Pass the `chunk_id` of a `search` row. |
| `E_FILE_NOT_FOUND` | `get_document` on a path that is not a file in the vault: take `file_path` from a `search` row, relative to the vault and without wikilink brackets. |
| `E_SECTION_NOT_FOUND` | `get_document(section=...)` with a heading the document does not have: the message lists up to 20 of its headings. |
| `E_FILE_NOT_IN_GRAPH` | `find_path` with an endpoint that is not in the link graph: no wikilink reaches it and it links to nothing, or it is not a document in the vault. Take `file_path` from a `search` row. |
| `E_FLOW_NOT_FOUND` | `get_flow` or `compare_flows` with a provider for which the vault has no flow document (a `Flow - <provider>.md` note); `compare_flows` names all the missing ones. `search(query='flow')` lists the ones that exist. |
| `E_SCOPE_EMPTY` | `sync_confluence` with a `scope_prefix` in which no file has `confluence_id`: the scope is wrong or the pages still need mapping. The message names up to 20 files with `confluence_id` outside the scope; pass the `scope_prefix` that contains them. |
| `E_RERANK_NOT_CONFIGURED` | `search(rerank="on")` on a vault whose `vault.yaml` does not declare `rerank.provider`: the caller asked for a judge the vault does not have. Omit `rerank` or ask the vault owner to declare the `rerank` block through a PR; the key is never declared, only the name of its variable. |
| `E_EXPORT_FAILED` | `confluence export` could not read the vault or prepare `--out` outside a specific page: check the permissions of the two paths. A page that fails on its own goes in the JSON's `errors` and the exit code is 1. |
| `E_WRITE_FENCED` | A write would land inside a vault: `sync_confluence(dry_run=False)` always, `confluence export` with an `--out` (or a symlink) inside a served vault, or `vault-lint --fix` on a served vault (or with its `--out` inside one): run `--fix` in a checkout that is not served or with `--out DIR` outside every vault. Use `dry_run=True` for the report and `confluence export --out <dir outside the vault>` for the files, and promote them through a PR ([`docs/runbooks/promote-confluence.md`](promote-confluence.md)). |
| `E_VAULT_CLONE_FAILED` | The entrypoint could not clone a vault from `KNOWLEDGE_VAULTS_YAML`; the message names the slug, the remote and the branch. Credentials embedded in the URL (`https://user:token@…`) appear as `***`, in the message and in what git prints; the real URL stays in `vaults.resolved.yaml` and in the clone's configuration, which is what git needs. Check that the remote responds from the container, that the branch exists and that the key matches that repo. Nothing is served. |
| `E_VAULTS_PATH_OVERLAP` | `KNOWLEDGE_VAULTS_YAML`: two vaults occupy the same directory or one is inside the other (a declared `path`, or `/data/vaults/<slug>` of a vault with `repo`; paths are compared after removing `..` and resolving symlinks). Give each vault its own directory. Nothing is cloned or served. |
| `E_CHUNK_ID_INVALID` | The `chunk_id` does not have the form `<doc_id>:<n>` or goes past the last heading: take it from a `search` row. |
| `E_DOC_ID_UNKNOWN` | No file in the vault has that `doc_id`, even after re-reading the disk: the document was moved or deleted; search again. |
| `E_DOC_ID_AMBIGUOUS` | Two files in the vault share a `doc_id`: read them by `file_path`. |
| `E_MEMORY_TOKEN_MISSING` | `context_pack(include_memories=true)` does not read memory because `MEMORY_HTTP_TOKEN` is not defined; it arrives in `degraded`, and the tool responds with the vault. |
| `E_MEMORY_URL_NOT_LOOPBACK` | `KNOWLEDGE_MEMORY_URL` is not `http` over `127.0.0.1`, `localhost` or `::1`: the memory client is turned off so the bearer does not leave the machine, with a warning when the vault is opened. |
| `E_MEMORY_PROJECT_MISSING` | The vault declares no memory project: add `memory.project` in its `vaults.yaml` entry or in its `vault.yaml`. |
| `E_SECRET_MISSING` | HTTP transport: a vault declares in `features` an extension that this transport loads (today, `confluence`) and its credentials' variables are missing; the message names the variables, never their values. Export them (the names come from `integrations.<extension>.secrets` in `vault.yaml` or, without them, `CONFLUENCE_EMAIL` and `CONFLUENCE_TOKEN`) or remove the extension from `features`. Over stdio the server starts anyway and warns with `W_EXTENSION_UNCONFIGURED`, and the `sync_confluence` call without those variables responds with this code as `isError`, with the same names. It also appears when a `bearers` entry names an empty `token_env`. |
| `E_MEMORY_UNAVAILABLE` | The memory server refused the connection, took longer than 1 s, responded with anything other than 200 (a 401 if the token does not match) or a body that is not a JSON list; the log says which, without token or URL. In `memory-bridge`, also a write that did not arrive (connection refused or more than 5 s; 30 s for runbooks) or a `memory` command that does not start, takes longer than 30 s or does not print JSON: install memory or set `MEMORY_BIN`. |
| `E_MEMORY_WRITE_FAILED` | `memory-bridge` (or `reindex` of a vault with `memory-bridge`): the memory server rejected a write. The report carries `http_status` and `reason` with memory's own code (`invalid_field`, `vault_not_found`, `vault_scan_failed`, `payload_too_large` for a 413, `unauthorized` if the token does not match), never the response body or the token. |
| `E_FEATURE_DISABLED` | A command or a tool of an extension on a vault that does not declare it, or a tool whose extension no served vault declares (`get_flow` without `flows`, for example); the message names the vaults that do declare it: add the extension (for example `memory-bridge`) to the `vault.yaml` `features` through a vault PR, or pick another vault with `--vault` (or `vault=` in the tool). |
| `E_TOOL_RETIRED` | A call to a tool the contract retired (`search_code`, since 2.0.0): the message says what to use instead (`graphify explain`, `graphify affected`, `rg`, `search`). The `legacy` profile (`serve --profile legacy` or `KNOWLEDGE_PROFILE=legacy`) serves it for one release. |
| `E_JIRA_UNAVAILABLE` | `sync_jira` or `jira sync`: Jira did not respond with a page of issues (query rejected with 400, check `integrations.jira.projects` and `jql_extra`; 5xx; more than 10 s; a body that is not JSON; a 429 that repeats after one retry). Nothing is written to memory and the cursor does not advance. See [`docs/runbooks/sync-jira.md`](sync-jira.md). |
| `E_JIRA_SYNC_STALLED` | `sync_jira` or `jira sync`: the run was truncated and all its issues were updated in the minute where it starts, so the cursor cannot advance (JQL compares minutes). Raise `--max-issues`, narrow `integrations.jira.jql_extra` or pass a later `since`. Nothing is written to memory and the cursor does not advance. |
| `E_JIRA_AUTH_FAILED` | Jira rejected the email and API token from the variables named by `integrations.jira.secrets` (401 or 403): generate another token for that account or check that the account can see the project. |
| `E_KNOWLEDGE_REF_INVALID` | `memory-bridge promote stamp`: `--knowledge-ref` is not the vault-relative path of a `.md` that exists in it (no leading `/`, no `..`). Use the path of the document the PR merged, after updating the checkout. |
| `E_CONTEXTS_INVALID` | `init`: a vault's `Contexts/config.json`, or this machine's `repos.local.json` for that vault, cannot be read or does not have the registry's shape (`repos.roots`, `repos.entries`, or an entry with a `path` that leaves its root with `..`; an empty registry, `[]` and `[]`, is valid in a vault and means it declares no applications); the message names the file and the key. Fix the `Contexts/config.json` through a vault PR, or delete the `repos.local.json` and run `cloudfrog-knowledge init` again. The other vaults are initialized anyway and the command exits with 1. |
| `E_ROOT_UNKNOWN` | `init --root NAME=DIR`: no vault being initialized declares the root `NAME` in its `Contexts/config.json`. Check the name (`python3 -m knowledge.repos --vault-dir <vault> --json` lists the roots) or use `--root <slug>:NAME=DIR`. Nothing is written. |
| `E_CONTEXTS_REPO_UNKNOWN` | `init --repo ID` or `init --drop ID`: no vault being initialized declares the checkout `ID` in its `Contexts/config.json` (with `--repo SLUG:ID`, the vault `SLUG` does not declare it). Nothing is written and the code is 2. Check the id with `python3 -m knowledge.repos --vault-dir <vault> --json`. |
| `E_CONTEXTS_CONFLICT` | `doctor --client`: a checkout's package cannot be installed because the checkout already has an unversioned file, directory or link of its own there (the message names the application, the path and the entry). What the checkout versions in git is never a conflict. Run `init --force` to set it aside as `<name>.pre-ai-context.bak-<epoch>` (nothing is deleted) or remove it by hand. |
| `E_CONTEXTS_CHECKOUT_INVALID` | `doctor --client`: a checkout is not the declared repository (its `origin` is another one), is not a git repository or its `origin` cannot be read (`unverified`), or its package cannot be installed in it (a package without `CLAUDE.md`, a checkout without git and without `--exclude gitignore`, a checkout inside a vault). Fix the root or set `repos.<id>` in `repos.local.json`; a checkout you know is the declared one is installed with `init --force`. |
| `W_CONTEXTS_CHECKOUT_UNVERIFIED` | `doctor --client`, warning with code 0: a checkout is a git repository without an `origin` remote (a clone not yet published), so it can be neither verified nor ruled out. `init` still reports it as `unverified` and installs nothing in it without `--force`. Give it its `origin` when the repository exists, or install it with `init --force` if you know it is the declared one. |
| `W_CONTEXTS_LINKED_NOT_SELECTED` | `init` and `doctor --client`, warning with code 0: a checkout has links to its package (`Contexts/<package>/`) and is not in the machine's `repos.local.json` `apply` list, so `init` no longer maintains it. Add it with `init --repo ID` or remove its links with `init --drop ID`. |
| `W_CONTEXTS_SELECTION_MISSING` | `doctor --client`, warning with code 0: the machine's `repos.local.json` has no `apply` list (it was written by an `init` that predates it). The next `init` creates it with the checkouts that already have their package installed; to pick others, `init --repo ID` or `init --all`. |

Warnings (`W_*`) do not stop startup, but they must be read: `W_REVIEW_THRESHOLDS_FALLBACK`, `W_VAULT_REFRESH_FAILED` (the entrypoint could not update a vault's clone and serves what is on disk), `W_WEBHOOK_SECRET_MISSING`, `W_VAULT_PATH_IGNORED`, `W_VAULT_SLUG_MISMATCH`, `W_FEATURE_WITHOUT_REPO`, `W_VAULT_GIT_PULL_OFF` (the vault is in a git checkout but declares no repository, so reindexing does not `git pull`: set `VAULT_REMOTE` or `repo`), `W_CONTRACT_RANGE`, `W_TRANSPORT_UNKNOWN`, `W_WIKILINK_AMBIGUOUS_STEM`, `W_TYPE_UNKNOWN` (`vault-lint` with rules 1.3: a note's `type` is outside `hub`, `service`, `architecture`, `flow`, `runbook`, `convention`, `decision`, `reference`, `note`, `pendientes` and `memory`), `W_SERVICES_MAP_MISSING` (`vault-lint` with rules 1.3: the vault has runbooks and no `services.json` against which to check their `service`), `W_EXTENSION_UNCONFIGURED` (over stdio, an extension declared without its credentials: the server starts and only that extension responds with an error), `W_EXTENSION_UNAVAILABLE_HTTP` (over HTTP, a vault declares `jira` or `memory-bridge`, which write to a local memory and are not loaded in a deployment: their tools do not appear; it also names a key that only those extensions read, such as `integrations.jira.kind_map` or `memory.hub`, with a value that over stdio would be `E_MANIFEST_INVALID`), `W_BEARERS_IGNORED_STDIO` (`vaults.yaml` declares `bearers` and the server runs over stdio, where no bearer is checked), `W_RERANK_KEY_MISSING` (a vault declares a `rerank` provider and the variable of its key, which the warning names, is empty: the server starts, also over HTTP, and that vault searches without a judge until you export it), `W_RERANK_INVALID` (a `vault.yaml` `rerank` block, or a `KNOWLEDGE_RERANK*` variable, is malformed: only that vault is left without a judge; the warning names the key to fix), `W_CONTEXTS_INIT_PENDING` (`doctor` over stdio, or `doctor --client` on any transport: a served vault declares `Contexts/config.json` and this machine has no `repos.local.json` for it, or with `--client` some checkout remains to be installed; the context packages are not linked into the checkouts until you run `cloudfrog-knowledge init` or the command named by `KNOWLEDGE_COMMAND_LABEL`), `W_CONTEXTS_CHECKOUT_UNVERIFIED` (`doctor --client`: a checkout is a git repository without an `origin` remote; code 0, see the table above), `W_CONTEXTS_LINKED_NOT_SELECTED` and `W_CONTEXTS_SELECTION_MISSING` (`init` and `doctor --client`: the machine's `apply` selection; code 0, see the table above) and `W_CONTEXTS_GITIGNORE` (`init`: the `.gitignore` of a vault that is a git repository does not cover `Contexts/*/.claude/settings.local.json`, `agent-memory/` or `worktrees/`, which Claude Code writes into the vault through a linked package; it does not block, and is fixed through a vault PR).
