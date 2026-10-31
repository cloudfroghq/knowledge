# Team installation

Three profiles, one line each. All of them come from the public `cloudfroghq` marketplace (`cloudfroghq/claude-plugins`):

| Plugin | What it serves | Tool prefix | For whom |
|---|---|---|---|
| `knowledge@cloudfroghq` | This server over stdio, launched with `uvx` from the public repository's tag | `mcp__plugin_knowledge_knowledge__` | Anyone with their vaults on disk |
| `knowledge-remote@cloudfroghq` | Your organization's deployed HTTP server | `mcp__plugin_knowledge-remote_knowledge__` | Anyone without access to the code |
| `memory@cloudfroghq` | Session memory (the `memory` binary) | `mcp__plugin_memory_memory__` | Everyone |

`knowledge` and `knowledge-remote` are not installed together: they would expose the same catalog twice.

## Prerequisites

- Claude Code 2.1.275 or later, the same minimum the `cloudfroghq/claude-plugins` marketplace requires (`claude --version`).
- For `knowledge` (stdio):
  - `uv` (`brew install uv`) with a `uv` CPython: the plugin launches the server with `uvx --managed-python --python 3.12`, which downloads a managed Python 3.12 the first time if you do not have one. This is needed because `sqlite-vec`, the extension that sorts the vectors, loads only in a Python with extensions enabled, and the python.org build for macOS does not ship them. Without it the server keeps working: it sorts the vectors with the standard library (`vector_backend: python` in `doctor` and `/health`, about 110 ms per query instead of about 10) and gives the same result; only the speed changes. `KNOWLEDGE_REQUIRE_SQLITE_VEC=1` turns that fallback into the error `E_SQLITE_VEC_UNAVAILABLE`. `uvx` has to resolve from the environment Claude Code starts with. If you open it from the Dock and it does not inherit your `PATH`, set `UVX_BIN` to the absolute path (`/opt/homebrew/bin/uvx`).
  - `bash` on the `PATH` for the plugin's session-start hook (`#!/usr/bin/env bash`): the doctor's 8-second cap needs job control without a terminal, which `dash` (the `/bin/sh` of Debian and Ubuntu) does not provide. macOS and desktop distributions ship it; Alpine does not by default (`apk add bash`). Without `bash` the hook does not start, but the MCP server does not depend on it: you only lose the `doctor --brief` warning.
  - Access to `github.com` over HTTPS: `uvx` fetches the public repository's tag over `git+https`, with no SSH key or credentials.
  - A vault registry at `~/.config/knowledge/vaults.yaml` (or the file you choose in the `vaults_config` option). The format is in [`runbooks/serve-multiple-vaults.md`](runbooks/serve-multiple-vaults.md).
  - `OPENAI_API_KEY` in the session environment if any vault uses the `openai` provider. A variable defined only in your shell configuration does not reach Claude Code when it is opened from the Dock; there you need `launchctl setenv` (or a LaunchAgent that does it at login), or the `none` provider.
- For `memory`: the `memory` binary on the `PATH` (below, with or without Homebrew).

## With code access (stdio with your vaults)

```bash
claude plugin marketplace add cloudfroghq/claude-plugins && claude plugin install knowledge@cloudfroghq --scope user && claude plugin install memory@cloudfroghq --scope user && uvx --managed-python --python 3.12 --from 'git+https://github.com/cloudfroghq/knowledge.git@vX.Y.Z' cloudfrog-knowledge doctor && memory setup claude-code
```

- The `uvx … doctor` at the end **warms up the cache** of `uv` (the first start clones the tag, downloads about 15 MB of dependencies and, if needed, the managed Python, and can take longer than the 30 s Claude Code waits for an MCP server by default) and diagnoses the installation: it exits with 0 if everything is fine.
- Add `"MCP_TIMEOUT": "60000"` to the `env` block of `~/.claude/settings.json` as a safety net for the first start.
- `memory setup claude-code` regenerates the memory tools' permissions.

Check: `/mcp` shows `plugin:knowledge:knowledge` and `plugin:memory:memory` connected.

## Without code access (HTTP with `knowledge-remote`)

`knowledge-remote` connects to the HTTP server your organization deploys: it requires that it serve over TLS and hand out one bearer per person. The MCP endpoint URL (`https://<deployment>/mcp`), the bearer and the scopes of each deployment are documented in its vault, under `Deploy/`. As long as the deployment does not serve over TLS or hand out one bearer per person, no bearer is distributed.

```bash
export CLAUDE_CODE_PLUGIN_PREFER_HTTPS=1 && claude plugin marketplace add cloudfroghq/claude-plugins && brew install cloudfroghq/tap/memory && claude plugin install memory@cloudfroghq --scope user && claude plugin install knowledge-remote@cloudfroghq --scope user && memory setup claude-code
```

- `CLAUDE_CODE_PLUGIN_PREFER_HTTPS=1` makes Claude Code clone the marketplace over HTTPS: without a GitHub SSH key it is mandatory, also for updates. Leave it exported in your shell.
- Without Homebrew, instead of `brew install cloudfroghq/tap/memory` (macOS arm64; on Linux replace `darwin_arm64` with `linux_amd64`):

  ```bash
  gh release download -R cloudfroghq/memory-releases -p 'memory_*_darwin_arm64.tar.gz' -p checksums.txt && shasum -a 256 -c checksums.txt --ignore-missing && tar xzf memory_*_darwin_arm64.tar.gz memory && install -m 0755 memory /usr/local/bin/memory && memory --version
  ```

  The repository is public: `gh release download` does not require logging in, and `curl -LO` against the same URLs also works.
- Afterwards, inside Claude Code: `/plugin` → `Installed` → `knowledge-remote` → `Configure`, with the URL `https://<deployment>/mcp` and your personal bearer. Outside the session: `claude plugin configure knowledge-remote@cloudfroghq --values-stdin < values.json`. Do not use `claude plugin install … --config` for the bearer: it ends up in your shell history. Restart Claude Code to apply it.
- Without the plugin, the same server is registered by hand: `claude mcp add -s user -t http knowledge https://<deployment>/mcp -H 'Authorization: Bearer <bearer>'`.

Check: `curl -s https://<deployment>/health` responds `status: ok` with `contract_version`, and `/mcp` inside Claude Code shows `plugin:knowledge-remote:knowledge` and `plugin:memory:memory` connected.

## Install the CLI

`init`, `doctor`, `vault-lint` and the rest of the subcommands run from your terminal. The same package is published through three channels, always with the same version:

| Channel | Install once | Run without installing |
|---|---|---|
| PyPI (`cloudfrog-knowledge`) | `uv tool install --managed-python --python 3.12 cloudfrog-knowledge` | `uvx --managed-python --python 3.12 cloudfrog-knowledge <subcommand>` |
| npm (`@cloudfrog/knowledge`) | `npm install -g @cloudfrog/knowledge` | `npx @cloudfrog/knowledge <subcommand>` or `pnpm dlx @cloudfrog/knowledge <subcommand>` |
| Git tag | `uv tool install --managed-python --python 3.12 git+https://github.com/cloudfroghq/knowledge.git@vX.Y.Z` | `uvx --managed-python --python 3.12 --from git+https://github.com/cloudfroghq/knowledge.git@vX.Y.Z cloudfrog-knowledge <subcommand>` |

- Each installation leaves two commands that are the same program: `knowledge` and `cloudfrog-knowledge`. The usage (`--help`), `--version` and the hints of `doctor` and `init` name the one you ran; `KNOWLEDGE_COMMAND_LABEL` still takes precedence over both (see the [README](../README.md), "Command label in hints").
- `--managed-python --python 3.12`: the package declares `requires-python >=3.12,<3.13`, but `uv` ignores the upper bound when choosing an interpreter and, without `--python`, would use the first newer Python it finds. The `uv`-managed CPython also loads `sqlite-vec` (see Prerequisites).
- The npm package ships no Python: on the first run it downloads `uv` 0.10.4 from its GitHub release, checks its SHA-256 against the checksum bundled inside the package, stores it in `~/.cache/cloudfrog-knowledge/uv/` (`%LOCALAPPDATA%` on Windows) and runs `uv tool run --managed-python --python 3.12 --from cloudfrog-knowledge==<version> <command>`. It works on macOS (arm64, x64), Linux (x64, arm64; glibc or musl) and Windows (x64, arm64). Behind a proxy that Node does not traverse, point `CLOUDFROG_KNOWLEDGE_UV` at a `uv` you already have. A package failure before the CLI starts prints `<code>: <message>` on stderr and exits with 2.
- Pin the version when you use it from a script or an `.mcp.json`: `cloudfrog-knowledge==X.Y.Z` (or `uvx … cloudfrog-knowledge@X.Y.Z`), `npx @cloudfrog/knowledge@X.Y.Z` or the tag `@vX.Y.Z`. Throughout this guide `vX.Y.Z` / `X.Y.Z` is a release version: take the latest one from the [releases](https://github.com/cloudfroghq/knowledge/releases) (the docs do not track `scripts/bump-version.sh`).

Check: `knowledge --version` (or `npx @cloudfrog/knowledge --version`) prints `knowledge X.Y.Z`; `knowledge doctor` diagnoses the installation with your vaults.

## Initialize the client

A vault that declares `Contexts/config.json` ships the context package of each application (`Contexts/<app>/`: `CLAUDE.md`, `.mcp.json` and `.claude/`). Linking it into your checkouts is your machine's job, never the server's: a remote deployment cannot see your disk. With your vaults already registered (`~/.config/knowledge/vaults.yaml`):

```bash
cloudfrog-knowledge init --dry-run --repo <id> && cloudfrog-knowledge init --repo <id>
```

Each machine chooses in which checkouts it installs a package: the `apply` list of its `repos.local.json`, with the registry's identifiers (`Contexts/config.json`). The registry is the same for everyone; the selection is yours.

- `--repo ID` adds `ID` to `apply` (repeatable; `--repo SLUG:ID` limits it to one vault). You can select a checkout you have not cloned yet: `init` installs it when it appears.
- `--all` adds every checkout present on this machine that has a package.
- `--drop ID` removes it from `apply` and deletes only that checkout's links that point to `Contexts/<package>/`; your files, your own links, a copy (`copy` mode) and the ignore entries stay.
- With no flag, `init` installs what `apply` already says. On a new machine (no `apply` and no installed packages) it asks you in a terminal which checkouts you want (`all`, `none`, a list of ids, or empty to decide later); with `--yes` or without a terminal it installs nothing and lists the present checkouts with the `--repo ID` hint.
- The first run of this version on a `repos.local.json` without `apply` creates it with the checkouts that already have their package installed (links to `Contexts/<package>/`, or the copy `init` made in `copy` mode): your configuration does not change.
- An id that the registry no longer declares leaves `apply` with a warning. `--dry-run` shows the selection's changes (`would add`, `would remove`) without writing anything.
- `apply` only decides where the packages are installed: the graph targets and the server read the registry without looking at it.

- For each vault with `Contexts/config.json`, it locates each root: `--root NAME=DIR` (or `--root SLUG:NAME=DIR` for a single vault), your `KNOWLEDGE_<ROOT>_ROOT` variables, your `repos.local.json`, the `default` the vault suggests or, if none of those exists, the git checkouts whose `origin` matches a declared remote under `~/Projects` (or under each `--search-dir`). If it does not find a root it asks you; with `--yes` it skips it.
- It writes `~/.config/knowledge/<slug>/repos.local.json` (under `$XDG_CONFIG_HOME` if you have it defined), links the package of each selected application whose checkout you have, reports as `not-selected` the present checkouts you did not select, and skips the ones you do not have, without error.
- It ends by saying where each file lives: `vaults.yaml`, each `repos.local.json` and each linked checkout. Edit them and run `init` again: a second run with no changes writes nothing.
- It installs only into a checkout whose `origin` is the declared remote: if it cannot be read (no remote, no `.git`) it is `unverified`, `init` exits with 1 and does not install unless you pass `--force`; if it is another repository, it is an error. An application's `path` never leaves its root (`..` is `E_CONTEXTS_INVALID`).
- It never writes inside a vault. If a checkout already has its own unversioned `CLAUDE.md` or `.mcp.json` (also a symlink of yours), `init` does not touch them and exits with 1: `--force` sets them aside as `<name>.pre-ai-context.bak-<epoch>`, and a copy (`copy` mode) that drifted from the package is resynchronized with `--refresh`.
- `link` is the only recommended mode: the package lives in the vault and each checkout sees it as is. `copy` still works for a vault that still declares it, but no case requires it.
- A `.claude` that is a **real directory** in the checkout (Claude Code creates its worktrees in `<checkout>/.claude/worktrees/` and does not accept a linked `.claude`) stays real: `init` links inside it each entry of the package's `.claude` (`agents`, `commands`, `skills`, `settings.json`…) and never touches what the package does not ship (`settings.local.json`, `agent-memory/`, `worktrees/`). An unversioned real entry is a conflict that `--force` sets aside inside the same `.claude`; a link that `init` made to a vault that has since moved is relinked automatically. The ignore entries name each link (`/.claude/agents`), never the real directory.
- What the checkout **versions in git** is its own: a versioned `CLAUDE.md` or `.mcp.json`, or a versioned `.claude/` entry, is kept with the warning "owned by the checkout", without conflict and without changing the exit code. A versioned `.claude/<collection>/` (for example `.claude/skills/` with a skill of the repository) also receives the package entries it lacks; further down, a skill or an agent is a unit and is not merged.

`cloudfrog-knowledge` in these examples is any of the forms in "Install the CLI": `knowledge`, `uvx … cloudfrog-knowledge` or `npx @cloudfrog/knowledge`.

A vault that has no applications to link declares `"repos": {"roots": [], "entries": []}` (and `"graph": {"targets": []}`) in its `Contexts/config.json`: `init` reports it as `no-apps`, writes nothing and exits 0, and `doctor` does not count it as `init pending`. A `roots` or `entries` that is not an array, or a malformed entry, is still `E_CONTEXTS_INVALID`; the empty registry is valid only in a vault's declaration.

In `link` mode, Claude Code writes into the vault through the linked `.claude` (`settings.local.json`, `agent-memory/`, `worktrees/`): the vault's `.gitignore` has to cover `Contexts/*/.claude/settings.local.json`, `Contexts/*/.claude/agent-memory/` and `Contexts/*/.claude/worktrees/`. If the vault is a git repository and does not cover them, `init` warns with `W_CONTEXTS_GITIGNORE` (it does not block).

Check: `cloudfrog-knowledge doctor --brief` stops saying `init pending for vault(s) …`, and the plugin's session-start hook goes quiet again.

On a machine that consumes a remote server (no index, no embeddings key, no bearers of its own), `cloudfrog-knowledge doctor --client` checks only what is yours: that the vault exists and can be read, that its `Contexts/config.json` loads, your `repos.local.json` and the state of each checkout (`init --dry-run`, without scanning or prompting). A present checkout you did not select is `not-selected` (information, not a problem); a selected one without its package counts as `init` pending (code 0); one with links to its package that is not in `apply` is the warning `W_CONTEXTS_LINKED_NOT_SELECTED` (code 0: add it with `--repo ID` or remove its links with `--drop ID`), and a `repos.local.json` without `apply` is the warning `W_CONTEXTS_SELECTION_MISSING` (code 0: the next `init` creates it). A checkout that is a git repository without an `origin` remote is a warning (`W_CONTEXTS_CHECKOUT_UNVERIFIED`, code 0); a conflict (`E_CONTEXTS_CONFLICT`) or a checkout that is not the declared one (`E_CONTEXTS_CHECKOUT_INVALID`) exits with 1. `--brief` works the same.

A vault that wraps `init` and `doctor` in its own scripts (for example `Contexts/scripts/init.sh` and `Contexts/scripts/doctor.sh`, which pin the package version and the vault) sets `KNOWLEDGE_COMMAND_LABEL='Contexts/scripts/{command}.sh'` in them: the hints of `init` and `doctor` then name those scripts and not this CLI. See the [README](../README.md), "Command label in hints".

## Bearers and scopes of an HTTP deployment

Each bearer of an HTTP deployment has scopes: `read` uses the server and `admin` can also call `reindex`, `sync_confluence` and `POST /webhook/reindex` with a manual bearer. A bearer without `admin` receives `E_SCOPE_REQUIRED` on those tools (403 on the webhook); the others respond the same. Over stdio there are no bearers and nothing is checked.

| Variable or key | What it does |
|---|---|
| `MCP_API_KEY` | Without `MCP_ADMIN_KEY`, it has `read` and `admin`, as in 1.2.0. With it, only `read`. |
| `MCP_ADMIN_KEY` | Bearer with `read` and `admin`. It must differ from `MCP_API_KEY` (if they coincide, startup exits with `E_MANIFEST_INVALID`). |
| `bearers` in `vaults.yaml` | One bearer per person (`id`, `token_env`, `scopes`); it replaces `MCP_API_KEY` and `MCP_ADMIN_KEY`: with `bearers` declared, those two variables are ignored. See [`runbooks/serve-multiple-vaults.md`](runbooks/serve-multiple-vaults.md), section 5. |

The bearer you put in `Configure` of `knowledge-remote` (or in `claude mcp add … -H 'Authorization: Bearer <bearer>'`) is read-only unless whoever administers the deployment gives you one with `admin`. `cloudfrog-knowledge doctor` shows the server's `auth` block: ids, scopes and variable names, never a token.

## Memory-bridge: publish a vault to the local memory

A vault that declares `memory-bridge` in `features` (and `memory.project` in its `vault.yaml`) publishes its project to the memory server running on your machine: the card (`cloudfrog-knowledge memory-bridge sync --card`) and the runbook index (`memory-bridge sync`, and every `reindex` over stdio). It works only over stdio: an HTTP deployment does not load the extension (`W_EXTENSION_UNAVAILABLE_HTTP`).

| Variable | What it does |
|---|---|
| `KNOWLEDGE_MEMORY_URL` | Where memory listens; `http://127.0.0.1:6366` by default. Only `http` on loopback: any other URL is rejected with `E_MEMORY_URL_NOT_LOOPBACK`. |
| `MEMORY_HTTP_TOKEN` | The bearer sent with every write. Without it nothing is written (`E_MEMORY_TOKEN_MISSING`). |
| `KNOWLEDGE_MEMORY_REQUIRE_TOKEN` | With exactly `0`, writes go out without a bearer. |
| `MEMORY_BIN` | The binary that `memory-bridge promote` runs; by default, `memory` on the `PATH`. |

Local exception: a person's `memory serve` currently runs without `MEMORY_HTTP_TOKEN` (the opencode, pi and obsidian integrations do not send a bearer yet), so its `requireAuth` accepts writes without it. For that memory, enable the bridge with `KNOWLEDGE_MEMORY_REQUIRE_TOKEN=0` in the knowledge server's environment, never in a deployment:

```bash
KNOWLEDGE_MEMORY_REQUIRE_TOKEN=0 uv run cloudfrog-knowledge memory-bridge sync --vault acme --card
```

The stdio server also reads that variable: without it and without `MEMORY_HTTP_TOKEN`, it starts with `W_EXTENSION_UNCONFIGURED` for `memory-bridge`. When `memory serve` runs with a token, export the same `MEMORY_HTTP_TOKEN` in both processes and remove the exception.

Check: `curl -s http://127.0.0.1:6366/projects/<memory.project>/runbooks | jq '.total'` counts the runbooks memory indexed.

## Jira: issues as memory tasks

A vault that declares `jira` in `features`, with `memory.project` and `integrations.jira` in its `vault.yaml`, copies the issues of its Jira projects as tasks of its project in the local memory, one per issue and per `jira_key`. Over stdio only: an HTTP deployment does not load the extension (`W_EXTENSION_UNAVAILABLE_HTTP`) and `sync_jira` does not appear in its catalog.

Only the vaults that declare it sync Jira: without the block, the vault does not enter `jira sync --auto` or `sync_jira`. To enable it on a vault, in a PR of that vault:

1. add `jira` to `features`, `memory.project` and `integrations.jira` (`site`, `projects` and `secrets`) to its `vault.yaml`, with the vault's own variable names: `<SLUG>_JIRA_EMAIL` and `<SLUG>_JIRA_TOKEN`;
2. export those two variables in your session environment (your Jira account's email and an API token for it).

```yaml
features: [jira, memory-bridge]
memory:
  project: acme
integrations:
  jira:
    site: https://acme.atlassian.net
    projects: [ACME]
    secrets: {email_env: ACME_JIRA_EMAIL, token_env: ACME_JIRA_TOKEN}
```

| Variable | What it does |
|---|---|
| `<PREFIX>_JIRA_EMAIL` / `<PREFIX>_JIRA_TOKEN` | Your Jira account's email and an API token for it (id.atlassian.com → Security → API tokens), with the names declared by `integrations.jira.secrets`. Exported in your session environment, never in a file of the repo. Without them, the stdio server starts with `W_EXTENSION_UNCONFIGURED` and `sync_jira` responds `E_SECRET_MISSING` with their names. |
| `MEMORY_HTTP_TOKEN` / `KNOWLEDGE_MEMORY_REQUIRE_TOKEN` | The same as for the memory-bridge: writing tasks requires the bearer unless `KNOWLEDGE_MEMORY_REQUIRE_TOKEN=0` for the local memory without a token. A `--dry-run` does not need them. |
| `KNOWLEDGE_JIRA_AUTOSYNC` | With `0`, the plugin's session hook does not launch the background sync. |

Before the first run, create the project card with its `jira_project` (without it, memory creates a minimal card with a placeholder `jira_project`):

```bash
KNOWLEDGE_MEMORY_REQUIRE_TOKEN=0 uv run cloudfrog-knowledge memory-bridge sync --vault acme --card
uv run cloudfrog-knowledge jira sync --vault acme --dry-run --since -30d
KNOWLEDGE_MEMORY_REQUIRE_TOKEN=0 uv run cloudfrog-knowledge jira sync --vault acme
```

With the plugin, every session that starts with a healthy `doctor` launches `cloudfrog-knowledge jira sync --auto` in the background, detached from the hook: it syncs the vaults that declare `jira`, have their variables exported and have already met `integrations.jira.sync.throttle_minutes` (60 by default). The full procedure, the report's shape and what to do with each code are in [`runbooks/sync-jira.md`](runbooks/sync-jira.md).

## `search` judge (rerank)

A vault that declares a `rerank` block in its `vault.yaml` reorders the `search` results with a judge: Jev, from TypeSafe (`provider: jev`), or Kev, a local server on loopback (`provider: kev`). The block is curated content and enters through a human PR; it names the key's variable, never the key:

```yaml
rerank:
  provider: jev
  default: on                    # search reorders without the caller asking; off: only with rerank="on"
  model: jev-1.13.0              # pinned version: jev-latest is rejected
  key_env: ACME_TYPESAFE_API_KEY    # TYPESAFE_API_KEY only in the default vault
  candidates: 10                 # documents the judge reads, 1-12
  units_per_doc: 2               # blocks per document, 1-3
  timeout_ms: 1500
  max_input_tokens: 20000
  max_usd_per_day: 1.00          # cap per vault per day; 0 means no cap
  conditional_gap: null
  never_send: []                 # path prefixes that never leave the machine, e.g. ["Personal/"]
```

| Variable | What it is |
|---|---|
| `TYPESAFE_API_KEY` / `<PREFIX>_TYPESAFE_API_KEY` | The TypeSafe key, with the name declared by `rerank.key_env`. Exported in the session environment (or the Dokploy service's), never in a file of the repo. Opened from the Dock, Claude Code does not inherit your shell: you need `launchctl setenv`. Without it the server starts anyway, over stdio and over HTTP, with `W_RERANK_KEY_MISSING`, and that vault searches without a judge. |
| `KNOWLEDGE_RERANK` | `off` turns the judge off for every vault in the process. |
| `KNOWLEDGE_RERANK_PROVIDERS` | Allowed providers (`jev,kev` by default). |
| `KNOWLEDGE_RERANK_BASE_URL` | Replaces every judge's URL: `https://api.typesafe.ai` or an `http` on loopback. |
| `KNOWLEDGE_RERANK_TIMEOUT_MS` | Total deadline of each judge call (200 to 60,000 ms). |

A vault that declares the judge names in its `vault.yaml` a variable with its slug as prefix, valid whether or not the vault is the process's default. For it to search with a judge, export that variable in the session (or service) environment; several vaults can carry the same key under different names. In fish: `set -Ux ACME_TYPESAFE_API_KEY $TYPESAFE_API_KEY`. How the key reaches each deployment is documented in its vault, under `Deploy/`.

Cost: about 11,700 input tokens per reranked search at 0.042 USD per million, ≈0.0005 USD; 1,000 searches ≈0.49 USD. Repeated queries come from the process cache at no cost. Content travels to `api.typesafe.ai` only from a vault that declares `jev`, never from `90 - Memory/` or with `include_memories=true`.

Check: `cloudfrog-knowledge doctor` shows a `rerank` block per vault with `key: ok` (or `missing:<VARIABLE>`), and `doctor --brief` ends in `rerank <slug>:jev/on`. A reranked `search` carries `rerank_score` in its rows and `_meta["knowledge/rerank"].applied: true`.

## With the `cloudfrog@cloudfrog` plugin installed

The memory plugin is called `memory@cloudfroghq` and its tools carry the prefix `mcp__plugin_memory_memory__`, the only one listed in the `allowed-tools` of this repository's skills and its context packages. The old plugin `cloudfrog@cloudfrog` (id `cloudfrog:memory`) exposes them with the retired prefix `mcp__plugin_cloudfrog_memory__`: on a machine that keeps it, those skills keep working, but every call to memory asks for permission, unless `~/.claude/settings.json` still allows the old entries. Migrating is recommended; the command below leaves `memory@cloudfroghq` installed.

```bash
claude plugin uninstall cloudfrog@cloudfrog && claude plugin marketplace remove cloudfrog && claude plugin marketplace add cloudfroghq/claude-plugins && claude plugin install memory@cloudfroghq --scope user && memory setup claude-code
```

`marketplace remove` also deletes the saved options of the `cloudfrog@cloudfrog` plugin: check with `claude plugin configure memory@cloudfroghq` which ones were left without a value. Then remove by hand from `permissions.allow` in `~/.claude/settings.json` the `mcp__plugin_cloudfrog_memory__*` entries, which no longer correspond to any tool.

## Check a vault for its PR

`cloudfrog-knowledge vault-lint <path-to-vault>` checks a vault without writing anything. With the `1.3` rules (the default) it requires an ISO `last_updated` on every note, warns about a `type` outside the closed set, rejects a `scope` that is not the vault's `slug`, `global` or one of the `scopes:` in its `vault.yaml`, requires NFC paths without case collisions, checks that every runbook (`type: runbook` under `Runbooks/`, or the manifest's `runbooks.dir`) can be indexed by the memory server, and does not count as a wikilink anything inside code. `--rules 1.2` applies only what `reindex.validation` counts. The manifest's `index.exclude` takes files out of the repeated-stem rule, the source links and the frontmatter rules (`last_updated`, `type`, `scope`): it is what lets `Contexts/*/CLAUDE.md`, which are not notes, need no frontmatter (`index: {exclude: ["Contexts/**"]}`). The `1.2` rules do not read the manifest.

`--fix` writes, and never in a vault this installation serves (`KNOWLEDGE_VAULTS`, your `vaults.yaml` or `VAULT_PATH`): on one it exits with code 2 and `E_WRITE_FENCED` without touching anything. Copy the vault with its `.git`, run `--fix` on the copy and deliver the change through a vault PR. `--out DIR` makes the copy for you (without `.git`, to compare with `diff -ru`).

```bash
cp -R <path-to-vault> /tmp/vault-fix && cd /tmp/vault-fix
cloudfrog-knowledge vault-lint . --fix > /tmp/vault-fix.json   # fixed, remaining, errors, warnings
git diff > /tmp/vault-fix.patch                                # frontmatter lines only
```

`--fix` takes the date from `updated`, `last_review` or `last_verified.date` (in that order) and sets it as `last_updated`; the rest of a `last_verified` map is kept as `verified_against`. Whatever has no date to start from comes out in `remaining` with its reason and is written by hand.

## Update

```bash
claude plugin marketplace update cloudfroghq && claude plugin update memory@cloudfroghq && claude plugin update knowledge@cloudfroghq
```

With `knowledge-remote`, update `knowledge-remote@cloudfroghq` instead of `knowledge@cloudfroghq`. Then `/reload-plugins` inside the session, and `brew upgrade memory` for the binary.

## Diagnostics

- `knowledge` (stdio): `uvx --managed-python --python 3.12 --from 'git+https://github.com/cloudfroghq/knowledge.git@vX.Y.Z' cloudfrog-knowledge doctor` prints JSON with the rule the vault registry chose, the state of each vault (directory, provider, whether the key is defined without showing it, whether there is an index), whether `KNOWLEDGE_HOME` is writable, and the package and contract versions. The plugin's session-start hook runs the brief version and speaks only when something fails, with a line of up to 200 bytes not counting the newline (the cap of `doctor --brief`).
- If a vault declares in `features` an extension without its credentials (for example `confluence` without `CONFLUENCE_EMAIL` and `CONFLUENCE_TOKEN`, or the names set by `integrations.confluence.secrets` in its `vault.yaml`), the stdio server starts anyway and leaves `W_EXTENSION_UNCONFIGURED` in its log with the names of the missing variables; only that extension responds with an error.
- `CONFLUENCE_URL` is the root of the Atlassian site (`https://<org>.atlassian.net`), **without** `/wiki`: this server's client appends `/wiki/api/v2/...` on its own. With a trailing `/wiki` every page responds 404 and `sync_confluence` and `confluence export` count them all in `errors`. The same rule applies to `integrations.confluence.site` in `vault.yaml`. The `CONFLUENCE_URL` of `mcp-atlassian` is a different variable, of a different process, and that one does take `/wiki`.
- A server in `failed` state in `/mcp` with a green doctor is usually startup time: raise `MCP_TIMEOUT` or warm up the cache again.
- Without a network, the stdio server does not start: `uvx` queries the tag on GitHub at every start.
- If `uvx` cannot even start the package (no network, or a tag the remote does not have), the doctor never gets to say anything and the hook reports it with `knowledge: server package failed (uvx rc N); diagnose: …` **once per day and per plugin version**, not on every start. The marker is a one-line file, `${XDG_CACHE_HOME:-$HOME/.cache}/knowledge/session-start-package-failed`: deleting it makes the warning come back on the next start, and if it cannot be written the warning appears every time. Between one warning and the next the server keeps showing as `failed` in `/mcp`. Retrying with `uvx --offline` does not help: with uv 0.10 a git tag is queried on the remote even with that flag. A doctor that does not answer within 8 seconds (cold cache) does not warn; the next start will.
