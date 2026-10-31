"""The store port, run against both stores: SQLite (FTS5 and sqlite-vec) and in-memory.

Every case runs on :class:`~knowledge.stores.InMemoryVectorStore` and on
:class:`~knowledge.stores.SQLiteStore` and expects the same paths in the same
order, with scores equal to ``rel=1e-6``: the in-memory store is the reference
the SQLite store is held to (BM25 as FTS5 computes it, exact cosine KNN).

``KNOWLEDGE_REQUIRE_SQLITE_VEC=1`` (the test pipeline sets it) turns the skip of
the sqlite-vec cases into a failure, so a green run means the extension served
the vectors and not the standard-library fallback.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from knowledge import errors, stores
from knowledge.ports import ChunkFilter, ChunkRecord, DocRecord, LexicalIndex, VectorStore
from knowledge.stores import (
    BACKEND_PYTHON,
    BACKEND_SQLITE_VEC,
    INDEX_FILE,
    SCHEMA_VERSION,
    InMemoryVectorStore,
    SQLiteStore,
    fts_match,
    fts_tokens,
    query_phrases,
    read_schema_version,
)

VEC_AVAILABLE = stores.probe_vector_backend() == BACKEND_SQLITE_VEC


def _require_vec() -> None:
    if VEC_AVAILABLE:
        return
    if stores.sqlite_vec_required():
        pytest.fail(f"{stores.REQUIRE_SQLITE_VEC_ENV}=1 and sqlite-vec does not load in this Python")
    pytest.skip("sqlite-vec does not load in this Python")


def _python_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(stores.REQUIRE_SQLITE_VEC_ENV, raising=False)
    monkeypatch.setattr(stores, "_load_sqlite_vec", lambda conn: False)


KINDS = ("memory", "sqlite")


def _open(kind: str, tmp_path: Path) -> Any:
    if kind == "memory":
        return InMemoryVectorStore()
    return SQLiteStore.open(tmp_path / f"index-{kind}")


@pytest.fixture(params=KINDS)
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Any]:
    if request.param == "sqlite":
        _require_vec()
    opened = _open(request.param, tmp_path)
    yield opened
    if isinstance(opened, SQLiteStore):
        opened.close()


@pytest.fixture()
def both(tmp_path: Path) -> Iterator[tuple[Any, Any]]:
    _require_vec()
    memory, sqlite = InMemoryVectorStore(), SQLiteStore.open(tmp_path / "parity")
    yield memory, sqlite
    sqlite.close()


def _doc(path: str, *, type: str = "", title: str | None = None, tags: tuple[str, ...] = (), body: str = "",
         service: str = "", status: str = "") -> DocRecord:
    return DocRecord(
        file_path=path, doc_id=f"id-{path}", title=title if title is not None else Path(path).stem, type=type,
        service=service, status=status, file_hash=f"h-{path}", tags=tags, body=body,
    )


def _chunks(path: str, *texts: str) -> list[ChunkRecord]:
    return [ChunkRecord(file_path=path, ord=n, text=text) for n, text in enumerate(texts)]


#: (document, chunk texts, vectors): four documents, one of them a memory.
CORPUS: list[tuple[DocRecord, tuple[str, ...], list[list[float]]]] = [
    (
        _doc("Services/Middleware.md", type="service", tags=("service/middleware", "gateway", "gateway"),
             body="The middleware gateway fronts auth.", service="middleware", status="active"),
        ("# Middleware\nThe middleware gateway fronts auth.", "## Outage\nWhen the middleware fails, page."),
        [[1.0, 0.0, 0.0], [0.8, 0.6, 0.0]],
    ),
    (
        _doc("Runbooks/Outage.md", type="runbook", tags=("incident",), body="Outage runbook for the gateway."),
        ("# Outage\nOutage runbook: restart the middleware gateway and check autenticación.",),
        [[0.6, 0.8, 0.0]],
    ),
    (
        _doc("90 - Memory/Note.md", type="decision", body="Remember the middleware."),
        ("Remember the middleware outage decision.",),
        [[0.9, 0.1, 0.0]],
    ),
    (
        _doc("Architecture/Auth.md", type="memory", tags=("service/middleware",), body="Auth flows."),
        ("# Auth\nOTP and session flows; the gateway is middleware.",),
        [[0.0, 0.0, 1.0]],
    ),
]


def _load(store: Any) -> Any:
    for doc, texts, vectors in CORPUS:
        store.replace_file(doc, _chunks(doc.file_path, *texts), vectors)
    return store


# --- the port --------------------------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
def test_each_store_implements_both_ports(kind: str, tmp_path: Path) -> None:
    if kind == "sqlite":
        _require_vec()
    opened = _open(kind, tmp_path)
    assert isinstance(opened, VectorStore)
    assert isinstance(opened, LexicalIndex)


def test_an_unknown_filter_field_is_refused() -> None:
    with pytest.raises(ValueError):
        ChunkFilter(equals={"owner": "x"})


# --- writes ---------------------------------------------------------------------------


def test_replace_file_counts_chunks_and_records_hashes(store: Any) -> None:
    _load(store)
    assert store.count() == 5
    assert store.file_hashes() == {doc.file_path: doc.file_hash for doc, _, _ in CORPUS}
    assert store.get_meta()["dim"] == "3"


def test_replacing_a_document_drops_its_old_chunks_everywhere(store: Any) -> None:
    _load(store)
    doc = CORPUS[0][0]
    store.replace_file(doc, _chunks(doc.file_path, "# Middleware\nNothing about failures here."), [[0.0, 1.0, 0.0]])

    assert store.count() == 4
    assert [c.text for c in store.chunks([doc.file_path])] == ["# Middleware\nNothing about failures here."]
    assert all(hit.file_path != doc.file_path for hit in store.lexical("page", 10))
    assert [h.ord for h in store.knn([0.0, 1.0, 0.0], 10, ChunkFilter(file_paths=(doc.file_path,)))] == [0]


def test_delete_files_and_clear(store: Any) -> None:
    _load(store)
    store.delete_files(["Runbooks/Outage.md", "Missing.md"])
    assert sorted(store.file_hashes()) == ["90 - Memory/Note.md", "Architecture/Auth.md", "Services/Middleware.md"]
    assert all(h.file_path != "Runbooks/Outage.md" for h in store.lexical("runbook", 10))
    assert all(h.file_path != "Runbooks/Outage.md" for h in store.knn([0.6, 0.8, 0.0], 10))

    store.clear()
    assert store.count() == 0
    assert store.documents() == []
    assert "dim" not in store.get_meta()
    # After clear a new dimension is accepted.
    store.replace_file(_doc("a.md"), _chunks("a.md", "text"), [[1.0, 0.0]])
    assert store.get_meta()["dim"] == "2"


def test_a_vector_of_another_dimension_is_refused(store: Any) -> None:
    _load(store)
    with pytest.raises(ValueError, match="dimension"):
        store.replace_file(_doc("x.md"), _chunks("x.md", "x"), [[1.0, 0.0]])
    with pytest.raises(ValueError, match="dimension"):
        store.knn([1.0, 0.0], 3)
    assert "x.md" not in store.file_hashes(), "a refused write leaves nothing behind"


def _observable(store: Any) -> Any:
    return (
        store.count(), store.documents(), sorted(store.file_hashes().items()),
        store.chunks([doc.file_path for doc, _, _ in CORPUS]), store.get_meta().get("dim"),
        store.knn([0.8, 0.6, 0.0], 10), store.knn([0.0, 0.0, 1.0], 2, ChunkFilter(exclude_types=("memory",))),
        store.lexical("middleware", 10), store.lexical("gateway outage", 2, ChunkFilter(tag="gateway")),
    )


@pytest.mark.parametrize("kind", KINDS)
def test_replace_files_stores_what_replace_file_stores_one_by_one(kind: str, tmp_path: Path) -> None:
    if kind == "sqlite":
        _require_vec()
    one_by_one, batched = _open(kind, tmp_path / "one"), _open(kind, tmp_path / "batch")
    try:
        _load(one_by_one)
        batched.replace_files([(doc, _chunks(doc.file_path, *texts), vectors) for doc, texts, vectors in CORPUS])
        assert _observable(batched) == _observable(one_by_one)
        # Replacing documents already stored, and a keyword-only one, in one batch.
        doc = CORPUS[0][0]
        items = [(doc, _chunks(doc.file_path, "# Middleware\nshorter now"), [[0.0, 1.0, 0.0]]),
                 (_doc("Plain.md"), _chunks("Plain.md", "keyword only text"), None),
                 (CORPUS[1][0], _chunks(CORPUS[1][0].file_path, "a", "b"), [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])]
        for item in items:
            one_by_one.replace_file(*item)
        batched.replace_files(items)
        assert _observable(batched) == _observable(one_by_one)
        batched.replace_files([])
        assert _observable(batched) == _observable(one_by_one)
    finally:
        for opened in (one_by_one, batched):
            if isinstance(opened, SQLiteStore):
                opened.close()


def test_a_failing_item_writes_nothing_of_its_batch(store: Any) -> None:
    _load(store)
    before = _observable(store)
    good = (_doc("New.md"), _chunks("New.md", "brand new middleware text"), [[0.0, 1.0, 0.0]])
    replaced = (CORPUS[0][0], _chunks(CORPUS[0][0].file_path, "changed"), [[0.0, 0.0, 1.0]])
    wrong_dim = (_doc("x.md"), _chunks("x.md", "x"), [[1.0, 0.0]])
    with pytest.raises(ValueError, match="dimension"):
        store.replace_files([good, replaced, wrong_dim])
    assert _observable(store) == before
    assert "New.md" not in store.file_hashes()


def test_a_failing_batch_rolls_back_in_sqlite(tmp_path: Path) -> None:
    _require_vec()
    store = _load(SQLiteStore.open(tmp_path))
    try:
        before = _observable(store)
        good = (_doc("New.md"), _chunks("New.md", "brand new"), [[0.0, 1.0, 0.0]])
        duplicate_ord = [ChunkRecord(file_path="Runbooks/Outage.md", ord=0, text="a"),
                         ChunkRecord(file_path="Runbooks/Outage.md", ord=0, text="b")]
        with pytest.raises(sqlite3.IntegrityError):
            store.replace_files([good, (CORPUS[1][0], duplicate_ord, [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])])
        assert _observable(store) == before
    finally:
        store.close()


def test_a_document_without_vectors_is_searchable_by_keywords_only(store: Any) -> None:
    store.replace_file(_doc("Plain.md"), _chunks("Plain.md", "keyword only text"), None)
    assert store.count() == 1
    assert store.knn([1.0], 5) == []
    assert [h.file_path for h in store.lexical("keyword", 5)] == ["Plain.md"]
    assert "dim" not in store.get_meta()


def test_two_documents_sharing_a_doc_id_are_both_stored(store: Any) -> None:
    for path in ("a/x.md", "b/x.md"):
        store.replace_file(
            DocRecord(file_path=path, doc_id="same", title="x", file_hash=path), _chunks(path, "x"), [[1.0]]
        )
    assert sorted(store.file_hashes()) == ["a/x.md", "b/x.md"]


def test_meta_round_trips(store: Any) -> None:
    store.set_meta({"provider": "openai", "model": "m"})
    store.set_meta({"model": "n"})
    meta = store.get_meta()
    assert (meta["provider"], meta["model"]) == ("openai", "n")


# --- reads ------------------------------------------------------------------------------

_FILTERS: list[ChunkFilter | None] = [
    None,
    ChunkFilter(equals={"type": "service"}),
    ChunkFilter(equals={"service": "middleware", "status": "active"}),
    ChunkFilter(tag="service/middleware"),
    ChunkFilter(tag="gateway"),
    ChunkFilter(exclude_types=("memory",)),
    ChunkFilter(exclude_prefixes=("90 - Memory",)),
    ChunkFilter(exclude_types=("memory",), exclude_prefixes=("90 - Memory",)),
    ChunkFilter(file_paths=("Runbooks/Outage.md", "Architecture/Auth.md")),
    ChunkFilter(file_paths=()),
    ChunkFilter(equals={"type": "service"}, tag="missing"),
]


@pytest.mark.parametrize("flt", _FILTERS, ids=[repr(f) for f in _FILTERS])
def test_documents_are_filtered_and_ordered_by_path(store: Any, flt: ChunkFilter | None) -> None:
    _load(store)
    expected = sorted((doc for doc, _, _ in CORPUS if flt is None or flt.admits(doc)), key=lambda d: d.file_path)
    assert store.documents(flt) == expected


def test_tags_keep_their_order_and_duplicates(store: Any) -> None:
    _load(store)
    (doc,) = store.documents(ChunkFilter(file_paths=("Services/Middleware.md",)))
    assert doc.tags == ("service/middleware", "gateway", "gateway")
    assert doc.metadata()["tags"] == ["service/middleware", "gateway", "gateway"]


def test_chunks_and_bodies_come_back_in_path_order(store: Any) -> None:
    _load(store)
    got = store.chunks(["Services/Middleware.md", "Runbooks/Outage.md"])
    assert [(c.file_path, c.ord) for c in got] == [
        ("Runbooks/Outage.md", 0), ("Services/Middleware.md", 0), ("Services/Middleware.md", 1)
    ]
    assert [path for path, _ in store.bodies()] == sorted(doc.file_path for doc, _, _ in CORPUS)


@pytest.mark.parametrize("flt", _FILTERS, ids=[repr(f) for f in _FILTERS])
def test_knn_agrees_between_stores(both: tuple[Any, Any], flt: ChunkFilter | None) -> None:
    memory, sqlite = (_load(s) for s in both)
    for k in (1, 2, 10):
        left, right = (s.knn([0.8, 0.6, 0.0], k, flt) for s in (memory, sqlite))
        assert [(h.file_path, h.ord) for h in left] == [(h.file_path, h.ord) for h in right]
        assert [h.score for h in right] == pytest.approx([h.score for h in left], rel=1e-6, abs=1e-6)


def test_knn_orders_by_similarity_then_path(store: Any) -> None:
    _load(store)
    hits = store.knn([0.8, 0.6, 0.0], 3)
    assert [(h.file_path, h.ord) for h in hits] == [
        ("Services/Middleware.md", 1), ("Runbooks/Outage.md", 0), ("90 - Memory/Note.md", 0)
    ]
    assert hits[0].score == pytest.approx(1.0, abs=1e-6)
    assert hits[0].own_text == hits[0].text


@pytest.mark.parametrize(
    "query", ["middleware", "middleware outage", "gateway runbook", "autenticacion", "otp", "nothing-matches", ""]
)
@pytest.mark.parametrize("flt", [None, ChunkFilter(exclude_types=("memory",), exclude_prefixes=("90 - Memory",))])
def test_bm25_agrees_between_stores(both: tuple[Any, Any], query: str, flt: ChunkFilter | None) -> None:
    memory, sqlite = (_load(s) for s in both)
    left, right = (s.lexical(query, 10, flt) for s in (memory, sqlite))
    assert [(h.file_path, h.ord) for h in left] == [(h.file_path, h.ord) for h in right]
    assert [h.score for h in right] == pytest.approx([h.score for h in left], rel=1e-6)


def test_diacritics_fold_both_ways(store: Any) -> None:
    _load(store)
    assert [h.file_path for h in store.lexical("autenticacion", 5)] == ["Runbooks/Outage.md"]
    assert [h.file_path for h in store.lexical("Autenticación", 5)] == ["Runbooks/Outage.md"]


def test_lexical_returns_each_files_best_chunk(store: Any) -> None:
    _load(store)
    hits = store.lexical("page outage", 10)
    by_path = {h.file_path: h for h in hits}
    assert by_path["Services/Middleware.md"].ord == 1
    assert [h.score for h in hits] == sorted((h.score for h in hits), reverse=True)


def test_query_phrases_quote_tokens_and_prefix_long_ones() -> None:
    assert fts_tokens("Autenticación del_usuario OTP-2") == ["autenticacion", "del", "usuario", "otp", "2"]
    assert query_phrases("otp session OTP") == [("otp", False), ("session", True)]
    assert fts_match(query_phrases('middleware "x" otp')) == '"middleware"* OR "x" OR "otp"'


# --- the SQLite file --------------------------------------------------------------------


def test_the_schema_is_versioned_in_the_file(tmp_path: Path) -> None:
    _require_vec()
    store = SQLiteStore.open(tmp_path)
    store.close()
    assert read_schema_version(tmp_path / INDEX_FILE) == SCHEMA_VERSION
    assert read_schema_version(tmp_path / "absent.sqlite") is None
    assert store.get_meta()["index_version"] == str(SCHEMA_VERSION)


def test_a_newer_layout_is_refused_with_a_code(tmp_path: Path) -> None:
    path = tmp_path / INDEX_FILE
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.close()
    with pytest.raises(errors.KnowledgeConfigError) as exc_info:
        SQLiteStore(path)
    assert exc_info.value.code == errors.E_INDEX_VERSION_NEWER


def test_reopening_keeps_everything(tmp_path: Path) -> None:
    _require_vec()
    first = _load(SQLiteStore.open(tmp_path))
    expected = (first.documents(), first.knn([0.8, 0.6, 0.0], 3), first.lexical("middleware", 3))
    first.close()
    second = SQLiteStore.open(tmp_path)
    assert (second.documents(), second.knn([0.8, 0.6, 0.0], 3), second.lexical("middleware", 3)) == expected
    second.close()


def test_the_python_backend_ranks_like_sqlite_vec(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _require_vec()
    vec = _load(SQLiteStore.open(tmp_path / "vec"))
    vec.count()  # opens this thread's reader before the loader is replaced
    _python_only(monkeypatch)
    plain = _load(SQLiteStore.open(tmp_path / "plain"))
    assert (vec.vector_backend, plain.vector_backend) == (BACKEND_SQLITE_VEC, BACKEND_PYTHON)
    flt = ChunkFilter(exclude_types=("memory",))
    left, right = vec.knn([0.8, 0.6, 0.0], 4, flt), plain.knn([0.8, 0.6, 0.0], 4, flt)
    assert [(h.file_path, h.ord) for h in left] == [(h.file_path, h.ord) for h in right]
    assert [h.score for h in right] == pytest.approx([h.score for h in left], abs=1e-6)
    vec.close()
    plain.close()


def test_an_index_written_without_sqlite_vec_is_mirrored_when_it_loads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _require_vec()
    real_loader = stores._load_sqlite_vec
    _python_only(monkeypatch)
    plain = _load(SQLiteStore.open(tmp_path))
    plain.delete_files(["Runbooks/Outage.md"])
    expected = plain.knn([0.8, 0.6, 0.0], 10)
    plain.close()

    monkeypatch.setattr(stores, "_load_sqlite_vec", real_loader)
    vec = SQLiteStore.open(tmp_path)
    assert vec.vector_backend == BACKEND_SQLITE_VEC
    assert "vec_dirty" not in vec.get_meta()
    got = vec.knn([0.8, 0.6, 0.0], 10)
    assert [(h.file_path, h.ord) for h in got] == [(h.file_path, h.ord) for h in expected]
    vec.close()


_WRITE_WITHOUT_SQLITE_VEC = """
import sys
from pathlib import Path
from knowledge.ports import ChunkRecord, DocRecord
from knowledge.stores import SQLiteStore

store = SQLiteStore.open(Path(sys.argv[1]))
assert store.vector_backend == "python", store.vector_backend
doc = DocRecord(file_path="Notes/New.md", doc_id="new", title="New", type="note", file_hash="h", body="new")
store.replace_file(doc, [ChunkRecord(file_path=doc.file_path, ord=0, text="new")], [[0.0, 0.6, 0.8]])
store.close()
"""


def test_vectors_written_by_a_process_without_sqlite_vec_are_found_by_an_open_store(tmp_path: Path) -> None:
    """A store already open with sqlite-vec sees what another process wrote without it."""
    _require_vec()
    store = _load(SQLiteStore.open(tmp_path / "index"))
    before = store.knn([0.0, 0.6, 0.8], 3)
    stub = tmp_path / "stub"
    stub.mkdir()
    (stub / "sqlite_vec.py").write_text('raise ImportError("sqlite-vec is not installed here")\n')
    env = {k: v for k, v in os.environ.items() if k != stores.REQUIRE_SQLITE_VEC_ENV}
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(stub), env.get("PYTHONPATH", "")]))

    subprocess.run(
        [sys.executable, "-c", _WRITE_WITHOUT_SQLITE_VEC, str(tmp_path / "index")], env=env, check=True, timeout=60
    )

    got = store.knn([0.0, 0.6, 0.8], 3)
    assert "Notes/New.md" not in [h.file_path for h in before]
    assert got[0].file_path == "Notes/New.md"
    expected = store._knn_python(store._reader(), [0.0, 0.6, 0.8], 3, None)
    assert [(h.file_path, h.ord) for h in got] == [(h.file_path, h.ord) for h in expected]
    assert "vec_dirty" not in store.get_meta()
    store.close()


def test_a_closed_store_reopens_on_its_next_use(tmp_path: Path) -> None:
    _require_vec()
    store = _load(SQLiteStore.open(tmp_path))
    expected = store.knn([0.8, 0.6, 0.0], 3)
    store.close()

    assert store.knn([0.8, 0.6, 0.0], 3) == expected
    store.delete_files(["Runbooks/Outage.md"])
    assert "Runbooks/Outage.md" not in store.file_hashes()
    store.close()


def test_knn_falls_back_when_a_rebuild_drops_the_vec_table_mid_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _require_vec()
    store = _load(SQLiteStore.open(tmp_path))
    expected = store.knn([0.8, 0.6, 0.0], 3)

    def dropped(*_args: Any) -> None:
        raise sqlite3.OperationalError("no such table: chunks_vec")

    monkeypatch.setattr(store, "_knn_vec", dropped)
    got = store.knn([0.8, 0.6, 0.0], 3)
    assert [(h.file_path, h.ord) for h in got] == [(h.file_path, h.ord) for h in expected]
    assert [h.score for h in got] == pytest.approx([h.score for h in expected], abs=1e-6)
    store.close()


def test_cosine_similarity_is_exact_and_rejects_mismatched_dimensions() -> None:
    assert stores.cosine_similarity([1.0, 0.0], [0.6, 0.8]) == pytest.approx(0.6)
    assert stores.cosine_similarity([0.0, 0.0], [0.6, 0.8]) == 0.0
    with pytest.raises(ValueError):
        stores.cosine_similarity([1.0, 0.0], [1.0, 0.0, 0.0])


def test_the_required_extension_turns_the_fallback_into_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(stores, "_load_sqlite_vec", lambda conn: False)
    monkeypatch.setenv(stores.REQUIRE_SQLITE_VEC_ENV, "1")
    with pytest.raises(errors.KnowledgeConfigError) as exc_info:
        SQLiteStore.open(tmp_path)
    assert exc_info.value.code == errors.E_SQLITE_VEC_UNAVAILABLE


def test_the_pipeline_serves_vectors_with_sqlite_vec() -> None:
    """With ``KNOWLEDGE_REQUIRE_SQLITE_VEC=1`` the suite proves the extension is the one serving."""
    if not stores.sqlite_vec_required():
        pytest.skip(f"{stores.REQUIRE_SQLITE_VEC_ENV} is not 1")
    assert stores.probe_vector_backend() == BACKEND_SQLITE_VEC


def test_a_failed_write_rolls_back(tmp_path: Path) -> None:
    _require_vec()
    store = _load(SQLiteStore.open(tmp_path))
    before = (store.count(), store.documents())
    bad = [ChunkRecord(file_path="Runbooks/Outage.md", ord=0, text="a"),
           ChunkRecord(file_path="Runbooks/Outage.md", ord=0, text="b")]
    with pytest.raises(sqlite3.IntegrityError):
        store.replace_file(CORPUS[1][0], bad, [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])
    assert (store.count(), store.documents()) == before
    assert [h.file_path for h in store.lexical("restart", 5)] == ["Runbooks/Outage.md"]
    store.close()


def test_readers_never_see_a_document_half_written(tmp_path: Path) -> None:
    """Rewrites on one thread, reads on others: every read sees whole documents."""
    _require_vec()
    store = _load(SQLiteStore.open(tmp_path))
    doc, texts, vectors = CORPUS[0]
    stop = threading.Event()
    problems: list[str] = []

    def write() -> None:
        for n in range(60):
            store.replace_file(doc, _chunks(doc.file_path, *texts), vectors if n % 2 else vectors[::-1])
        stop.set()

    def read(fn: Callable[[], Any]) -> None:
        while not stop.is_set():
            try:
                got = fn()
            except Exception as exc:  # noqa: BLE001 - any failure is the finding
                problems.append(repr(exc))
                return
            ords = sorted(c.ord for c in store.chunks([doc.file_path]))
            if ords not in ([0, 1], []):
                problems.append(f"chunks {ords}")
            del got

    readers = [
        threading.Thread(target=read, args=(lambda: store.knn([0.8, 0.6, 0.0], 5),)),
        threading.Thread(target=read, args=(lambda: store.lexical("middleware", 5),)),
        threading.Thread(target=read, args=(lambda: store.documents(ChunkFilter(tag="gateway")),)),
    ]
    writer = threading.Thread(target=write)
    for thread in (*readers, writer):
        thread.start()
    for thread in (writer, *readers):
        thread.join(timeout=60)
    store.close()
    assert problems == []


def test_two_stores_on_one_file_create_the_schema_once(tmp_path: Path) -> None:
    _require_vec()
    opened: list[SQLiteStore] = []
    failures: list[str] = []

    def open_one() -> None:
        try:
            opened.append(SQLiteStore.open(tmp_path))
        except Exception as exc:  # noqa: BLE001
            failures.append(repr(exc))

    threads = [threading.Thread(target=open_one) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    for store in opened:
        store.close()
    assert failures == []
    assert read_schema_version(tmp_path / INDEX_FILE) == SCHEMA_VERSION
