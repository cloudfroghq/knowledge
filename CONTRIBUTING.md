# Contributing to cloudfrog-knowledge

Thanks for your interest in improving `cloudfrog-knowledge`. This guide covers how to set up a development environment, which gates a change must pass, the rules that protect the tool contract, and how commits, pull requests and releases work.

Please read the [Code of Conduct](CODE_OF_CONDUCT.md) first. To report a vulnerability, follow [SECURITY.md](SECURITY.md) and do not open a public issue. Questions go through the channels in [SUPPORT.md](SUPPORT.md).

## Scope: server code, not vault content

This repository is the **server**: the MCP server, its CLI, the npm wrapper, the Claude Code plugin and their tests. No vault lives here; `/vault/` is in `.gitignore` so that none comes back.

Each vault has its own repository. A change to vault content is proposed by a human pull request in that vault's repository, with the vault owner as reviewer (D-11): an agent may propose, but never writes vault content. The only folder an automated job writes in a vault repository is `90 - Memory/` (`scripts/weekly-memory-bridge.sh` commits and pushes there, in the vault's repository, never in this one). A pull request to this repository never carries vault content.

## Development environment

- [`uv`](https://docs.astral.sh/uv/) manages the environment and the lockfile (`uv.lock`).
- Python 3.12, provisioned by `uv`. The project declares `requires-python = ">=3.12,<3.13"` and `.python-version` pins 3.12; **Python 3.13 is not supported**. If your system only has 3.13, `uv` downloads a managed 3.12 automatically (or run `uv python install 3.12`); do not point it at another interpreter.
- Node.js 18 or later, only for the npm wrapper in `npm/` (its tests have no dependencies; no `npm install` is needed).
- If your shell exports an empty `UV_NATIVE_TLS=`, unset it: every `uv` call fails with "expected a boolish value".

```bash
git clone https://github.com/cloudfroghq/knowledge.git
cd knowledge
uv sync                     # creates .venv; installs the dev group too (pytest, ruff, import-linter, ...)
uv run knowledge --version  # knowledge X.Y.Z
```

To run the server against something, use a copy of the fixture vault and the offline embeddings provider, as in the README's [Quick start](README.md#quick-start-offline-about-5-minutes):

```bash
DEMO="$(mktemp -d)" && cp -r tests/fixtures/vault "$DEMO/vault"
VAULT_PATH="$DEMO/vault" KNOWLEDGE_INDEX_DIR="$DEMO/index" KNOWLEDGE_EMBEDDINGS_PROVIDER=bag-of-words \
  uv run cloudfrog-knowledge doctor --brief
```

`cloudfrog-knowledge` without a subcommand is `serve`, which over stdio waits for JSON-RPC on stdin; `python -m knowledge.server` is an equivalent entry point. A `.env` at the checkout root is loaded automatically (it never overrides variables already set); `.env.example` is the template to copy it from. With the default `openai` provider you need `OPENAI_API_KEY`; for local work prefer `KNOWLEDGE_EMBEDDINGS_PROVIDER=bag-of-words` or `none`.

`sqlite-vec` needs a Python with loadable SQLite extensions. `uv`'s CPython has them; the python.org build for macOS does not. Without the extension the server and the suite fall back to the standard library (`vector_backend: python`, same results, about ten times slower), and `KNOWLEDGE_REQUIRE_SQLITE_VEC=1` turns that fallback into the error `E_SQLITE_VEC_UNAVAILABLE`.

## Gates

Run these before you open a pull request. They are the same commands ring 1 runs in CI.

| Command | Expected result |
|---|---|
| `uv sync` | exit 0 |
| `uv run pytest` (or `-q`) | all tests pass (unit, tool and contract suites; a few skips are normal, see below) |
| `KNOWLEDGE_REQUIRE_SQLITE_VEC=1 uv run pytest` | the same, but it fails if `sqlite-vec` does not load in this Python; this is how ring 1 runs |
| `uv run ruff check` | `All checks passed!` |
| `uv run lint-imports` | `Contracts: 4 kept, 0 broken.` |
| `(cd npm && npm test)` | `node --test` passes with 0 failures (a trailing `npm notice` about a newer npm is noise) |
| `uv run mypy src/knowledge` | optional: a development tool in the dev group, **not** a gate; no workflow runs it |

`uv build --out-dir <temporary dir outside the repo>` (for example `uv build --out-dir "$(mktemp -d)"`) is fine as an optional local packaging check. Never commit `dist/`, and never upload an artifact you built locally: every published sdist and wheel is built and published only by `.github/workflows/publish.yml` on the runner, and `release-check.yml` checks them (what they carry is asserted by `tests/test_sdist_contents.py`).

Coverage is gated on the patch, not on the repository total. In CI, on pull requests, ring 1 runs `diff-cover` against `origin/main`, and the lines your change touches must reach 80 % (`fail_under = 80` under `[tool.diff_cover]` in `pyproject.toml`, so the gate and your machine cannot disagree). There is no floor on total coverage, on purpose: on a codebase with historical debt a global floor is either unreachable or met with tests that assert nothing (see the comment in `pyproject.toml`).

### Known environment caveats

- **No IPv6.** On a host without IPv6, `tests/test_suite_isolation.py::test_a_connection_outside_the_loopback_fails[2001:db8::1]` fails with `OSError: [Errno 97] Address family not supported by protocol`. It is environmental, not a bug; it passes on CI and on a normal machine.
- **Running as root.** A couple of tests that rely on file permissions are skipped when the suite runs as root.
- **Expected skips.** One test runs only with `KNOWLEDGE_REQUIRE_SQLITE_VEC=1`, and one runs an external template validator that is not part of this repository, so it skips without it.
- **Case sensitivity.** CI runs on Linux. macOS's case-insensitive filesystem once hid a real defect, so verify any new path with real case sensitivity before the pull request.

### Gate canaries

The gates have their own battery: it breaks each one on purpose and requires the exact failure, exit code included.

```bash
/bin/bash scripts/tests/ring1-gates-canaries.sh                 # without Docker
/bin/bash scripts/tests/ring1-gates-canaries.sh --with-semgrep  # includes the pinned Semgrep image
```

It always works on temporary copies and never modifies your tree. Run it every time you change a gate: a gate nobody has seen fail is a promise, not a gate.

What each CI job does and why, and the Semgrep caveats, are in [What CI runs](#what-ci-runs); operating CI (required checks, bypass, rollback, releases) is in [`docs/runbooks/ci-ring1.md`](docs/runbooks/ci-ring1.md).

## What CI runs

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

Two more workflows verify and publish a release, and neither is a pull request gate. `.github/workflows/release-check.yml` runs on every pushed `v*` tag and by hand, reads no secrets or environments, and checks that the five versions agree, that the frozen goldens hold, that the plugin validates with `claude plugin validate --strict` and that the sdist and the wheel carry only the package. `.github/workflows/publish.yml` verifies again and publishes the tag to PyPI and npm by trusted publishing. What each one checks, in order, and how to operate them: [`docs/runbooks/ci-ring1.md`](docs/runbooks/ci-ring1.md), sections 6 and 7.

Two things to read carefully before trusting a green run:

- **Semgrep's zero does not mean "nothing to report".** One rule (`dynamic-urllib-use-detected`, on the Confluence client) still fires and is silenced with an audited comment. What removes the risk is the scheme allowlist of `confluence_sync._api_get`, pinned by its own tests. Every suppression in the code is declared in `tests/test_suppressions.py` with its reason and the test that backs it; a new undeclared one breaks the suite.
- **Semgrep scans only files tracked by git.** In a workflow run that is the whole tree; on your machine, with unadded work, it is not, and the count of scanned files will be lower.

The gates have their own canary battery, which breaks each gate on purpose and requires the exact failure; see [Gate canaries](#gate-canaries).

Performance is fenced in the suite by deterministic work counts (SQL statements and rows, files opened, YAML parses; never a clock) in `tests/test_perf_budgets.py` and `tests/test_perf_budget_*.py`. Wall-clock numbers come from the `benchmarks/` harness (`uv run python -m benchmarks run`, then `compare`), which is not a gate; how to run it against a base commit, the result schema and the proposed SLOs are in [`docs/performance/benchmarks.md`](docs/performance/benchmarks.md), and the before/after results of the phase 0 and 1 work are in [Performance](docs/performance/results-phase-0-1.md).

## Testing rules

- Tests run only against the fixtures under `tests/fixtures/` (`tests/fixtures/vault/` and the small vaults in `tests/fixtures/vaults/`). No test ever uses a real vault.
- The suite uses the real store: a `SQLiteStore` in a temporary directory, with FTS5 and `sqlite-vec`. Embeddings come from the deterministic `bag-of-words` provider, which `tests/conftest.py` and `tests/golden_stdio_server.py` select with `KNOWLEDGE_EMBEDDINGS_PROVIDER=bag-of-words`. Nothing is patched, and no test calls OpenAI.
- No network. The suite sets `MEMORY_HTTP_TOKEN` and `KNOWLEDGE_MEMORY_URL` empty and fails on any connection to port 6366 (the local memory server); tests that need memory, Jira or a judge use a fake server on an ephemeral loopback port.
- Every tool has at least one async test that calls it through its real interface, and a schema test that freezes its response shape.
- An indexing bug is reproduced with `tests/fixtures/vault/`, never with a real vault.
- Coverage thresholds live in `pyproject.toml`, never only in a document.

## The tool contract

The tool catalog and every response shape are a versioned contract (`CONTRACT_VERSION` in `src/knowledge/contract.py`, today `2.0.0`; `TOOL_SINCE` records since which version each tool exists).

- **No response shape changes without a declared contract version bump.** That includes keys, types, defaults, error codes, and any order a tool emits: every order is contract, and no golden depends on `PYTHONHASHSEED`.
- **Goldens.** Each contract version has golden responses in `tests/golden/v<version>/`. A directory with a `SHA256SUMS` file is frozen (`v1.0.0` through `v2.0.0` today), and `tests/test_contract_frozen.py` fails if a single byte changes, on any Python and whether or not `sqlite-vec` loads. Goldens are recorded only with `uv`'s Python 3.12 and `KNOWLEDGE_REQUIRE_SQLITE_VEC=1`. Never edit a frozen golden.
- **Inventory.** After any change to a tool's name, parameters or description, regenerate [`docs/tools/inventory.md`](docs/tools/inventory.md) with `uv run python scripts/tool_inventory.py`.
- **Changelog.** Record the change in [`docs/contract/CHANGELOG.md`](docs/contract/CHANGELOG.md) under the new version, in the subsection that fits (for example "Added", "Changed (non-additive)" or "Breaking"); the contract tests read those subsections.
- **Text is canonical.** The text blocks are the response; `structuredContent` and `outputSchema` travel only with `KNOWLEDGE_STRUCTURED_OUTPUT=1`.
- **Errors.** Every error is `isError` with a stable `E_` code (`src/knowledge/errors.py`) and an actionable message, never a Python traceback, a bearer or an API key.
- **Paths.** `file_path` is always returned vault-relative, without a prefix and without wikilink brackets.
- **Broken tools.** A tool documented as broken is repaired with a regression test that fails first (red) with the exact input documented as failing, and only then fixed, without touching the tools that work. Today no tool is documented as broken.

## Layered architecture

`src/knowledge` holds 35 flat modules (no subpackages) in three groups that [`.importlinter`](.importlinter) enforces without moving them into directories:

- **core** (indexing and querying vaults, with no HTTP or MCP): `errors`, `ports`, `contract`, `settings`, `config`, `graph`, `usage`, `profiles`, `pointers`, `chunker`, `principals`, `extensions`, `embeddings`, `stores`, `vault_source`, `indexer`, `vault_lint`, `rerank`, `registry`;
- **ext** (optional integrations): `repos`, `gitsync`, `memory_http`, `memory_bridge`, `review`, `confluence_sync`, `graph_targets`, `webhook`, `flows`, `jira_client`, `jira_sync`, `judges`, `contexts`;
- **http**: `auth`, `server`, `cli`.

Four contracts: `layers` (a full top-to-bottom order; a level never imports upward or across siblings), `core-isolated` (core imports neither ext nor http), `ext-below-http` (ext does not import http) and `core-no-transport` (core does not import `mcp`, `starlette` or `uvicorn`). `tests/test_dynamic_imports.py` forbids `importlib` in core and `import_module` outside `server`, which is the only module that loads extensions. `repos` is ext but sits at the bottom of the order because it imports nothing internal and runs as `python3 -m knowledge.repos` without the virtual environment. Check with `uv run lint-imports`.

## Architecture diagram

[`docs/architecture.md`](docs/architecture.md) and the README show the same diagram twice: inline as Mermaid, which GitHub renders, and as [`docs/architecture.png`](docs/architecture.png), which also renders where Mermaid does not (PyPI, mirrors, offline viewers). Its source is [`docs/architecture.mmd`](docs/architecture.mmd). If you change the diagram, edit that file, copy it into the Mermaid blocks of `README.md` and `docs/architecture.md`, and regenerate the PNG:

```bash
npx -y @mermaid-js/mermaid-cli -i docs/architecture.mmd -o docs/architecture.png -b white -s 2
```

mermaid-cli drives a headless Chromium through puppeteer. Where puppeteer cannot download its own browser, point it at one you have with `-p puppeteer.json`, a file such as `{"executablePath": "/path/to/chromium", "args": ["--no-sandbox"]}` kept outside the repository.

## The write fence

The server never writes inside a vault outside `90 - Memory/` ([ADR-044](docs/adr/ADR-044.md)). `sync_confluence(dry_run=False)` answers `E_WRITE_FENCED` without reading credentials; `confluence export` and `vault-lint --fix --out` write only outside every served vault; `init` never writes inside a vault. `tests/test_write_fence.py` calls every tool and the export over a copy of the fixture and requires the tree to stay intact. `include_memories=false` is the default of `context_pack`: the fence is tested, not assumed. A change that adds a write path must keep that test green.

## Methodology by type of work (D-05)

| Type of work | Route | Gate |
|---|---|---|
| New tool or response-shape change | Full spec-driven design (SDD) and a high-rigor review (RDD High) | Schema test plus the RFC's contract suite |
| Repair of a broken tool | SDD-lite; a regression test in red with the exact input that fails today | The input documented as failing returns a result |
| Indexing bugfix | Direct (3 files or fewer); reproduce with `tests/fixtures/vault/`, never with a real vault | The indexer's suite |
| Vault content change | Human PR in the vault's repository with the owner as reviewer (D-11) | Frontmatter validated, wikilinks resolved, Mermaid rendered |
| Layer refactor | SDD plus characterization tests; the golden master of every golden session, with no diff | `import-linter` plus an empty response diff |
| Incident (MCP down or stale index) | Runbook plus RDD High; check `/health` first | Incident write-up on its tracking issue, with the `/health` output |

## Commits and pull requests

- **Branches.** Branch from an up-to-date `origin/main` (`git fetch origin && git checkout -b <branch> origin/main`), never from another work branch. Names follow Conventional Branch, for example `feat/<topic>`, `fix/<topic>`, `docs/<topic>`, `refactor/<topic>` or `chore/<topic>`; the exact pattern is in [`.claude/rules/git.md`](.claude/rules/git.md).
- **Commits.** [Conventional Commits](https://www.conventionalcommits.org/) (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`, ...), with no ticket prefix in the subject and no AI attribution trailers.
- **Pull requests** go to `main` and follow [`.github/PULL_REQUEST_TEMPLATE.md`](.github/PULL_REQUEST_TEMPLATE.md). The `protect-main` ruleset requires five checks from ring 1: `Lint + pytest with coverage`, `Layered architecture: import-linter (ADR-022)`, `Secrets: gitleaks`, `SAST: Semgrep CE` and `Diff coverage (threshold from pyproject.toml)`. The Docker image job does not run on pull requests; before merging a change to `Dockerfile`, `infra/` or `tests/test_image.py`, dispatch ring 1 on the branch (`gh workflow run ring1.yml --ref <branch>`) and wait for the image job.
- **Secrets.** gitleaks scans every PR's commits. Never commit a token, a key or a real-looking credential, not even as an example; documentation names the environment variable that holds a secret, never its value.
- Use `gh` for repositories hosted on GitHub, like this one.

## Releases

Maintainers cut releases; a contributor's PR never bumps the version.

- The version is the same on PyPI, npm and the git tag ([ADR-050](docs/adr/ADR-050.md)).
- `scripts/bump-version.sh X.Y.Z` writes it into `pyproject.toml`, `plugin/claude-code/.claude-plugin/plugin.json`, the `cloudfrog-knowledge==X.Y.Z` package source in `plugin/claude-code/.mcp.json` (the plugin launches the PyPI release) and `npm/package.json`, refreshes `uv.lock` and runs `scripts/check-version.sh`. It never commits or tags; the tag `vX.Y.Z` is cut on the merge of the release pull request.
- On the tag, `.github/workflows/release-check.yml` verifies versions, frozen goldens, the plugin (`claude plugin validate --strict`), the sdist and the wheel; its green run is expected before the marketplace pull request (`cloudfroghq/claude-plugins`).
- `.github/workflows/publish.yml` verifies again and publishes to PyPI and npm by trusted publishing, skipping a registry only if that exact version already exists.
- The release history is in [`CHANGELOG.md`](CHANGELOG.md). Operating releases, the first manual npm publish and the trusted-publisher setup are in [`docs/runbooks/ci-ring1.md`](docs/runbooks/ci-ring1.md), section 7.

## Licensing of contributions

This repository is licensed under the [MIT License](LICENSE). Contributions are accepted under the same license (inbound = outbound): by submitting a contribution, you agree that it is licensed under the MIT License, as the rest of the repository is.
