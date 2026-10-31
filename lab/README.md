# Dockerized lab

Uses fake tenants to prove that the server is not tied to any organization: **a single compose file** (`infra/compose.yml`), **N instances**, and each instance gets its vaults injected through the environment. It also tests the other side: **two fake clients** that run `cloudfrog-knowledge init` on the host, like a person who has their vault cloned and talks to a remote server.

```
acme    -> knowledge-acme    127.0.0.1:18101   acme-handbook (git, file://)   acme-ops (folder)
globex  -> knowledge-globex  127.0.0.1:18102   globex-docs   (git, file://)   globex-notes (folder, no apps)

alice (acme)   HOME=lab/.work/clients/alice   acme-web and acme-deploy in Projects/; acme-api is absent
bob   (globex) HOME=lab/.work/clients/bob     globex-core in Projects/; globex-notes declares no apps
```

Each instance has its read bearer, its `admin` bearer, its index, its clones and its network. Embeddings are `bag-of-words`: no OpenAI key and no network (`OPENAI_API_KEY` is empty on purpose).

## Usage

Requires `docker` (compose v2), `git`, `curl` and `uv`.

```bash
lab/run.sh          # seeds, builds, brings up, waits for /health and runs the two check tables
lab/run.sh --check  # only the checks, against the instances that are already running
lab/run.sh --down   # deletes containers, volumes, networks and lab/.work
```

`run.sh` exits with 0 only if every row of both tables is PASS.

## What is in it

- `seed.sh` builds `lab/.work/` (ignored by git). Servers: two bare repos (`repos/acme-handbook.git`, `repos/globex-docs.git`, which the container sees as `file:///repos/...`) and the folders `vaults/acme-ops` and `vaults/globex-notes`. Each vault ships a `vault.yaml` (with `index.exclude: ["Contexts/**"]`), a `.gitignore` with `Contexts/*/.claude/settings.local.json`, three notes that pass `vault-lint` 1.3 (with their `services.json`), `90 - Memory/` and `Contexts/`: a `config.json` with the `repos` section and, per app, `CLAUDE.md`, `.claude/agents/x.md` and `.mcp.json`. `acme-handbook` also declares `features: [governance]` (for `get_stale_docs`), a stale note (`Architecture/Legacy Notes.md`) and an old `history.md` inside `Contexts/acme-web/`. `globex-notes` is the vault without apps: its `config.json` carries `roots: []`, `entries: []` and `graph.targets: []`. `seed.sh --clients` rebuilds only the clients.
- Clients: each has its own `HOME`, its `XDG_CONFIG_HOME` (`.config/knowledge/vaults.yaml`), its local copy of each vault (a clone of the bare repo, or the folder itself for `acme-ops`) and the fake checkouts (`git init` + `origin`) under `Projects/`. Alice also has a synthetic tree of ~2,000 directories with 60 repos that match nothing, to measure the scan.
- Each tenant has words that the others never use: `zephyr` (acme), `quokka` (globex), `marmot` (acme-handbook), `nautilus` (acme-ops), `pangolin` (globex-docs) and `lemur` (globex-notes). Each context package has one word in its `CLAUDE.md` and another in its `.claude/agents/x.md`.
- `compose.yml` extends the `knowledge-mcp` service of `infra/compose.yml` twice; the only things specific to each instance are `KNOWLEDGE_VAULTS_YAML`, its bearers, its port and its volumes. The bearers are generated in `lab/.work/lab.env` (0600 permissions) and are never versioned.
- `check.py` talks to both servers over HTTP with the `mcp` SDK. `check_clients.py` runs `uv run --no-sync cloudfrog-knowledge ...` as each client, with a minimal environment (only `PATH`, its `HOME`, its `XDG_CONFIG_HOME` and the uv cache): nothing from the real environment or home reaches the process. It starts by rebuilding the clients, so it can be repeated with `--check`. `report.py` prints the table.

## What it checks

Servers (`check.py`):

| Check | What it shows |
|---|---|
| `list_vaults` | acme lists its two vaults and globex its own (`globex-docs` by default and `globex-notes`) |
| vault without apps (`globex-notes`) | it loads, is searched and is read like any other; its word does not appear in `globex-docs` |
| `search` for its own word | each tenant finds its own content |
| `search` for the other tenant's word (`keyword` mode) | no tenant finds the other's content |
| folder vault (`acme-ops`) | it is searched and read just like one with git |
| `vault=` | the vaults of one tenant do not get mixed; another tenant's vault does not exist |
| no bearer, fake bearer, the other tenant's bearer | 401 |
| `reindex` with the read bearer | `E_SCOPE_REQUIRED`; with the `admin` one, it works |
| `POST /webhook/reindex` | 401 without credentials, 403 with read, 202 with `admin` |
| signed `POST /webhook/reindex`, to another branch or deleting it | 200 with `"status": "ignored"` (`not_served_branch`, `ref_deleted`) and no pull; the push to `main`, 202 |
| new commit on the remote + `reindex(pull=true)` | the git-backed vault follows its remote |
| word from `Contexts/<app>/CLAUDE.md` and from `.claude/agents/x.md` | the `CLAUDE.md` is indexed; what hangs under `.claude/` is not |
| `get_stale_docs(vault="acme-handbook", missing_last_updated=true)` | lists the vault's stale note and nothing under `Contexts/` (neither the package's old `history.md` nor the `CLAUDE.md` files without a date): `index.exclude` takes them out of the queue |

Clients (`check_clients.py`, the same rows for alice and for bob):

| Check | What it shows |
|---|---|
| `doctor --brief` before `init` | says `init pending` and names only the vaults that have apps to link |
| `init --dry-run` | plans `would-install` and writes nothing (not even mtimes) |
| `init` | exits 0, installs the apps that are present and reports the absent one as `skipped` |
| `<xdg>/knowledge/<slug>/repos.local.json` | exists for each vault with apps, with the root detected through the `origin` |
| vault without apps (bob) | `init` reports it as `no-apps` and exits 0, does not write its `repos.local.json`, and `doctor` does not count it as `init pending` |
| `CLAUDE.md`, `.mcp.json`, `.claude` | links that resolve to the client's local copy of the vault |
| absent app | `skipped`, and not even its directory is created |
| checkout `git status` | clean: the links are listed in `.git/info/exclude` |
| vault tree before and after | identical: `init` does not write inside the vault |
| `vault-lint` on each copy of the vault | exits 0: `Contexts/` is left outside the frontmatter rules by `index.exclude` |
| second run | everything `unchanged` and no file changes (mtimes and hashes) |
| `.gitignore` of the vault copy | `init` reports `covered` for the clones (they cover `settings.local.json`, `agent-memory/` and `worktrees/`) and `not-applicable` for the folder without git, with no warnings |
| `doctor --brief` after `init` | `ok`: the session hook stays silent |
| scan of ~2,000 directories (alice) | finds the checkouts and reports how long it takes |

## Injection contract

The detail is in [`docs/runbooks/serve-multiple-vaults.md`](../docs/runbooks/serve-multiple-vaults.md) (section "Generic Compose file"). In short:

- `KNOWLEDGE_VAULTS_YAML`: the content of a `vaults.yaml`. An entry with `repo` is cloned or updated at `/data/vaults/<slug>`; one with only `path` is an already-mounted folder and git does not run for it.
- Key for an ssh remote: `<SLUG_IN_UPPERCASE>_GIT_DEPLOY_KEY_B64`, and if it does not exist, `GIT_DEPLOY_KEY_B64`. `file://` and `https://` remotes do not need it (the lab uses `file://`).
- Without `KNOWLEDGE_VAULTS_YAML`, the server serves one vault with `VAULT_PATH`/`VAULT_REMOTE`/`VAULT_BRANCH`, as always.

## Notes

- `/webhook/reindex` and `/health` serve each instance's default vault; the other vaults with `repo` are updated with `reindex(vault=..., pull=true)`.
- If you change the `Dockerfile` or `infra/`, `run.sh` rebuilds the image. To leave the lab without images: `docker image rm knowledge-lab-knowledge-acme knowledge-lab-knowledge-globex`.
- The compose project name is fixed (`knowledge-lab`): a new `run.sh` replaces the instances brought up by another checkout.
- The `init` scan reads the `origin` from git's config file (without launching `git` per repo; `include`, quotes, comments and other odd cases do go to `git`) and is shared among the vaults of a single run. With alice's 2,016 directories and 62 repos: ~60 ms of scanning and ~0.45 s of `init`; launching `git` per repo would cost ~910 ms and ~2.3 s. `check_clients.py` prints the measurement of each run.
- The lab does not touch any real vault.
