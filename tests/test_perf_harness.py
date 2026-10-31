"""The performance test helpers: ``tests/synthetic_vault.py`` and ``tests/perf_counters.py``.

Both are shared by the budget tests of later work, so they are tested here on
their own: the generator is byte-deterministic and writes a vault the linter and
the indexer accept, and the counters give exact numbers (no clock) that agree
with an independent count. Only ``tests/fixtures/**`` and vaults generated into
``tmp_path`` are used; embeddings come from the ``bag-of-words`` provider.
"""

from __future__ import annotations

import re
import sqlite3
import threading
import unicodedata
from collections.abc import Iterator
from pathlib import Path

import frontmatter
import pytest
import yaml
from perf_counters import (
    count_calls,
    file_opens,
    modules_after_cli,
    normalize_sql,
    sql_trace,
    yaml_parses,
)
from synthetic_vault import (
    SKIPPED_MARKER,
    Manifest,
    Shape,
    generate,
    make_vocabulary,
    max_vocab,
    measure_selectivity,
    queries,
    tree_sha256,
)

from knowledge import chunker, stores, vault_lint
from knowledge.embeddings import build_embedder
from knowledge.indexer import VaultIndexer
from knowledge.ports import ChunkRecord, DocRecord
from knowledge.stores import SQLiteStore, fts_match, query_phrases
from knowledge.vault_source import DirectoryVaultSource

DOCS = 60


def _need_vec() -> None:
    if stores.probe_vector_backend() != stores.BACKEND_SQLITE_VEC and stores.sqlite_vec_required():
        pytest.fail("KNOWLEDGE_REQUIRE_SQLITE_VEC=1 and sqlite-vec does not load in this Python")


@pytest.fixture(scope="module")
def manifest(tmp_path_factory: pytest.TempPathFactory) -> Manifest:
    return generate(tmp_path_factory.mktemp("synthetic") / "vault", docs=DOCS, seed=7)


@pytest.fixture(scope="module")
def indexed(tmp_path_factory: pytest.TempPathFactory, manifest: Manifest) -> Iterator[VaultIndexer]:
    _need_vec()
    index_dir = tmp_path_factory.mktemp("synthetic-index")
    indexer = VaultIndexer.open(
        vault_path=manifest.root, index_dir=index_dir, embedder=build_embedder("bag-of-words", model=None, api_key=None)
    )
    indexer.index()
    yield indexer
    indexer.store.close()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# synthetic_vault
# ---------------------------------------------------------------------------


def test_same_seed_gives_the_same_bytes_and_another_seed_a_different_vault(tmp_path: Path) -> None:
    first = generate(tmp_path / "a", docs=25, seed=3)
    again = generate(tmp_path / "b", docs=25, seed=3)
    other = generate(tmp_path / "c", docs=25, seed=4)
    assert first.tree_sha256 == again.tree_sha256 == tree_sha256(tmp_path / "a") == tree_sha256(tmp_path / "b")
    assert first.tree_sha256 != other.tree_sha256
    assert first.paths == again.paths
    assert first.doc_freq == again.doc_freq
    assert queries(first, seed=1, n=5) == queries(again, seed=1, n=5)
    assert first.total_bytes == sum(p.stat().st_size for p in (tmp_path / "a").rglob("*") if p.is_file())


def test_growing_a_vault_keeps_the_names_and_types_of_the_documents_it_had(tmp_path: Path) -> None:
    small = generate(tmp_path / "small", docs=20, seed=5)
    large = generate(tmp_path / "large", docs=30, seed=5)
    assert set(small.paths) <= set(large.paths)
    assert small.specials == large.specials
    assert small.vocabulary == large.vocabulary


def test_the_vault_has_the_requested_documents_and_passes_the_linter(manifest: Manifest) -> None:
    assert len(manifest.paths) == DOCS == manifest.docs
    assert DirectoryVaultSource(manifest.root).markdown_files() == [manifest.root / p for p in sorted(manifest.paths)]
    report = vault_lint.lint(manifest.root, rules=vault_lint.RULES_1_3)
    assert report.errors == []
    assert report.warnings == []


def test_the_guaranteed_cases_exist(manifest: Manifest) -> None:
    root = manifest.root
    specials = manifest.specials
    assert set(specials) == {"empty", "accented", "memory", "multichunk"}

    empty = frontmatter.load(str(root / specials["empty"]))
    assert empty.content.strip() == ""

    accented = specials["accented"]
    assert " " in accented and any(ord(ch) > 127 for ch in accented)
    assert unicodedata.normalize("NFC", accented) == accented

    memory = specials["memory"]
    assert memory.startswith("90 - Memory/")
    assert frontmatter.load(str(root / memory)).metadata["type"] == "memory"

    body = frontmatter.load(str(root / specials["multichunk"])).content
    assert len(chunker.sized_chunks(body, 2500, 0.1)) >= 3

    # About half of the ordinary documents have more than one chunk.
    multi = sum(len(chunker.sized_chunks(frontmatter.load(str(root / p)).content, 2500, 0.1)) > 1 for p in manifest.paths)
    assert 0.25 * DOCS < multi < 0.95 * DOCS


def test_dot_directories_hold_files_the_indexer_must_skip(manifest: Manifest, indexed: VaultIndexer) -> None:
    assert manifest.skipped
    for rel in manifest.skipped:
        assert (manifest.root / rel).is_file()
        assert SKIPPED_MARKER in (manifest.root / rel).read_text(encoding="utf-8")
    walked = {p.relative_to(manifest.root).as_posix() for p in DirectoryVaultSource(manifest.root).markdown_files()}
    assert walked.isdisjoint(manifest.skipped)
    assert indexed.store.lexical(SKIPPED_MARKER, 10) == []  # type: ignore[attr-defined]


def test_wikilinks_come_by_name_path_alias_and_heading(manifest: Manifest) -> None:
    text = "\n".join((manifest.root / p).read_text(encoding="utf-8") for p in manifest.paths)
    links = re.findall(r"\[\[([^\]]+)\]\]", text)
    assert len(links) == manifest.link_count > DOCS
    assert any("/" in link and "|" not in link and "#" not in link for link in links), "by path"
    assert any("|" in link for link in links), "with an alias"
    assert any("#" in link for link in links), "with a heading"
    assert any("/" not in link and "|" not in link and "#" not in link for link in links), "by name"


def test_frontmatter_matches_the_fixture_vault(manifest: Manifest) -> None:
    runbook = manifest.by_type["runbook"][0]
    meta = frontmatter.load(str(manifest.root / runbook)).metadata
    assert {"title", "type", "service", "tags", "status", "last_updated", "id"} <= set(meta)
    assert meta["type"] == "runbook"
    fixture = frontmatter.load("tests/fixtures/vault/Runbooks/Runbook - Middleware Outage.md").metadata
    assert {"title", "type", "service", "tags", "status"} <= set(fixture)


def test_vocabulary_words_never_share_a_four_letter_stem() -> None:
    words = make_vocabulary(1, max_vocab())
    assert len(words) == max_vocab()
    assert len({w[:4] for w in words}) == len(words)
    assert make_vocabulary(1, 50) == words[:50]
    assert make_vocabulary(2, 50) != words[:50]


@pytest.mark.parametrize("target", [0.05, 0.25])
def test_queries_hit_the_requested_document_frequency(manifest: Manifest, target: float) -> None:
    batch = queries(manifest, seed=1, n=30, selectivity=target, terms=1)
    assert len(batch) == 30
    assert len(set(batch)) > 1
    for word in batch:
        assert target * 0.7 <= manifest.fraction(word) <= target * 1.4


def test_the_selectivity_knob_separates_selective_from_zipf_queries(manifest: Manifest) -> None:
    selective = queries(manifest, seed=2, n=40, selectivity=0.05, terms=1)
    unselective = queries(manifest, seed=2, n=40, selectivity=None, terms=1)
    mean = lambda words: sum(manifest.fraction(w) for w in words) / len(words)  # noqa: E731
    assert mean(selective) < 0.1 < 0.4 < mean(unselective)
    assert all(len(q.split()) == 3 for q in queries(manifest, seed=2, n=5, selectivity=0.1, terms=3))
    with pytest.raises(ValueError):
        queries(manifest, seed=2, n=1, selectivity=0.0)


def test_the_store_matches_exactly_the_documents_the_manifest_predicts(manifest: Manifest, indexed: VaultIndexer) -> None:
    store = indexed.store
    words = queries(manifest, seed=3, n=12, selectivity=0.15, terms=1)
    measured = measure_selectivity(store, words, documents=DOCS)  # type: ignore[arg-type]
    assert measured == [manifest.fraction(w) for w in words]
    mixed = queries(manifest, seed=3, n=1, selectivity=None, terms=2)[0]
    union = measure_selectivity(store, [mixed], documents=DOCS)[0]  # type: ignore[arg-type]
    assert union >= max(manifest.fraction(w) for w in mixed.split())


def test_shape_from_export_calibrates_the_generator(tmp_path: Path) -> None:
    export = {
        "shape_version": 1,
        "docs": 400,
        "body_chars": {"p10": 800, "p50": 1600, "p90": 6400, "p99": 9000, "max": 12000},
        "chunks_per_doc": {"p50": 1},
        "sections_per_doc": {"p10": 2, "p50": 3, "p90": 5},
        "links_per_doc": {"p10": 0, "p50": 2, "p90": 4},
        "type_counts": {"service": 3, "runbook": 1, "note": 6},
        "query_term_df": {"p50": 0.03, "p90": 0.2, "p99": 0.6},
    }
    shape = Shape.from_export(export)
    assert shape.body_median_chars == 1600 and shape.body_max_chars == 12000
    assert shape.sections == (2, 5) and shape.links == (0, 4)
    assert dict(shape.type_weights) == {"service": 3.0, "runbook": 1.0, "note": 6.0}
    assert shape.query_selectivity == 0.03
    calibrated = generate(tmp_path / "v", docs=40, seed=1, shape=shape)
    assert set(calibrated.by_type) <= {"service", "runbook", "note"} | {"memory", "reference", "architecture"}
    assert all(q for q in queries(calibrated, seed=1, n=3))  # defaults to the exported selectivity
    assert Shape.from_export({**export, "query_term_df": {"p50": 12}}).query_selectivity == 12 / 400
    with pytest.raises(ValueError):
        Shape.from_export({**export, "shape_version": 2})


def test_the_generator_refuses_nonsense(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        generate(tmp_path / "v", docs=0, seed=1)
    with pytest.raises(ValueError):
        Shape(vocab=0)
    with pytest.raises(ValueError):
        Shape(sections=(0, 3))


# ---------------------------------------------------------------------------
# perf_counters: SQL
# ---------------------------------------------------------------------------


def test_normalize_sql_keeps_the_shape_and_drops_the_values() -> None:
    first = normalize_sql("SELECT  a\n FROM t WHERE k = 'x''y' AND n = 12 AND id IN (1, 2, 3) AND b = x'00ff'")
    second = normalize_sql("SELECT a FROM t WHERE k = 'z' AND n = 3.5 AND id IN (7, 8) AND b = x'aa'")
    assert first == second == "SELECT a FROM t WHERE k = ? AND n = ? AND id IN (?...) AND b = ?"
    assert normalize_sql("SELECT c1 FROM t2") == "SELECT c1 FROM t2"


def test_a_lexical_search_is_one_statement_and_its_rows_are_counted(manifest: Manifest, indexed: VaultIndexer) -> None:
    store: SQLiteStore = indexed.store  # type: ignore[assignment]
    word = queries(manifest, seed=4, n=1, selectivity=0.2, terms=1)[0]
    with sql_trace(store) as trace:
        hits = store.lexical(word, 100)
    assert len(trace.statements) == 1 and trace.selects == 1 and trace.writes == 0
    assert "bm25(chunks_fts" in trace.statements[0]
    assert trace.internal, "FTS5 shadow-table reads are reported apart"
    assert all(not s.lstrip().startswith("--") for s in trace.statements)
    independent = sqlite3.connect(f"file:{store.path}?mode=ro", uri=True)
    try:
        (matching,) = independent.execute(
            "SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH ?", [fts_match(query_phrases(word))]
        ).fetchone()
    finally:
        independent.close()
    assert trace.rows_fetched == matching >= len(hits) > 0
    assert trace.rows_with_column("file_path") == matching


def test_column_reads_are_visible(indexed: VaultIndexer) -> None:
    store: SQLiteStore = indexed.store  # type: ignore[assignment]
    with sql_trace(indexed) as trace:
        hashes = store.file_hashes()
    assert len(hashes) == DOCS and trace.selects_column("docs", "body") == 0
    assert trace.rows_with_column("file_hash") == DOCS
    with sql_trace(store) as trace:
        store.documents()
    assert trace.selects_column("docs", "body") == 1
    assert trace.rows_with_column("body") == DOCS
    assert trace.shapes()["SELECT file_path, file_hash FROM docs"] == 0


def test_writes_are_counted_once_not_once_per_trigger(tmp_path: Path) -> None:
    _need_vec()
    store = SQLiteStore.open(tmp_path / "idx")
    try:
        doc = DocRecord(file_path="a.md", doc_id="id-a", title="A", type="note", file_hash="h", body="x")
        chunk_rows = [ChunkRecord(file_path="a.md", ord=n, text=f"alpha beta {n}") for n in range(4)]
        vectors = [[1.0, 0.0, float(n)] for n in range(4)]
        with sql_trace(store) as trace:
            store.replace_file(doc, chunk_rows, vectors)
        assert trace.count(r"^INSERT INTO chunks\(") == 4
        assert trace.count(r"^INSERT INTO chunk_vectors\(") == 4
        assert trace.count(r"^INSERT INTO docs\(") == 1
        assert trace.collapsed >= 4  # the trigger firings that were not counted
        assert trace.commits == 1 and trace.count(r"^BEGIN IMMEDIATE") == 1
        assert trace.writes > trace.selects > 0
    finally:
        store.close()


def test_the_trace_follows_reading_threads_and_stops_with_the_block(tmp_path: Path) -> None:
    store = SQLiteStore.open(tmp_path / "idx")
    try:
        with sql_trace(store) as trace:
            worker = threading.Thread(target=store.count, name="reader-1")
            worker.start()
            worker.join()
            store.count()
        assert trace.count(r"^SELECT count\(\*\) FROM chunks") == 2
        store.count()
        assert trace.count(r"^SELECT count\(\*\) FROM chunks") == 2
        store.observe()  # idempotent
        store.close()
        with sql_trace(store) as again:  # reopened connections are observed too
            store.count()
        assert again.count(r"^SELECT count\(\*\) FROM chunks") == 1
    finally:
        store.close()


def test_observing_does_not_change_what_the_store_returns(manifest: Manifest, indexed: VaultIndexer) -> None:
    store: SQLiteStore = indexed.store  # type: ignore[assignment]
    word = queries(manifest, seed=5, n=1, selectivity=0.3, terms=1)[0]
    plain = store.lexical(word, 50)
    with sql_trace(store):
        observed = store.lexical(word, 50)
        rows = store.file_hashes()
    assert observed == plain
    assert rows == store.file_hashes()
    assert isinstance(next(iter(rows.items())), tuple)


def test_sql_trace_refuses_a_store_without_connections() -> None:
    with pytest.raises(TypeError):
        with sql_trace(stores.InMemoryVectorStore()):
            pass


# ---------------------------------------------------------------------------
# perf_counters: files, calls, imports
# ---------------------------------------------------------------------------


def test_file_opens_counts_reads_and_writes_by_thread(tmp_path: Path) -> None:
    inside = tmp_path / "root"
    inside.mkdir()
    (inside / "a.md").write_text("a", encoding="utf-8")
    (inside / "b.txt").write_text("b", encoding="utf-8")
    outside = tmp_path / "outside.md"
    outside.write_text("o", encoding="utf-8")

    def work() -> None:
        (inside / "a.md").read_bytes()
        (inside / "c.md").write_text("c", encoding="utf-8")

    with file_opens(inside) as opens:
        (inside / "a.md").read_text(encoding="utf-8")
        (inside / "b.txt").read_bytes()
        outside.read_text(encoding="utf-8")
        thread = threading.Thread(target=work, name="worker-9")
        thread.start()
        thread.join()
    assert opens.reads("*.md") == 2 and opens.reads("a.md") == 2 and opens.reads("*") == 3
    assert opens.writes("*") == ["worker-9"] and opens.writes("*.txt") == []
    assert opens.paths("w") == ["c.md"]
    (inside / "a.md").read_text(encoding="utf-8")
    assert opens.reads("*") == 3, "nothing is recorded after the block"


def test_file_opens_nest_and_see_a_walk_of_the_vault(manifest: Manifest) -> None:
    with file_opens(manifest.root) as outer:
        source = DirectoryVaultSource(manifest.root)
        with file_opens(manifest.root / "Runbooks") as inner:
            files = source.markdown_files()
            frontmatter.load(str(manifest.root / manifest.by_type["runbook"][0]))
        assert len(files) == DOCS
    assert outer.reads("*.md") == 1 and inner.reads("*.md") == 1
    assert outer.reads(manifest.by_type["runbook"][0]) == 1


def test_count_calls_counts_without_wrapping() -> None:
    class Box:
        def method(self) -> int:
            return 1

    def plain(x: int) -> int:
        return x

    code_before = (plain.__code__, Box.method.__code__)
    box = Box()
    with count_calls(plain, box.method) as calls:
        plain(1)
        plain(2)
        box.method()
        worker = threading.Thread(target=plain, args=(3,), name="w1")
        worker.start()
        worker.join()
    assert calls.count() == 4 and calls.count("plain") == 3 and calls.count("Box.method") == 1
    assert calls.threads("plain") == {threading.current_thread().name, "w1"}
    plain(4)
    assert calls.count("plain") == 3
    assert (plain.__code__, Box.method.__code__) == code_before and Box.method(box) == 1


def test_count_calls_rejects_what_it_cannot_watch() -> None:
    with pytest.raises(TypeError):
        with count_calls(len):
            pass


def test_count_calls_blocks_can_nest() -> None:
    def one() -> None: ...

    def two() -> None: ...

    with count_calls(one) as outer, count_calls(two) as inner:
        one()
        two()
        two()
    assert outer.count() == 1 and inner.count() == 2


def test_yaml_parses_counts_documents_loaded(manifest: Manifest) -> None:
    paths = [str(manifest.root / p) for p in manifest.paths[:5]]
    with yaml_parses() as parses:
        for path in paths:
            frontmatter.load(path)
        yaml.safe_load("a: 1")
        yaml.load("b: 2", Loader=yaml.SafeLoader)
    assert parses.count() == 5 + 2


def test_a_full_index_reads_and_parses_every_document(manifest: Manifest, tmp_path: Path) -> None:
    _need_vec()
    indexer = VaultIndexer.open(
        vault_path=manifest.root, index_dir=tmp_path / "idx", embedder=build_embedder("bag-of-words", model=None, api_key=None)
    )
    try:
        with file_opens(manifest.root) as opens, yaml_parses() as parses, sql_trace(indexer) as trace:
            stats = indexer.index()
        assert stats["new"] == DOCS
        assert opens.reads("*.md") >= DOCS
        assert parses.count() == DOCS  # one frontmatter parse per new document
        assert not opens.writes("*")
        assert trace.count(r"^INSERT INTO docs\(") == DOCS
        with sql_trace(indexer) as noop:
            assert indexer.index()["unchanged"] == DOCS
        assert noop.count(r"^INSERT INTO (?:docs|chunks)\(") == 0 and noop.count(r"^DELETE\b") == 0
    finally:
        indexer.store.close()  # type: ignore[attr-defined]


def test_modules_after_cli_reports_the_imports_of_a_clean_process() -> None:
    modules = modules_after_cli(["--version"])
    assert "knowledge.cli" in modules and "json" in modules
    assert "knowledge.confluence_sync" not in modules
