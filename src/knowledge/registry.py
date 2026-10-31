"""The vaults one process serves, and the live objects behind each of them.

A :class:`VaultRuntime` bundles what the tools read for a vault: its
:class:`~knowledge.settings.VaultSpec`, the indexer over its vector store, its
usage tracker, its memory client, its judge and the rerank state around it
(cache and circuit breaker), and the small mutable state the server keeps
between calls (whether the lazy index ran, how many Hebbian updates wait to
be saved). :func:`build_runtime` composes the adapters the spec asks for — a
directory source, a SQLite store in the spec's index directory and the
spec's embeddings provider — and never indexes, which stays lazy on the first
tool call. Any adapter can be passed in instead.

A :class:`VaultRegistry` holds one runtime per registered vault and opens each
one only when something asks for it, so serving vault ``alpha`` never opens
the store of vault ``beta``. Every runtime owns its own index directory;
nothing is shared between two vaults.

How a vault is kept fresh is injected (``sync_factory``), so this module never
imports git helpers.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from knowledge import errors
from knowledge.embeddings import build_embedder
from knowledge.indexer import VaultIndexer
from knowledge.ports import (
    SYNC_PULLED,
    SYNC_UP_TO_DATE,
    EmbeddingProvider,
    Judge,
    MemoryClient,
    NullJudge,
    NullMemoryClient,
    VaultSync,
    VectorStore,
)
from knowledge.rerank import Reranker
from knowledge.settings import EmbeddingSettings, Settings, VaultSpec, parse_vaults_file
from knowledge.stores import INDEX_FILE, SCHEMA_VERSION, SQLiteStore, read_live_schema_version, read_schema_version
from knowledge.usage import UsageTracker
from knowledge.vault_source import DirectoryVaultSource

log = logging.getLogger(__name__)

#: Seconds a runtime's usage tracker waits, after the first pending hit, before
#: its background writer writes ``usage_stats.json`` (sooner at 20 hits). A
#: hard crash loses at most this much of the counts; ``VaultRegistry.close``
#: and interpreter exit write the rest.
USAGE_FLUSH_INTERVAL_S = 2.0

try:  # POSIX only; elsewhere two migrations of one directory are not serialized.
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]


@dataclass
class VaultRuntime:
    """Mutable on purpose: tests swap ``indexer`` and ``usage`` in place."""

    spec: VaultSpec
    indexer: VaultIndexer
    usage: UsageTracker
    memory: MemoryClient = field(default_factory=NullMemoryClient)
    #: The judge that reorders this vault's ``search`` results; a :class:`NullJudge` when it declares none.
    judge: Judge = field(default_factory=NullJudge)
    #: Cache of judged blocks and circuit breaker of :attr:`judge`, for this vault only.
    reranker: Reranker = field(default_factory=Reranker)
    index_state: dict[str, bool] = field(default_factory=lambda: {"ready": False})
    hebbian_dirty: dict[str, int] = field(default_factory=lambda: {"count": 0})
    #: Held for each refresh of this vault (pull, then reindex) by the reindex
    #: webhook and the ``reindex`` tool alike: two ``git pull`` in one checkout
    #: at once make the second fail and leave its run on a stale tree.
    refresh_lock: threading.Lock = field(default_factory=threading.Lock, compare=False, repr=False)

    @property
    def vault_path(self) -> Path:
        return self.indexer.vault_path

    @property
    def embedder(self) -> EmbeddingProvider:
        """The provider the index and its queries are embedded with."""
        return self.indexer.embedder

    def retarget(self, vault_path: Path) -> None:
        """Point the same index at another copy of the vault (tests only)."""
        self.indexer.vault_path = vault_path

    def sync_vault(self) -> bool:
        """Refresh the vault on disk; ``False`` when nothing was pulled."""
        return self.indexer.source.sync()

    def sync_vault_outcome(self) -> str:
        """Refresh the vault on disk and say how it ended (``ports.SYNC_OUTCOMES``)."""
        source = self.indexer.source
        report = getattr(source, "sync_outcome", None)
        if callable(report):
            return str(report())
        return SYNC_PULLED if source.sync() else SYNC_UP_TO_DATE


def embedder_for(emb: EmbeddingSettings) -> EmbeddingProvider:
    """The embeddings provider a vault's settings select."""
    return build_embedder(emb.provider, model=emb.model, api_key=emb.api_key)


def build_runtime(
    spec: VaultSpec,
    *,
    sync: VaultSync | None = None,
    store: VectorStore | None = None,
    embedder: EmbeddingProvider | None = None,
    memory: MemoryClient | None = None,
    judge: Judge | None = None,
) -> VaultRuntime:
    """Open the indexer and usage tracker for ``spec`` without indexing."""
    vault_path = spec.require_path()
    if embedder is None:
        embedder = embedder_for(spec.embeddings)
    if store is None:
        store = open_store(spec)
    else:
        spec.index_dir.mkdir(parents=True, exist_ok=True)
    indexer = VaultIndexer(
        source=DirectoryVaultSource(vault_path, sync=sync),
        store=store,
        embedder=embedder,
        state_dir=spec.index_dir,
    )
    usage = UsageTracker(spec.index_dir / "usage_stats.json", flush_interval_s=USAGE_FLUSH_INTERVAL_S)
    return VaultRuntime(
        spec=spec,
        indexer=indexer,
        usage=usage,
        memory=memory if memory is not None else NullMemoryClient(),
        judge=judge if judge is not None else NullJudge(),
    )


def open_store(spec: VaultSpec) -> VectorStore:
    """The SQLite index of ``spec``, in its index directory."""
    return SQLiteStore.open(spec.index_dir)


SyncFactory = Callable[[VaultSpec], VaultSync | None]
StoreFactory = Callable[[VaultSpec], VectorStore]
EmbedderFactory = Callable[[EmbeddingSettings], EmbeddingProvider]
MemoryFactory = Callable[[VaultSpec], MemoryClient]
JudgeFactory = Callable[[VaultSpec], Judge]


def _no_sync(spec: VaultSpec) -> VaultSync | None:
    del spec
    return None


def _null_memory(spec: VaultSpec) -> MemoryClient:
    del spec
    return NullMemoryClient()


def _null_judge(spec: VaultSpec) -> Judge:
    del spec
    return NullJudge()


class VaultRegistry:
    """One lazily opened :class:`VaultRuntime` per registered vault."""

    def __init__(self, specs: Sequence[VaultSpec], build: Callable[[VaultSpec], VaultRuntime]) -> None:
        if not specs:
            raise errors.KnowledgeConfigError(
                errors.E_VAULT_PATH_MISSING,
                "no vault is registered; set VAULT_PATH or KNOWLEDGE_VAULTS",
            )
        self._specs: dict[str, VaultSpec] = {spec.slug: spec for spec in specs}
        self._default = next((spec.slug for spec in specs if spec.is_default), specs[0].slug)
        self._build = build
        self._runtimes: dict[str, VaultRuntime] = {}
        self._lock = threading.Lock()

    @classmethod
    def from_settings(
        cls,
        s: Settings,
        *,
        sync_factory: SyncFactory = _no_sync,
        store_factory: StoreFactory = open_store,
        embedder_factory: EmbedderFactory = embedder_for,
        memory_factory: MemoryFactory = _null_memory,
        judge_factory: JudgeFactory = _null_judge,
    ) -> VaultRegistry:
        """A registry over every vault ``s`` declares, none of them opened yet.

        ``judge_factory`` runs when a vault is first opened, with its spec, and
        returns the judge of that vault; the default gives every vault a
        :class:`~knowledge.ports.NullJudge`.
        """

        def build(spec: VaultSpec) -> VaultRuntime:
            return build_runtime(
                spec,
                sync=sync_factory(spec),
                store=store_factory(spec),
                embedder=embedder_factory(spec.embeddings),
                memory=memory_factory(spec),
                judge=judge_factory(spec),
            )

        return cls(s.vaults, build)

    @classmethod
    def of(cls, *runtimes: VaultRuntime) -> VaultRegistry:
        """A registry over runtimes that are already open (the first is the default)."""
        by_slug = {rt.spec.slug: rt for rt in runtimes}

        def build(spec: VaultSpec) -> VaultRuntime:
            return by_slug[spec.slug]

        registry = cls([rt.spec for rt in runtimes], build)
        if runtimes:
            registry._default = runtimes[0].spec.slug
        registry._runtimes.update(by_slug)
        return registry

    def slugs(self) -> tuple[str, ...]:
        """The registered slugs, in declaration order."""
        return tuple(self._specs)

    def spec(self, slug: str) -> VaultSpec:
        try:
            return self._specs[slug]
        except KeyError:
            raise errors.KnowledgeConfigError(
                errors.E_VAULT_UNKNOWN,
                f"no vault named {slug!r} is registered; registered: {', '.join(self._specs)}",
            ) from None

    def get(self, slug: str) -> VaultRuntime:
        """The runtime of vault ``slug``, opened on first request."""
        spec = self.spec(slug)
        with self._lock:
            runtime = self._runtimes.get(slug)
            if runtime is None:
                runtime = self._build(spec)
                self._runtimes[slug] = runtime
            return runtime

    @property
    def default_slug(self) -> str:
        """The slug a tool serves without ``vault``; reading it opens nothing."""
        return self._default

    @property
    def default(self) -> VaultRuntime:
        """The runtime every tool serves."""
        return self.get(self._default)

    def opened(self) -> tuple[str, ...]:
        """The slugs whose runtime has been opened so far."""
        return tuple(slug for slug in self._specs if slug in self._runtimes)

    def default_if_open(self) -> VaultRuntime | None:
        """The runtime of the default vault when it is already open; never opens it."""
        with self._lock:
            return self._runtimes.get(self._default)

    def close(self) -> None:
        """Close the usage tracker of every opened runtime (writing what it has pending) and close its store."""
        with self._lock:
            runtimes = list(self._runtimes.values())
        for runtime in runtimes:
            try:
                runtime.usage.close()
            except Exception:
                log.exception("usage: flush on close failed for %s", runtime.spec.slug)
            close = getattr(runtime.indexer.store, "close", None)
            if callable(close):
                close()


# ---------------------------------------------------------------------------
# migrate-index
# ---------------------------------------------------------------------------

#: What ``migrate_index`` found and did, as ``outcome`` reports it.
MIGRATE_CREATED = "created"
MIGRATE_MIGRATED = "migrated"
MIGRATE_UP_TO_DATE = "up_to_date"
MIGRATE_UPGRADED = "upgraded"

#: File inside the index directory that serializes migrations.
MIGRATE_LOCK_FILE = ".migrate.lock"
#: The file that marks a directory as a Chroma persist directory.
CHROMA_FILE = "chroma.sqlite3"
#: Sidecars a Chroma index directory carries that the SQLite index keeps.
USAGE_FILE = "usage_stats.json"
GRAPH_FILE = "link_graph.json"


@dataclass(frozen=True)
class Migration:
    """How ``migrate_index`` left one vault's index directory."""

    vault: str
    outcome: str
    index_version: int
    source: str | None
    pruned: tuple[str, ...] | None = None

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "vault": self.vault,
            "outcome": self.outcome,
            "index_version": self.index_version,
            "from": self.source,
        }
        if self.pruned is not None:
            out["pruned"] = list(self.pruned)
        return out


@contextlib.contextmanager
def _locked(path: Path) -> Iterator[None]:
    if fcntl is None:
        yield
        return
    with path.open("a") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _write_atomically(path: Path, data: bytes) -> None:
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _copy_sidecars(origin: Path, index_dir: Path) -> None:
    """Copy usage statistics and the learned link weights from a Chroma directory.

    Edges are not copied: the indexer rebuilds them from the vault. A sidecar
    the index directory already has is never overwritten.
    """
    if origin.resolve() == index_dir.resolve():
        return
    usage = origin / USAGE_FILE
    if usage.is_file() and not (index_dir / USAGE_FILE).exists():
        _write_atomically(index_dir / USAGE_FILE, usage.read_bytes())
    graph = origin / GRAPH_FILE
    if graph.is_file() and not (index_dir / GRAPH_FILE).exists():
        weights = _learned_weights(graph)
        if weights is not None:
            _write_atomically(
                index_dir / GRAPH_FILE, json.dumps({"weights": weights}, ensure_ascii=False).encode("utf-8")
            )


def _learned_weights(graph: Path) -> dict[str, Any] | None:
    """The ``weights`` of a Chroma directory's ``link_graph.json``; ``None`` when it cannot be read.

    The weights are the only thing taken from it and the index rebuilds the
    edges anyway, so a truncated or foreign file costs the weights, never the
    migration.
    """
    try:
        data = json.loads(graph.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("Skipping the learned link weights of %s: %s", graph, exc)
        return None
    weights = data.get("weights", {}) if isinstance(data, dict) else None
    if not isinstance(weights, dict):
        log.warning("Skipping the learned link weights of %s: no weights object", graph)
        return None
    return weights


def _is_segment_dir(path: Path) -> bool:
    return path.is_dir() and (path / "data_level0.bin").is_file()


def prune_chroma(origin: Path, vault_paths: Sequence[Path]) -> tuple[str, ...]:
    """Delete Chroma's own files from ``origin``: ``chroma.sqlite3`` and its segment directories.

    Nothing else in the directory is touched (the sidecars and ``index.sqlite``
    may live there). Refused with ``E_PRUNE_REFUSED`` when ``origin`` is, or
    contains, a registered vault.
    """
    root = origin.resolve()
    for vault in vault_paths:
        resolved = vault.resolve()
        if resolved == root or root in resolved.parents:
            raise errors.KnowledgeConfigError(
                errors.E_PRUNE_REFUSED,
                f"{origin} is or contains the vault at {vault}; nothing was deleted",
            )
    removed: list[str] = []
    chroma = origin / CHROMA_FILE
    if chroma.is_file():
        chroma.unlink()
        removed.append(CHROMA_FILE)
    for child in sorted(origin.iterdir()) if origin.is_dir() else []:
        if _is_segment_dir(child):
            for entry in sorted(child.iterdir()):
                if entry.is_file():
                    entry.unlink()
            try:
                child.rmdir()
            except OSError as exc:
                log.warning("Left the Chroma segment directory %s in place: %s", child, exc)
                continue
            removed.append(child.name)
    return tuple(removed)


def migrate_index(
    spec: VaultSpec,
    *,
    from_chroma: Path | None = None,
    prune: bool = False,
    vault_paths: Sequence[Path] = (),
) -> Migration:
    """Bring ``spec``'s index directory to the current layout without embedding anything.

    * No ``index.sqlite``: an empty one is created (``created``), and when a
      Chroma directory is found (``from_chroma``, else ``spec.chroma_dir``)
      its usage statistics and learned link weights are copied first
      (``migrated``). The file is built under a temporary name and moved into
      place last, so an interrupted run leaves nothing a later run would take
      for a finished one. Its vectors are never read: the chunks they were
      cut from are not the ones this index stores.
    * An ``index.sqlite`` of this layout: nothing is written (``up_to_date``).
      One whose layout is only in its WAL is checkpointed and also
      ``up_to_date``.
    * An older layout: upgraded in place (``upgraded``).
    * A newer layout: ``E_INDEX_VERSION_NEWER``.
    * A file that is not a SQLite database: ``E_INDEX_UNREADABLE``.

    ``prune`` deletes Chroma's own files from the source directory afterwards
    (see :func:`prune_chroma`). The index fills on the first tool call or webhook.
    """
    index_dir = spec.index_dir
    index_dir.mkdir(parents=True, exist_ok=True)
    origin = from_chroma or spec.chroma_dir
    found_chroma = origin is not None and (origin / CHROMA_FILE).is_file()
    target = index_dir / INDEX_FILE
    with _locked(index_dir / MIGRATE_LOCK_FILE):
        version = read_schema_version(target)
        if version is not None and version > SCHEMA_VERSION:
            raise errors.KnowledgeConfigError(
                errors.E_INDEX_VERSION_NEWER,
                f"{target} has index layout {version}; this package reads up to {SCHEMA_VERSION}",
            )
        source: str | None = None
        if version == SCHEMA_VERSION:
            outcome = MIGRATE_UP_TO_DATE
        elif version is not None:
            # A header still at 0 may hide a current schema that sits in the
            # WAL of a process that never closed its store.
            live = read_live_schema_version(target)
            SQLiteStore(target).close()
            outcome = MIGRATE_UP_TO_DATE if live == SCHEMA_VERSION else MIGRATE_UPGRADED
        else:
            tmp = index_dir / f"{INDEX_FILE}.tmp"
            for leftover in (tmp, tmp.with_name(tmp.name + "-wal"), tmp.with_name(tmp.name + "-shm")):
                leftover.unlink(missing_ok=True)
            store = SQLiteStore(tmp)
            try:
                store.set_meta({"vault_slug": spec.slug})
                if found_chroma and origin is not None:
                    _copy_sidecars(origin, index_dir)
                    store.set_meta({"migrated_from": "chroma"})
                    source = "chroma"
            finally:
                store.close()
            os.replace(tmp, target)
            outcome = MIGRATE_MIGRATED if source else MIGRATE_CREATED
        pruned: tuple[str, ...] | None = None
        if prune:
            pruned = prune_chroma(origin, vault_paths) if found_chroma and origin is not None else ()
    return Migration(spec.slug, outcome, SCHEMA_VERSION, source, pruned)


__all__ = [
    "Migration",
    "VaultRegistry",
    "VaultRuntime",
    "build_runtime",
    "embedder_for",
    "migrate_index",
    "open_store",
    "parse_vaults_file",
]
