# Operating the server

Each deployment is an instance of the same server with its own environment: its vaults, index, keys and domain. What is specific to each one (domain, platform application, keys by name, webhooks, verification, rollback) is documented in its vault, under `Deploy/`; only what they share is here.

## What runs

`infra/compose.yml` is the Compose file of every instance (the procedure for vaults and entries is in [`docs/runbooks/serve-multiple-vaults.md`](runbooks/serve-multiple-vaults.md), "Generic Compose file"):

- **knowledge-mcp**: built from this repository, with no published ports. A platform routes to the container's port 8000, and whoever runs `docker compose` by hand publishes it with an override.
- **memory-cloud** (profile `memory`): image `ghcr.io/horacioespinosa/memory`, tag in `MEMORY_IMAGE_TAG` (default `latest`) with `pull_policy: always`, no published ports. The GHCR package is private: pulling it requires a registry credential loaded in the platform.
- **Postgres** (profile `memory`): internal, no exposed port.

## Docker image

The `Dockerfile` builds a two-stage image on `python:3.12-slim-trixie` that serves HTTP on port 8000 (with a `HEALTHCHECK` on `/health`) and starts with `knowledge-bootstrap`, which clones the vaults, runs `migrate-index` and then `serve`. `infra/compose.yml` is the generic Compose file for every instance; it publishes no ports (add a `ports:` override such as `127.0.0.1:8000:8000`). How to declare vaults and entries for it: [`docs/runbooks/serve-multiple-vaults.md`](runbooks/serve-multiple-vaults.md), "Generic Compose file". [`lab/README.md`](../lab/README.md) runs several fake instances and clients with it.

## How it is wired

In case it has to be recreated:

1. **Source.** The platform builds this repository with `infra/compose.yml` (a GitHub connection to `cloudfroghq/knowledge`, branch `main`). On a platform that deploys from a Git connection, automatic deployment **does not deploy anything on its own**: the server does not register the webhook, so it must be created by hand in GitHub (step 4). If it is missing, pushes land in the repository and the server keeps running the old image, with no visible error.
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

## Health endpoint

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

## Generate the keys

```bash
openssl rand -hex 32   # MCP_API_KEY (clients)
openssl rand -hex 32   # MCP_ADMIN_KEY (admin bearer)
openssl rand -hex 32   # MCP_WEBHOOK_SECRET (the repository webhook)
```

## Bearer scopes

Over HTTP every request presents a bearer, and each bearer is a principal with the `read` scope or with `read` and `admin`; over stdio no bearer is checked. Which bearer holds which scope (`MCP_API_KEY`, `MCP_ADMIN_KEY` or one bearer per person with `bearers` in `vaults.yaml`) and what `admin` unlocks are in [`install.md`, "Bearers and scopes of an HTTP deployment"](install.md#bearers-and-scopes-of-an-http-deployment).

## Vault sync

The server keeps its own clone of each vault that has a repository (in `/data/vaults/<slug>` with the generic Compose file) and reindexes in response to the webhooks.

1. **On your machine**: you edit the vault and push (or the Obsidian Git plugin commits every N minutes).
2. **GitHub** delivers the push webhook.
3. **Container**: `/webhook/reindex` validates the HMAC (`X-Hub-Signature-256`, or `X-Hub-Signature` when it carries a `sha256=` digest, like Bitbucket's), brings the vault clone to the tip of the served branch on `origin`, and triggers the incremental reindex. Cost per one-file commit: about $0.0003 in OpenAI.
   - The clone converges with the remote whatever happened upstream: `git fetch --prune origin`; if `HEAD` is an ancestor of `origin/<branch>` (the normal push), `git merge --ff-only`; if not (the branch was rewritten with a force push, the clone diverged, or the fast-forward is rejected), `git checkout -f -B <branch> origin/<branch>`, the same command `infra/bootstrap-vault.sh` uses to adopt a clone at startup, with a `WARNING` in the log naming the old and the new commit. Untracked files stay, as in the bootstrap. If the `fetch` fails (key, network), the clone is not touched, the log carries an `ERROR`, what is on disk is indexed, and `/health` reports `last_sync_result: failed`.
   - Only a push to the served branch (`git.branch` in `vaults.yaml`, `VAULT_BRANCH` with `VAULT_PATH`; `main` by default) brings anything into the clone. A signed push to another branch or to a tag, or one that deletes a branch (the one `--delete-branch` sends on merge, or "Close source branch" on merge in Bitbucket), gets 200 with `"status": "ignored"` and runs neither git nor reindex. A Bitbucket Cloud `repo:push` carries no `ref`: it is judged by `push.changes` and runs only if some change leaves the served branch on a commit (`new` of type `branch` with that name).
   - Consecutive deliveries do not trample each other: while a pull and reindex of a vault runs, the ones that arrive add a single extra pass at the end, which brings the latest from the remote. The `reindex` tool takes the same lock, so two git updates never run on the same clone.

### Manual reindex

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

## Server troubleshooting

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

Why several Compose constructs behave differently on a platform that **orchestrates with Docker Swarm**: its troubleshooting docs talk about `docker service update --endpoint-mode dnsrr` and the `tasks.<app>` prefix, which are Swarm syntax. This stack, however, is deployed with `docker compose`, not `docker stack deploy`: the stack schema rejects `pull_policy` and the long form of `depends_on`, and ignores `build:`, so `infra/compose.yml` does not even parse that way. Two practical consequences: `restart`, `depends_on` with `condition: service_healthy` and `pull_policy` apply exactly as written; and the network, declared without `driver:`, is created as `bridge` (the same declaration under `docker stack deploy` would be `overlay`, which is precisely the reason not to pin the driver).

## Local memory stack (`infra/`)

`infra/compose.yml` with the `memory` profile starts, besides this MCP server (HTTP plus bearer), the authenticated replica of memory, the cloud build of the memory server named by the image below: `memory cloud serve` plus Postgres.

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

Counting changes no response and not the contract: if counting fails, the server logs it and the call goes on. Each process adds its own counts to what is already in the file, under the same lock as the judge's spending, so several processes on one vault do not trample each other. The server writes them on a background thread at most every 2 seconds, and when the process closes or exits; a process killed hard loses the counts of its last 2 seconds or so. Each call is also timed: the `tool_latency` section keeps, per UTC day and tool, a histogram over fixed buckets (1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000 and 5000 ms, then `inf`) for the same 120 days, with no client, principal or argument. A binary older than this section drops it when it rewrites the file.

`doctor` reads them without writing anything, in `vaults[i].tool_calls` of the vault with `"default": true`:

| Key | What it carries |
|---|---|
| `last_7_days`, `last_30_days`, `kept` | `calls`, `errors`, and calls `by_tool`, `by_client` and `by_principal` over the last 7 and 30 UTC days and over everything kept |
| `first_day`, `last_day`, `days_kept` | First and last day with calls, and the retention (120) |
| `deprecated` | Calls, over everything kept, to a tool the contract marks `knowledge/deprecated` (`search_code`, retired in 2.0.0: under `all` its `E_TOOL_RETIRED` counts, under `legacy` its redirect) |
| `unregistered` | Calls to a name the contract does not define (a client with an old catalog, or a probe) |
| `latency_ms` | Only once some call was timed: per tool, over every kept day, `calls`, `p50` and `p95` (the upper bound of the bucket, in ms; `null` in the open bucket) |

The procedure to read them on a deployment is in [`docs/runbooks/serve-multiple-vaults.md`](runbooks/serve-multiple-vaults.md), section "Reading per-tool calls".
