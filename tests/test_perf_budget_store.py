"""Work budgets of the store and the index: exact counts of statements, commits and calls, never a clock.

Each budget pins how much work one operation of the store or of the indexer may
do on a generated vault (``tests/synthetic_vault.py``), counted with the
observers of ``tests/perf_counters.py``: SQLite's trace callback and
``row_factory`` through ``SQLiteStore.observe`` and ``sys.monitoring`` for calls.
Nothing is patched and nothing is timed.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from perf_counters import count_calls, sql_trace
from synthetic_vault import Manifest, generate

from knowledge import stores
from knowledge.embeddings import build_embedder
from knowledge.indexer import VaultIndexer
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
    from knowledge.ports import ChunkRecord, DocRecord

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
