"""The index a build leaves behind does not depend on how the store groups its writes.

The reference below is a frozen copy of the per-document write path as it was
before batching (``SQLiteStore._delete`` and ``SQLiteStore.replace_file`` at
``a89cd5e``): one ``BEGIN IMMEDIATE … COMMIT`` per document and vectors deleted
through ``id IN (SELECT …)``. It is injected into a :class:`VaultIndexer` as its
store (a delegating wrapper over a real :class:`SQLiteStore`), so the indexer,
the chunker and the embedder are the same on both sides and only the write path
differs. Nothing is patched.

Both indexes are compared row by row: ``docs``, ``doc_tags``, ``chunks`` (ids
included), ``chunk_vectors``, ``chunks_vec`` and ``meta`` (but ``built_at``).
FTS5 is compared logically, through an ``fts5vocab`` instance table (every
term, chunk, column and offset) plus FTS5's own ``integrity-check``, and by the
answers of ``lexical`` and ``knn``. The shadow blobs of FTS5 (``chunks_fts_data``)
are not compared: their segment layout follows commit boundaries and is not
output.
"""

from __future__ import annotations

import hashlib
import random
import shutil
import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from synthetic_vault import Manifest, generate, queries

from knowledge import stores
from knowledge.embeddings import BagOfWordsEmbeddings, build_embedder
from knowledge.indexer import VaultIndexer
from knowledge.ports import ChunkFilter, ChunkRecord, DocRecord
from knowledge.stores import _DOC_COLUMNS, SQLiteStore, _check_vectors, _pack
from knowledge.vault_source import DirectoryVaultSource

FIXTURE_VAULT = Path(__file__).parent / "fixtures" / "vault"
DOCS = 300


def _need_vec() -> None:
    if stores.probe_vector_backend() == stores.BACKEND_SQLITE_VEC:
        return
    if stores.sqlite_vec_required():
        pytest.fail(f"{stores.REQUIRE_SQLITE_VEC_ENV}=1 and sqlite-vec does not load in this Python")
    pytest.skip("sqlite-vec does not load in this Python")


# ---------------------------------------------------------------------------
# The frozen reference: the per-document write path before batching
# ---------------------------------------------------------------------------


def _reference_delete(store: SQLiteStore, conn: sqlite3.Connection, file_paths: Sequence[str]) -> None:
    if not file_paths:
        return
    marks = ",".join("?" * len(file_paths))
    if store._has_table(conn, "chunks_vec"):
        if store._vec:
            conn.execute(
                f"DELETE FROM chunks_vec WHERE id IN (SELECT id FROM chunks WHERE file_path IN ({marks}))",
                list(file_paths),
            )
        else:
            conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('vec_dirty', '1')")
    conn.execute(f"DELETE FROM docs WHERE file_path IN ({marks})", list(file_paths))


def _reference_replace_file(
    store: SQLiteStore, doc: DocRecord, chunks: Sequence[ChunkRecord], vectors: Sequence[Sequence[float]] | None
) -> None:
    with store._lock:
        conn = store._write_conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            stored = store._dim(conn)
            dim = _check_vectors(chunks, vectors, stored)
            _reference_delete(store, conn, [doc.file_path])
            conn.execute(
                f"INSERT INTO docs({_DOC_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (doc.file_path, doc.doc_id, doc.title, doc.type, doc.service, doc.status, doc.owner,
                 doc.last_verified, doc.last_updated, doc.file_hash, doc.body),
            )
            conn.executemany(
                "INSERT INTO doc_tags(file_path, pos, tag) VALUES (?, ?, ?)",
                [(doc.file_path, pos, tag) for pos, tag in enumerate(doc.tags)],
            )
            ids: list[int] = []
            for chunk in chunks:
                cursor = conn.execute(
                    "INSERT INTO chunks(file_path, ord, chunk_key, title, section, overlap, text) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (doc.file_path, chunk.ord, f"{doc.doc_id}#{chunk.ord}", doc.title, chunk.section,
                     chunk.overlap, chunk.text),
                )
                ids.append(int(cursor.lastrowid or 0))
            if vectors is not None and chunks:
                blobs = [_pack(vector) for vector in vectors]
                conn.executemany(
                    "INSERT INTO chunk_vectors(id, embedding) VALUES (?, ?)", list(zip(ids, blobs, strict=True))
                )
                if stored is None and dim is not None:
                    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('dim', ?)", (str(dim),))
                if store._vec:
                    if not store._has_table(conn, "chunks_vec") and dim is not None:
                        store._create_vec(conn, dim)
                    conn.executemany(
                        "INSERT INTO chunks_vec(id, embedding, type, file_path) VALUES (?, ?, ?, ?)",
                        [(i, blob, doc.type, doc.file_path) for i, blob in zip(ids, blobs, strict=True)],
                    )
                else:
                    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('vec_dirty', '1')")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise


def _reference_delete_files(store: SQLiteStore, file_paths: Iterable[str]) -> None:
    paths = sorted(set(file_paths))
    if not paths:
        return
    with store._lock:
        conn = store._write_conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            _reference_delete(store, conn, paths)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise


class ReferenceStore:
    """A real :class:`SQLiteStore` whose writes go through the frozen per-document path."""

    def __init__(self, inner: SQLiteStore) -> None:
        self.inner = inner

    def __getattr__(self, name: str) -> Any:  # reads, meta, clear: the store itself
        return getattr(self.inner, name)

    def replace_file(
        self, doc: DocRecord, chunks: Sequence[ChunkRecord], vectors: Sequence[Sequence[float]] | None
    ) -> None:
        _reference_replace_file(self.inner, doc, chunks, vectors)

    def replace_files(
        self, items: Iterable[tuple[DocRecord, Sequence[ChunkRecord], Sequence[Sequence[float]] | None]]
    ) -> None:
        for doc, chunks, vectors in items:
            _reference_replace_file(self.inner, doc, chunks, vectors)

    def delete_files(self, file_paths: Iterable[str]) -> None:
        _reference_delete_files(self.inner, file_paths)


# ---------------------------------------------------------------------------
# Building both sides and dumping what they hold
# ---------------------------------------------------------------------------


def _pair(vault: Path, tmp_path: Path, provider: str) -> tuple[VaultIndexer, VaultIndexer]:
    def build(name: str, wrap: bool) -> VaultIndexer:
        index_dir = tmp_path / name
        store = SQLiteStore.open(index_dir)
        return VaultIndexer(
            source=DirectoryVaultSource(vault),
            store=ReferenceStore(store) if wrap else store,  # type: ignore[arg-type]
            embedder=build_embedder(provider),
            state_dir=index_dir,
        )

    return build("reference", True), build("candidate", False)


def _close(*indexers: VaultIndexer) -> None:
    for indexer in indexers:
        store = indexer.store
        (store.inner if isinstance(store, ReferenceStore) else store).close()  # type: ignore[attr-defined]


def _digest(rows: Iterable[Sequence[Any]]) -> tuple[int, str]:
    sha = hashlib.sha256()
    count = 0
    for row in rows:
        sha.update(repr(tuple(row)).encode())
        count += 1
    return count, sha.hexdigest()


def _dump(path: Path) -> dict[str, Any]:
    """Everything the index holds that any read can observe, read through its own connection."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        vec = stores._load_sqlite_vec(conn)
        tables = {
            "docs": "SELECT * FROM docs ORDER BY file_path",
            "doc_tags": "SELECT * FROM doc_tags ORDER BY file_path, pos",
            "chunks": "SELECT * FROM chunks ORDER BY id",
            "chunk_vectors": "SELECT * FROM chunk_vectors ORDER BY id",
            "meta": "SELECT key, value FROM meta WHERE key != 'built_at' ORDER BY key",
            "fts_content": "SELECT rowid, title, section, text FROM chunks_fts ORDER BY rowid",
        }
        dump: dict[str, Any] = {name: conn.execute(sql).fetchall() for name, sql in tables.items()}
        has_vec = conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'chunks_vec'").fetchone() is not None
        dump["has_chunks_vec"] = has_vec
        if has_vec and vec:
            dump["chunks_vec"] = conn.execute(
                "SELECT id, embedding, type, file_path FROM chunks_vec ORDER BY id"
            ).fetchall()
        conn.execute("CREATE VIRTUAL TABLE temp.vocab USING fts5vocab(main, chunks_fts, 'instance')")
        dump["fts_instances"] = _digest(
            conn.execute("SELECT term, doc, col, offset FROM temp.vocab ORDER BY term, doc, col, offset")
        )
        conn.execute("CREATE VIRTUAL TABLE temp.vocab_row USING fts5vocab(main, chunks_fts, 'row')")
        dump["fts_rows"] = conn.execute("SELECT term, doc, cnt FROM temp.vocab_row ORDER BY term").fetchall()
    finally:
        conn.close()
    check = sqlite3.connect(str(path))
    try:
        check.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('integrity-check')")
    finally:
        check.close()
    return dump


FILTERS: tuple[ChunkFilter | None, ...] = (
    None,
    ChunkFilter(equals={"type": "runbook"}),
    ChunkFilter(exclude_types=("memory",), exclude_prefixes=("90 - Memory/",)),
    ChunkFilter(tag="incident"),
)


def _answers(store: SQLiteStore, words: Sequence[str], vectors: bool) -> list[Any]:
    out: list[Any] = []
    embedder = BagOfWordsEmbeddings()
    for query in words:
        for k in (2, 10, 40):
            for flt in FILTERS:
                out.append(store.lexical(query, k, flt))
    if vectors:
        for query in words[:: max(1, len(words) // 10)][:10]:
            vector = embedder.embed([query])[0]
            for k in (2, 10, 40):
                for flt in FILTERS:
                    out.append(store.knn(vector, k, flt))
    out.append(store.documents())
    out.append(sorted(store.file_hashes().items()))
    return out


def _assert_same(reference: VaultIndexer, candidate: VaultIndexer, words: Sequence[str], *, vectors: bool) -> None:
    ref_store: SQLiteStore = reference.store.inner  # type: ignore[attr-defined]
    cand_store: SQLiteStore = candidate.store  # type: ignore[assignment]
    expected, got = _dump(ref_store.path), _dump(cand_store.path)
    assert got.keys() == expected.keys()
    for key in expected:
        assert got[key] == expected[key], key
    assert _answers(cand_store, words, vectors) == _answers(ref_store, words, vectors)


def _mutate(manifest: Manifest, seed: int) -> None:
    """Change, shrink, delete and add documents: what an edit session between two reindexes does."""
    rng = random.Random(seed)
    root = manifest.root
    paths = list(manifest.paths)
    for rel in paths[::7]:
        target = root / rel
        target.write_text(target.read_text(encoding="utf-8") + f"\n\n## Added {rng.random()}\nfresh text appended\n",
                          encoding="utf-8")
    multichunk = manifest.specials.get("multichunk")
    if multichunk:
        target = root / multichunk
        head = target.read_text(encoding="utf-8")[:600]
        target.write_text(head + "\n", encoding="utf-8")
    for rel in paths[3::11]:
        if rel not in manifest.specials.values():
            (root / rel).unlink()
    for n in range(5):
        source = root / paths[(n * 37) % len(paths)]
        if source.exists():
            shutil.copyfile(source, root / f"Added Copy {n}.md")


def _words(manifest: Manifest) -> list[str]:
    picked = queries(manifest, seed=11, n=100, selectivity=0.02, terms=1)
    picked += queries(manifest, seed=12, n=60, selectivity=None, terms=2)
    picked += queries(manifest, seed=13, n=40, selectivity=0.2, terms=3)
    return picked


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider", ["bag-of-words", "none"])
def test_the_fixture_vault_indexes_to_the_same_store(tmp_path: Path, provider: str) -> None:
    _need_vec()
    vault = tmp_path / "vault"
    shutil.copytree(FIXTURE_VAULT, vault)
    reference, candidate = _pair(vault, tmp_path, provider)
    try:
        assert candidate.index() == reference.index()
        words = ["middleware", "gateway outage", "auth", "runbook", "otp sesión", "flujo", "deploy"]
        _assert_same(reference, candidate, words, vectors=provider != "none")
        assert candidate.index(force=True) == reference.index(force=True)
        _assert_same(reference, candidate, words, vectors=provider != "none")
    finally:
        _close(reference, candidate)


@pytest.fixture()
def generated(tmp_path: Path) -> Iterator[Manifest]:
    yield generate(tmp_path / "vault", docs=DOCS, seed=7)


def test_a_generated_vault_indexes_to_the_same_store_across_updates_and_deletions(
    tmp_path: Path, generated: Manifest
) -> None:
    _need_vec()
    reference, candidate = _pair(generated.root, tmp_path, "bag-of-words")
    words = _words(generated)
    try:
        assert candidate.index() == reference.index()
        assert candidate.store.count() > DOCS  # multi-chunk documents, so batches span documents
        _assert_same(reference, candidate, words[:40], vectors=True)

        # Every answer (200 queries x k in {2, 10, 40} x 4 filters) once, after an
        # edit session: the state where ids were freed and reused.
        _mutate(generated, seed=3)
        assert candidate.index() == reference.index()
        _assert_same(reference, candidate, words, vectors=True)

        assert candidate.index(force=True) == reference.index(force=True)
        _assert_same(reference, candidate, words[:40], vectors=True)

        gone = sorted(candidate.store.file_hashes())[::5]
        reference.store.delete_files(gone)
        candidate.store.delete_files(gone)
        _assert_same(reference, candidate, words[:40], vectors=True)
    finally:
        _close(reference, candidate)
