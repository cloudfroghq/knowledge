"""Baseline work-count fences: how much work each operation does TODAY, as upper bounds.

Nothing here reads a clock. Each test runs one operation on a small synthetic vault
(``tests/synthetic_vault.py``, 60 documents, seed 7) and counts what it did with the
helpers of ``tests/perf_counters.py``: SQL statements, commits, rows handed to Python,
rows that carry a given column, Markdown files opened, YAML documents parsed. The
generator is byte-deterministic and the embeddings are the ``bag-of-words`` provider,
so the numbers are the same on a laptop and on the Linux pipeline, and a regression
fails the suite without any wall-clock flake. Wall-clock claims belong to
``benchmarks/`` (``docs/performance/benchmarks.md``).

How to read a budget. The limit is the measured value on 2026-10-31 (commit ``f163991``),
not a goal. Counts that are structural (files opened, YAML parsed, commits) are exact.
Statement counts allow ``max(2, 10 %)`` and fetched-row counts 20 % of room, so an
unrelated, legitimate change does not trip them while an N+1 per result still does. A
faster implementation passes these tests untouched; a slower one fails them.

Budgets expected to TIGHTEN (the owner of each item lowers its limit in the same change,
and the integrator re-checks the rest after the streams merge). Each carries a
``# TIGHTEN`` comment at its limit:

* index, 1.3 / 1.4 (store-index): no-op reindex parses 0 YAML documents and opens each
  Markdown file once; one transaction per embedding batch instead of per document; no
  ``DELETE FROM chunks_vec`` for a document that is new.
* lexical and ``search`` family, 2.1 (two-phase keyword path): the lexical statement hands
  back text for at most ``k`` chunks, not for every match.
* ``search``, 1.7a / 1.7b (``with_body``): no ``docs.body`` column is fetched to build a
  result row.
* usage, 1.5 / 0.3 (request-path): a flush reads the file once, and never on a request thread.
* start-up, 1.2 (cli-launcher): ``--version`` and ``doctor --brief`` no longer import
  ``knowledge.server``.

Only fixtures and generated vaults in ``tmp_path`` are used, nothing is patched, and the
registry the tools serve is installed with ``server.set_registry`` (and put back).
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from perf_counters import (
    FileOpens,
    SqlTrace,
    file_opens,
    modules_after_cli,
    sql_trace,
    yaml_parses,
)
from synthetic_vault import Manifest, generate, queries

from knowledge import server, settings, stores
from knowledge.embeddings import build_embedder
from knowledge.indexer import VaultIndexer
from knowledge.registry import VaultRegistry, VaultRuntime
from knowledge.usage import UsageTracker

DOCS = 60
SEED = 7
CONFIG_JSON = Path(__file__).parent / "fixtures" / "config.json"

# Sibling modules of the harness are plain scripts; make the package importable for the unit tests below.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from benchmarks import common as bench_common  # noqa: E402
from benchmarks import report as bench_report  # noqa: E402
from benchmarks import run as bench_run  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _need_vec() -> None:
    """The statement counts below are those of the ``sqlite-vec`` backend; the stdlib fallback issues others."""
    if stores.probe_vector_backend() == stores.BACKEND_SQLITE_VEC:
        return
    if stores.sqlite_vec_required():
        pytest.fail("KNOWLEDGE_REQUIRE_SQLITE_VEC=1 and sqlite-vec does not load in this Python")
    pytest.skip("work counts are fenced for the sqlite-vec backend; this Python cannot load it")


def statements(count: int) -> int:
    """Room for a statement count: 10 %, at least 2 (an N+1 over 5 results is +5 and still fails)."""
    return count + max(2, count // 10)


def rows(count: int) -> int:
    """Room for a fetched-row count: 20 %."""
    return count + max(1, count // 5)


@dataclass
class Work:
    """What one operation did, as the counters saw it."""

    sql: SqlTrace
    files: FileOpens
    yaml_documents: int

    @property
    def md_reads(self) -> int:
        return self.files.reads("*.md")


def measure(target: Any, root: Path, action: Callable[[], Any]) -> Work:
    """Run ``action`` and report its SQL (on ``target``'s store), file opens under ``root`` and YAML parses."""
    with sql_trace(target) as sql, file_opens(root) as files, yaml_parses() as yaml:
        action()
    return Work(sql, files, yaml.count())


def _call(coro: Any) -> Any:
    return asyncio.run(coro)


def _indexer(vault: Path, index_dir: Path) -> VaultIndexer:
    index_dir.mkdir(parents=True, exist_ok=True)
    embedder = build_embedder("bag-of-words", model=None, api_key=None)
    return VaultIndexer.open(vault_path=vault, index_dir=index_dir, embedder=embedder)


# ---------------------------------------------------------------------------
# Indexing: first build, no-op reindex, one changed file
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def index_lifecycle(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Work]]:
    """First index, then a no-op reindex, then a reindex after one edit, each measured once."""
    _need_vec()
    root = tmp_path_factory.mktemp("budget-index")
    manifest = generate(root / "vault", docs=DOCS, seed=SEED)
    indexer = _indexer(manifest.root, root / "index")
    out: dict[str, Work] = {}
    out["first"] = measure(indexer, manifest.root, indexer.index)
    out["noop"] = measure(indexer, manifest.root, indexer.index)
    edited = manifest.root / manifest.paths[5]
    edited.write_text(
        edited.read_text(encoding="utf-8") + "\nOne more sentence for the budget test.\n", encoding="utf-8"
    )
    out["changed"] = measure(indexer, manifest.root, indexer.index)
    yield out
    indexer.store.close()  # type: ignore[attr-defined]


def test_first_index_work_per_document(index_lifecycle: dict[str, Work]) -> None:
    work = index_lifecycle["first"]

    assert len(work.sql.statements) <= statements(1039)
    assert work.sql.writes <= 735
    # TIGHTEN (1.4): one transaction per embedding batch, so about DOCS / 32 commits, not one per document.
    assert work.sql.commits <= DOCS
    # TIGHTEN (1.3): a new document has no vectors to delete; the DELETE scans the whole vec0 table.
    assert work.sql.count(r"^DELETE FROM chunks_vec") <= DOCS - 1
    # Each Markdown file is opened twice (hash, then frontmatter) and its YAML parsed once.
    assert work.md_reads <= 2 * DOCS
    assert work.yaml_documents <= DOCS
    assert work.files.writes("*.md") == []  # the indexer never writes into the vault


def test_a_noop_reindex_does_work_proportional_to_the_vault(index_lifecycle: dict[str, Work]) -> None:
    work = index_lifecycle["noop"]

    assert len(work.sql.statements) <= statements(10)
    assert work.sql.commits == 0
    # Only the meta bookkeeping rows are written.
    assert work.sql.count(r"^(INSERT|UPDATE|DELETE)\b") == work.sql.count(r"^INSERT OR REPLACE INTO meta")
    assert work.sql.rows_fetched <= rows(71)  # one (path, hash) row per document and a few counters
    assert work.sql.rows_with_column("body") == 0
    # TIGHTEN (1.3): hash each file once (60 opens, not 120) and parse no YAML for an unchanged file (0, not 60).
    assert work.md_reads <= 2 * DOCS
    assert work.yaml_documents <= DOCS


def test_a_one_file_change_reindexes_one_document_but_rereads_the_vault(index_lifecycle: dict[str, Work]) -> None:
    work = index_lifecycle["changed"]

    assert len(work.sql.statements) <= statements(26)
    assert work.sql.commits <= 1
    assert work.sql.count(r"^DELETE FROM chunks_vec") <= 1
    # TIGHTEN (1.3): unchanged files are not reopened or parsed again.
    assert work.md_reads <= 2 * DOCS
    assert work.yaml_documents <= DOCS


# ---------------------------------------------------------------------------
# The tools: search, context_pack, search_deep, get_document, listings
# ---------------------------------------------------------------------------


@dataclass
class Served:
    manifest: Manifest
    runtime: VaultRuntime
    selective: list[str]
    unselective: list[str]

    @property
    def root(self) -> Path:
        return self.manifest.root


def _open_registry(vault: Path, index_dir: Path, project_root: Path) -> VaultRegistry:
    loaded = settings.load_settings(
        {
            "VAULT_PATH": str(vault),
            "KNOWLEDGE_INDEX_DIR": str(index_dir),
            "KNOWLEDGE_EMBEDDINGS_PROVIDER": "bag-of-words",
            "KNOWLEDGE_CONFIG_JSON": str(CONFIG_JSON),
        },
        project_root=project_root,
    )
    return VaultRegistry.from_settings(loaded)


@pytest.fixture(scope="module")
def served(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Served]:
    """The tools served over a 60-document index of their own, put back afterwards."""
    _need_vec()
    root = tmp_path_factory.mktemp("budget-served")
    manifest = generate(root / "vault", docs=DOCS, seed=SEED)
    index_dir = root / "index"
    index_dir.mkdir()
    registry = _open_registry(manifest.root, index_dir, root)
    runtime = registry.default
    previous = server._registry  # the module keeps the registry it serves in a private global
    server.set_registry(registry)
    try:
        runtime.indexer.index()
        runtime.index_state["ready"] = True
        # The first query of each set: two words that match about 2 % of the documents, and two that match most.
        selective = queries(manifest, seed=1, n=20, selectivity=0.02)
        unselective = queries(manifest, seed=1, n=20, selectivity=None)
        for query in (selective[0], unselective[0]):  # warm the query-embedding cache and the graph
            _call(server.search(query))
        yield Served(manifest, runtime, selective, unselective)
    finally:
        server.set_registry(previous)
        runtime.indexer.store.close()  # type: ignore[attr-defined]


def test_the_served_vault_is_the_synthetic_one(served: Served) -> None:
    """Guards the numbers below: they hold for this vault, this seed and these two queries."""
    assert len(served.manifest.paths) == DOCS
    assert served.selective[0] == "nifeltl cizo"
    assert served.unselective[0] == "rasuten coloton"
    assert served.runtime.indexer.store.count() == 142


def test_search_reads_one_file_per_result(served: Served) -> None:
    for query in (served.selective[0], served.unselective[0]):
        work = measure(served.runtime, served.root, lambda q=query: _call(server.search(q, limit=5)))

        assert work.md_reads <= 5  # TIGHTEN (1.4): the section selector reads the chunk, not the file
        assert work.yaml_documents <= 5
        assert work.files.writes("*") == []


def test_search_statements_and_rows_on_a_selective_query(served: Served) -> None:
    work = measure(served.runtime, served.root, lambda: _call(server.search(served.selective[0], limit=5)))

    assert len(work.sql.statements) <= statements(15)
    assert work.sql.rows_fetched <= rows(45)
    # TIGHTEN (1.7a/1.7b): building a result row needs the title and review state, not the document body.
    assert work.sql.rows_with_column("body") <= 5
    assert work.sql.selects_column("docs", "body") <= 1


def test_search_statements_and_rows_on_an_unselective_query(served: Served) -> None:
    work = measure(served.runtime, served.root, lambda: _call(server.search(served.unselective[0], limit=5)))

    assert len(work.sql.statements) <= statements(15)
    # TIGHTEN (2.1): the keyword statement returns every matching chunk with its text; two-phase returns about k.
    assert work.sql.rows_fetched <= rows(131)
    assert work.sql.rows_with_column("text") <= rows(100)
    assert work.sql.rows_with_column("body") <= 13


def test_the_keyword_mode_is_one_lexical_statement_plus_the_row_lookups(served: Served) -> None:
    work = measure(served.runtime, served.root, lambda: _call(server.search(served.unselective[0], mode="keyword")))

    assert work.sql.count(r"FROM chunks_fts") == 1
    assert len(work.sql.statements) <= statements(5)
    assert work.sql.rows_fetched <= rows(94)  # TIGHTEN (2.1)


def test_the_semantic_mode_runs_one_knn(served: Served) -> None:
    work = measure(served.runtime, served.root, lambda: _call(server.search(served.selective[0], mode="semantic")))

    assert work.sql.count(r"FROM chunks_vec WHERE embedding MATCH") == 1
    assert work.sql.count(r"FROM chunks_fts") == 0
    assert len(work.sql.statements) <= statements(14)
    assert work.sql.rows_fetched <= rows(43)


def test_context_pack_work(served: Served) -> None:
    work = measure(served.runtime, served.root, lambda: _call(server.context_pack(served.unselective[0])))

    assert len(work.sql.statements) <= statements(15)
    assert work.sql.rows_fetched <= rows(173)  # TIGHTEN (2.1)
    assert work.md_reads <= 8  # one file per packed row; TIGHTEN (1.4)
    assert work.yaml_documents <= 8
    assert work.sql.rows_with_column("body") <= 22  # TIGHTEN (1.7)


def test_search_deep_work(served: Served) -> None:
    work = measure(served.runtime, served.root, lambda: _call(server.search_deep(served.unselective[0])))

    assert len(work.sql.statements) <= statements(28)
    # TIGHTEN (2.1, 2.3): the hop pass re-ranks every chunk of the hop files and loads their bodies for titles.
    assert work.sql.rows_fetched <= rows(522)
    assert work.sql.rows_with_column("body") <= 63
    assert work.sql.selects_column("docs", "body") <= 2
    assert work.md_reads <= 5
    assert work.yaml_documents <= 5


@pytest.mark.parametrize("by", ["file_path", "chunk_id"])
def test_get_document_is_one_file_read_and_no_sql(served: Served, by: str) -> None:
    found = _call(server.search(served.unselective[0], limit=5))
    row = next(r for r in found if r["chunk_id"])  # a row has no chunk_id when no block of it was selected
    kwargs = {"file_path": row["file_path"]} if by == "file_path" else {"chunk_id": row["chunk_id"], "max_chars": 800}

    work = measure(served.runtime, served.root, lambda: _call(server.get_document(**kwargs)))

    assert len(work.sql.statements) == 0
    assert work.md_reads <= 2  # TIGHTEN (1.4): one read
    assert work.yaml_documents <= 1


def test_search_by_metadata_selects_the_body_of_every_match(served: Served) -> None:
    work = measure(served.runtime, served.root, lambda: _call(server.search_by_metadata(type="runbook")))

    assert len(work.sql.statements) <= statements(4)
    assert work.md_reads == 0
    # TIGHTEN (1.7b): a listing row needs no body.
    assert work.sql.rows_with_column("body") <= 10
    assert work.sql.selects_column("docs", "body") <= 1


def test_the_graph_tools_and_list_vaults_touch_neither_sql_nor_files(served: Served) -> None:
    first, second = served.manifest.paths[0], served.manifest.paths[7]
    calls: list[Callable[[], Any]] = [
        lambda: _call(server.find_path(first, second)),
        lambda: _call(server.get_related(first)),
        lambda: _call(server.get_impact(first)),
        lambda: _call(server.list_vaults()),
    ]
    for call in calls:
        work = measure(served.runtime, served.root, call)

        assert len(work.sql.statements) == 0
        assert work.md_reads == 0
        assert work.yaml_documents == 0


def test_the_lexical_statement_hands_back_every_matching_chunk(served: Served) -> None:
    store = served.runtime.indexer.store
    with sql_trace(store) as unselective:
        hits = store.lexical(served.unselective[0], 10, None)
    with sql_trace(store) as selective:
        store.lexical(served.selective[0], 10, None)

    assert len(hits) == 10
    assert len(unselective.statements) == 1 and len(selective.statements) == 1
    # TIGHTEN (2.1): both rows counts fall to about k (10); text and bm25 come back for every match today.
    assert unselective.rows_fetched <= 70
    assert unselective.rows_with_column("text") <= 70
    assert selective.rows_fetched <= 2


# ---------------------------------------------------------------------------
# Usage persistence
# ---------------------------------------------------------------------------


def _tracker_files(index_dir: Path) -> tuple[Path, ...]:
    return (index_dir / "usage_stats.json", *index_dir.glob("usage_stats.json.*"))


def test_a_usage_flush_reads_the_file_twice_and_writes_it_once(tmp_path: Path) -> None:
    tracker = UsageTracker(tmp_path / "usage_stats.json")
    tracker.record_uses(["a.md", "b.md"])
    tracker.flush()

    with file_opens(tmp_path) as files:
        tracker.flush()

    # TIGHTEN (1.5): one read (the spend and the call days come from a single parse).
    assert files.reads("usage_stats.json") == 2
    assert len(files.writes("usage_stats.json.*.tmp")) == 1  # written beside, then renamed over
    assert len(files.writes("usage_stats.json.lock")) <= 1


def test_search_hits_flush_every_twenty_recorded_hits(tmp_path: Path) -> None:
    """Twenty-four searches of five results are 144 recorded hits: six flushes, each rewriting the whole file."""
    tracker = UsageTracker(tmp_path / "usage_stats.json")
    results = [f"Doc {i}.md" for i in range(5)]

    with file_opens(tmp_path) as files:
        for _ in range(24):
            tracker.record_uses(results)
            tracker.record_pairs(results)

    flushes = len(files.writes("usage_stats.json.*.tmp"))
    # TIGHTEN (1.5, 0.3): a flush no longer reads the file twice, and it happens off the calling (request) thread:
    # today every one of these writes is made by the thread that recorded the hit, which is the latency tail.
    assert flushes <= 6
    assert files.reads("usage_stats.json") <= 2 * flushes


# ---------------------------------------------------------------------------
# Process start-up
# ---------------------------------------------------------------------------

#: The ``knowledge`` modules that ``--version`` and ``doctor --brief`` import today (27, because ``cli`` imports
#: ``server``). A new import at start-up fails here; an extension module imported eagerly fails here.
# TIGHTEN (1.2): drop ``server`` and everything only it pulls in (``gitsync``, ``memory_http``, ``review``, ``rerank``,
# ``principals``, ``profiles``, ``registry``, ``contract``, ``extensions``, ``contexts``, ...) from the allowed set.
STARTUP_MODULES = frozenset(
    {
        "knowledge",
        "knowledge.chunker",
        "knowledge.cli",
        "knowledge.config",
        "knowledge.contexts",
        "knowledge.contract",
        "knowledge.embeddings",
        "knowledge.errors",
        "knowledge.extensions",
        "knowledge.gitsync",
        "knowledge.graph",
        "knowledge.indexer",
        "knowledge.memory_http",
        "knowledge.pointers",
        "knowledge.ports",
        "knowledge.principals",
        "knowledge.profiles",
        "knowledge.registry",
        "knowledge.repos",
        "knowledge.rerank",
        "knowledge.review",
        "knowledge.server",
        "knowledge.settings",
        "knowledge.stores",
        "knowledge.usage",
        "knowledge.vault_lint",
        "knowledge.vault_source",
    }
)
#: Loaded only the first time a vault needs them; importing one at start-up is a regression.
LAZY_MODULES = (
    "knowledge.flows",
    "knowledge.judges",
    "knowledge.jira_client",
    "knowledge.jira_sync",
    "knowledge.confluence_sync",
    "knowledge.memory_bridge",
    "knowledge.graph_targets",
    "knowledge.webhook",
    "knowledge.auth",
)


@pytest.mark.parametrize("argv", [["--version"], ["doctor", "--brief"]], ids=["version", "doctor-brief"])
def test_startup_imports_no_new_knowledge_module(argv: list[str]) -> None:
    env = {**os.environ, "KNOWLEDGE_EMBEDDINGS_PROVIDER": "bag-of-words"}

    modules = modules_after_cli(argv, env)

    imported = {name for name in modules if name == "knowledge" or name.startswith("knowledge.")}
    assert imported <= STARTUP_MODULES, f"new start-up imports: {sorted(imported - STARTUP_MODULES)}"
    assert not imported & set(LAZY_MODULES)


# ---------------------------------------------------------------------------
# The harness itself (statistics and rendering; no clock, no subprocess)
# ---------------------------------------------------------------------------


def test_percentiles_interpolate_and_the_mad_is_robust() -> None:
    values = [float(v) for v in range(1, 101)]

    assert bench_common.percentile(values, 50) == pytest.approx(50.5)
    assert bench_common.percentile(values, 95) == pytest.approx(95.05)
    assert bench_common.percentile([7.0], 99) == 7.0
    assert bench_common.mad([1.0, 2.0, 3.0, 4.0, 1000.0]) == 1.0


def test_a_summary_withholds_percentiles_the_sample_cannot_support() -> None:
    few = bench_common.summarize([1.0, 2.0, 3.0])
    some = bench_common.summarize(range(1, 51))
    many = bench_common.summarize(range(1, 201))

    assert (few["p95"], few["p99"]) == (None, None)
    assert some["p95"] is not None and some["p99"] is None
    assert many["p95"] is not None and many["p99"] is not None and many["n"] == 200


def test_the_runner_parses_its_documented_flags_and_renders_a_comparison() -> None:
    parsed = bench_run.build_parser().parse_args(["run", "--docs", "500,5000", "--selectivity", "0.02,zipf", "--quick"])
    assert (parsed.docs, parsed.quick) == ("500,5000", True)

    def doc(p50: float) -> dict[str, Any]:
        record = bench_common.summarize([p50] * 20)
        return {"meta": {"commit": "x", "cpu": "c", "nproc": 4}, "results": {"d500.search.hybrid.zipf": record}}

    table = bench_report.render_comparison(doc(10.0), doc(5.0), 0.15)

    assert "-50.0%" in table and "faster" in table
