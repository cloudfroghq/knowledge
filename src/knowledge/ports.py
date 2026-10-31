"""The seams the indexer and the tools depend on, as structural protocols.

Each port names one thing the server needs from the outside world and nothing
about how it is provided:

* :class:`EmbeddingProvider` turns texts into vectors;
* :class:`VectorStore` keeps one row per document, its chunks and their
  vectors, and answers filtered reads and nearest-neighbour searches;
* :class:`LexicalIndex` ranks chunks by keywords;
* :class:`VaultSource` lists the Markdown files of a vault and, optionally,
  refreshes them (:class:`VaultSync`);
* :class:`MemoryClient` reads a session memory store;
* :class:`Judge` scores how well passages answer a query.

The adapters live in their own modules (``embeddings``, ``stores``,
``vault_source``) and import their heavy dependencies lazily, so depending on a
port never imports SQLite extensions, OpenAI or git. This module imports only
the standard library.

Reads are filtered with :class:`ChunkFilter`, a closed filter with exactly the
predicates the tools use (document paths, frontmatter equality, excluded types
and path prefixes, one tag), and return typed records: :class:`DocRecord` per
document, :class:`ChunkRecord` per stored chunk and :class:`ChunkHit` per
ranked chunk.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

#: Frontmatter fields :attr:`ChunkFilter.equals` may compare.
FILTER_FIELDS: tuple[str, ...] = ("type", "service", "status")


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Embeds texts. ``semantic`` is false for a provider whose vectors carry no meaning."""

    name: str
    model: str
    semantic: bool

    def embed(self, texts: list[str]) -> list[list[float]]: ...


@dataclass(frozen=True)
class ChunkFilter:
    """Which documents (and so which chunks) a read may return; every predicate is AND-ed.

    * ``file_paths``: only these documents; ``None`` is every document.
    * ``equals``: frontmatter ``type``, ``service`` or ``status`` equal to the value.
    * ``exclude_types``: documents whose ``type`` is one of these are left out.
    * ``exclude_prefixes``: documents whose path starts with one of these are
      left out (a plain string prefix, as the memory folder has always been
      matched).
    * ``tag``: only documents that list this tag.
    """

    file_paths: tuple[str, ...] | None = None
    equals: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    exclude_types: tuple[str, ...] = ()
    exclude_prefixes: tuple[str, ...] = ()
    tag: str | None = None

    def __post_init__(self) -> None:
        unknown = sorted(set(self.equals) - set(FILTER_FIELDS))
        if unknown:
            raise ValueError(f"ChunkFilter.equals accepts {FILTER_FIELDS}, not {unknown}")

    def admits(self, doc: DocRecord) -> bool:
        """Whether ``doc`` passes every predicate; the reference every store agrees with."""
        if self.file_paths is not None and doc.file_path not in self.file_paths:
            return False
        if any(getattr(doc, key) != value for key, value in self.equals.items()):
            return False
        if doc.type in self.exclude_types:
            return False
        if any(doc.file_path.startswith(prefix) for prefix in self.exclude_prefixes):
            return False
        return self.tag is None or self.tag in doc.tags


@dataclass(frozen=True)
class DocRecord:
    """One indexed document: its frontmatter fields, its hash and its body.

    ``tags`` keeps the frontmatter order, duplicates included. ``body`` is the
    Markdown without frontmatter, as the indexer read it.
    """

    file_path: str
    doc_id: str
    title: str
    type: str = ""
    service: str = ""
    status: str = ""
    owner: str = ""
    last_verified: str = ""
    last_updated: str = ""
    file_hash: str = ""
    tags: tuple[str, ...] = ()
    body: str = ""

    def metadata(self) -> dict[str, Any]:
        """The frontmatter fields as the review rules read them (``knowledge.review``)."""
        return {
            "file_path": self.file_path,
            "title": self.title,
            "type": self.type,
            "service": self.service,
            "status": self.status,
            "owner": self.owner,
            "last_verified": self.last_verified,
            "last_updated": self.last_updated,
            "tags": list(self.tags),
        }


@dataclass(frozen=True)
class ChunkRecord:
    """One chunk of a document, numbered from 0 in document order.

    ``text`` is what is embedded and searched, including the ``overlap``
    leading characters it repeats from the previous chunk; ``section`` is the
    first heading inside the chunk's own text, or ``""``.
    """

    file_path: str
    ord: int
    text: str
    overlap: int = 0
    section: str = ""

    @property
    def own_text(self) -> str:
        """The chunk without the characters it repeats from the previous one."""
        return self.text[self.overlap :]


@dataclass(frozen=True)
class ChunkHit:
    """A ranked chunk. ``score`` is a cosine similarity (dense) or ``-bm25`` (lexical)."""

    file_path: str
    ord: int
    text: str
    overlap: int
    score: float

    @property
    def own_text(self) -> str:
        """The chunk without the characters it repeats from the previous one."""
        return self.text[self.overlap :]


@runtime_checkable
class VectorStore(Protocol):
    """Documents, their chunks and their vectors.

    ``replace_file`` swaps everything stored for one document at once: a reader
    sees the old version or the new one, never a mix. ``replace_files`` does
    the same for a batch of ``(doc, chunks, vectors)`` items, in order, as one
    step: it stores exactly what ``replace_file`` on each item would, and a
    failure on any item leaves none of them written. ``vectors`` is ``None``
    for a provider without meaning; otherwise it holds one vector per chunk,
    and the first vector ever written fixes the dimension. ``knn`` returns at
    most ``k`` chunks by descending cosine similarity, ties by path and then
    chunk number; ``documents`` returns documents by path. With
    ``with_body=False`` it does not read the bodies and every record carries
    ``body=""``, its other fields unchanged: a caller that uses ``body`` must
    leave it ``True``.
    """

    @property
    def vector_backend(self) -> str: ...

    def count(self) -> int: ...

    def file_hashes(self) -> dict[str, str]: ...

    def replace_file(
        self, doc: DocRecord, chunks: Sequence[ChunkRecord], vectors: Sequence[Sequence[float]] | None
    ) -> None: ...

    def replace_files(
        self, items: Iterable[tuple[DocRecord, Sequence[ChunkRecord], Sequence[Sequence[float]] | None]]
    ) -> None: ...

    def delete_files(self, file_paths: Iterable[str]) -> None: ...

    def clear(self) -> None: ...

    def knn(self, vector: Sequence[float], k: int, flt: ChunkFilter | None = None) -> list[ChunkHit]: ...

    def documents(self, flt: ChunkFilter | None = None, *, with_body: bool = True) -> list[DocRecord]: ...

    def chunks(self, file_paths: Iterable[str]) -> list[ChunkRecord]: ...

    def bodies(self) -> Iterator[tuple[str, str]]: ...

    def get_meta(self) -> dict[str, str]: ...

    def set_meta(self, values: Mapping[str, str]) -> None: ...


@runtime_checkable
class LexicalIndex(Protocol):
    """Keyword ranking: each matching document's best chunk, the ``k`` best documents."""

    def lexical(self, query: str, k: int, flt: ChunkFilter | None = None) -> list[ChunkHit]: ...


@runtime_checkable
class VaultSync(Protocol):
    """Refreshes the vault rooted at ``root``; ``True`` only when something was pulled."""

    def __call__(self, root: Path, /) -> bool: ...


# How one refresh of a vault ended, as ``reindex`` reports it under
# ``vault_sync``. The five values are part of the tool contract.
SYNC_PULLED = "pulled"
SYNC_UP_TO_DATE = "up_to_date"
SYNC_SKIPPED_NO_REPO = "skipped_no_repo"
SYNC_SKIPPED_PULL_FALSE = "skipped_pull_false"
SYNC_FAILED = "failed"
SYNC_OUTCOMES: tuple[str, ...] = (
    SYNC_PULLED,
    SYNC_UP_TO_DATE,
    SYNC_SKIPPED_NO_REPO,
    SYNC_SKIPPED_PULL_FALSE,
    SYNC_FAILED,
)


@runtime_checkable
class ReportingVaultSync(VaultSync, Protocol):
    """A :class:`VaultSync` that can also say how a refresh ended.

    ``outcome`` performs the refresh, like a call does, and returns one of
    :data:`SYNC_OUTCOMES` instead of a bare boolean.
    """

    def outcome(self, root: Path, /) -> str: ...


@runtime_checkable
class VaultSource(Protocol):
    """Where the vault's Markdown files come from.

    ``root`` is assignable: re-pointing a source at another copy of the same
    vault keeps whatever refresh it was given.
    """

    root: Path

    def markdown_files(self) -> list[Path]: ...

    def sync(self) -> bool: ...


@runtime_checkable
class MemoryClient(Protocol):
    """Reads a session memory store for one project.

    ``search`` returns the hits, best first, as plain mappings. A client that
    cannot read (no credentials, no project, the store is down) raises
    :class:`knowledge.errors.MemoryUnavailable` with an ``E_MEMORY_*`` code
    instead of returning an empty list, so "nothing found" and "could not
    look" stay apart.
    """

    def search(self, query: str, *, project: str, limit: int = 5) -> list[dict[str, Any]]: ...


class NullMemoryClient:
    """A memory store with nothing in it: every search returns no hits, and never fails."""

    def search(self, query: str, *, project: str, limit: int = 5) -> list[dict[str, Any]]:
        del query, project, limit
        return []


@dataclass(frozen=True)
class JudgeUnit:
    """One passage a judge scores against the query.

    ``key`` identifies the unit in the verdict; ``document`` is the file stem
    and ``section`` the heading the passage sits under (``""`` for none);
    ``text`` is the passage itself, already cut to the size the caller sends.
    """

    key: str
    document: str
    section: str
    text: str


@dataclass(frozen=True)
class JudgeVerdict:
    """What a judge answered for one query.

    ``scores`` maps each unit key to the probability, from 0 to 1, that the
    passage answers the query. ``input_tokens`` is what the provider billed
    for the request (0 when it bills nothing) and ``model`` the exact model
    version that answered.
    """

    scores: Mapping[str, float]
    input_tokens: int = 0
    model: str = ""


class JudgeUnavailable(RuntimeError):
    """A judge that could not answer, with the class of the failure in ``reason``.

    ``reason`` is one of ``timeout``, ``unavailable``, ``auth`` or
    ``invalid_response``. The message names the failure class only: never the
    request, the body the provider sent back or any credential.
    """

    def __init__(self, reason: str, message: str = "") -> None:
        super().__init__(message or reason)
        self.reason = reason


@runtime_checkable
class Judge(Protocol):
    """Scores passages against a query, all in one request.

    ``name`` is the provider (``jev``, ``kev``, ``none``) and ``model`` the
    exact model version it runs. ``judge`` returns the verdict for ``units``
    or raises :class:`JudgeUnavailable` within ``timeout_s``: a judge that
    cannot answer says so instead of returning empty scores, so "nothing
    relevant" and "could not look" stay apart.
    """

    name: str
    model: str

    def judge(self, query: str, units: Sequence[JudgeUnit], *, timeout_s: float) -> JudgeVerdict: ...


class NullJudge:
    """The judge of a vault that declares none: it is never asked, and answering is an error."""

    name = "none"
    model = ""

    def judge(self, query: str, units: Sequence[JudgeUnit], *, timeout_s: float) -> JudgeVerdict:
        del query, units, timeout_s
        raise JudgeUnavailable("unavailable", "no judge is configured")
