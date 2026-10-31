"""Vault parsing, chunking and indexing over injected source, store and embedder."""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import math
import re
import threading
from collections.abc import Iterator, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import frontmatter

from knowledge import contract, errors, pointers
from knowledge.chunker import EMPTY_BODY, Chunk, ChunkPlan, chunks, plan_for
from knowledge.embeddings import CachedQueryEmbedder
from knowledge.graph import RESOLVER_VERSION, LinkGraph, LinkIndex, extract_wikilinks, resolve_link
from knowledge.ports import ChunkRecord, DocRecord, EmbeddingProvider, VaultSource, VaultSync, VectorStore
from knowledge.stores import SCHEMA_VERSION, SQLiteStore
from knowledge.vault_source import DirectoryVaultSource

try:  # POSIX only; elsewhere two processes indexing one directory are not serialized.
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

#: Chunks embedded per provider call.
EMBED_BATCH = 32

#: File inside the index directory that serializes indexing across processes.
INDEX_LOCK_FILE = ".index.lock"

# ---------------------------------------------------------------------------
# Keyword (TF-IDF) index
# ---------------------------------------------------------------------------


class KeywordIndex:
    """TF-IDF over whole document bodies, built in memory from the store.

    One entry per indexed document: its body, or ``(empty)`` for a body with
    no text, lowercased. Its ``idf`` is what the section selector of the
    pointers weighs query terms with, so it never depends on how bodies were
    chunked.
    """

    def __init__(self) -> None:
        self.docs: dict[str, str] = {}  # file_path → lowercased text
        self.doc_count: int = 0

    def add(self, doc_id: str, text: str) -> None:
        self.docs[doc_id] = text.lower()
        self.doc_count = len(self.docs)

    def search(self, query: str, limit: int = 10) -> list[tuple[str, float]]:
        """Return ``(doc_id, score)`` ranked by TF-IDF-like scoring."""
        terms = query.lower().split()
        if not terms:
            return []

        scores: dict[str, float] = {}
        for doc_id, text in self.docs.items():
            score = 0.0
            for term in terms:
                tf = text.count(term)
                if tf > 0:
                    df = sum(1 for d in self.docs.values() if term in d)
                    idf = math.log((self.doc_count + 1) / (df + 1))
                    score += (1 + math.log(tf)) * idf
            if score > 0:
                scores[doc_id] = score

        ranked = sorted(scores.items(), key=lambda x: (-x[1], x[0]))
        return ranked[:limit]

    def idf(self, term: str) -> float:
        """Inverse document frequency of ``term``, with the smoothing ``search`` uses."""
        needle = term.lower()
        df = sum(1 for d in self.docs.values() if needle in d)
        return math.log((self.doc_count + 1) / (df + 1))

    @classmethod
    def from_bodies(cls, bodies: Iterator[tuple[str, str]]) -> KeywordIndex:
        """One entry per ``(file_path, body)``."""
        index = cls()
        for file_path, body in bodies:
            index.add(file_path, _keyword_text(body))
        return index


# Files that failed embedding during the last indexing run of any indexer in
# this process — cleared at each reindex. Each indexer also keeps its own list
# (``VaultIndexer.skipped_files``), which is what the server reports.
_skipped_files: list[dict[str, str]] = []


def get_skipped_files() -> list[dict[str, str]]:
    """Return list of files that were skipped during the LAST indexing run."""
    return list(_skipped_files)


def _clear_skipped_files() -> None:
    """Clear the skipped files list at the start of a new indexing run."""
    _skipped_files.clear()


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------

def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rel(path: Path, vault: Path) -> str:
    """Vault-relative path with ``/`` separators: the key of every index entry."""
    return path.relative_to(vault).as_posix()


def _link_index(md_files: list[Path], vault: Path) -> LinkIndex:
    return LinkIndex.build(_rel(path, vault) for path in md_files)


def _body(path: Path) -> str | None:
    """The Markdown body of ``path`` without its frontmatter; ``None`` if the frontmatter does not parse."""
    try:
        return frontmatter.load(str(path)).content
    except Exception as exc:  # noqa: BLE001 - the indexer skips such a file the same way
        log.warning("Skipping links of %s: failed to parse frontmatter: %s", path, exc)
        return None


def _keyword_text(body: str) -> str:
    """The text the keyword index holds for a file: its body, or the empty-body chunk."""
    return body if body.strip() else EMPTY_BODY


def _parse_tags(tags: Any) -> tuple[str, ...]:
    """Normalise frontmatter tags to strings, in order, duplicates kept."""
    if isinstance(tags, list):
        return tuple(str(t) for t in tags)
    if isinstance(tags, str) and tags:
        return (tags,)
    return ()


_HEADING = re.compile(r"^#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$", re.MULTILINE)


def _section_of(text: str) -> str:
    """The first Markdown heading inside ``text``, or ``""``."""
    match = _HEADING.search(text)
    return match.group(1).strip() if match else ""


def _doc_record(rel: str, md_path: Path, meta: dict[str, Any], body: str, file_hash: str) -> DocRecord:
    """The document row of one parsed file.

    ``owner`` and ``last_verified`` are indexed verbatim from frontmatter. Only
    ``type: service`` hubs declare ``owner`` (see the vault's
    ``Conventions/Frontmatter.md``); everything else resolves it through
    ``service`` at query time in ``knowledge.review``, so the field stays empty
    here rather than being guessed at index time and going stale when a hub
    changes hands.
    """
    title = meta.get("title", md_path.stem)
    return DocRecord(
        file_path=rel,
        doc_id=pointers.doc_id(rel),
        title="" if title is None else str(title),
        type=str(meta.get("type", "")),
        service=str(meta.get("service", "")),
        status=str(meta.get("status", "")),
        owner=str(meta.get("owner", "")),
        last_verified=str(meta.get("last_verified", "")),
        last_updated=str(meta.get("last_updated", "")),
        file_hash=file_hash,
        tags=_parse_tags(meta.get("tags")),
        body=body,
    )


def _chunk_records(rel: str, cut: Sequence[Chunk]) -> list[ChunkRecord]:
    return [
        ChunkRecord(file_path=rel, ord=n, text=c.text, overlap=c.overlap, section=_section_of(c.own_text))
        for n, c in enumerate(cut)
    ]


# ---------------------------------------------------------------------------
# Indexer
# ---------------------------------------------------------------------------

class VaultIndexer:
    """Walks a vault source, embeds its documents and keeps them in a store.

    The three collaborators are injected: where the Markdown comes from
    (``source``), where documents, chunks and vectors live (``store``) and how
    texts become vectors (``embedder``). ``state_dir`` is the index directory:
    it holds the sidecar files next to the store (the link graph, the index
    metadata and the lock that serializes indexing across processes).
    """

    def __init__(
        self,
        *,
        source: VaultSource,
        store: VectorStore,
        embedder: EmbeddingProvider,
        state_dir: Path,
    ) -> None:
        self.source = source
        self._store = store
        self.embedder = embedder
        self._state_dir = state_dir
        self._graph = LinkGraph()
        # Whether the persisted graph (and its learned weights) is in memory;
        # an index run loads it first so a rebuild never drops those weights.
        self._graph_loaded = False
        self._graph_path = state_dir / "link_graph.json"
        self._index_meta_path = state_dir / "index_meta.json"
        self._keyword_index: KeywordIndex | None = None
        self._keyword_lock = threading.Lock()
        self._index_lock = threading.Lock()
        #: Files that failed embedding during this indexer's last run.
        self.skipped_files: list[dict[str, str]] = []
        # doc_id -> vault-relative paths, built from the files on disk on first
        # lookup and dropped by every index() run (the webhook reindexes
        # without going through the reindex tool).
        self._doc_ids: dict[str, tuple[str, ...]] | None = None

    @classmethod
    def open(
        cls,
        *,
        vault_path: Path,
        index_dir: Path,
        embedder: EmbeddingProvider,
        sync: VaultSync | None = None,
    ) -> VaultIndexer:
        """A directory vault over the SQLite index in ``index_dir``."""
        return cls(
            source=DirectoryVaultSource(vault_path, sync=sync),
            store=SQLiteStore.open(index_dir),
            embedder=embedder,
            state_dir=index_dir,
        )

    @property
    def vault_path(self) -> Path:
        """The vault root this indexer walks."""
        return self.source.root

    @vault_path.setter
    def vault_path(self, value: Path) -> None:
        # Re-points the same source (and index) at another copy of the vault,
        # keeping whatever refresh the source was given.
        self.source.root = value

    @property
    def embedder(self) -> EmbeddingProvider:
        return self._embedder

    @embedder.setter
    def embedder(self, value: EmbeddingProvider) -> None:
        # Documents and queries must embed with the same provider, so the
        # query cache is rebuilt whenever the provider changes.
        self._embedder = value
        self._query_embedder = CachedQueryEmbedder(value)

    @property
    def chunk_plan(self) -> ChunkPlan:
        """How this indexer cuts bodies: the provider's plan, guarded by its input limit."""
        return plan_for(self.embedder.name, max_input_chars=getattr(self.embedder, "max_input_chars", None))

    @property
    def graph(self) -> LinkGraph:
        return self._graph

    @property
    def store(self) -> VectorStore:
        return self._store

    @property
    def keyword_index(self) -> KeywordIndex:
        """TF-IDF over the bodies the store holds, built on first use after each index run."""
        with self._keyword_lock:
            if self._keyword_index is None:
                self._keyword_index = KeywordIndex.from_bodies(self._store.bodies())
            return self._keyword_index

    # -- public API ---------------------------------------------------------

    def embed_query(self, text: str) -> list[float]:
        """Embed a search query, cached by case/whitespace-normalized text."""
        return self._query_embedder.embed(text)

    def is_indexed(self) -> bool:
        """Return True if the store already holds chunks."""
        return self._store.count() > 0

    def identity(self) -> dict[str, str]:
        """What the chunks and vectors of an index built by this indexer depend on."""
        return {
            "provider": self.embedder.name,
            "model": self.embedder.model,
            "chunker_version": self.chunk_plan.version,
        }

    def stale_identity(self) -> dict[str, tuple[str | None, str]]:
        """Each identity field the stored index disagrees on: ``{field: (stored, current)}``.

        Empty for an empty store, which has nothing to disagree with.
        """
        if self._store.count() == 0:
            return {}
        stored = self._store.get_meta()
        return {
            key: (stored.get(key), value) for key, value in self.identity().items() if stored.get(key) != value
        }

    def load_graph(self) -> None:
        """Load the persisted graph.

        A graph saved under another resolver version has its edges
        re-registered from the vault and saved again, keeping its learned
        weights: it would otherwise serve stale edges until the next reindex.
        No document is embedded.
        """
        self._graph = LinkGraph.load(self._graph_path)
        self._graph_loaded = True
        with self._keyword_lock:
            self._keyword_index = None
        if self._graph_path.exists() and self._graph.resolver != RESOLVER_VERSION:
            log.info(
                "Link graph at %s was resolved with %s; re-registering links with %s",
                self._graph_path,
                self._graph.resolver,
                RESOLVER_VERSION,
            )
            self.rebuild_links()

    def rebuild_links(self) -> None:
        """Re-register every wikilink of the vault and save the graph; weights are kept."""
        vault = self.vault_path
        md_files = self.source.markdown_files()
        link_index = _link_index(md_files, vault)
        graph = self._graph.successor()
        for md_path in md_files:
            body = _body(md_path)
            if body is not None:
                self._register_links(graph, _rel(md_path, vault), body, link_index)
        self._graph = graph
        graph.save(self._graph_path)

    def save_graph(self) -> None:
        """Persist the link graph (including learned Hebbian weights)."""
        self._graph.save(self._graph_path)

    @contextlib.contextmanager
    def _exclusive(self) -> Iterator[None]:
        """One index run at a time: across threads of this process and across processes."""
        with self._index_lock:
            self._state_dir.mkdir(parents=True, exist_ok=True)
            if fcntl is None:
                yield
                return
            with (self._state_dir / INDEX_LOCK_FILE).open("a") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def index(self, *, force: bool = False, target: str = "vault") -> dict[str, int]:
        """Index (or re-index) the vault.

        ``target`` only accepts "vault": ADR-039 retired code indexing, so
        "code" and "all" are no longer valid scopes. Kept as an explicit
        parameter rather than dropped, so a caller still asking for one of
        them fails loudly here instead of silently reindexing the vault it
        did not request.

        An index built under another provider, model or chunk plan is emptied
        and rebuilt in full: chunks and vectors from two of them do not mix.

        Returns stats: {files, new, updated, unchanged, chunks}.
        """
        if target != "vault":
            raise ValueError(
                f"unsupported reindex target {target!r} — only 'vault' remains after ADR-039"
            )
        with self._exclusive():
            _clear_skipped_files()
            self.skipped_files = []
            self._doc_ids = None
            if not self._graph_loaded:
                # A first run in this process starts from the saved graph, so
                # learned weights survive the rebuild of its edges.
                self._graph = LinkGraph.load(self._graph_path)
                self._graph_loaded = True
            try:
                known: set[str] = set()
                stale = self.stale_identity()
                if stale:
                    log.warning(
                        "%s: the index was built with %s and this run uses %s; emptying it and "
                        "re-embedding every file (full embedding cost)",
                        errors.E_INDEX_PROVIDER_MISMATCH,
                        {key: old for key, (old, _) in stale.items()},
                        {key: new for key, (_, new) in stale.items()},
                    )
                    known = set(self._store.file_hashes())
                    self._store.clear()
                    force = True
                if self._store.count() == 0:
                    self._store.set_meta(self.identity())
                stats = self._index_vault(force=force, known=known)
            finally:
                self._doc_ids = None
                with self._keyword_lock:
                    self._keyword_index = None
        log.info("Indexing complete: %s", stats)
        return stats

    def doc_id_paths(self, doc: str, *, refresh: bool = False) -> tuple[str, ...]:
        """The vault-relative paths whose ``doc_id`` is ``doc``, in path order.

        Empty when no Markdown file of the vault has that ``doc_id``; more than
        one path is a ``doc_id`` collision. ``refresh`` rebuilds the map from the
        files on disk first, for a file added since the last lookup.
        """
        mapping = self._doc_ids
        if mapping is None or refresh:
            vault = self.vault_path
            built: dict[str, list[str]] = {}
            for path in self.source.markdown_files():
                rel = path.relative_to(vault).as_posix()
                built.setdefault(pointers.doc_id(rel), []).append(rel)
            mapping = {key: tuple(sorted(paths)) for key, paths in built.items()}
            self._doc_ids = mapping
        return mapping.get(doc, ())

    def _index_vault(self, *, force: bool = False, known: set[str] | frozenset[str] = frozenset()) -> dict[str, int]:
        """Walk the vault, write every changed document and rebuild the graph.

        ``known`` names documents an emptied index held: written again, they
        count as ``updated``, not ``new``.
        """
        vault = self.vault_path
        md_files = self.source.markdown_files()
        stats = {"files": len(md_files), "new": 0, "updated": 0, "unchanged": 0, "chunks": 0}

        # Wikilinks resolve against every Markdown file of this walk.
        link_index = _link_index(md_files, vault)

        existing = self._store.file_hashes()
        # Edges are registered on a successor that keeps the learned weights,
        # and swapped in once complete: readers never walk a half-built graph.
        graph = self._graph.successor()
        pending: list[tuple[DocRecord, list[ChunkRecord]]] = []
        pending_chunks = 0

        for md_path in md_files:
            rel = _rel(md_path, vault)
            file_h = _file_hash(md_path)

            # Change detection
            if not force and existing.get(rel) == file_h:
                stats["unchanged"] += 1
                # Still need to rebuild graph links, from the body as for a
                # changed file: a link inside the frontmatter is never an edge.
                body = _body(md_path)
                if body is not None:
                    self._register_links(graph, rel, body, link_index)
                continue

            try:
                post = frontmatter.load(str(md_path))
            except Exception as e:
                log.warning("Skipping %s: failed to parse frontmatter: %s", md_path, e)
                continue
            content: str = post.content
            self._register_links(graph, rel, content, link_index)

            records = _chunk_records(rel, self._chunk(content))
            pending.append((_doc_record(rel, md_path, dict(post.metadata), content, file_h), records))
            pending_chunks += len(records)
            stats["chunks"] += len(records)
            stats["updated" if rel in existing or rel in known else "new"] += 1
            if pending_chunks >= EMBED_BATCH:
                self._write(pending, existing)
                pending, pending_chunks = [], 0

        if pending:
            self._write(pending, existing)

        # Delete stale documents whose files were removed from disk
        current = {_rel(p, vault) for p in md_files}
        removed = sorted(set(existing) - current)
        if removed:
            self._store.delete_files(removed)
            for old_rel in removed:
                log.info("Cleaned stale vault doc: %s", old_rel)

        self._graph = graph
        graph.save(self._graph_path)
        self._write_index_meta(stats)
        return stats

    def _embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for i in range(0, len(texts), EMBED_BATCH):
            vectors.extend(self.embedder.embed(texts[i : i + EMBED_BATCH]))
        if len(vectors) != len(texts):
            raise ValueError(f"the provider returned {len(vectors)} vectors for {len(texts)} texts")
        return vectors

    def _write(self, pending: list[tuple[DocRecord, list[ChunkRecord]]], existing: dict[str, str]) -> None:
        """Embed the chunks of ``pending`` and replace its documents in the store, as one batch.

        A document whose chunks cannot all be embedded is not written at all
        (and an older version of it is removed), so the store never holds a
        document without its vectors and the keyword ``idf`` counts only
        indexed files. Each failing chunk is reported in ``skipped_files``.

        The batch is one ``replace_files`` call (one transaction in the SQLite
        store). A store error inside it (a database error, a vector dimension
        that disagrees with the stored one) rolls back every document of the
        batch, where one write per document kept the earlier ones; the run
        fails the same way, since that error is not caught here either.
        """
        if not self.embedder.semantic:
            self._store.replace_files([(doc, records, None) for doc, records in pending])
            return
        texts = [record.text for _, records in pending for record in records]
        try:
            vectors = self._embed(texts)
        except Exception as exc:
            log.warning("Embedding %d chunks failed: %s — falling back to one-by-one", len(texts), exc)
            for doc, records in pending:
                self._write_one_by_one(doc, records, existing)
            return
        batch: list[tuple[DocRecord, list[ChunkRecord], list[list[float]]]] = []
        start = 0
        for doc, records in pending:
            batch.append((doc, records, vectors[start : start + len(records)]))
            start += len(records)
        self._store.replace_files(batch)
        log.info("Wrote %d documents (%d chunks)", len(pending), len(texts))

    def _write_one_by_one(self, doc: DocRecord, records: list[ChunkRecord], existing: dict[str, str]) -> None:
        vectors: list[list[float]] = []
        failed = False
        for record in records:
            try:
                vectors.append(self.embedder.embed([record.text])[0])
            except Exception as item_exc:
                failed = True
                log.warning("Skipped %s: %s", doc.file_path, item_exc)
                skipped = {"file_path": doc.file_path, "reason": str(item_exc), "chars": str(len(record.text))}
                self.skipped_files.append(skipped)
                _skipped_files.append(skipped)
        if failed:
            if doc.file_path in existing:
                self._store.delete_files([doc.file_path])
            return
        self._store.replace_file(doc, records, vectors)

    def _write_index_meta(self, stats: dict[str, int]) -> None:
        """Record when and how the index was built: in the store and in ``index_meta.json``.

        ``/health`` reads the JSON file to detect a stale index without
        opening the store.
        """
        built_at = datetime.now(timezone.utc).isoformat()
        self._store.set_meta(
            {
                **self.identity(),
                "built_at": built_at,
                "index_version": str(SCHEMA_VERSION),
                "contract": contract.CONTRACT_VERSION,
            }
        )
        payload = {
            "last_indexed_at": built_at,
            "stats": stats,
            "embedding": {"provider": self.embedder.name, "model": self.embedder.model},
            "chunker": self.chunk_plan.version,
            "index_version": SCHEMA_VERSION,
        }
        try:
            self._index_meta_path.write_text(json.dumps(payload))
        except Exception:
            log.exception("Failed to write index meta to %s", self._index_meta_path)

    # -- internals ----------------------------------------------------------

    def _chunk(self, content: str) -> list[Chunk]:
        return chunks(content, self.chunk_plan)

    @staticmethod
    def _register_links(graph: LinkGraph, rel: str, content: str, link_index: LinkIndex) -> None:
        resolved = [path for raw in extract_wikilinks(content) if (path := resolve_link(raw, link_index, rel))]
        graph.set_links(rel, resolved)
