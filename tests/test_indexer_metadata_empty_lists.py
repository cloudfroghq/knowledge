"""What the indexer writes for a document, through the store port.

A document without ``tags`` is indexed with no tags (no special case: a
document simply has no ``doc_tags`` rows), a tagged one stays filterable by
any of its tags, in frontmatter order. A document whose chunks cannot all be
embedded is not written at all, so the store never holds a document without
its vectors and the keyword ``idf`` counts only indexed files. Learned link
weights saved before an index existed survive the first index run.

Every case builds a throwaway vault and index directory under ``tmp_path``;
none reads the real ``vault/``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from knowledge.embeddings import BagOfWordsEmbeddings
from knowledge.graph import RESOLVER_VERSION
from knowledge.indexer import VaultIndexer, get_skipped_files
from knowledge.ports import ChunkFilter
from knowledge.stores import INDEX_FILE

FIXTURE_VAULT = Path(__file__).parent / "fixtures" / "vault"


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


@pytest.fixture()
def isolated_indexer(tmp_path: Path) -> VaultIndexer:
    """A VaultIndexer over a disposable vault and SQLite index directory."""
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    return VaultIndexer.open(vault_path=vault_dir, index_dir=tmp_path / "index", embedder=BagOfWordsEmbeddings())


def test_document_without_tags_is_still_indexed(isolated_indexer: VaultIndexer) -> None:
    _write(
        isolated_indexer.vault_path / "No Tags.md",
        "---\ntitle: No Tags\ntype: note\n---\n\n# No Tags\n\nThis document has no tags in its frontmatter.\n",
    )

    stats = isolated_indexer.index(force=True, target="vault")

    assert stats["chunks"] == 1
    assert get_skipped_files() == []
    (doc,) = isolated_indexer.store.documents(ChunkFilter(file_paths=("No Tags.md",)))
    assert doc.tags == ()
    assert (doc.title, doc.type) == ("No Tags", "note")
    assert (isolated_indexer.vault_path.parent / "index" / INDEX_FILE).is_file()


def test_document_with_tags_is_indexed_and_filterable(isolated_indexer: VaultIndexer) -> None:
    _write(
        isolated_indexer.vault_path / "Tagged.md",
        "---\ntitle: Tagged\ntype: runbook\ntags:\n  - service/middleware\n  - type/runbook\n---\n\n# Tagged\n\n"
        "This document has tags and must remain filterable by them.\n",
    )

    stats = isolated_indexer.index(force=True, target="vault")

    assert stats["chunks"] == 1
    assert get_skipped_files() == []
    found = isolated_indexer.store.documents(ChunkFilter(tag="service/middleware"))
    assert [doc.file_path for doc in found] == ["Tagged.md"]
    assert found[0].tags == ("service/middleware", "type/runbook")


class _FailingOn(BagOfWordsEmbeddings):
    """Bag of words that refuses any text containing ``marker``."""

    def __init__(self, marker: str) -> None:
        super().__init__()
        self.marker = marker

    def embed(self, texts: list[str]) -> list[list[float]]:
        if any(self.marker in text for text in texts):
            raise RuntimeError("input rejected")
        return super().embed(texts)


def test_a_document_that_fails_to_embed_is_not_written(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _write(vault / "Good.md", "# Good\nmiddleware gateway\n")
    _write(vault / "Bad.md", "# Bad\nmiddleware BOOM\n")
    indexer = VaultIndexer.open(vault_path=vault, index_dir=tmp_path / "index", embedder=_FailingOn("BOOM"))

    indexer.index()

    assert sorted(indexer.store.file_hashes()) == ["Good.md"]
    assert [(s["file_path"], s["reason"]) for s in indexer.skipped_files] == [("Bad.md", "input rejected")]
    # The selector's idf counts indexed files only: N = 1, and "middleware" is in it.
    assert indexer.keyword_index.doc_count == 1
    assert indexer.keyword_index.idf("middleware") == pytest.approx(0.0)


def test_a_document_that_starts_failing_leaves_the_index(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _write(vault / "Doc.md", "# Doc\nmiddleware\n")
    indexer = VaultIndexer.open(vault_path=vault, index_dir=tmp_path / "index", embedder=_FailingOn("BOOM"))
    indexer.index()
    _write(vault / "Doc.md", "# Doc\nmiddleware BOOM\n")

    indexer.index()

    assert indexer.store.file_hashes() == {}
    assert indexer.store.count() == 0


def test_learned_weights_saved_before_the_index_survive_its_first_run(tmp_path: Path) -> None:
    """A ``link_graph.json`` carrying only weights (what ``migrate-index`` leaves) keeps them."""
    index_dir = tmp_path / "index"
    index_dir.mkdir()
    pair = "Services/Lookup Service.md||Services/Middleware Service.md"
    (index_dir / "link_graph.json").write_text(json.dumps({"weights": {pair: 1.5}}))
    indexer = VaultIndexer.open(vault_path=FIXTURE_VAULT, index_dir=index_dir, embedder=BagOfWordsEmbeddings())

    assert not indexer.is_indexed()
    indexer.index()

    assert indexer.graph.weight("Services/Middleware Service.md", "Services/Lookup Service.md") == 1.5
    saved = json.loads((index_dir / "link_graph.json").read_text())
    assert saved["weights"] == {pair: 1.5}
    assert saved["resolver"] == RESOLVER_VERSION
