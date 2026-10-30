"""The ports and their adapters: embeddings, stores, vault sources, memory.

Each adapter is checked against its protocol; the two stores have their own
contract suite in ``test_stores.py``, and here the indexer runs over both. The
bag-of-words vectors are pinned with hand-counted terms: the golden master is
recorded with them, so a change in their arithmetic is a contract change, not
a refactor.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from knowledge import errors, gitsync, server, settings
from knowledge.embeddings import (
    HARD_TRUNCATE_CHARS,
    VOCABULARY,
    BagOfWordsEmbeddings,
    CachedQueryEmbedder,
    NullEmbeddings,
    OpenAIEmbeddings,
    build_embedder,
)
from knowledge.indexer import VaultIndexer
from knowledge.ports import (
    EmbeddingProvider,
    Judge,
    JudgeUnavailable,
    JudgeUnit,
    JudgeVerdict,
    MemoryClient,
    NullJudge,
    NullMemoryClient,
    VaultSource,
    VaultSync,
    VectorStore,
)
from knowledge.registry import VaultRegistry, build_runtime
from knowledge.stores import InMemoryVectorStore, SQLiteStore
from knowledge.vault_source import DirectoryVaultSource

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_VAULT = Path(__file__).parent / "fixtures" / "vault"

STORES = ("sqlite", "memory")


def _open_store(kind: str, tmp_path: Path) -> VectorStore:
    if kind == "sqlite":
        return SQLiteStore.open(tmp_path / f"index-{kind}")
    return InMemoryVectorStore()


# --- every adapter honours its port ---------------------------------------------


@pytest.mark.parametrize(
    ("factory", "port"),
    [
        pytest.param(lambda tmp: OpenAIEmbeddings(api_key="", model="m"), EmbeddingProvider, id="openai"),
        pytest.param(lambda tmp: NullEmbeddings(), EmbeddingProvider, id="none"),
        pytest.param(lambda tmp: BagOfWordsEmbeddings(), EmbeddingProvider, id="bag-of-words"),
        pytest.param(lambda tmp: SQLiteStore.open(tmp / "c"), VectorStore, id="sqlite"),
        pytest.param(lambda tmp: InMemoryVectorStore(), VectorStore, id="in-memory"),
        pytest.param(lambda tmp: DirectoryVaultSource(tmp), VaultSource, id="directory"),
        pytest.param(lambda tmp: gitsync.pull_vault, VaultSync, id="git-pull"),
        pytest.param(lambda tmp: NullMemoryClient(), MemoryClient, id="null-memory"),
        pytest.param(lambda tmp: NullJudge(), Judge, id="null-judge"),
    ],
)
def test_each_adapter_implements_its_port(factory: Any, port: type, tmp_path: Path) -> None:
    assert isinstance(factory(tmp_path), port)


# --- embeddings -----------------------------------------------------------------


def _bag(counts: dict[str, int]) -> list[float]:
    vec = [float(counts.get(word, 0)) for word in VOCABULARY]
    norm = sum(v * v for v in vec) ** 0.5
    return [v / norm for v in vec]


@pytest.mark.parametrize(
    ("text", "counts"),
    [
        ("Telvia OTP login", {"telvia": 1, "otp": 1, "login": 1}),
        ("middleware outage, outage runbook", {"middleware": 1, "outage": 2, "runbook": 1}),
        ("Lookup Service: lookup by token", {"lookup": 2, "service": 1, "token": 1}),
        ("nothing in the vocabulary", {"telvia": 1}),
    ],
)
def test_bag_of_words_vectors_are_pinned(text: str, counts: dict[str, int]) -> None:
    assert BagOfWordsEmbeddings().embed([text]) == [_bag(counts)]


def test_the_bag_of_words_vocabulary_is_pinned() -> None:
    assert VOCABULARY == (
        "telvia", "comet", "argentina", "mexico", "otp", "login", "session",
        "outage", "middleware", "runbook", "lookup", "service", "dashboard",
        "portal", "architecture", "token", "gateway",
    )


@pytest.mark.parametrize(
    ("provider", "name", "semantic"),
    [("openai", "openai", True), ("none", "none", False), ("bag-of-words", "bag-of-words", True), (" Bag-Of-Words ", "bag-of-words", True)],
)
def test_build_embedder_selects_the_provider(provider: str, name: str, semantic: bool) -> None:
    embedder = build_embedder(provider, model="text-embedding-3-large", api_key="")
    assert (embedder.name, embedder.semantic) == (name, semantic)


def test_an_unknown_provider_is_a_coded_error() -> None:
    with pytest.raises(errors.KnowledgeConfigError) as exc_info:
        build_embedder("ollama")
    assert exc_info.value.code == errors.E_EMBEDDINGS_PROVIDER_UNKNOWN


def test_the_null_provider_embeds_a_constant_vector() -> None:
    vectors = NullEmbeddings().embed(["a", "b"])
    assert vectors == [[1.0], [1.0]]
    vectors[0].append(2.0)
    assert vectors[1] == [1.0], "each text gets its own list"


class _FakeOpenAIClient:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], str]] = []
        self.embeddings = self

    def create(self, *, input: list[str], model: str) -> SimpleNamespace:  # noqa: A002 - OpenAI's keyword
        self.calls.append((input, model))
        return SimpleNamespace(data=[SimpleNamespace(embedding=[float(len(t))]) for t in input])


def test_openai_truncates_inside_the_adapter() -> None:
    client = _FakeOpenAIClient()
    provider = OpenAIEmbeddings(api_key="unused", model="text-embedding-3-large", client=client)

    vectors = provider.embed(["x" * (HARD_TRUNCATE_CHARS + 50), "short"])

    assert vectors == [[float(HARD_TRUNCATE_CHARS)], [5.0]]
    assert client.calls == [(["x" * HARD_TRUNCATE_CHARS, "short"], "text-embedding-3-large")]


def test_openai_repr_never_shows_the_key() -> None:
    assert "sk-secret" not in repr(OpenAIEmbeddings(api_key="sk-secret", model="m"))


class _CountingProvider(BagOfWordsEmbeddings):
    def __init__(self) -> None:
        super().__init__()
        self.seen: list[str] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.seen.extend(texts)
        return super().embed(texts)


def test_query_embeddings_are_normalised_and_cached() -> None:
    provider = _CountingProvider()
    cached = CachedQueryEmbedder(provider)

    first = cached.embed("  Telvia   OTP ")
    second = cached.embed("telvia otp")

    assert first == second == BagOfWordsEmbeddings().embed(["telvia otp"])[0]
    assert provider.seen == ["telvia otp"]


def test_replacing_the_indexer_provider_replaces_the_query_cache(tmp_path: Path) -> None:
    indexer = VaultIndexer(
        source=DirectoryVaultSource(tmp_path),
        store=InMemoryVectorStore(),
        embedder=BagOfWordsEmbeddings(),
        state_dir=tmp_path,
    )
    before = indexer.embed_query("telvia")
    indexer.embedder = BagOfWordsEmbeddings(vocabulary=("otp", "telvia"))

    assert indexer.embed_query("telvia") == [0.0, 1.0]
    assert before != indexer.embed_query("telvia")


def _close(values: list[float]) -> Any:
    # Both stores keep vectors as float32.
    return pytest.approx(values, abs=1e-5)


# --- the indexer over each store -----------------------------------------------------


def _indexer(kind: str, tmp_path: Path, vault: Path) -> VaultIndexer:
    state = tmp_path / f"state-{kind}"
    state.mkdir()
    return VaultIndexer(
        source=DirectoryVaultSource(vault),
        store=_open_store(kind, tmp_path),
        embedder=BagOfWordsEmbeddings(),
        state_dir=state,
    )


@pytest.fixture()
def vault_copy(tmp_path: Path) -> Path:
    copy = tmp_path / "vault"
    shutil.copytree(FIXTURE_VAULT, copy)
    return copy


def test_the_indexer_builds_the_same_index_on_either_store(tmp_path: Path, vault_copy: Path) -> None:
    sqlite, memory = (_indexer(kind, tmp_path, vault_copy) for kind in STORES)

    stats = [ix.index(force=True, target="vault") for ix in (sqlite, memory)]
    assert stats[0] == stats[1]
    assert stats[0]["files"] == 14

    assert sqlite.store.documents() == memory.store.documents()
    assert sqlite.store.chunks(sqlite.store.file_hashes()) == memory.store.chunks(memory.store.file_hashes())
    query = sqlite.embed_query("middleware outage runbook")
    hits = [ix.store.knn(query, 5) for ix in (sqlite, memory)]
    assert [(h.file_path, h.ord) for h in hits[0]] == [(h.file_path, h.ord) for h in hits[1]]
    assert [h.score for h in hits[0]] == _close([h.score for h in hits[1]])
    assert sqlite.keyword_index.docs == memory.keyword_index.docs


@pytest.mark.parametrize("kind", STORES)
def test_an_index_built_by_another_provider_is_rebuilt_in_full(
    kind: str, tmp_path: Path, vault_copy: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    indexer = _indexer(kind, tmp_path, vault_copy)
    indexer.index(force=True, target="vault")
    meta = json.loads((tmp_path / f"state-{kind}" / "index_meta.json").read_text())
    assert meta["embedding"] == {"provider": "bag-of-words", "model": "bag-of-words"}
    assert indexer.store.get_meta()["provider"] == "bag-of-words"
    assert indexer.store.get_meta()["dim"] == str(len(VOCABULARY))

    indexer.embedder = NullEmbeddings()
    assert set(indexer.stale_identity()) == {"provider", "model"}
    # A library configuring logging earlier in the run (dictConfig with its
    # default disable_existing_loggers) can leave this logger disabled.
    monkeypatch.setattr(logging.getLogger("knowledge.indexer"), "disabled", False)
    with caplog.at_level(logging.WARNING, logger="knowledge.indexer"):
        stats = indexer.index(force=False, target="vault")
    assert any(errors.E_INDEX_PROVIDER_MISMATCH in r.getMessage() for r in caplog.records)
    assert (stats["updated"], stats["unchanged"]) == (14, 0), "every file is written again"
    assert indexer.stale_identity() == {}
    assert "dim" not in indexer.store.get_meta(), "the none provider writes no vectors"
    assert indexer.store.knn([1.0] * len(VOCABULARY), 3) == []


# --- vault source ---------------------------------------------------------------------


def test_the_directory_source_lists_markdown_in_sorted_order(vault_copy: Path) -> None:
    files = DirectoryVaultSource(vault_copy).markdown_files()
    assert files == sorted(vault_copy.rglob("*.md"))
    assert len(files) == 14


def test_a_source_without_sync_pulls_nothing(tmp_path: Path) -> None:
    assert DirectoryVaultSource(tmp_path).sync() is False


def test_a_retargeted_runtime_keeps_its_sync(tmp_path: Path, vault_copy: Path) -> None:
    calls: list[Path] = []

    def _sync(root: Path) -> bool:
        calls.append(root)
        return True

    spec = dataclasses.replace(settings.current().default_vault(), path=vault_copy, index_dir=tmp_path / "rt")
    rt = build_runtime(spec, sync=_sync, store=InMemoryVectorStore(), embedder=BagOfWordsEmbeddings())
    other = tmp_path / "other"
    other.mkdir()
    rt.retarget(other)

    assert rt.sync_vault() is True
    assert calls == [other]
    assert rt.vault_path == other


# --- memory -----------------------------------------------------------------------------


def test_the_null_memory_client_finds_nothing(tmp_path: Path) -> None:
    assert NullMemoryClient().search("anything", project="knowledge") == []
    spec = dataclasses.replace(settings.current().default_vault(), index_dir=tmp_path / "rt")
    rt = build_runtime(spec, store=InMemoryVectorStore(), embedder=BagOfWordsEmbeddings())
    assert isinstance(rt.memory, NullMemoryClient)


# --- judge ------------------------------------------------------------------------------


def test_the_null_judge_is_never_a_silent_zero() -> None:
    judge = NullJudge()

    assert (judge.name, judge.model) == ("none", "")
    with pytest.raises(JudgeUnavailable) as excinfo:
        judge.judge("how do I create a tenant", [JudgeUnit("a", "Doc", "Steps", "text")], timeout_s=1.5)
    assert excinfo.value.reason == "unavailable"


def test_a_vault_runtime_carries_the_null_judge_unless_given_one(tmp_path: Path) -> None:
    spec = dataclasses.replace(settings.current().default_vault(), index_dir=tmp_path / "rt")

    assert isinstance(build_runtime(spec, store=InMemoryVectorStore(), embedder=BagOfWordsEmbeddings()).judge, NullJudge)

    class Fixed:
        name = "fixed"
        model = "fixed-1"

        def judge(self, query: str, units: Any, *, timeout_s: float) -> JudgeVerdict:
            return JudgeVerdict({u.key: 1.0 for u in units}, 0, self.model)

    given = Fixed()
    rt = build_runtime(spec, store=InMemoryVectorStore(), embedder=BagOfWordsEmbeddings(), judge=given)
    assert rt.judge is given


def test_the_judge_records_are_immutable_values() -> None:
    unit = JudgeUnit("k", "Doc", "", "text")
    verdict = JudgeVerdict({"k": 0.5}, 12, "m")

    with pytest.raises(dataclasses.FrozenInstanceError):
        unit.text = "other"  # type: ignore[misc]
    assert verdict == JudgeVerdict({"k": 0.5}, 12, "m")
    assert JudgeVerdict({}).input_tokens == 0


# --- the server with the "none" provider ---------------------------------------------


@pytest.fixture()
def keyword_only_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    monkeypatch.setenv("KNOWLEDGE_EMBEDDINGS_PROVIDER", "none")
    spec = dataclasses.replace(settings.load_settings().default_vault(), index_dir=tmp_path / "none")
    assert spec.embeddings.provider == "none"
    rt = build_runtime(spec, store=InMemoryVectorStore())
    monkeypatch.setattr(server, "_registry", VaultRegistry.of(rt))
    yield server


def test_the_none_provider_serves_keyword_search(keyword_only_server: Any) -> None:
    assert keyword_only_server.default_runtime().embedder.name == "none"

    hybrid = asyncio.run(keyword_only_server.search("middleware outage", limit=3))
    assert hybrid
    assert hybrid[0]["file_path"] == "Runbooks/Runbook - Middleware Outage.md"

    assert asyncio.run(keyword_only_server.search("middleware", mode="semantic")) == []
    assert len(asyncio.run(keyword_only_server.search_by_metadata(type="service"))["items"]) == 3
    assert asyncio.run(keyword_only_server.search_deep("middleware outage", depth=1))


# --- importing stays light ---------------------------------------------------------------


def test_importing_the_server_loads_neither_chroma_nor_openai() -> None:
    """Importing ``knowledge.*`` loads neither openai (until a provider embeds) nor chromadb.

    chromadb is not a dependency: an import of it would fail on a clean install.
    """
    probe = (
        "import sys\n"
        "import knowledge.server\n"
        "import knowledge.cli\n"
        "from knowledge.embeddings import build_embedder\n"
        "build_embedder('openai', model='m', api_key='')\n"
        "heavy = sorted(m for m in ('chromadb', 'openai') if m in sys.modules)\n"
        "print('HEAVY=' + ','.join(heavy))\n"
        "sys.exit(1 if heavy else 0)\n"
    )
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


# --- an index of another configuration, and reads during a reindex -------------------


def _sqlite_server(tmp_path: Path, vault: Path, monkeypatch: pytest.MonkeyPatch, embedder: Any) -> Any:
    spec = dataclasses.replace(settings.current().default_vault(), path=vault, index_dir=tmp_path / "idx")
    rt = build_runtime(spec, embedder=embedder)
    monkeypatch.setattr(server, "_registry", VaultRegistry.of(rt))
    return rt


def test_the_first_tool_rebuilds_an_index_of_another_provider_before_answering(
    tmp_path: Path, vault_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = _sqlite_server(tmp_path, vault_copy, monkeypatch, BagOfWordsEmbeddings())
    built.indexer.index()
    assert built.indexer.store.get_meta()["provider"] == "bag-of-words"
    built.indexer.store.close()  # type: ignore[attr-defined]

    rt = _sqlite_server(tmp_path, vault_copy, monkeypatch, NullEmbeddings())
    assert rt.indexer.stale_identity() != {}

    rows = asyncio.run(server.search("middleware outage", limit=3))

    assert rows and rows[0]["file_path"] == "Runbooks/Runbook - Middleware Outage.md"
    meta = rt.indexer.store.get_meta()
    assert (meta["provider"], "dim" in meta) == ("none", False)
    assert rt.indexer.stale_identity() == {}


def test_searches_keep_answering_while_the_index_is_rebuilt(
    tmp_path: Path, vault_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    rt = _sqlite_server(tmp_path, vault_copy, monkeypatch, BagOfWordsEmbeddings())
    rt.indexer.index()
    rt.index_state["ready"] = True
    stop = threading.Event()
    failures: list[str] = []

    def reindex() -> None:
        try:
            for _ in range(5):
                rt.indexer.index(force=True)
        finally:
            stop.set()

    def search() -> None:
        while not stop.is_set():
            try:
                rows = server._search_impl("middleware outage", limit=3)
            except Exception as exc:  # noqa: BLE001 - any failure is the finding
                failures.append(repr(exc))
                return
            if not rows or rows[0]["file_path"] != "Runbooks/Runbook - Middleware Outage.md":
                failures.append(f"rows {rows}")
                return

    threads = [threading.Thread(target=reindex), *(threading.Thread(target=search) for _ in range(3))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)

    assert failures == []
