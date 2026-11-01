# Benchmarks and performance fences

Status: Phase 0 of [`improvement-plan.md`](improvement-plan.md) (items 0.1 and 0.2). Two instruments, with different jobs:

| Instrument | Measures | Runs | Blocks CI |
|---|---|---|---|
| `benchmarks/` (this page) | Wall-clock latency and build times, with percentiles | On demand, on a quiet machine | No |
| `tests/test_perf_budgets.py` | Work counts: SQL statements, rows fetched, files opened, YAML parsed, modules imported | In the normal `pytest` run | Yes |

Wall clock on a shared runner is good to about 10-15 %, so it never gates a merge. Work counts are exact, so they do. A change that makes `search` faster should show up in the harness; a change that makes it do more work should fail a budget.

Everything uses synthetic vaults (`tests/synthetic_vault.py`) in a temporary directory and the `bag-of-words` embedding provider. No real vault, no network, no API key.

The measured results of the phase 0 and 1 work, before and after, are in [`results-phase-0-1.md`](results-phase-0-1.md).

## Quick start

```bash
uv sync
uv run python -m benchmarks run --docs 500                       # about 3 minutes
uv run python -m benchmarks run --docs 500,5000                  # the plan's two sizes, about 15 minutes
uv run python -m benchmarks run --quick --docs 60                # smoke run, 20 seconds, numbers are not for comparison
uv run --with pyperf==2.10.0 python -m benchmarks run --docs 500 # also runs the start-up probes under pyperf
```

`uv run python benchmarks/run.py run ...` is the same program. A run prints the machine and the checkout, logs progress on stderr, prints the Markdown summary on stdout, and writes to `.data/bench/<utc>-<label>/` (git-ignored; change it with `--out`):

- `results.json`: `{"schema": 1, "meta": {...}, "results": {"<scenario>": {...}}, "notes": [...]}`
- `summary.md`: the tables below, plus a check against the proposed SLOs
- `pyperf-startup.json`: the pyperf suite, only when pyperf ran

The harness needs no change to `pyproject.toml` or `uv.lock`: `benchmarks/` is not packaged (the wheel includes `src/knowledge` only), uses the standard library, and treats pyperf as optional.

### pyperf

Statistical rigour for process-level measurements comes from [pyperf](https://pyperf.readthedocs.io/): worker processes, warm-up, calibrated runs. It is used only through `uv run --with pyperf==2.10.0 ...`, never added to the lock. When it is importable (`--pyperf auto`, the default) the start-up probes also run as `startup.pyperf.*` scenarios; `--pyperf on` insists on it and `--pyperf off` skips it. Two pyperf suites compare with pyperf's own tool, which applies a significance test:

```bash
uv run --with pyperf==2.10.0 python -m pyperf compare_to base/pyperf-startup.json head/pyperf-startup.json
```

Without pyperf the harness falls back to repeated `time.perf_counter` samples summarised with median and MAD (median absolute deviation), and says so in the summary notes. In both modes the percentile scenarios (`search`, `get_document`, ...) use the harness's own sampling, because pyperf reports means and standard deviations, not p95 and p99.

## What each scenario measures

Names are `d<docs>.<scenario>` for a vault of that many documents, and `startup.*` / `stdio.*` for processes. All times are milliseconds unless the unit column of the summary says otherwise.

| Scenario | What it times | Samples |
|---|---|---|
| `index.full` | `VaultIndexer.index()` into an empty index directory: walk, hash, YAML, chunk, embed (bag-of-words), write | `--build-reps` fresh builds (3 up to 1,000 documents, 1 above) |
| `index.noop` | `index()` again with nothing changed, on a warm indexer (what the reindex webhook pays for an unchanged tree) | `--reps` (5, or 3 above 1,000 documents) |
| `index.one_changed` | `index()` after appending a line to one file, on a copy of the vault and index | `--reps` |
| `search.hybrid.<sel>` | `server.search(query, limit=5)` through the async wrapper FastMCP awaits, usage tracking on, over `--calls` queries (default 200) after `--warmup` untimed calls | `--calls` |
| `search.keyword.<sel>`, `search.semantic.<sel>` | The other two modes (`--modes hybrid,keyword,semantic`) | `--calls` |
| `search.limit20_snippet.<sel>` | `limit=20, snippet=True` | `--calls` |
| `search.first_call` | The first `search` after opening the runtime and index in a fresh process | 1 |
| `search_deep.<sel>` | `search_deep(query, depth=2, limit=5)` | `--calls` |
| `context_pack.<sel>` | `context_pack(query, budget_tokens=3000)` | `--calls` |
| `get_document.file_path`, `get_document.chunk_id` | A document by path (random order over the vault), and a block by the `chunk_id` of earlier search rows (`max_chars=800`). Each read records usage, so the inline usage flush shows in the tail | `--calls` |
| `memory.rss_after_tools` | Resident set size of the tool process after all of the above (unit MiB) | 1 |
| `usage.flush.h<N>` | `UsageTracker.flush()` after N recorded searches (`--usage-history`, default `100,1000`; 5000 is slow to build) | 10 |
| `usage.record_search.h<N>` | `record_uses` + `record_pairs` for one 5-result search at that history size, inline flushes included: this is the latency the caller sees | 200 |
| `usage.file_kib.h<N>` | Size of `usage_stats.json` at that history | 1 |
| `startup.import.knowledge.cli`, `startup.import.knowledge.server` | `import <module>` in a fresh interpreter, timed inside the child (interpreter start-up excluded) | `--startup-runs` (10) |
| `startup.interpreter_floor` | `python -c pass` wall clock, the floor under the wall-clock probes | `--startup-runs` |
| `startup.doctor_brief` | Wall clock of `doctor --brief` (the SessionStart hook) | `--startup-runs` |
| `startup.initialize` | Spawn `python -m knowledge.server` over stdio until the `initialize` response arrives | `--startup-runs` |
| `startup.tools_list_first` | The first `tools/list` after `initialize` | `--startup-runs` |
| `stdio.search`, `stdio.search.first_call`, `stdio.list_vaults` | `tools/call` round trips over a real stdio server on the smallest vault's index | `--stdio-calls` (100) |
| `startup.pyperf.import_cli`, `.import_server`, `.doctor_brief` | The same commands under pyperf: wall clock including interpreter start-up | `--pyperf-processes` x `--pyperf-values` |

The workload also fixes the vault: a document count and a seed (default 7) give byte-identical Markdown (`meta.workloads.<docs>.tree_sha256` is recorded), so two runs measure the same data.

### Selectivity

The plan's baseline used queries built from Zipf-distributed words, where every token of four or more characters is a prefix match and a query hits about 85 % of the chunks. That is an upper bound of what real queries cost. `--selectivity` takes a comma list and runs every tool scenario for each entry:

- `0.02`: each query word is in about 2 % of the documents (a query of two words matches at most about 4 %). Scenario suffix `sel0.02`.
- `zipf`: the old behaviour. Scenario suffix `zipf`.

The default is `0.02,zipf`. `--terms` sets the words per query (default 2). The achieved share is recorded per scenario as `query_matched_docs_median` (the median per-document match share over 40 queries, from the store's own lexical search), so a calibrated set (plan item 0.4) can be compared with what the generator produced. Use at least 200 calls (`--calls`, the default) for a stable p95, and 100 for a p99; below that the summary shows `-` rather than an unsupported percentile.

## Environment and isolation

Every measured process gets `bench_env()` (`benchmarks/common.py`): `KNOWLEDGE_EMBEDDINGS_PROVIDER=bag-of-words`, a temporary `KNOWLEDGE_INDEX_DIR`, `VAULT_PATH` pointing at the generated vault, `KNOWLEDGE_CONFIG_JSON` at `tests/fixtures/config.json`, `MEMORY_HTTP_TOKEN` and `KNOWLEDGE_MEMORY_URL` empty, every rerank, Jira, Confluence, OpenAI and bearer variable emptied, and `KNOWLEDGE_REQUIRE_SQLITE_VEC=1`. If `sqlite-vec` does not load in your Python the harness fails instead of silently measuring the stdlib fallback (about 10 times slower); `--no-require-vec` overrides that on purpose, and `vector_backend` is in the machine info either way.

Scenarios that import `knowledge` run in a worker subprocess (`benchmarks/scenarios.py`), one per group, so import state, caches and usage files never leak between them. The generated vaults, indexes and scratch copies live in a temporary directory removed at the end (`--workdir DIR` chooses it, `--keep` keeps it).

## Comparing two commits

The harness measures the `knowledge` that the interpreter running it imports, so "before" and "after" differ only in the environment that `uv run` selects. Keep the harness from one place (head) and point it at each checkout:

```bash
HARNESS="$PWD"                                   # head, with benchmarks/ and tests/synthetic_vault.py
BASE="$(mktemp -d)" && git archive <base-sha> | tar -x -C "$BASE"   # or any other checkout of the base

uv run --directory "$BASE" python "$HARNESS/benchmarks/run.py" run --docs 500,5000 --label base --out "$HARNESS/.data/bench/base"
uv run                     python "$HARNESS/benchmarks/run.py" run --docs 500,5000 --label head --out "$HARNESS/.data/bench/head"
uv run python -m benchmarks compare "$HARNESS/.data/bench/base/results.json" "$HARNESS/.data/bench/head/results.json"
```

Use absolute `--out` paths: `--directory` changes the working directory. A base taken with `git archive` has no git metadata, so its `commit` is `null`: pass `--label` to name it (a `git worktree` checkout records the commit). `compare` prints a table of base and head p50 and p95, the relative change, and flags a p50 change beyond `--threshold` (default 15 %) as `faster` or `SLOWER`. It warns when the two files come from different machines. Scenarios present in only one file are listed, not compared.

Both sides must run on the same interpreter and the same SQLite. A fresh base checkout gets its own `.venv` on its first `uv run`, and `uv` may pick a different Python for it than the head's `.venv` uses (for example a `uv`-managed CPython with a newer SQLite against a system CPython): every Python-heavy scenario then differs by the interpreter, not by the change. Create the base environment with the head's interpreter first, `uv sync --directory "$BASE" --frozen --all-groups --python "$(uv run python -c 'import sys; print(sys._base_executable)')"`, and check that `meta.python` and `meta.sqlite` agree in both `results.json` files: `compare` does not check them.

Rules for a credible comparison:

1. Same machine, quiet (no other build or benchmark running; `loadavg_start` and `loadavg_end` are in the summary), same Python and `sqlite-vec`.
2. Run base and head back to back, and repeat once. A delta under about 15 % on the first run is noise until it repeats.
3. Compare like with like: the same `--docs`, `--seed`, `--selectivity`, `--calls`.
4. Read the work counts too. A change that lowers a budget in `tests/test_perf_budgets.py` and moves p50 by a third is a real gain; one that moves only wall clock is probably the machine.
5. Byte-identity is proved separately, by the frozen goldens and the differential test of each item, never by this harness.

A base checkout older than the harness works: the harness uses only `VaultIndexer`, the tool functions of `knowledge.server`, `UsageTracker` and the stdio server, and takes the vault generator from `tests/` of the head. A scenario that cannot run on a base (an API that did not exist yet) fails with the worker's traceback tail; run the others with `--scenarios`.

## Command reference

```text
python -m benchmarks run [--docs 500,5000] [--scenarios index,tools,usage,startup|all]
                         [--calls 200] [--warmup 10] [--selectivity 0.02,zipf] [--terms 2]
                         [--modes hybrid,keyword,semantic] [--seed 7]
                         [--build-reps N] [--reps N] [--usage-history 100,1000]
                         [--startup-runs 10] [--stdio-calls 100]
                         [--pyperf auto|on|off] [--pyperf-processes 6] [--pyperf-values 2]
                         [--no-require-vec] [--workdir DIR] [--keep] [--out DIR] [--label NAME] [--quick]
python -m benchmarks compare BASE.json HEAD.json [--threshold 0.15] [--out FILE.md]
python -m benchmarks info                          # machine and checkout metadata, as JSON
```

`results.json` `meta` carries what makes two files comparable: `commit` and `checkout_dirty` of the measured checkout, `cpu`, `nproc`, `mem_gib`, `platform`, `python` and `python_build`, `sqlite`, `sqlite_vec`, `vector_backend`, `uv`, `pyperf`, `loadavg_start` and `loadavg_end`, the arguments, and the size, chunk count and tree hash of each generated vault. Each result is `{unit, n, p50, p95, p99, min, max, mean, mad, ...}`; `p95` is present from 20 samples and `p99` from 100.

## Work-count budgets

`tests/test_perf_budgets.py` runs the common operations on a 60-document synthetic vault and counts what they did, with the helpers of `tests/perf_counters.py`:

| Counter | How | Example fence (today) |
|---|---|---|
| SQL statements, writes, commits | SQLite trace callback on every connection of the store (`SQLiteStore.observe`) | `search` issues at most 15 statements; a first index at most 5 commits |
| Rows handed to Python, rows carrying a column | `row_factory` on the same connections | `search` on an unselective query fetches at most 131 rows, none of them with `docs.body` |
| Markdown files opened | PEP 578 audit hook on `open` | `search` opens at most 5 files; a no-op reindex at most 2 per document |
| YAML documents parsed | `sys.monitoring` on `BaseConstructor.get_single_data` | a no-op reindex parses at most 60 |
| Usage file reads and writes | The same audit hook | a flush reads `usage_stats.json` once |
| `knowledge.*` modules after `--version` and `doctor --brief` | A fresh interpreter | 15 modules after `--version` and 22 after `doctor --brief`, and no extension module |

Nothing is patched and nothing reads a clock, so the numbers are the same on a laptop and on the Linux pipeline. The test needs `sqlite-vec` (it fails when `KNOWLEDGE_REQUIRE_SQLITE_VEC=1` and the extension does not load, and skips otherwise: the stdlib fallback issues different statements). The file takes about 3 seconds.

These limits are today's values, not targets. A faster implementation passes them untouched. Structural counts (files opened, YAML parsed, commits) are exact; statement counts allow 10 % (at least 2) and fetched rows 20 %, so that a legitimate change elsewhere does not trip them while an N+1 over the results still does.

### Tightening a budget

Each limit that a roadmap item is expected to lower carries a `# TIGHTEN (<item>)` comment at the assertion, and the module docstring lists them by item. The change that lowers the work lowers the limit in the same commit, to the new measured value; that is what keeps a gain from eroding. Streams add their own, tighter files (`tests/test_perf_budget_*.py`); the phase 1 changes lowered the limits listed in the module docstring, and the ones below are still to drop.

| Item | Budgets that should drop |
|---|---|
| 2.1 (keyword path) | Rows fetched by the lexical statement fall from every matching chunk to about `k`; `search`, `context_pack` and `search_deep` row counts follow |
| 2.2 (incremental reindex) | No-op reindex: YAML parses 60 to 0, Markdown opens 120 to 60. One changed file: opens and parses fall with the vault |
| 2.3 (`search_deep`) | The hop pass re-ranks fewer rows |

Phase 1 already lowered the first-index commits (60 to 5), the `DELETE FROM chunks_vec` on a first index (59 to 0), the rows carrying `docs.body` (to 0), the usage-file reads per flush (2 to 1) and the modules loaded by `--version` and `doctor --brief` (27 to 15 and 22). The Markdown opens of `search` and `get_document` have no roadmap item yet.

A budget that must go up is a regression to explain in the change that raises it, not an inconvenience to route around.

## Proposed SLOs

From the plan: 5,000-document synthetic vault, reference runner (4 vCPU, `sqlite-vec` loaded), in-process tool time with embedding and judge network excluded. They are targets for the harness, not alerts, and are re-baselined when the generator is calibrated (plan item 0.4). `summary.md` ends with a table that checks a run against the ones that map to a scenario.

| Path | Scenario | Today (plan baseline, syn5000) | SLO |
|---|---|---|---|
| `search` (default) | `d5000.search.hybrid.*` | 122 / 214 / 244 ms | p50 35 ms, p95 60 ms, p99 100 ms on the calibrated query set |
| `search` at 500 documents | `d500.search.hybrid.*` | 17.8 / 29.8 / 34.3 ms | p50 8 ms, p95 20 ms |
| `get_document` | `d5000.get_document.file_path` | 1.1 / 25.4 / 33.7 ms | p99 5 ms |
| `context_pack` | `d5000.context_pack.*` | 147 / 267 / 307 ms | p50 45 ms, p95 80 ms on the calibrated set |
| `search_deep` | `d5000.search_deep.*` | 258 / 387 / 455 ms | p50 80 ms, p95 150 ms on the calibrated set |
| Spawn to `initialize` | `startup.initialize` | 559-737 ms | p95 650 ms |
| `doctor --brief` | `startup.doctor_brief` | 749-912 ms | 300 ms |
| Launcher overhead (uvx) | not in this harness; measured by hand, plan item 1.1 | 469-754 ms | 150 ms |
| Full build, bag-of-words | `d5000.index.full` | 18.8 s | 8 s |
| No-op reindex | `d5000.index.noop` | 1.8 s | 600 ms |
| Usage flush | `d*.usage.flush.h*` | 3.6-666 ms, on request threads | 5 ms, never on a request thread |
| RSS, steady | `d5000.memory.rss_after_tools` | 162-265 MB | 250 MB after the tool mix |

The `p95` of `search` at 5,000 documents depends on query selectivity: on the unselective synthetic queries FTS5 `bm25()` alone has a 95 ms p95, so the 60 ms target needs either selective queries (`sel0.02`) or Phase 3.3. Read the `sel` and `zipf` rows separately.

## Adding a scenario

1. A group function in `benchmarks/scenarios.py` (anything that imports `knowledge`) returns `{"results": {name: summarize(samples, ...)}, "extra": {...}}`, or a probe in `benchmarks/startup.py` (anything that needs a fresh process).
2. Register the group in `GROUPS` and call it from `cmd_run` in `benchmarks/run.py`.
3. If it has an SLO, add it to `SLOS` in `benchmarks/report.py`.
4. If it is a work count rather than a time, it belongs in `tests/test_perf_budgets.py` instead.
