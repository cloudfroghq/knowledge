"""A store error inside a write batch: what ``reindex`` reports and what the next run does.

Since 1.3 the indexer writes each embedding batch (32 chunks or more) with one
``replace_files`` call, one transaction in :class:`SQLiteStore`. A store error
on any document of the batch rolls back every document of it, where one
transaction per document kept the earlier ones. These tests pin the error
path end to end, through the ``reindex`` tool:

* the call fails with the store's error, and no file is reported as indexed:
  no stats, no ``skipped_files.json``, nothing in ``skipped_files``;
* the store keeps the earlier batches and nothing of the failing one: a new
  document is absent, an edited one keeps its previous version;
* the next ``reindex`` writes every document the failed run did not.

The failure comes from the real store: :class:`FailingStore` is a
``SQLiteStore`` that hands one chosen file a vector one dimension too long, so
``SQLiteStore`` itself raises in the middle of the batch's transaction. The
vaults are a copy of the fixture or generated in ``tmp_path``, with the
``bag-of-words`` provider. Nothing is patched.
"""

from __future__ import annotations

import asyncio
import hashlib
import shutil
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

import pytest
from synthetic_vault import generate

from knowledge import server, settings
from knowledge.ports import ChunkRecord, DocRecord, VectorStore
from knowledge.registry import VaultRegistry, VaultRuntime
from knowledge.stores import SQLiteStore

FIXTURE_VAULT = Path(__file__).parent / "fixtures" / "vault"


class FailingStore(SQLiteStore):
    """A ``SQLiteStore`` whose write of ``fail_on`` carries a vector of the wrong dimension."""

    fail_on: str | None = None

    def replace_files(
        self, items: Iterable[tuple[DocRecord, Sequence[ChunkRecord], Sequence[Sequence[float]] | None]]
    ) -> None:
        super().replace_files(
            (doc, chunks, [[*v, 0.0] for v in vectors] if vectors and doc.file_path == self.fail_on else vectors)
            for doc, chunks, vectors in items
        )


@contextmanager
def _served(vault: Path, index: Path) -> Iterator[tuple[VaultRuntime, FailingStore]]:
    loaded = settings.load_settings(
        {"VAULT_PATH": str(vault), "KNOWLEDGE_INDEX_DIR": str(index), "KNOWLEDGE_EMBEDDINGS_PROVIDER": "bag-of-words"},
        project_root=index.parent,
    )
    settings.install(loaded)
    opened: list[FailingStore] = []

    def store_factory(spec: settings.VaultSpec) -> VectorStore:
        opened.append(FailingStore.open(spec.index_dir))
        return opened[-1]

    reg = VaultRegistry.from_settings(loaded, store_factory=store_factory)
    server.set_registry(reg)
    try:
        rt = reg.default
        yield rt, opened[0]
    finally:
        reg.close()
        server.set_registry(None)
        settings.reset_current()


def _walk(rt: VaultRuntime) -> list[str]:
    """The vault's documents in the order the indexer writes them."""
    return [path.relative_to(rt.vault_path).as_posix() for path in rt.indexer.source.markdown_files()]


def _hashes(vault: Path, files: Iterable[str]) -> dict[str, str]:
    return {rel: hashlib.sha256((vault / rel).read_bytes()).hexdigest() for rel in files}


def test_a_failed_batch_of_a_first_index_is_absent_and_the_next_reindex_writes_it(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    generate(vault, docs=60, seed=7)
    with _served(vault, tmp_path / "index") as (rt, store):
        files = _walk(rt)
        target = files[len(files) // 2]
        store.fail_on = target

        with pytest.raises(ValueError, match="does not match the stored dimension"):
            asyncio.run(server.reindex())

        stored = store.file_hashes()
        kept = len(stored)
        # The earlier batches stay; the failing batch is gone from its first document on.
        assert sorted(stored) == sorted(files[:kept])
        assert 0 < kept < files.index(target)
        assert stored == _hashes(vault, files[:kept])
        assert rt.indexer.skipped_files == []
        assert not rt.spec.skipped_log.exists()
        assert not (tmp_path / "index" / "index_meta.json").exists()

        store.fail_on = None
        stats = asyncio.run(server.reindex())

    assert (stats["files"], stats["new"], stats["unchanged"], stats["updated"]) == (
        len(files),
        len(files) - kept,
        kept,
        0,
    )
    assert "skipped_count" not in stats
    assert store.file_hashes() == _hashes(vault, files)
    assert not rt.spec.skipped_log.exists()


def test_a_failed_incremental_batch_keeps_every_previous_version(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    shutil.copytree(FIXTURE_VAULT, vault)
    with _served(vault, tmp_path / "index") as (rt, store):
        files = _walk(rt)
        first = asyncio.run(server.reindex())
        assert first["new"] == len(files)
        before = store.file_hashes()

        middle = len(files) // 2
        edited = [files[1], files[middle], files[middle + 2]]  # one batch: the fixture is 14 one-chunk documents
        for rel in edited:
            with (vault / rel).open("a", encoding="utf-8") as fh:
                fh.write("\n\nOne more line.\n")
        store.fail_on = files[middle]

        with pytest.raises(ValueError, match="does not match the stored dimension"):
            asyncio.run(server.reindex())

        # Not one edited file is stored at its new version, the one before the failure included.
        assert store.file_hashes() == before
        assert rt.indexer.skipped_files == []
        assert not rt.spec.skipped_log.exists()

        store.fail_on = None
        stats = asyncio.run(server.reindex())

    assert (stats["updated"], stats["unchanged"], stats["new"]) == (len(edited), len(files) - len(edited), 0)
    assert store.file_hashes() == _hashes(vault, files)
