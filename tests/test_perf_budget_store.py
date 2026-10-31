"""Work budgets of the store and the index: exact counts of statements, commits and calls, never a clock.

Each budget pins how much work one operation of the store or of the indexer may
do on a generated vault (``tests/synthetic_vault.py``), counted with the
observers of ``tests/perf_counters.py``: SQLite's trace callback and
``row_factory`` through ``SQLiteStore.observe`` and ``sys.monitoring`` for calls.
Nothing is patched and nothing is timed.
"""

from __future__ import annotations

import dataclasses
import sqlite3
import threading
import tracemalloc
from collections.abc import Iterator
from pathlib import Path

import pytest
from perf_counters import count_calls, sql_trace
from synthetic_vault import Manifest, generate, queries

from knowledge import pointers, server, settings, stores
from knowledge.embeddings import build_embedder
from knowledge.indexer import KeywordIndex, VaultIndexer, _keyword_text, _RunVocabulary
from knowledge.ports import ChunkFilter, ChunkRecord, DocRecord
from knowledge.registry import build_runtime
from knowledge.stores import SQLiteStore

DOCS = 300


def _need_vec() -> None:
    if stores.probe_vector_backend() == stores.BACKEND_SQLITE_VEC:
        return
    if stores.sqlite_vec_required():
        pytest.fail(f"{stores.REQUIRE_SQLITE_VEC_ENV}=1 and sqlite-vec does not load in this Python")
    pytest.skip("sqlite-vec does not load in this Python")


@pytest.fixture(scope="module")
def manifest(tmp_path_factory: pytest.TempPathFactory) -> Manifest:
    return generate(tmp_path_factory.mktemp("budget-store") / "vault", docs=DOCS, seed=7)


def _indexer(manifest: Manifest, index_dir: Path, provider: str = "bag-of-words") -> VaultIndexer:
    return VaultIndexer.open(vault_path=manifest.root, index_dir=index_dir, embedder=build_embedder(provider))


@pytest.fixture()
def indexer(manifest: Manifest, tmp_path: Path) -> Iterator[VaultIndexer]:
    _need_vec()
    built = _indexer(manifest, tmp_path / "index")
    yield built
    built.store.close()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# 1.3 index writes: one transaction per embedding batch, vectors deleted by id
# ---------------------------------------------------------------------------


def test_a_first_build_commits_once_per_batch_and_deletes_no_vector(indexer: VaultIndexer) -> None:
    with sql_trace(indexer) as trace, count_calls(VaultIndexer._write) as calls:
        stats = indexer.index(force=True)
    batches = calls.count("_write")
    assert stats["new"] == DOCS and stats["chunks"] > DOCS
    # One transaction per _write batch; the metadata writes autocommit, and a
    # first build deletes no stale document. It was one commit per document.
    assert trace.commits == trace.count(r"^BEGIN IMMEDIATE") == batches
    # Every batch but the last holds at least EMBED_BATCH (32) chunks.
    assert 0 < batches <= stats["chunks"] // 32 + 1
    # An empty store holds no vector to delete.
    assert trace.count(r"^DELETE FROM chunks_vec") == 0
    assert trace.count(r"DELETE FROM chunks_vec WHERE id IN \(SELECT") == 0


def test_a_forced_rebuild_deletes_each_stored_vector_once_by_id(indexer: VaultIndexer) -> None:
    indexer.index()
    stored = indexer.store.count()
    with sql_trace(indexer) as trace, count_calls(VaultIndexer._write) as calls:
        indexer.index(force=True)
    assert trace.commits == trace.count(r"^BEGIN IMMEDIATE") == calls.count("_write") < DOCS
    assert trace.count(r"DELETE FROM chunks_vec WHERE id IN \(SELECT") == 0
    assert trace.count(r"^DELETE FROM chunks_vec WHERE id = \d+$") == stored
    # One id lookup per document that had chunks, none for the others.
    assert trace.count(r"^SELECT id FROM chunks WHERE file_path IN \(") == DOCS


def test_a_keyword_only_build_also_commits_once_per_batch(manifest: Manifest, tmp_path: Path) -> None:
    built = _indexer(manifest, tmp_path / "index-none", provider="none")
    try:
        with sql_trace(built) as trace, count_calls(VaultIndexer._write) as calls:
            built.index(force=True)
        assert trace.commits == calls.count("_write") < DOCS
        assert trace.count(r"^(?:INSERT INTO|DELETE FROM) chunks_vec") == 0
    finally:
        built.store.close()  # type: ignore[attr-defined]


def test_replace_file_is_still_one_transaction(tmp_path: Path) -> None:
    _need_vec()
    store = SQLiteStore.open(tmp_path / "idx")
    try:
        doc = DocRecord(file_path="a.md", doc_id="a", title="A", file_hash="h", body="x")
        rows = [ChunkRecord(file_path="a.md", ord=n, text=f"alpha {n}") for n in range(3)]
        store.replace_file(doc, rows, [[1.0, 0.0, float(n)] for n in range(3)])
        with sql_trace(store) as trace:
            store.replace_file(doc, rows, [[0.0, 1.0, float(n)] for n in range(3)])
        assert trace.commits == trace.count(r"^BEGIN IMMEDIATE") == 1
        assert trace.count(r"^DELETE FROM chunks_vec WHERE id = \d+$") == 3
        with sql_trace(store) as trace:
            store.replace_files([])
        assert trace.statements == []
    finally:
        store.close()


# ---------------------------------------------------------------------------
# 1.7a documents without their body
# ---------------------------------------------------------------------------


def test_documents_without_body_never_read_the_body_column(indexer: VaultIndexer) -> None:
    indexer.index()
    store = indexer.store
    for flt in (None, ChunkFilter(equals={"type": "runbook"}), ChunkFilter(tag="incident")):
        with sql_trace(store) as full:
            with_body = store.documents(flt)
        with sql_trace(store) as lean:
            without = store.documents(flt, with_body=False)
        assert len(without) == len(with_body)
        assert full.selects_column("docs", "body") == 1
        assert lean.selects_column("docs", "body") == 0
        assert lean.rows_with_column("body") == 0
        assert not any(" body" in statement.split(" FROM ")[0] for statement in lean.statements)
        # The same statements otherwise, and the same rows (documents plus tags).
        assert len(lean.statements) == len(full.statements)
        assert lean.rows_fetched == full.rows_fetched


# ---------------------------------------------------------------------------
# 1.4 idf from a vocabulary of whitespace runs
# ---------------------------------------------------------------------------


def test_searches_never_materialize_the_bodies_and_each_term_is_scanned_once(
    manifest: Manifest, tmp_path: Path
) -> None:
    spec = dataclasses.replace(settings.current().default_vault(), path=manifest.root, index_dir=tmp_path / "rt")
    runtime = build_runtime(spec, embedder=build_embedder("none"))
    try:
        runtime.indexer.index()
        asked = queries(manifest, seed=21, n=10, selectivity=0.02) + queries(manifest, seed=22, n=10, selectivity=None)
        asked[3] = asked[3].upper()  # the same terms in another case are the same needles
        asked.append(asked[0])  # a repeated query
        with count_calls(_RunVocabulary.document_frequency) as scans:
            answers = [server._query_idf(runtime, query) for query in asked]
        keyword_index = runtime.indexer.keyword_index
        assert keyword_index._docs is None, "no body was materialized"
        needles = {term.lower() for query in asked for term in pointers.query_terms(query)}
        assert scans.count("document_frequency") == len(needles)
        assert all(value >= 0 for answer in answers for value in answer.values())
    finally:
        runtime.indexer.store.close()  # type: ignore[attr-defined]


def test_the_vocabulary_holds_less_than_the_bodies_it_replaces(manifest: Manifest, tmp_path: Path) -> None:
    built = _indexer(manifest, tmp_path / "index-none", provider="none")
    try:
        built.index()
        bodies = list(built.store.bodies())
    finally:
        built.store.close()  # type: ignore[attr-defined]
    tracemalloc.start()
    try:
        reference = {path: _keyword_text(body).lower() for path, body in bodies}  # what idf scanned before
        held_before, _ = tracemalloc.get_traced_memory()
        del reference
        tracemalloc.reset_peak()
        base, _ = tracemalloc.get_traced_memory()
        index = KeywordIndex.from_bodies(iter(bodies), reload=lambda: iter(bodies))
        held, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert index._docs is None
    # What stays in memory between reindexes: well under half of the bodies.
    assert held - base <= held_before // 2
    # The build itself peaks near the size of the bodies on a small vault; the
    # vocabulary grows sublinearly, so the peak falls below them as the vault grows.
    assert peak - base <= held_before * 5 // 4


# ---------------------------------------------------------------------------
# 1.8 mmap_size on every store connection
# ---------------------------------------------------------------------------


def test_every_store_connection_maps_the_index(tmp_path: Path) -> None:
    probe = sqlite3.connect(str(tmp_path / "probe.sqlite"))
    try:
        # What this SQLite build grants (it clamps to SQLITE_MAX_MMAP_SIZE).
        expected = probe.execute(f"PRAGMA mmap_size={stores.MMAP_SIZE}").fetchone()[0]
    finally:
        probe.close()
    assert expected == stores.MMAP_SIZE, "the pipeline's SQLite maps up to MMAP_SIZE"
    store = SQLiteStore.open(tmp_path / "idx")
    try:
        with store._lock:
            assert store._write_conn().execute("PRAGMA mmap_size").fetchone()[0] == expected
        assert store._reader().execute("PRAGMA mmap_size").fetchone()[0] == expected
        seen: list[int] = []
        worker = threading.Thread(target=lambda: seen.append(store._reader().execute("PRAGMA mmap_size").fetchone()[0]))
        worker.start()
        worker.join()
        assert seen == [expected]
        store.close()  # a reopened connection maps it too
        assert store._reader().execute("PRAGMA mmap_size").fetchone()[0] == expected
        with sql_trace(store) as trace:
            store.count()
        assert trace.count(r"mmap_size") == 0, "the pragma is connection setup, not a per-read statement"
    finally:
        store.close()
