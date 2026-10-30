"""Stores behind :class:`~knowledge.ports.VectorStore` and :class:`~knowledge.ports.LexicalIndex`.

* :class:`SQLiteStore` is the store every deployment uses: one SQLite file per
  vault (``index.sqlite``) with a row per document, its chunks, an FTS5 index
  over them (``unicode61 remove_diacritics 2``, BM25 weighted 3/2/1 over
  title, section and text) and their vectors. Vectors are float32 BLOBs in
  ``chunk_vectors``; when the ``sqlite-vec`` extension loads, a ``vec0`` table
  (``chunks_vec``) mirrors them and answers exact KNN with ``k = ?``. When it
  does not load (a Python built without loadable extensions), the same BLOBs
  are ranked by brute force in the standard library and
  :attr:`SQLiteStore.vector_backend` says ``python`` instead of ``sqlite-vec``.
* :class:`InMemoryVectorStore` keeps the same records in process, with an exact
  cosine search and a BM25 that follows FTS5's arithmetic, so it can stand in
  for SQLite wherever persistence does not matter.

Both stores order KNN hits by descending similarity, then path, then chunk
number, and lexical hits by descending score, then path.
"""

from __future__ import annotations

import logging
import math
import os
import re
import sqlite3
import threading
import time
import unicodedata
from array import array
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from knowledge import errors
from knowledge.ports import ChunkFilter, ChunkHit, ChunkRecord, DocRecord

log = logging.getLogger(__name__)

#: ``PRAGMA user_version`` of the layout this module writes; also the
#: ``index_version`` the server reports.
SCHEMA_VERSION = 1

#: File name of a vault's index inside its index directory.
INDEX_FILE = "index.sqlite"

#: How vectors are stored.
VECTOR_TYPE = "float32"

#: Backend names :attr:`SQLiteStore.vector_backend` reports.
BACKEND_SQLITE_VEC = "sqlite-vec"
BACKEND_PYTHON = "python"

#: Set to ``1`` to make a store that cannot load ``sqlite-vec`` fail instead of
#: falling back to the standard library (the test pipeline and golden
#: recordings run with it, so a green run always means the extension served).
REQUIRE_SQLITE_VEC_ENV = "KNOWLEDGE_REQUIRE_SQLITE_VEC"

#: BM25 column weights: title, section, text.
BM25_WEIGHTS: tuple[float, float, float] = (3.0, 2.0, 1.0)
BM25_K1 = 1.2
BM25_B = 0.75
#: FTS5 floors a non-positive idf at this value.
BM25_IDF_FLOOR = 1e-6

#: A query token this long or longer matches as a prefix.
PREFIX_MIN_CHARS = 4

#: Largest ``k`` sqlite-vec accepts in one KNN query.
VEC_MAX_K = 4096

_TOKEN = re.compile(r"[^\W_]+")


def sqlite_vec_required() -> bool:
    """Whether the environment forbids the standard-library fallback."""
    return os.environ.get(REQUIRE_SQLITE_VEC_ENV, "").strip() == "1"


def fts_tokens(text: str) -> list[str]:
    """``text`` split as ``unicode61 remove_diacritics 2`` splits it: folded alphanumeric runs."""
    decomposed = unicodedata.normalize("NFKD", text.lower())
    plain = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return _TOKEN.findall(plain)


def query_phrases(query: str) -> list[tuple[str, bool]]:
    """The phrases of a keyword query: each distinct token, as a prefix when long enough."""
    return [(token, len(token) >= PREFIX_MIN_CHARS) for token in dict.fromkeys(fts_tokens(query))]


def fts_match(phrases: Sequence[tuple[str, bool]]) -> str:
    """The FTS5 ``MATCH`` expression of ``phrases``: quoted tokens joined by ``OR``."""
    return " OR ".join(f'"{token}"*' if prefix else f'"{token}"' for token, prefix in phrases)


def _pack(vector: Sequence[float]) -> bytes:
    return array("f", (float(x) for x in vector)).tobytes()


def _unpack(blob: bytes) -> array[float]:
    values = array("f")
    values.frombytes(blob)
    return values


def _norm(vector: Sequence[float]) -> float:
    return math.sqrt(math.sumprod(vector, vector))


def _cosine(query: Sequence[float], query_norm: float, vector: Sequence[float]) -> float:
    """Cosine similarity of ``vector`` to a query whose norm is already known."""
    norm = _norm(vector)
    if query_norm == 0 or norm == 0:
        return 0.0
    return math.sumprod(query, vector) / (query_norm * norm)


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """``1 - cosine distance``; 0 when either vector has no length."""
    if len(a) != len(b):
        raise ValueError(f"vectors of different dimensions: {len(a)} and {len(b)}")
    return _cosine(a, _norm(a), b)


def _check_vectors(
    chunks: Sequence[ChunkRecord], vectors: Sequence[Sequence[float]] | None, dim: int | None
) -> int | None:
    """The dimension of ``vectors``, after checking them against ``chunks`` and the stored one."""
    if vectors is None:
        return dim
    if len(vectors) != len(chunks):
        raise ValueError(f"{len(chunks)} chunks but {len(vectors)} vectors")
    dims = {len(vector) for vector in vectors}
    if len(dims) > 1:
        raise ValueError(f"vectors of one document have different dimensions: {sorted(dims)}")
    found = dims.pop() if dims else dim
    if found is not None and dim is not None and found != dim:
        raise ValueError(f"embedding dimension {found} does not match the stored dimension {dim}")
    if found == 0:
        raise ValueError("an embedding needs at least one dimension")
    return found


def _best_per_file(hits: Iterable[ChunkHit], k: int) -> list[ChunkHit]:
    """Each file's best hit (lowest chunk number on a tie), the ``k`` best files."""
    best: dict[str, ChunkHit] = {}
    for hit in hits:
        kept = best.get(hit.file_path)
        if kept is None or (hit.score, -hit.ord) > (kept.score, -kept.ord):
            best[hit.file_path] = hit
    return sorted(best.values(), key=lambda hit: (-hit.score, hit.file_path))[: max(k, 0)]


def _knn_order(hits: list[ChunkHit], k: int) -> list[ChunkHit]:
    hits.sort(key=lambda hit: (-hit.score, hit.file_path, hit.ord))
    return hits[: max(k, 0)]


# ---------------------------------------------------------------------------
# In-memory store
# ---------------------------------------------------------------------------


class _Row:
    """One chunk of the in-memory store, with its tokens per searchable column."""

    __slots__ = ("chunk", "columns", "length", "vector")

    def __init__(self, chunk: ChunkRecord, title: str, vector: array[float] | None) -> None:
        self.chunk = chunk
        self.columns = (fts_tokens(title), fts_tokens(chunk.section), fts_tokens(chunk.text))
        self.length = sum(len(column) for column in self.columns)
        self.vector = vector

    def count(self, token: str, prefix: bool) -> list[int]:
        if prefix:
            return [sum(1 for t in column if t.startswith(token)) for column in self.columns]
        return [column.count(token) for column in self.columns]


class InMemoryVectorStore:
    """Documents, chunks and vectors in process, with exact cosine KNN and FTS5-style BM25."""

    vector_backend = BACKEND_PYTHON

    def __init__(self) -> None:
        self._docs: dict[str, DocRecord] = {}
        self._rows: dict[str, list[_Row]] = {}
        self._meta: dict[str, str] = {}
        self._dim: int | None = None
        self._lock = threading.RLock()

    def count(self) -> int:
        with self._lock:
            return sum(len(rows) for rows in self._rows.values())

    def file_hashes(self) -> dict[str, str]:
        with self._lock:
            return {path: doc.file_hash for path, doc in self._docs.items()}

    def replace_file(
        self, doc: DocRecord, chunks: Sequence[ChunkRecord], vectors: Sequence[Sequence[float]] | None
    ) -> None:
        with self._lock:
            dim = _check_vectors(chunks, vectors, self._dim)
            packed = [None] * len(chunks) if vectors is None else [array("f", map(float, v)) for v in vectors]
            self._docs[doc.file_path] = doc
            self._rows[doc.file_path] = [
                _Row(chunk, doc.title, vector) for chunk, vector in zip(chunks, packed, strict=True)
            ]
            if vectors is not None and self._dim is None and dim is not None:
                self._dim = dim
                self._meta["dim"] = str(dim)

    def delete_files(self, file_paths: Iterable[str]) -> None:
        with self._lock:
            for path in list(file_paths):
                self._docs.pop(path, None)
                self._rows.pop(path, None)

    def clear(self) -> None:
        with self._lock:
            self._docs.clear()
            self._rows.clear()
            self._dim = None
            self._meta.pop("dim", None)

    def _admitted(self, flt: ChunkFilter | None) -> list[str]:
        return sorted(path for path, doc in self._docs.items() if flt is None or flt.admits(doc))

    def knn(self, vector: Sequence[float], k: int, flt: ChunkFilter | None = None) -> list[ChunkHit]:
        with self._lock:
            if self._dim is not None and len(vector) != self._dim:
                raise ValueError(f"embedding dimension {len(vector)} does not match the stored dimension {self._dim}")
            query = array("f", map(float, vector))
            hits = [
                ChunkHit(row.chunk.file_path, row.chunk.ord, row.chunk.text, row.chunk.overlap,
                         cosine_similarity(query, row.vector))
                for path in self._admitted(flt)
                for row in self._rows[path]
                if row.vector is not None
            ]
            return _knn_order(hits, k)

    def documents(self, flt: ChunkFilter | None = None) -> list[DocRecord]:
        with self._lock:
            return [self._docs[path] for path in self._admitted(flt)]

    def chunks(self, file_paths: Iterable[str]) -> list[ChunkRecord]:
        with self._lock:
            return [row.chunk for path in sorted(set(file_paths)) for row in self._rows.get(path, [])]

    def bodies(self) -> Iterator[tuple[str, str]]:
        with self._lock:
            pairs = [(path, self._docs[path].body) for path in sorted(self._docs)]
        return iter(pairs)

    def get_meta(self) -> dict[str, str]:
        with self._lock:
            return dict(self._meta)

    def set_meta(self, values: Mapping[str, str]) -> None:
        with self._lock:
            self._meta.update({key: str(value) for key, value in values.items()})

    def lexical(self, query: str, k: int, flt: ChunkFilter | None = None) -> list[ChunkHit]:
        """BM25 as FTS5's ``bm25()`` computes it, over every chunk, then each file's best chunk."""
        phrases = query_phrases(query)
        with self._lock:
            rows = [row for path in sorted(self._rows) for row in self._rows[path]]
            if not phrases or not rows:
                return []
            total = len(rows)
            avgdl = sum(row.length for row in rows) / total
            counts = {id(row): [row.count(token, prefix) for token, prefix in phrases] for row in rows}
            idf: list[float] = []
            for index in range(len(phrases)):
                hit_rows = sum(1 for row in rows if sum(counts[id(row)][index]) > 0)
                value = math.log((total - hit_rows + 0.5) / (hit_rows + 0.5))
                idf.append(value if value > 0 else BM25_IDF_FLOOR)
            admitted = set(self._admitted(flt))
            hits: list[ChunkHit] = []
            for row in rows:
                if row.chunk.file_path not in admitted:
                    continue
                per_phrase = counts[id(row)]
                if not any(sum(columns) for columns in per_phrase):
                    continue
                score = 0.0
                for index, columns in enumerate(per_phrase):
                    freq = sum(weight * n for weight, n in zip(BM25_WEIGHTS, columns, strict=True))
                    score += idf[index] * (
                        (freq * (BM25_K1 + 1.0)) / (freq + BM25_K1 * (1 - BM25_B + BM25_B * row.length / avgdl))
                    )
                chunk = row.chunk
                hits.append(ChunkHit(chunk.file_path, chunk.ord, chunk.text, chunk.overlap, score))
            return _best_per_file(hits, k)


# ---------------------------------------------------------------------------
# SQLite store
# ---------------------------------------------------------------------------

_SCHEMA = (
    "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE docs (
        file_path TEXT PRIMARY KEY, doc_id TEXT NOT NULL, title TEXT NOT NULL,
        type TEXT NOT NULL DEFAULT '', service TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT '',
        owner TEXT NOT NULL DEFAULT '', last_verified TEXT NOT NULL DEFAULT '',
        last_updated TEXT NOT NULL DEFAULT '', file_hash TEXT NOT NULL, body TEXT NOT NULL)""",
    "CREATE INDEX docs_type ON docs(type)",
    "CREATE INDEX docs_service ON docs(service)",
    "CREATE INDEX docs_status ON docs(status)",
    """CREATE TABLE doc_tags (
        file_path TEXT NOT NULL REFERENCES docs(file_path) ON DELETE CASCADE,
        pos INTEGER NOT NULL, tag TEXT NOT NULL, PRIMARY KEY (file_path, pos))""",
    "CREATE INDEX doc_tags_tag ON doc_tags(tag)",
    # chunk_key ('<doc_id>#<ord>') is internal and not unique: two paths may
    # share a doc_id; (file_path, ord) identifies a chunk.
    """CREATE TABLE chunks (
        id INTEGER PRIMARY KEY, file_path TEXT NOT NULL REFERENCES docs(file_path) ON DELETE CASCADE,
        ord INTEGER NOT NULL, chunk_key TEXT NOT NULL, title TEXT NOT NULL,
        section TEXT NOT NULL DEFAULT '', overlap INTEGER NOT NULL DEFAULT 0, text TEXT NOT NULL,
        UNIQUE (file_path, ord))""",
    """CREATE VIRTUAL TABLE chunks_fts USING fts5(
        title, section, text, content='chunks', content_rowid='id', tokenize='unicode61 remove_diacritics 2')""",
    """CREATE TRIGGER chunks_ai AFTER INSERT ON chunks BEGIN
        INSERT INTO chunks_fts(rowid, title, section, text) VALUES (new.id, new.title, new.section, new.text);
    END""",
    """CREATE TRIGGER chunks_ad AFTER DELETE ON chunks BEGIN
        INSERT INTO chunks_fts(chunks_fts, rowid, title, section, text)
        VALUES ('delete', old.id, old.title, old.section, old.text);
    END""",
    """CREATE TRIGGER chunks_au AFTER UPDATE ON chunks BEGIN
        INSERT INTO chunks_fts(chunks_fts, rowid, title, section, text)
        VALUES ('delete', old.id, old.title, old.section, old.text);
        INSERT INTO chunks_fts(rowid, title, section, text) VALUES (new.id, new.title, new.section, new.text);
    END""",
    """CREATE TABLE chunk_vectors (
        id INTEGER PRIMARY KEY REFERENCES chunks(id) ON DELETE CASCADE, embedding BLOB NOT NULL)""",
)

_DOC_COLUMNS = (
    "file_path, doc_id, title, type, service, status, owner, last_verified, last_updated, file_hash, body"
)


def _while_locked(step: Callable[[], Any], *, seconds: float = 5.0) -> Any:
    """Run ``step``, retrying while another connection holds the lock it needs.

    Switching a new file to WAL takes an exclusive lock and SQLite reports
    ``database is locked`` at once instead of waiting on the busy timeout, so
    two processes opening one new index would otherwise race.
    """
    deadline = time.monotonic() + seconds
    while True:
        try:
            return step()
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) or time.monotonic() >= deadline:
                raise
            time.sleep(0.01)


def _load_sqlite_vec(conn: sqlite3.Connection) -> bool:
    """Load ``sqlite-vec`` into ``conn``; ``False`` when this Python or build cannot."""
    try:
        import sqlite_vec  # type: ignore[import-untyped]

        conn.enable_load_extension(True)
        try:
            sqlite_vec.load(conn)
        finally:
            conn.enable_load_extension(False)
    except (AttributeError, ImportError, sqlite3.Error, OSError) as exc:
        log.debug("sqlite-vec did not load: %s", exc)
        return False
    return True


def probe_vector_backend() -> str:
    """The vector backend a store opened by this process would use, from a throwaway connection."""
    conn = sqlite3.connect(":memory:")
    try:
        return BACKEND_SQLITE_VEC if _load_sqlite_vec(conn) else BACKEND_PYTHON
    finally:
        conn.close()


#: The first bytes of every SQLite database file.
SQLITE_MAGIC = b"SQLite format 3\x00"


def read_schema_version(path: Path) -> int | None:
    """``PRAGMA user_version`` of the database at ``path`` without writing anything; ``None`` if absent.

    Read from the file header: opening a WAL database, even read-only, may
    create its ``-shm`` and ``-wal`` files. ``user_version`` is only written by
    a schema change, which a store checkpoints right after making it, so the
    header carries it. An empty file is an empty database (version 0); a file
    with anything else in its header raises ``E_INDEX_UNREADABLE``.
    """
    try:
        with path.open("rb") as handle:
            header = handle.read(100)
    except FileNotFoundError:
        return None
    if not header:
        return 0
    if len(header) < 64 or not header.startswith(SQLITE_MAGIC):
        raise errors.KnowledgeConfigError(
            errors.E_INDEX_UNREADABLE,
            f"{path} is not a SQLite database; move it aside so the index can be rebuilt",
        )
    return int.from_bytes(header[60:64], "big")


def read_live_schema_version(path: Path) -> int:
    """``PRAGMA user_version`` of ``path`` as a connection sees it, the WAL included."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


def _doc_where(flt: ChunkFilter | None, alias: str = "d") -> tuple[str, list[Any]]:
    """SQL predicate over ``docs`` (as ``alias``) for ``flt``, with its parameters."""
    if flt is None:
        return "1", []
    parts: list[str] = []
    params: list[Any] = []
    if flt.file_paths is not None:
        if not flt.file_paths:
            return "0", []
        parts.append(f"{alias}.file_path IN ({','.join('?' * len(flt.file_paths))})")
        params.extend(flt.file_paths)
    for key, value in sorted(flt.equals.items()):
        parts.append(f"{alias}.{key} = ?")
        params.append(value)
    if flt.exclude_types:
        parts.append(f"{alias}.type NOT IN ({','.join('?' * len(flt.exclude_types))})")
        params.extend(flt.exclude_types)
    for prefix in flt.exclude_prefixes:
        parts.append(f"substr({alias}.file_path, 1, ?) != ?")
        params.extend((len(prefix), prefix))
    if flt.tag is not None:
        parts.append(f"EXISTS (SELECT 1 FROM doc_tags t WHERE t.file_path = {alias}.file_path AND t.tag = ?)")
        params.append(flt.tag)
    return " AND ".join(parts) or "1", params


class SQLiteStore:
    """One vault's index in one SQLite file, safe to read from many threads while one writes.

    Writes go through a single connection under a lock, one document per
    ``BEGIN IMMEDIATE`` transaction; each reading thread has its own
    connection, and WAL lets it read the last committed state while a write is
    in flight.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._local = threading.local()
        self._conns: list[sqlite3.Connection] = []
        self._conns_lock = threading.Lock()
        self._vec: bool | None = None
        self._writer: sqlite3.Connection | None = self._connect()
        self._init_schema()
        if self._vec:
            self._sync_vec_mirror()

    @classmethod
    def open(cls, index_dir: Path) -> SQLiteStore:
        """The store of ``index_dir`` (``<index_dir>/index.sqlite``), created on first use."""
        return cls(index_dir / INDEX_FILE)

    # -- connections ----------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None, timeout=5.0)
        if self._vec is None:
            self._vec = _load_sqlite_vec(conn)
            if not self._vec:
                if sqlite_vec_required():
                    conn.close()
                    raise errors.KnowledgeConfigError(
                        errors.E_SQLITE_VEC_UNAVAILABLE,
                        f"{REQUIRE_SQLITE_VEC_ENV}=1 and sqlite-vec does not load in this Python "
                        f"(SQLite {sqlite3.sqlite_version})",
                    )
                log.warning(
                    "sqlite-vec does not load in this Python; vectors are ranked by the standard library"
                )
        elif self._vec and not _load_sqlite_vec(conn):
            conn.close()
            raise sqlite3.OperationalError("sqlite-vec loaded once in this process and then failed to load")
        _while_locked(lambda: conn.execute("PRAGMA journal_mode=WAL"))
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        with self._conns_lock:
            self._conns.append(conn)
        return conn

    def _reader(self) -> sqlite3.Connection:
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._connect()
            self._local.conn = conn
        return conn

    def _write_conn(self) -> sqlite3.Connection:
        """The connection writes go through (callers hold ``_lock``), reopened after :meth:`close`."""
        if self._writer is None:
            self._writer = self._connect()
        return self._writer

    def close(self) -> None:
        """Checkpoint the WAL into the file and close every connection this store opened.

        The store stays usable: the next read or write opens a new connection.
        """
        with self._lock:
            if self._writer is not None:
                try:
                    self._writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except sqlite3.Error:
                    log.debug("WAL checkpoint of %s failed on close", self.path)
            self._writer = None
        with self._conns_lock:
            conns, self._conns = self._conns, []
        for conn in conns:
            try:
                conn.close()
            except sqlite3.Error:
                pass
        self._local = threading.local()

    @property
    def vector_backend(self) -> str:
        return BACKEND_SQLITE_VEC if self._vec else BACKEND_PYTHON

    # -- schema ---------------------------------------------------------------

    def _init_schema(self) -> None:
        with self._lock:
            conn = self._write_conn()
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise errors.KnowledgeConfigError(
                    errors.E_INDEX_VERSION_NEWER,
                    f"{self.path} has index layout {version}; this package reads up to {SCHEMA_VERSION}. "
                    "Upgrade cloudfrog-knowledge or point the vault at another index directory",
                )
            if version == SCHEMA_VERSION:
                return
            _while_locked(lambda: conn.execute("BEGIN IMMEDIATE"))
            try:
                # Another process may have created the schema while this one
                # waited for the write lock.
                if int(conn.execute("PRAGMA user_version").fetchone()[0]) == SCHEMA_VERSION:
                    conn.execute("COMMIT")
                    return
                for statement in _SCHEMA:
                    conn.execute(statement)
                conn.executemany(
                    "INSERT INTO meta(key, value) VALUES (?, ?)",
                    [
                        ("schema_version", str(SCHEMA_VERSION)),
                        ("index_version", str(SCHEMA_VERSION)),
                        ("vector_type", VECTOR_TYPE),
                    ],
                )
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            # Move the new schema from the WAL into the file, so its header
            # (what ``read_schema_version`` reads) carries the layout even when
            # the process ends without closing the store.
            try:
                conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            except sqlite3.Error:
                log.debug("WAL checkpoint of %s failed after creating its schema", self.path)

    def _has_table(self, conn: sqlite3.Connection, name: str) -> bool:
        row = conn.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (name,)).fetchone()
        return row is not None

    def _vec_dirty(self, conn: sqlite3.Connection) -> bool:
        return conn.execute("SELECT 1 FROM meta WHERE key = 'vec_dirty'").fetchone() is not None

    def _dim(self, conn: sqlite3.Connection) -> int | None:
        row = conn.execute("SELECT value FROM meta WHERE key = 'dim'").fetchone()
        return int(row[0]) if row else None

    def _create_vec(self, conn: sqlite3.Connection, dim: int) -> None:
        conn.execute(
            f"CREATE VIRTUAL TABLE chunks_vec USING vec0("
            f"id INTEGER PRIMARY KEY, embedding float[{dim}] distance_metric=cosine, type TEXT, file_path TEXT)"
        )

    def _sync_vec_mirror(self) -> None:
        """Rebuild ``chunks_vec`` from ``chunk_vectors`` when a write without sqlite-vec left it behind."""
        with self._lock:
            conn = self._write_conn()
            dirty = conn.execute("SELECT value FROM meta WHERE key = 'vec_dirty'").fetchone()
            dim = self._dim(conn)
            has_vec = self._has_table(conn, "chunks_vec")
            vectors = conn.execute("SELECT count(*) FROM chunk_vectors").fetchone()[0]
            if not dirty and (has_vec or not vectors):
                return
            log.info("Rebuilding the sqlite-vec table of %s from its stored vectors", self.path)
            conn.execute("BEGIN IMMEDIATE")
            try:
                if has_vec:
                    conn.execute("DROP TABLE chunks_vec")
                if dim is not None and vectors:
                    self._create_vec(conn, dim)
                    conn.execute(
                        "INSERT INTO chunks_vec(id, embedding, type, file_path) "
                        "SELECT v.id, v.embedding, d.type, c.file_path FROM chunk_vectors v "
                        "JOIN chunks c ON c.id = v.id JOIN docs d ON d.file_path = c.file_path"
                    )
                conn.execute("DELETE FROM meta WHERE key = 'vec_dirty'")
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    # -- writes ---------------------------------------------------------------

    def _delete(self, conn: sqlite3.Connection, file_paths: Sequence[str]) -> None:
        if not file_paths:
            return
        marks = ",".join("?" * len(file_paths))
        if self._has_table(conn, "chunks_vec"):
            if self._vec:
                conn.execute(
                    f"DELETE FROM chunks_vec WHERE id IN (SELECT id FROM chunks WHERE file_path IN ({marks}))",
                    list(file_paths),
                )
            else:
                conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('vec_dirty', '1')")
        conn.execute(f"DELETE FROM docs WHERE file_path IN ({marks})", list(file_paths))

    def replace_file(
        self, doc: DocRecord, chunks: Sequence[ChunkRecord], vectors: Sequence[Sequence[float]] | None
    ) -> None:
        with self._lock:
            conn = self._write_conn()
            conn.execute("BEGIN IMMEDIATE")
            try:
                stored = self._dim(conn)
                dim = _check_vectors(chunks, vectors, stored)
                self._delete(conn, [doc.file_path])
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
                    if self._vec:
                        if not self._has_table(conn, "chunks_vec") and dim is not None:
                            self._create_vec(conn, dim)
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

    def delete_files(self, file_paths: Iterable[str]) -> None:
        paths = sorted(set(file_paths))
        if not paths:
            return
        with self._lock:
            conn = self._write_conn()
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._delete(conn, paths)
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def clear(self) -> None:
        """Drop every document, chunk and vector, and forget the vector dimension."""
        with self._lock:
            conn = self._write_conn()
            conn.execute("BEGIN IMMEDIATE")
            try:
                if self._has_table(conn, "chunks_vec"):
                    if self._vec:
                        conn.execute("DROP TABLE chunks_vec")
                    else:
                        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('vec_dirty', '1')")
                conn.execute("DELETE FROM docs")
                conn.execute("DELETE FROM meta WHERE key = 'dim'")
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def get_meta(self) -> dict[str, str]:
        return {str(k): str(v) for k, v in self._reader().execute("SELECT key, value FROM meta").fetchall()}

    def set_meta(self, values: Mapping[str, str]) -> None:
        with self._lock:
            self._write_conn().executemany(
                "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                [(key, str(value)) for key, value in values.items()],
            )

    # -- reads ----------------------------------------------------------------

    def count(self) -> int:
        return int(self._reader().execute("SELECT count(*) FROM chunks").fetchone()[0])

    def file_hashes(self) -> dict[str, str]:
        rows = self._reader().execute("SELECT file_path, file_hash FROM docs").fetchall()
        return {str(path): str(digest) for path, digest in rows}

    def documents(self, flt: ChunkFilter | None = None) -> list[DocRecord]:
        conn = self._reader()
        where, params = _doc_where(flt)
        conn.execute("BEGIN")
        try:
            rows = conn.execute(
                f"SELECT {_DOC_COLUMNS} FROM docs d WHERE {where} ORDER BY d.file_path", params
            ).fetchall()
            tags: dict[str, list[str]] = {}
            if rows:
                for path, tag in conn.execute(
                    f"SELECT t.file_path, t.tag FROM doc_tags t JOIN docs d ON d.file_path = t.file_path "
                    f"WHERE {where} ORDER BY t.file_path, t.pos",
                    params,
                ):
                    tags.setdefault(str(path), []).append(str(tag))
        finally:
            conn.execute("COMMIT")
        return [
            DocRecord(
                file_path=row[0], doc_id=row[1], title=row[2], type=row[3], service=row[4], status=row[5],
                owner=row[6], last_verified=row[7], last_updated=row[8], file_hash=row[9],
                tags=tuple(tags.get(row[0], ())), body=row[10],
            )
            for row in rows
        ]

    def chunks(self, file_paths: Iterable[str]) -> list[ChunkRecord]:
        paths = sorted(set(file_paths))
        if not paths:
            return []
        rows = self._reader().execute(
            f"SELECT file_path, ord, text, overlap, section FROM chunks "
            f"WHERE file_path IN ({','.join('?' * len(paths))}) ORDER BY file_path, ord",
            paths,
        ).fetchall()
        return [ChunkRecord(file_path=r[0], ord=r[1], text=r[2], overlap=r[3], section=r[4]) for r in rows]

    def bodies(self) -> Iterator[tuple[str, str]]:
        rows = self._reader().execute("SELECT file_path, body FROM docs ORDER BY file_path").fetchall()
        return iter([(str(path), str(body)) for path, body in rows])

    def knn(self, vector: Sequence[float], k: int, flt: ChunkFilter | None = None) -> list[ChunkHit]:
        if k <= 0:
            return []
        conn = self._reader()
        dim = self._dim(conn)
        if dim is None:
            return []
        if len(vector) != dim:
            raise ValueError(f"embedding dimension {len(vector)} does not match the stored dimension {dim}")
        if self._vec and self._vec_dirty(conn):
            # Another process wrote vectors without sqlite-vec since this store
            # opened: ``chunks_vec`` misses them until it is rebuilt.
            try:
                self._sync_vec_mirror()
            except sqlite3.Error as exc:
                log.warning("Could not rebuild the sqlite-vec table of %s: %s", self.path, exc)
        if self._vec and not self._vec_dirty(conn) and self._has_table(conn, "chunks_vec"):
            try:
                hits = self._knn_vec(conn, vector, k, flt)
            except sqlite3.OperationalError as exc:
                # A rebuild dropped ``chunks_vec`` after the check above.
                if "no such table" not in str(exc):
                    raise
                hits = None
            if hits is not None:
                return hits
        return self._knn_python(conn, vector, k, flt)

    def _knn_python(
        self, conn: sqlite3.Connection, vector: Sequence[float], k: int, flt: ChunkFilter | None
    ) -> list[ChunkHit]:
        where, params = _doc_where(flt)
        query = array("f", map(float, vector))
        query_norm = _norm(query)
        hits = [
            ChunkHit(path, ord_, text, overlap, _cosine(query, query_norm, _unpack(blob)))
            for path, ord_, text, overlap, blob in conn.execute(
                f"SELECT c.file_path, c.ord, c.text, c.overlap, v.embedding FROM chunk_vectors v "
                f"JOIN chunks c ON c.id = v.id JOIN docs d ON d.file_path = c.file_path WHERE {where}",
                params,
            )
        ]
        return _knn_order(hits, k)

    def _knn_vec(
        self, conn: sqlite3.Connection, vector: Sequence[float], k: int, flt: ChunkFilter | None
    ) -> list[ChunkHit] | None:
        """KNN through ``chunks_vec``; ``None`` when the filter needs more than one query may return.

        ``type`` and ``file_path`` filter inside the KNN; the other predicates
        filter the joined documents, and ``k`` doubles until enough chunks pass.
        """
        clauses = ["embedding MATCH ?", "k = ?"]
        params: list[Any] = [_pack(vector), 0]
        if flt is not None and flt.file_paths is not None:
            if not flt.file_paths:
                return []
            clauses.append(f"file_path IN ({','.join('?' * len(flt.file_paths))})")
            params.extend(flt.file_paths)
        if flt is not None:
            for excluded in flt.exclude_types:
                clauses.append("type != ?")
                params.append(excluded)
        residual: ChunkFilter | None = None
        if flt is not None and (flt.equals or flt.exclude_prefixes or flt.tag is not None):
            residual = ChunkFilter(equals=flt.equals, exclude_prefixes=flt.exclude_prefixes, tag=flt.tag)
        total = int(conn.execute("SELECT count(*) FROM chunk_vectors").fetchone()[0])
        wanted = k
        while True:
            fetch = min(wanted, total)
            if fetch > VEC_MAX_K:
                return None
            if fetch <= 0:
                return []
            params[1] = fetch
            conn.execute("BEGIN")
            try:
                found = conn.execute(
                    f"SELECT id, distance FROM chunks_vec WHERE {' AND '.join(clauses)}", params
                ).fetchall()
                ids = [row[0] for row in found]
                rows = conn.execute(
                    f"SELECT c.id, c.file_path, c.ord, c.text, c.overlap FROM chunks c "
                    f"WHERE c.id IN ({','.join('?' * len(ids))})",
                    ids,
                ).fetchall() if ids else []
                admitted: set[str] | None = None
                if residual is not None and rows:
                    paths = tuple(sorted({row[1] for row in rows}))
                    where, where_params = _doc_where(
                        ChunkFilter(file_paths=paths, equals=residual.equals,
                                    exclude_prefixes=residual.exclude_prefixes, tag=residual.tag)
                    )
                    admitted = {
                        str(row[0])
                        for row in conn.execute(f"SELECT d.file_path FROM docs d WHERE {where}", where_params)
                    }
            finally:
                conn.execute("COMMIT")
            by_id = {row[0]: row for row in rows}
            hits = [
                ChunkHit(by_id[i][1], by_id[i][2], by_id[i][3], by_id[i][4], 1.0 - float(distance))
                for i, distance in found
                if i in by_id and (admitted is None or by_id[i][1] in admitted)
            ]
            if len(hits) >= k or fetch >= total or len(found) < fetch:
                return _knn_order(hits, k)
            wanted = fetch * 2

    def lexical(self, query: str, k: int, flt: ChunkFilter | None = None) -> list[ChunkHit]:
        phrases = query_phrases(query)
        if not phrases or k <= 0:
            return []
        where, params = _doc_where(flt)
        weights = ", ".join(str(w) for w in BM25_WEIGHTS)
        rows = self._reader().execute(
            f"SELECT c.file_path, c.ord, c.text, c.overlap, bm25(chunks_fts, {weights}) "
            f"FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid JOIN docs d ON d.file_path = c.file_path "
            f"WHERE chunks_fts MATCH ? AND {where}",
            [fts_match(phrases), *params],
        ).fetchall()
        return _best_per_file((ChunkHit(r[0], r[1], r[2], r[3], -float(r[4])) for r in rows), k)
