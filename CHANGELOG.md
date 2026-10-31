# Changelog

All notable changes to the `cloudfrog-knowledge` package (PyPI), `@cloudfrog/knowledge` (npm) and the `knowledge` Claude Code plugin are documented in this file. The three are published with the same version number (ADR-050).

The format is based on [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/), and the package follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html) (still 0.x, so minor releases may include breaking changes; they are declared below).

The MCP tool contract is versioned separately from the package. Every change to a tool's response shape, parameters or catalog is recorded in [docs/contract/CHANGELOG.md](docs/contract/CHANGELOG.md), which is the authoritative reference; this file only summarizes it. Contract version and package version map as follows:

| Contract | Package |
|---|---|
| 1.1.0 | 0.2.0 |
| 1.2.0 | 0.3.0 |
| 1.3.0 | 0.4.0 |
| 1.4.0 | 0.5.0 |
| 2.0.0 | 0.6.0 (unchanged in 0.7.0, 0.8.0 and 0.9.0) |

Release dates come from the contract changelog and the release commit. Detail for 0.7.0 and 0.8.0 is thin: their dates are not recorded in this repository (only `v0.9.0` is tagged here, and the history starts at the initial public release). Releases before 0.9.0 were not tagged in this repository, so their headings below are plain text and have no release page to link to.

## [Unreleased]

### Changed

- The project is relicensed under the MIT License starting with the next release. Releases up to 0.9.0 remain under PolyForm Shield 1.0.0.
- Project documentation migrated to English (README, guides, runbooks and contributor files). Documentation only, plus the tool-inventory generator (`scripts/tool_inventory.py`), whose prose is now English, and the tests that assert on documentation headings; no tool contract, response or package behavior changes.

### Added

- Community files: `CODE_OF_CONDUCT.md`, `SECURITY.md`, `SUPPORT.md`, this changelog, GitHub issue and pull request templates, and `.editorconfig`.

## [0.9.0] - 2026-10-30

Contract unchanged at 2.0.0. First release on PyPI and npm. See [contract changelog, package 0.9.0](docs/contract/CHANGELOG.md).

### Added

- Three distribution channels with the same version (ADR-050): the `v0.9.0` git tag, PyPI (`cloudfrog-knowledge`) and npm (`@cloudfrog/knowledge`, a dependency-free wrapper that downloads a pinned `uv`, verifies its SHA-256 and runs the PyPI version). `publish.yml` publishes each `v*` tag by trusted publishing.
- A second console script, `knowledge`, identical to `cloudfrog-knowledge`, and the `--version` flag. Usage text, `--version` and the hints of `doctor` and `init` name the command that was run; `KNOWLEDGE_COMMAND_LABEL` still takes precedence.

### Changed

- The `knowledge` plugin and the docs install from `git+https://github.com/cloudfroghq/knowledge.git@v0.9.0`: the repository is public and no SSH key is needed.

## [0.8.0]

Contract unchanged at 2.0.0. Date not recorded in this repository. See [contract changelog, package 0.8.0](docs/contract/CHANGELOG.md).

### Added

- `doctor --client` checks only what concerns a client machine (vault, `Contexts/config.json`, `repos.local.json` and each checkout) and skips embeddings, index, judge, memory and bearers. A git checkout without `origin` is the warning `W_CONTEXTS_CHECKOUT_UNVERIFIED` (exit code 0).

### Changed

- `init` links inside a `.claude` directory that the checkout owns as a real directory, keeping what the checkout versions in git without conflict (ADR-048). `init --force` moves an unversioned real entry aside as `<name>.pre-ai-context.bak-<epoch>`; nothing is deleted.
- Hints that name a command take it from `KNOWLEDGE_COMMAND_LABEL` or, without it, name `cloudfrog-knowledge`.
- The webhook serves one refresh per vault at a time and answers `ignored` to a push of another branch, a tag or a ref deletion, without `git pull` or reindexing. `reindex(pull=true)` shares the same lock.
- `infra/compose.yml` passes `MEMORY_CLOUD_IDENTITY_MODE` (default `grace`) to the memory-cloud service.

## [0.7.0]

Contract unchanged at 2.0.0. Date not recorded in this repository. See [contract changelog, package 0.7.0](docs/contract/CHANGELOG.md).

### Added

- A generic Compose file (`infra/compose.yml`) and multi-vault startup (`KNOWLEDGE_VAULTS_YAML`, `infra/resolve-vaults.py`), with a dockerized lab (`lab/run.sh`).
- `init` installs into each checkout the context package a vault declares in `Contexts/` (ADR-047); `doctor` warns about a pending `init` without failing.

### Changed

- **Breaking for some deployments:** the slug of a vault served with `VAULT_PATH` now comes from `CHROMA_COLLECTION`, then the `vault.yaml` `slug`, then `vault`. Whoever relied on `<root>/.data/cloudfrog` as the index directory must set `KNOWLEDGE_INDEX_DIR` or `CHROMA_COLLECTION`.
- The indexer skips dot directories; `vault-lint` and `get_stale_docs` respect `index.exclude` in `vault.yaml`.
- The repository no longer ships any tenant data; the repository registry reads only a vault's `Contexts/config.json` and the machine's `repos.local.json`.

## [0.6.0] - 2026-10-13

Tool contract 2.0.0 (breaking; ADR-046). See [contract changelog 2.0.0](docs/contract/CHANGELOG.md).

### Changed

- **Breaking:** `search` and `search_deep` rows carry `chunk_id` and `section` and no `excerpt`; `get_document` reads fewer characters by default, filters metadata through an allow list unless `full_metadata=true`, and accepts `chunk_id` without `file_path`; `search_by_metadata` returns `{total, offset, items}`; `get_related` joins both directions in `both`; every `limit` has a cap; every error is `isError` with an `E_` code.
- **Breaking:** `search_code` is retired from the default profile and answers `E_TOOL_RETIRED` with a redirect to `graphify`, `rg` and `search`.
- **Breaking:** the default catalog is the 10 core tools; `get_flow`, `compare_flows`, `get_stale_docs`, `suggest_links` and `sync_confluence` register only when a served vault declares the matching feature (otherwise `E_FEATURE_DISABLED`). `reindex` with a read-only bearer answers `E_SCOPE_REQUIRED`.
- `tools/list` carries no `outputSchema` by default.

### Added

- Tool profiles: `all` (default) and `legacy`, chosen with `serve --profile` or `KNOWLEDGE_PROFILE`. `legacy` serves the 1.4.0 catalog for one release; any other value is `E_PROFILE_UNKNOWN`.
- `GET /health` reports `profile`.
- Call telemetry in `usage_stats.json` (`tool_calls`: per day, tool, client and principal; never arguments), shown by `doctor`.
- Frozen goldens in `tests/golden/v2.0.0/` (six sessions).

## [0.5.0] - 2026-10-12

Tool contract 1.4.0 (additive). See [contract changelog 1.4.0](docs/contract/CHANGELOG.md).

### Added

- Optional reranking of `search` with `rerank='on'|'off'`, using a judge declared in the vault's `vault.yaml` (`rerank.provider`: `jev` or `kev`). Rows gain `rerank_score` and the result carries `_meta["knowledge/rerank"]`. Without it, responses are those of 1.3.0.
- Per-vault judge limits (timeout, token and daily USD budgets, cache, circuit breaker) and a `rerank` block in `doctor` that shows variable names, never values.
- Judge usage aggregates in `usage_stats.json`; never queries or passages.

## [0.4.0] - 2026-10-12

Tool contract 1.3.0 (additive, with declared exceptions). See [contract changelog 1.3.0](docs/contract/CHANGELOG.md).

### Added

- Declarative extension registry: a vault turns on `flows`, `governance`, `confluence`, `git-webhook`, `jira` and `memory-bridge` through `features`.
- `read` and `admin` scopes per HTTP bearer (`MCP_ADMIN_KEY`, `bearers` in `vaults.yaml`), opt-in; `E_SCOPE_REQUIRED` for a read-only bearer.
- `sync_jira` tool (stdio only, admin, `dry_run=True` by default) and the `jira sync` command; `memory-bridge sync` command; `confluence export` command; `doctor` `auth` block.
- `vault.yaml` `integrations` key that names credential variables, never credentials (`E_SECRET_IN_MANIFEST`).

### Changed

- **Non-additive:** `sync_confluence(dry_run=False)` answers `E_WRITE_FENCED`: the server does not write inside a vault outside `90 - Memory/` (ADR-044).
- The reindex webhook is registered only when the default vault declares a repository, and requires a signature (`MCP_WEBHOOK_SECRET`) or an `admin` bearer.

## [0.3.0] - 2026-10-12

Tool contract 1.2.0. See [contract changelog 1.2.0](docs/contract/CHANGELOG.md).

### Changed

- The store moved to one SQLite file per vault (`index.sqlite`) with FTS5 and `sqlite-vec` (ADR-043). `score`, order and row membership of `search`, `search_deep` and `context_pack` now come from BM25 and exact KNN and are no longer stable across indexes; keys and parameters are unchanged.

### Added

- `migrate-index` command to prepare `index.sqlite` and copy sidecars from a Chroma directory.
- `KNOWLEDGE_INDEX_DIR`, `KNOWLEDGE_REQUIRE_SQLITE_VEC` and `vector_backend` in `doctor` and `GET /health`; `index_version` in `reindex`.

### Removed

- `chromadb` is no longer a dependency.

## [0.2.0] - 2026-10-12

Tool contract 1.1.0 (additive). See [contract changelog 1.1.0](docs/contract/CHANGELOG.md).

### Added

- Multi-vault serving: the optional `vault` parameter on every tool and the `list_vaults` tool.
- `context_pack` tool (task context in one call; memory is read only with `include_memories=true`).
- Pointer-based reading: `chunk_id`, `section`, `fields` and `snippet` for `search`, and `chunk_id` for `get_document`; handshake `instructions` that start with `knowledge-contract: <version>`.
- `KNOWLEDGE_STRUCTURED_OUTPUT` and `KNOWLEDGE_TEXT_INDENT` output settings; `contract_version` in `GET /health`.

[Unreleased]: https://github.com/cloudfroghq/knowledge/compare/v0.9.0...HEAD
[0.9.0]: https://github.com/cloudfroghq/knowledge/releases/tag/v0.9.0
