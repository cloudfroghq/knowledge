# Performance results: phases 0 and 1

Wall-clock numbers for the phase 0 and 1 performance work (`perf/phase-0-1`, unreleased after 0.9.0) against its base, 0.9.0 at `a89cd5e`, measured with the `benchmarks/` harness. The change is contract class A: every response is byte-identical to the base (the frozen goldens pass untouched) and the contract stays `2.0.0`. The numbers are milliseconds unless a header or a cell says otherwise; "selective" and "Zipf" queries are defined under the first table.

## Conditions

- Machine: 4 vCPU Intel Xeon @ 2.10 GHz, 15.7 GiB, Linux 6.18, idle at the start. Wall clock on that VM is good to about 10-15 %.
- Both sides on the same interpreter: CPython 3.12.3, SQLite 3.45.1, `sqlite-vec` 0.1.9.
- Generated vaults, seed 7: 500 documents (1,370 chunks) and 5,000 documents (14,364 chunks, 85 MiB index). `bag-of-words` embeddings, no network.
- pyperf 2.10.0, 200 calls per scenario after a warm-up.

## Search and retrieval

A selective query has each word in about 2 % of the documents (`sel0.02` in the harness); a Zipf query is the old baseline, where a query hits about 85 % of the chunks and is an upper bound of what real queries cost.

| Scenario | Docs | p50 before | p50 after | Δ p50 | p95 before | p95 after |
|---|---:|---:|---:|---:|---:|---:|
| `search`, hybrid, selective query | 500 | 8.60 | 4.85 | -43.6% | 12.1 | 6.18 |
| `search`, hybrid, selective query | 5,000 | 51.4 | 5.70 | -88.9% | 64.9 | 8.14 |
| `search`, hybrid, Zipf query | 500 | 14.9 | 11.0 | -26.3% | 32.9 | 15.7 |
| `search`, hybrid, Zipf query | 5,000 | 108 | 79.4 | -26.6% | 145 | 133 |
| `search`, `limit=20` with snippet, selective query | 500 | 26.7 | 11.0 | -59.0% | 35.8 | 17.1 |
| `search`, `limit=20` with snippet, selective query | 5,000 | 62.5 | 14.3 | -77.1% | 80.2 | 22.8 |
| `search`, `limit=20` with snippet, Zipf query | 500 | 43.1 | 20.9 | -51.5% | 55.6 | 30.7 |
| `search`, `limit=20` with snippet, Zipf query | 5,000 | 117 | 88.2 | -24.7% | 153 | 132 |
| `context_pack`, selective query | 500 | 13.1 | 7.47 | -43.1% | 31.6 | 10.7 |
| `context_pack`, selective query | 5,000 | 55.9 | 8.86 | -84.1% | 70.6 | 16.8 |
| `context_pack`, Zipf query | 500 | 18.3 | 12.8 | -30.5% | 46.4 | 19.6 |
| `context_pack`, Zipf query | 5,000 | 108 | 84.0 | -22.3% | 153 | 130 |
| `search_deep`, selective query | 500 | 44.8 | 30.5 | -31.9% | 65.8 | 45.6 |
| `search_deep`, selective query | 5,000 | 693 | 690 | -0.4% | 788 | 801 |
| `search_deep`, Zipf query | 500 | 55.1 | 39.7 | -28.0% | 81.7 | 53.3 |
| `search_deep`, Zipf query | 5,000 | 752 | 791 | +5.3% | 867 | 957 |

## Reading

| Scenario | Docs | p50 before | p50 after | Δ p50 | p95 before | p95 after |
|---|---:|---:|---:|---:|---:|---:|
| `get_document` by `chunk_id` | 500 | 0.88 | 0.87 | -1.1% | 21.8 | 1.25 |
| `get_document` by `chunk_id` | 5,000 | 1.03 | 1.12 | +8.6% | 4.02 | 1.62 |
| `get_document` by `file_path` | 500 | 0.67 | 0.78 | +16.4% | 21.5 | 1.10 |
| `get_document` by `file_path` | 5,000 | 0.82 | 1.06 | +29.5% | 3.12 | 1.58 |

At these sizes the p50 of `get_document` is sub-millisecond and moved within noise (the comparison flags the `file_path` rows only because 0.67 to 0.78 ms is a 16 % change of a tenth of a millisecond). What changed is the tail: the usage file is no longer rewritten on the request thread, so p95 drops from about 21 ms to about 1 ms at 500 documents.

## Indexing

Milliseconds for the whole `VaultIndexer.index()` call.

| Scenario | Docs | p50 before | p50 after | Δ p50 | p95 before | p95 after |
|---|---:|---:|---:|---:|---:|---:|
| Full build | 500 | 981 | 645 | -34.3% | – | – |
| Full build | 5,000 | 18,611 | 6,596 | -64.6% | – | – |
| Reindex, nothing changed | 500 | 134 | 125 | -6.8% | – | – |
| Reindex, nothing changed | 5,000 | 1,436 | 1,524 | +6.2% | – | – |
| Reindex, one file changed | 500 | 140 | 136 | -2.9% | – | – |
| Reindex, one file changed | 5,000 | 1,469 | 1,478 | +0.6% | – | – |

The first build writes one transaction per embedding batch instead of one per document and no longer deletes vectors that do not exist yet. A reindex with nothing to do, or with one changed file, did not change.

## Startup and CLI

| Scenario | Docs | p50 before | p50 after | Δ p50 | p95 before | p95 after |
|---|---:|---:|---:|---:|---:|---:|
| `doctor --brief` | – | 812 | 185 | -77.3% | – | – |
| `import knowledge.cli` | – | 594 | 116 | -80.5% | – | – |
| stdio `search` | – | 14.0 | 7.71 | -44.9% | 42.0 | 8.74 |
| stdio `list_vaults` | – | 2.06 | 1.93 | -6.1% | 14.9 | 2.23 |

Over stdio the figures come from a real server on the smallest vault's index. Separately from the harness, the plugin launcher (`uvx --from cloudfrog-knowledge==<version> … python -c pass`) went from 536 to 92 ms warm (median of 10) and from 10.0 to 3.7 s cold (one sample, network-dependent). It takes effect with the first plugin release after 0.9.0.

## Trade-offs

Two costs came with the gains, and one expected gain did not show up. They are stated here rather than left to the tables.

| Cost | Docs | Before | After | Δ |
|---|---:|---:|---:|---:|
| `search`, first call after start or reindex (ms) | 500 | 66.8 | 104 | +55.4% |
| `search`, first call after start or reindex (ms) | 5,000 | 447 | 831 | +85.9% |
| stdio `search`, first call (ms) | – | 26.5 | 98.9 | +273.6% |
| RSS after the tool mix (MiB) | 500 | 109 | 147 | +35.0% |
| RSS after the tool mix (MiB) | 5,000 | 160 | 437 | +172.2% |

- **First `search` after a start or a reindex.** The keyword `idf` now comes from a vocabulary built lazily by that first search, instead of a dictionary held in memory. The build costs about 0.75 s at 5,000 documents once, then every search then saves about 35 ms of `idf` work at that size; it pays for itself in about 20 searches per reindex. Memory held between reindexes fell from 24.9 to 6.9 MB at 5,000 documents.
- **Resident memory.** Every connection now maps the index with `PRAGMA mmap_size` (256 MiB), which makes lexical queries 18 to 22 % faster on the 85 MiB index. All of the RSS increase is file-backed mapped pages that the kernel can reclaim; anonymous memory fell from 116.7 to 97.5 MiB. The other cost, a SIGBUS instead of a `sqlite3.Error` on an I/O error of a mapped page, is recorded at `MMAP_SIZE` in `src/knowledge/stores.py`.
- **`search_deep` at 5,000 documents** is unchanged within noise (693 to 690 ms and 752 to 791 ms at p50): the work removed here is not what that tool spends its time on.

Rows with no code of this work on their path moved within noise and are omitted: the interpreter floor, spawn to `initialize`, the first `tools/list`, the sub-millisecond `get_document` p50 and the micro-timings of the usage flush and `record_search`. The comparison flags a few of them (the interpreter floor 18.4 to 23.8 ms, the first `tools/list` 2.59 to 3.47 ms, in-child `import knowledge.server` 574 to 716 ms); the same import under pyperf, which includes interpreter start-up, moved 790 to 853 ms (+7.9 %), and they are not attributed to this work. The raw comparison is 55 rows and is not committed; the result of each item, with its commits, is in [`docs/performance/improvement-plan.md`](improvement-plan.md).

## Deterministic work counts

These are exact counts, not timings, and the suite fences them (`tests/test_perf_budgets.py` and `tests/test_perf_budget_*.py`), so they hold on any machine.

| Work | Before | After |
|---|---:|---:|
| First build, 60 documents: statements / commits / `chunks_vec` deletes | 1,039 / 60 / 59 | 929 / 5 / 0 |
| First build, 500 documents: commits | 500 | 41 |
| First build, 5,000 documents: commits / statements | 5,000 / 93,105 | 421 / 83,947 |
| `docs.body` rows read by `search` (5 or 13), `context_pack` (22), `search_deep` (63), `search_by_metadata` (10) | 5 / 13 / 22 / 63 / 10 | 0 |
| Reads of `usage_stats.json` per flush | 2 | 1 |
| Usage-file writes on request threads (700 calls, 500 documents) | 155 | 0 |
| `knowledge` modules imported by `--version` / `doctor --brief` | 27 / 27 | 15 / 22 |
| Keyword `idf` memory held between reindexes, 5,000 documents | 24.9 MB | 6.9 MB |
| `link_graph.json`, 500 documents | 153 KB | 121 KB |

## Reproduce

```bash
uv run --with pyperf==2.10.0 python -m benchmarks run --docs 500,5000
uv run python -m benchmarks compare BASE.json HEAD.json
```

Results go to `.data/bench/` and are never committed. A base commit is run from a `git archive` of it, and both sides must use the same interpreter and the same SQLite: a fresh base checkout gets its own `.venv`, and `uv` may pick a different Python for it, which shows up as a false regression in every Python-heavy scenario (see [Comparing two commits](benchmarks.md#comparing-two-commits)). `compare` does not check it, so compare `meta.python` and `meta.sqlite` in both result files. Every scenario, the result schema and the proposed SLOs are in [`docs/performance/benchmarks.md`](benchmarks.md); what was done, in which commits, and what is next is in [`docs/performance/improvement-plan.md`](improvement-plan.md).
