# Scripts

## setup.sh -- First-Time Setup

Installs dependencies, creates `.env` and runs `cloudfrog-knowledge migrate-index`, which prepares the vault's `index.sqlite` without embedding anything. The index rebuilds from the vault on the first tool call; there is no snapshot to download or upload.

```bash
./scripts/setup.sh
```

## weekly-stale-report.py — Stale documents report, by owner

Reads the frontmatter of the versioned vault, applies the per-type thresholds from
`review.thresholds_days` (the vault's `vault.yaml`, or the `config.json` named by
`KNOWLEDGE_CONFIG_JSON`) and emits the list of stale documents
**grouped by the team that has to revalidate them**.

It does not use OpenAI and does not need an index: the same
command runs on a laptop and inside a GitHub Actions job with
nothing more than a clone of the repository.

### How the owner is resolved

Only `type: service` hubs declare `owner` (vault contract:
`Conventions/Frontmatter.md`). Runbooks and playbooks declare `service`.
The report walks the chain `document.service → hub type:service →
hub.owner`, so it delivers the owner without duplicating the field across hundreds of
hand-curated documents. When the chain does not resolve, the document falls into
the `unassigned` group instead of being assigned to someone for convenience.

The `get_stale_docs` tool (`knowledge/review.py`) uses the same resolution,
so the report and the tool cannot give different answers.

### Usage

```bash
# Full report as text
uv run python scripts/weekly-stale-report.py

# Only one team's queue
uv run python scripts/weekly-stale-report.py --owner dev-php

# Artifacts for a build
uv run python scripts/weekly-stale-report.py --format markdown --output reports/stale-report.md
uv run python scripts/weekly-stale-report.py --format json    --output reports/stale-report.json

# Fixed reference date (reproducible)
uv run python scripts/weekly-stale-report.py --today 2026-09-05

# Require zero stale documents (exits with code 1 if there is any)
uv run python scripts/weekly-stale-report.py --fail-on-stale
```

Exit codes:

| Code | When |
|---|---|
| `0` | Report emitted. With `--fail-on-stale`: the vault was shown to be clean. |
| `1` | `--fail-on-stale` was passed and something needs attention: a stale document, **or** one whose age cannot be read. |
| `2` | The check could not be made: the vault does not exist, `--today` is not a valid date, `--owner`/`--type` match nothing in the vault, or the scan read no curated document. |

Two deliberate decisions here:

- **A nonexistent `--owner` or `--type` is rejected.** A misspelled owner
  used to filter everything out, leave `totals.stale` at zero and turn the gate green
  on top of a vault with stale documents. A filter that matches nobody does not
  prove that nobody is stale: it proves the filter is wrong. The message lists
  the values that do exist.
- **A scan that read zero documents is not a pass.** It is a wrong
  path or an empty clone. `OK: 0` never counts as a PASS.

Left out of the corpus, both here and in `get_stale_docs`: anything under
a directory whose name starts with a dot (`.trash`, `.space`, `.obsidian`), the folder of
memories exported from memory, and the Obsidian templates whose frontmatter
still has `{{date}}` placeholders.

### What cannot be read is listed, not silenced

A document can escape the review rules in two ways, and
both are reported separately with name and owner:

- **Unreadable frontmatter**: no YAML parser accepts it, so the
  index does not see it either.
- **Unreadable `last_updated`**: the field is there but is not a date
  (`not-a-date`, or the `YYYY-MM-DD` placeholder of an unrendered template).
  This case was invisible on both paths: it did not show up as stale, did not count as
  "missing `last_updated`" and appeared in no list. "There is no data" and "I
  could not read the data" are different answers and are now counted differently.

Neither becomes "stale": inventing an age would be
worse than not reporting it. They are listed so that someone repairs the frontmatter, and
they count toward `--fail-on-stale`.

## notify-stale-report.sh — Owner notification

Posts the report to a chat webhook. The URL is read from the repository secret
`STALE_REPORT_WEBHOOK_URL` (a GitHub Actions secret) and is never printed, not even when the request fails. The
`curl` diagnostics are shown, but passed through a redaction that
replaces the webhook URL and any other absolute URL that `curl`
mentions (a redirect target, for example).

Without that variable the script is a documented no-op and exits with `0`, so the
workflow stays green until someone provisions the webhook.

```bash
STALE_REPORT_WEBHOOK_URL=... bash scripts/notify-stale-report.sh reports/stale-report.md
```

Exit codes:

| Code | When |
|---|---|
| `0` | Sent, or documented no-op (no webhook configured). |
| `1` | Delivery was attempted and failed: a non-2xx response, or `curl` did not complete the request. |
| `2` | The report file it was asked to send does not exist. |
| `3` | A binary it depends on is missing (`curl`, `python3`). It could not even try. |

**Why `3` exists.** A minimal image such as `python:3.12-slim`
ships `python3` but **not** `curl`. The script sent `curl`'s error output
to `/dev/null`, so `curl: command not found` reached the
operator as "the webhook did not respond (network failure or invalid URL)", with
the receiver alive inside the same container. Now the dependencies are
checked before anything else, even before the no-op: a step that could not
send even if it were configured is broken **today**, and finding out the day the
secret is loaded is exactly the failure this guard prevents. The job
that calls the notification in the vault's workflow runs on `ubuntu-latest`, which already
ships `curl` and `ca-certificates`.

Variables it reads (names only; the values live in the repository settings on GitHub):

| Variable | Required | What it is for |
|---|---|---|
| `STALE_REPORT_WEBHOOK_URL` | no (without it, no notification) | Chat webhook that receives the report. Stored as a repository *secret*. |
| `STALE_REPORT_MAX_CHARS` | no (default 3500) | Cut-off for the chat message. The full report is always kept as a run artifact. |

Canary battery (13 cases: the four exit codes pinned with exact
equality, the two missing dependencies, and the URL never leaking
in any branch — including the branch that does print the `curl` diagnostic,
because "does not leak" is trivially satisfied by printing nothing). Note that the
script's own output, which the canaries assert, is still in Spanish (for example
`Notificación desactivada: STALE_REPORT_WEBHOOK_URL no está definida.`):

```bash
/bin/bash scripts/tests/notify-stale-report-canaries.sh
```

Run it on Linux too, not only on a laptop: a battery that was
only executed on macOS says nothing about the Linux runner where the
step failed.

## Cadence: local launchd → GitHub Actions

A `launchd` agent only fires while one particular laptop is on and
logged in. It suits what originates on that laptop —the Confluence sync and
the local memory of memory—; it does not suit a governance cadence that several
teams will be looking at.

That is why the vault's weekly review is not added as a local agent: it lives in the
`stale-report.yml` workflow of the vault's repository. That workflow fetches
`scripts/weekly-stale-report.py` and `scripts/notify-stale-report.sh` from this
repository at a pinned commit or tag, generates the three formats as run artifacts
and calls the notification. Its cadence, its secrets (`KNOWLEDGE_DEPLOY_KEY`, and
optionally `STALE_REPORT_WEBHOOK_URL`) and its variables (`STALE_REPORT_NOTIFY`,
`STALE_REPORT_MAX_CHARS`) are configuration of that repository on GitHub, not a
file in this one.

The `launchd` agents remain responsible for the Confluence sync and for the
memory bridge, which are not part of the weekly review. The server's index has
no local agent: it is reindexed when the GitHub push webhook notifies it (see
"Vault sync" in the main README), and the memory bridge goes in through that same
door because it ends with a push.

## weekly-memory-bridge.sh — Memory → vault bridge (weekly launchd)

A `launchd` agent (for example, Sunday 20:00 with its log in `/tmp`) runs this script, which chains four
steps on the `main` branch of the **vault repository**: the checkout that
`VAULT_PATH` points to or, without it, the default vault of `vaults.yaml` (a clone of the
vault repository). This repository receives
no commit from the bridge:

1. `git pull --ff-only`: the export is incremental over the committed
   state (`90 - Memory/memory/.memory-sync-state.json`), and a push from
   a branch that is behind is rejected anyway.
2. `export_memories.py --project knowledge`: dumps into `90 - Memory/`
   the observations of type `decision` and `discovery` (config
   `memory.export_types`), dropping hubs, types outside the allowlist and
   already promoted memories (`knowledge_ref`).
3. `memory sync --cloud`: replicates the local memory to the stack's memory-cloud.
   If it fails, it warns and continues: it does not block the vault.
4. `git commit` + `git push` of **only** `90 - Memory/` in the vault
   repository. The push triggers the server's reindex webhook. If the only thing that
   changed is the state timestamp, there is no commit: that push would only
   trigger a reindex for nothing.

Variables: `MEMORY_PROJECT` (default `knowledge`), `BRIDGE_BRANCH`
(default `main`; on any other branch it exits with code `3` without touching anything),
`MEMORY_BIN` (the `memory` binary; default `/opt/homebrew/bin/memory`) and `BRIDGE_DRY_RUN`
(`1` exports and reports, but never fast-forwards, commits or pushes). The only argument
it accepts is `--dry-run`, equivalent to `BRIDGE_DRY_RUN=1`. Its own exit codes: `2` unknown argument, `3` wrong branch
in the vault repository, `4` could not fast-forward, `5`
the vault is not a git checkout.

Dry run with no commit or push, with the same minimal environment that `launchd` uses
(`BRIDGE_DRY_RUN=1` and `--dry-run` do the same):

```bash
env -i PATH=/usr/bin:/bin:/usr/sbin:/sbin HOME=$HOME \
  /bin/bash scripts/weekly-memory-bridge.sh --dry-run
```

Instead of the fast-forward it only fetches and reports how far the branch is behind and ahead of
`origin`. It really exports (the files are left in the working tree), prints the
`git status --porcelain` of `90 - Memory/` and runs `git push --dry-run` as an
authentication test. To force a real run:

```bash
launchctl kickstart gui/$(id -u)/<agent label>
tail -f <agent log>
```

## build-platform-graph.sh — Multi-repository platform graph

Merges the per-repository `graphify` graphs into a single platform `merged-graph.json` and
writes, next to it, a provenance sidecar with the `built_at_commit` of
each input. The sidecar exists because `graphify merge-graphs` loses that datum: its
output has five top-level keys and none is `built_at_commit`, and ADR-026
forbids a project card with a graph summary and a null `graph_commit`.

The default inputs are the ones declared in `graph.targets` of the default
vault's `Contexts/config.json` (ADR-039, ADR-047), resolved by the repository registry (`knowledge.graph_targets.graph_targets_detail()`,
invoked with `uv run` from this very checkout). A declared target is merged
only if it resolves to a directory on this machine **and** that directory already has
`graphify-out/graph.json` — the script never builds a graph, that is the job of
`bootstrap-graph`, repository by repository. Everything else —unresolved on this
machine, no graph yet, or stale against its repository's HEAD without
`--allow-stale`— is skipped and named, never silently dropped: it ends up in
`skipped_targets` of the sidecar and in the table "Objetivos de `graph.targets` sin fusionar"
(Spanish, as the script still prints it; "`graph.targets` entries not merged") of the `.md` report.

No input is hand-written in the script: all of them come from `graph.targets` and from the
repository registry. A checkout vendored inside another is read in place,
like any other target, as long as it has its own `.git`.

```bash
# Normal run: artifacts in platform-graph/ (ignored by git)
./scripts/build-platform-graph.sh

# Another destination, or explicit inputs
./scripts/build-platform-graph.sh --out /tmp/pg \
  --source /path/to/repo-a/graphify-out/graph.json \
  --source /path/to/repo-b/graphify-out/graph.json::/path/to/repo-b
```

Exit codes, each with at least one canary that produces it and another that does not:

| Exit | What triggers it |
|---|---|
| 0 | merged graph and sidecar written (it may carry skipped declared targets: that is recorded, not a failure) |
| 2 | usage error, nonexistent input, declared repository that git cannot read, two inputs that would resolve to the same merge prefix, or fewer than two inputs survive discovery/the staleness filter |
| 3 | input without `built_at_commit` (ADR-026 does not allow a graph fact without a commit) |
| 4 | an explicit `--source` that is stale against its repository's HEAD, degradable with `--allow-stale`; a stale `graph.targets` target, by contrast, is skipped (it does not die) unless `--allow-stale` is passed, in which case it is included with `fresh: false` |
| 5 | the credential scan did not come out clean: it found a shape, or it could not run. In both cases it deletes the temporary file and does not publish |
| 6 | retired. No path in the script produces it; it is left out of reuse so that an old reference to "exit 6" is not reinterpreted as something else |
| 7 | `graphify merge-graphs` failed |
| 8 | two inputs come from two different checkouts of the same repository. The graph reads each repository through a single copy: a repository can be on disk several times, one deployment slot each, and merging surfaces from two slots describes a tree that exists in neither. It fires on the pair of **checkouts**, not on the pair of commits, because two slots can coincide on `HEAD` today and diverge with the next commit (ADR-048 §3) |

No gate fails open: "I found nothing" and "I could not look" are different
results, and the second one never lets anything be published. A declared repository that git cannot
read stops the run (exit 2) instead of recording `fresh: null`; an input with *no*
repository does continue, but warns and stays in the sidecar as `fresh: null`.

The identity that exit 8 compares is the URL of the `origin` remote, normalized without a
trailing slash or `.git` suffix. An input without `origin` is "unknown identity", never
"matching identity": it falls back to its own checkout root, which can only collide
with itself, and the script warns about it. Each sidecar input also carries `repo_root`,
`repo_identity` and `repo_branch` —the branch, because the commit alone does not distinguish two copies
on different branches as long as neither has advanced—, and the sidecar closes with
`sibling_checkouts`: the other working copies of each merged repository that the
registry knows about, with their branch and their uncommitted files. They are not inputs and gate
nothing; they are there so that what the artifact leaves out gets written down. A run with
an explicit `--source` does not consult the registry and leaves them empty.

The credential scan lives in `scripts/lib/scan-credential-shapes.py` and can be
run on its own over any file (0 clean, 1 found, 2 could not look). It reports the
rule and the line number, never the value. It has three layers, because anchoring to
provider prefixes only finds credentials from providers that publish a prefix:

1. Known **prefixes**: `AKIA`, `ghp_`, `AIza`, `ATATT`, `sk-ant`, `eyJ...`, PEM.
2. **Shape in context**: `<name> = <value>` and `?<name>=<value>` **with optional
   quotes**, plus the legacy authentication parameter names (`authpt`, `authpw`,
   `authtoken`), which carry neither a prefix nor a keyword. Requiring quotes is what
   let `password=<value>` through; anchoring to a prefix is what let through
   `authpt=<value>`.
3. **Opaque value without context**: an alphanumeric string of 32 or more characters that
   mixes lowercase, uppercase and digits and also changes character class in at
   least 45 % of its contiguous pairs or carries 4.5 bits of Shannon entropy per
   character.

Layer 3 is a heuristic and is documented as such. Measured against 20,000 random
tokens per length, it detects 98.8 % at 32 characters, 99.9 % at 40 and 100 % at
64. Measured against the published platform graph (138.7 MB, 3,677,412 lines) it gives zero
false positives: identifiers like `isOutgoingServer2serverGroupShareEnabled`
stay at 0.31 class change and 4.10 bits, and lowercase hexadecimal digests
never qualify because they carry no uppercase letters.

Measured limit of the tool: `merge-graphs` does not infer a single edge between
repositories, so the platform graph is a set of disconnected components
and `graphify path` between two services gets no answer. It is useful for inventory
and metrics; symbolic navigation remains per repository. The detail is in
`platform-graph/merged-graph.report.md` and in ADR-023.

Canary battery. It runs on the `bash` 3.2 that ships with macOS and on `bash` 5; it pins each
exit code by exact equality, never by "non-zero". The script's report text and
its canaries' expected output are still in Spanish:

```bash
/bin/bash scripts/tests/build-platform-graph-canaries.sh
/opt/homebrew/bin/bash scripts/tests/build-platform-graph-canaries.sh
```

Group C —the default input set— declares its own registry and its
own `graph.targets` in a synthetic `config.json` under `tmp`. The seam is `PYTHONPATH`: `knowledge.config` derives the
project root from the resolved location of the package itself, so a copy of
`src/` under the synthetic root turns that root into the project root. A symlink
does not work — `resolve()` follows it back to the real checkout. Case C0 checks the
seam before measuring anything through it, so that a broken seam is reported as such and
not as four regressions of the gate. That way the battery does not depend on what the
machine it runs on declares.

## promote-memories.py — Promoting a memory to curated knowledge

The reverse bridge of `export_memories.py`. That one dumps memories into `90 - Memory/`
as second-class notes, isolated from search; this one takes an observation that has
earned it —pinned and of a type on the allowlist, `decision` or `discovery`— and turns it
into a document with an owner.

**The vault is read-only for this bridge.** Its only writer is a merged
pull request. The script generates the candidate documents outside the vault, along with a
patch that adds them, and stops there. If the output directory would resolve inside the
vault, it aborts before calling memory.

The patch is relative to the root of the repository that contains the vault (for example,
a clone of the vault repository, whose root is the vault: paths start with
`Architecture/…`). It is applied in that checkout, never in this repository. A vault that is not
inside a git repository exits with code `2`.

### The two steps, in the order they go

```bash
# 1. Before the PR: build the package. It touches neither the vault nor memory.
uv run python scripts/promote-memories.py plan --project nextcloud

# 2. After merging the PR: stamp knowledge_ref on each observation.
uv run python scripts/promote-memories.py stamp \
  --manifest build/promotions/<run>/manifest.json
```

`stamp` checks that the document really exists in the vault before stamping anything.
Stamping before the merge would leave the observation pointing to a document that no
checkout has, which is exactly what `memory doctor --check knowledge_ref_dangling`
reports as a defect.

### What `plan` produces

In `build/promotions/<run>/` (configurable in `config.json`,
`memory.promotion.output_dir`):

| File | What it is |
|---|---|
| `documents/<path in the vault>` | One document per candidate, with the observation's content as is and the provenance in the frontmatter |
| `promotion.patch` | Creation patch, with git's real blob id on the `index` line, ready for `git apply` |
| `manifest.json` | The run record: scan counters, and for each candidate its `sync_id`, its path, its `knowledge_ref` and its sha256 |
| `PR.md` | The pull request description, with what was inspected, the documents and the `stamp` command |

### The allowlist lives in memory, not here

`decision` and `discovery` share a property that nothing else has: they state a fact
that outlives the session that produced it. The rest of what memory stores —`bugfix`,
`manual`, session summaries— is a trace of work, and a vault full of traces is
a vault nobody reads.

The list is in memory's code (`internal/store/projects_promotion.go`) and not in
configuration on purpose: widening it is a governance decision with review, not a
flag that someone flips in the small hours. `--types` can only **restrict**; naming a
type outside the allowlist is an error, never a widening.

### Target folder by type

`config.json`, `memory.promotion.target_folders`. A type with no declared folder is an
error and not a default value: dropping a promoted document into a
plausible folder is how a vault ends up with a taxonomy nobody agreed on.

`decision` and `discovery` come in as new vault document types. The
alternative was to relabel them as `architecture` or `reference`, which would claim that a
person wrote them that way. Since they are not in `review.thresholds_days`, they fall under the
`default` threshold (270 days), which is the right cadence for a fact that nobody has
revalidated yet.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | The run finished and its output is trustworthy |
| 2 | Nothing usable to inspect: zero pinned observations, output inside the vault, unreadable vault, empty manifest, or a filter that names nothing promotable |
| 3 | The run is partial and a person is needed: a candidate collides with a document that already exists (`plan`), or a document is still not in the vault because the PR has not been merged (`stamp`) |
| 4 | The environment could not run the bridge: a dependency is missing, the memory binary is missing, or a call to memory failed |

Zero inspected units is never a PASS: a project with nothing pinned exits with
code 2 and says so, instead of reporting "0 candidates" as if it had looked.

### Canary battery

```bash
uv run pytest tests/test_promotion_bridge.py -q
```

## bump-version.sh / check-version.sh -- Package and plugin version

`bump-version.sh X.Y.Z` writes the same number into `version` of `pyproject.toml`, into `version` of `plugin/claude-code/.claude-plugin/plugin.json` and into the `@vX.Y.Z` of the package source in `plugin/claude-code/.mcp.json` and into `version` of `npm/package.json`, regenerates `uv.lock` with `uv lock` (`--no-lock` skips it) and ends with `check-version.sh`. It neither commits nor cuts the tag: the tag is created on the merge of the release pull request.

`check-version.sh [vX.Y.Z]` compares those four values and the project version in `uv.lock`; also the tag, if it is passed as an argument or if `HEAD` has a `v*` one. Read-only. It exits with 0 if everything matches, 1 if something differs and 2 on a usage error.

```bash
scripts/bump-version.sh X.Y.Z
scripts/check-version.sh vX.Y.Z
```

`.github/workflows/release-check.yml` runs it on every `v*` tag: with the tag name when the ref is a tag and with no argument when dispatched from a branch, because a branch name is not a tag and the script rejects it with 2.
