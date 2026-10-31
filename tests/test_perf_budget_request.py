"""Work budgets of the request path (stream C): exact counts, never a clock.

What a tool call may do besides answering, counted with ``tests/perf_counters.py``:

* the usage file: one read per write, no write on a request thread, no rescan
  of every used file in ``boost`` (1.5);
* the link graph: learned weights saved on the graph-save worker, at most one
  save per ten changes, coalesced per runtime, as compact JSON (1.6);
* the server shuts down with every count and weight written.

Every vault here is generated in ``tmp_path`` or copied from the fixture, with
the ``bag-of-words`` provider and no network. Nothing is patched.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import math
import os
import random
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import Implementation
from perf_counters import count_calls, file_opens, sql_trace
from synthetic_vault import Manifest, generate, queries

from knowledge import registry, server, settings, usage
from knowledge.graph import LinkGraph
from knowledge.registry import VaultRuntime
from knowledge.stores import SQLiteStore
from knowledge.usage import UsageTracker

TESTS_DIR = Path(__file__).resolve().parent
FIXTURE_VAULT = TESTS_DIR / "fixtures" / "vault"
SERVER_SCRIPT = TESTS_DIR / "golden_stdio_server.py"
USAGE_TMP = "usage_stats.json.*.tmp"
GRAPH_TMP = "link_graph.json.*.tmp"


def _wait_until(predicate: Any, *, seconds: float = 60.0) -> None:
    """Synchronise with a background thread; the bound only keeps a broken one from hanging the suite."""
    deadline = time.monotonic() + seconds
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("the background thread never got there")
        time.sleep(0.02)


def _compact(text: str) -> bool:
    """``text`` is JSON written with no whitespace between tokens."""
    return "\n" not in text and text == json.dumps(json.loads(text), ensure_ascii=False, separators=(",", ":"))


@contextmanager
def _graph_worker_held() -> Iterator[None]:
    """Keep the graph-save worker busy until the block ends, so saves queue up behind it."""
    release = threading.Event()
    started = threading.Event()

    def hold() -> None:
        started.set()
        release.wait(timeout=60)

    held = server._GRAPH_SAVES.submit(hold)
    assert started.wait(timeout=60)
    try:
        yield
    finally:
        release.set()
        held.result(timeout=60)


def _seed_usage(path: Path, *, files: int = 300, pairs: int = 2000, seed: int = 3) -> None:
    """A usage file with some history, written the way the tracker writes it."""
    rng = random.Random(seed)
    names = [f"Docs/doc-{n:04d}.md" for n in range(files)]
    tracker = UsageTracker(path)
    for _ in range(pairs // 10):
        picked = rng.sample(names, 5)
        tracker.record_uses(picked)
        tracker.record_pairs(picked)
    tracker.flush()


@contextmanager
def _served(tmp_path: Path, *, docs: int = 60) -> Iterator[tuple[VaultRuntime, Manifest]]:
    """A registry-built runtime over a generated vault, installed on the server module for one test."""
    manifest = generate(tmp_path / "vault", docs=docs, seed=7)
    loaded = settings.load_settings(
        {
            "VAULT_PATH": str(tmp_path / "vault"),
            "KNOWLEDGE_INDEX_DIR": str(tmp_path / "index"),
            "KNOWLEDGE_EMBEDDINGS_PROVIDER": "bag-of-words",
        },
        project_root=tmp_path,
    )
    settings.install(loaded)
    reg = server._registry_for(loaded)
    server.set_registry(reg)
    try:
        yield reg.default, manifest
    finally:
        reg.close()
        server.set_registry(None)
        settings.reset_current()


# ---------------------------------------------------------------------------
# 1.5 usage file: one read per write, never on a request thread
# ---------------------------------------------------------------------------


def test_a_flush_reads_the_usage_file_once(tmp_path: Path) -> None:
    path = tmp_path / "usage_stats.json"
    _seed_usage(path)
    tracker = UsageTracker(path)
    tracker.record_call("search", client="c", principal="stdio", error=False, latency_ms=4.0)

    with file_opens(tmp_path) as flushed:
        tracker.flush()
    with file_opens(tmp_path) as clean:
        tracker.flush_if_dirty()
    with file_opens(tmp_path) as paid:
        tracker.record_rerank(applied=True, reason=None, units=4, usd=0.01)

    assert flushed.reads("usage_stats.json") == 1 and len(flushed.writes(USAGE_TMP)) == 1
    assert clean.events == []
    assert paid.reads("usage_stats.json") == 1 and len(paid.writes(USAGE_TMP)) == 1


def test_an_inline_tracker_writes_once_every_twenty_hits(tmp_path: Path) -> None:
    path = tmp_path / "usage_stats.json"
    _seed_usage(path)
    tracker = UsageTracker(path)

    with file_opens(tmp_path) as opens, count_calls(UsageTracker._write) as writes:
        for n in range(200):
            tracker.record_uses([f"Docs/doc-{n % 7:04d}.md"])

    assert writes.count() == 10
    assert opens.reads("usage_stats.json") == 10
    assert opens.writes(USAGE_TMP) == ["MainThread"] * 10


def test_boost_never_rescans_every_used_file(tmp_path: Path) -> None:
    path = tmp_path / "usage_stats.json"
    _seed_usage(path)

    with count_calls(UsageTracker._recompute_max) as loaded:
        tracker = UsageTracker(path)
    with count_calls(UsageTracker._recompute_max, UsageTracker.boost) as calls:
        for n in range(50):
            tracker.record_uses([f"Docs/doc-{n:04d}.md"], weight=2.0)
            for m in range(20):
                tracker.boost(f"Docs/doc-{(n + m) % 300:04d}.md")

    assert loaded.count() == 1  # the load scans once; no decay is due
    assert calls.count("boost") == 1000 and calls.count("_recompute_max") == 0


def test_tool_calls_never_write_the_usage_file_on_a_request_thread(tmp_path: Path) -> None:
    with _served(tmp_path) as (rt, manifest):
        index = rt.spec.index_dir
        qs = queries(manifest, seed=1, n=20)
        docs = list(manifest.paths)

        async def calls() -> None:
            for n in range(100):
                await server.search(query=qs[n % len(qs)], limit=5)
                await server.get_document(file_path=docs[n % len(docs)], max_chars=800)

        rt.indexer.index(force=False, target="vault")
        with file_opens(index) as opens:
            asyncio.run(calls())
            _wait_until(lambda: "usage-flush" in opens.writes(USAGE_TMP))  # the 2 s timer fires
            during = list(opens.writes(USAGE_TMP))
            rt.usage.close()
        at_close = opens.writes(USAGE_TMP)[len(during) :]

        assert during and set(during) == {"usage-flush"}
        assert at_close in ([], ["MainThread"])
        assert not any(name.startswith("asyncio") for name in opens.writes("usage_stats.json*"))
        saved = json.loads((index / "usage_stats.json").read_text())["files"]
        assert sum(entry["uses"] for entry in saved.values()) == pytest.approx(
            sum(rt.usage.uses(fp) for fp in saved)
        )


def test_the_registry_builds_trackers_with_a_background_writer_and_closes_them(tmp_path: Path) -> None:
    with _served(tmp_path) as (rt, _manifest):
        rt.usage.record_uses(["a.md"])
        flusher = rt.usage._flusher
        assert flusher is not None and flusher.name == "usage-flush"
        assert registry.USAGE_FLUSH_INTERVAL_S == 2.0
    assert not flusher.is_alive()
    assert UsageTracker(tmp_path / "index" / "usage_stats.json").uses("a.md") == 1.0


# ---------------------------------------------------------------------------
# 1.6 link graph: saved off the request thread, coalesced, compact
# ---------------------------------------------------------------------------


def test_learned_weights_are_saved_on_the_graph_save_worker(tmp_path: Path) -> None:
    with _served(tmp_path, docs=80) as (rt, manifest):
        index = rt.spec.index_dir
        rt.indexer.index(force=False, target="vault")
        qs = queries(manifest, seed=2, n=4)

        async def searches() -> None:
            for n in range(200):
                await server.search(query=qs[n % len(qs)], limit=10)

        with file_opens(index) as opens, count_calls(LinkGraph.save, LinkGraph.reinforce) as calls:
            asyncio.run(searches())
            server.drain_graph_saves(timeout=60)

        writers = opens.writes(GRAPH_TMP)
        assert writers and all(name.startswith("graph-save") for name in writers)
        assert calls.threads("save") <= set(writers)
        saves, reinforced = calls.count("save"), calls.count("reinforce")
        assert 1 <= saves <= math.ceil(reinforced / server._HEBBIAN_SAVE_EVERY)
        text = (index / "link_graph.json").read_text()
        assert _compact(text) and "\n " not in text
        assert json.loads(text)["weights"]


def test_queued_saves_of_one_runtime_coalesce_into_one(tmp_path: Path) -> None:
    with _served(tmp_path) as (rt, _manifest):
        rt.indexer.index(force=False, target="vault")
        with count_calls(LinkGraph.save) as saves:
            with _graph_worker_held():
                for _ in range(5):
                    server._queue_graph_save(rt)
            server.drain_graph_saves(timeout=60)
            assert saves.count() == 1
            server._queue_graph_save(rt)  # the earlier save has run: this one is queued again
            server.drain_graph_saves(timeout=60)
        assert saves.count() == 2
        assert saves.threads() == {"graph-save_0"}


def test_a_save_queued_before_a_reindex_never_writes_the_old_graph_last(tmp_path: Path) -> None:
    with _served(tmp_path) as (rt, _manifest):
        rt.indexer.index(force=False, target="vault")
        graph_file = rt.spec.index_dir / "link_graph.json"
        old = rt.indexer.graph
        with _graph_worker_held():
            server._queue_graph_save(rt)
            (tmp_path / "vault" / "Linked Later.md").write_text(
                "---\ntitle: Linked Later\ntype: note\n---\n# Linked Later\n\nSee [[" + Path(_manifest.paths[0]).stem + "]].\n"
            )
            rt.indexer.index(force=False, target="vault")  # swaps in a new graph and saves it
        server.drain_graph_saves(timeout=60)

        assert rt.indexer.graph is not old
        on_disk = LinkGraph.load(graph_file)
        assert on_disk._outgoing == rt.indexer.graph._outgoing
        assert "Linked Later.md" in on_disk._outgoing
        assert _compact(graph_file.read_text())


def test_the_hebbian_save_cadence_is_unchanged(tmp_path: Path) -> None:
    """Ten changes queue one save; nine wait for the next search that changes a weight."""
    assert server._HEBBIAN_SAVE_EVERY == 10
    with _served(tmp_path, docs=80) as (rt, manifest):
        rt.indexer.index(force=False, target="vault")
        qs = queries(manifest, seed=2, n=4)
        with count_calls(server._queue_graph_save, LinkGraph.reinforce) as calls:
            for n in range(60):
                asyncio.run(server.search(query=qs[n % len(qs)], limit=10))
        server.drain_graph_saves(timeout=60)
        assert calls.count("_queue_graph_save") <= math.ceil(calls.count("reinforce") / 10)
        assert rt.hebbian_dirty["count"] < server._HEBBIAN_SAVE_EVERY


# ---------------------------------------------------------------------------
# 1.7b documents listed without their bodies
# ---------------------------------------------------------------------------

#: Whether the store can leave the bodies out (stream B, item 1.7a).
STORE_READS_WITHOUT_BODY = "with_body" in inspect.signature(SQLiteStore.documents).parameters


class _StoreWithoutKeyword:
    """A store written before ``with_body``: it must still be called the old way."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def documents(self, flt: Any = None) -> list[Any]:
        self.calls.append((flt,))
        return []


class _StoreWithKeyword(_StoreWithoutKeyword):
    def documents(self, flt: Any = None, *, with_body: bool = True) -> list[Any]:  # type: ignore[override]
        self.calls.append((flt, with_body))
        return []


class _ForwardingStore(_StoreWithoutKeyword):
    def documents(self, flt: Any = None, **kwargs: Any) -> list[Any]:  # type: ignore[override]
        self.calls.append((flt, *kwargs))
        return []


def test_the_body_is_left_out_only_where_the_store_takes_the_keyword() -> None:
    flt = server.ChunkFilter(equals={"type": "runbook"})
    old, new, forwarding = _StoreWithoutKeyword(), _StoreWithKeyword(), _ForwardingStore()

    for store in (old, new, forwarding, old, new):
        assert server._documents_without_body(store, flt) == []

    assert old.calls == [(flt,), (flt,)]
    assert new.calls == [(flt, False), (flt, False)]
    assert forwarding.calls == [(flt,)]


def test_every_tool_that_lists_documents_asks_for_them_without_bodies(tmp_path: Path) -> None:
    with _served(tmp_path) as (rt, manifest):
        rt.indexer.index(force=False, target="vault")
        qs = queries(manifest, seed=3, n=2)

        async def tools() -> None:
            await server.search(query=qs[0], limit=5)
            await server.search_deep(query=qs[1], limit=5)
            await server.search_by_metadata(type="runbook", limit=3)
            await server.get_stale_docs()

        with count_calls(server._documents_without_body, rt.indexer.store.documents) as calls:
            asyncio.run(tools())

    # search: 1, search_deep: its seed search and its hops, search_by_metadata: 1, get_stale_docs: 2.
    assert calls.count("_documents_without_body") >= 5
    assert calls.count("documents") == calls.count("_documents_without_body")


@pytest.mark.skipif(not STORE_READS_WITHOUT_BODY, reason="needs SQLiteStore.documents(with_body=False), item 1.7a")
def test_no_tool_that_lists_documents_reads_their_bodies(tmp_path: Path) -> None:
    with _served(tmp_path) as (rt, manifest):
        rt.indexer.index(force=False, target="vault")
        qs = queries(manifest, seed=3, n=4)
        asyncio.run(server.search(query=qs[0], limit=5))  # builds the keyword index outside the count

        async def tools() -> None:
            await server.search(query=qs[1], limit=5)
            await server.search_deep(query=qs[2], limit=5)
            await server.search_by_metadata(type="runbook", limit=3)
            await server.search_by_metadata(tag="area/a1", limit=3)
            await server.get_stale_docs()

        with sql_trace(rt.indexer.store) as trace:
            asyncio.run(tools())

    assert trace.count(r"FROM docs") > 0
    assert trace.selects_column("docs", "body") == 0
    assert trace.rows_with_column("body") == 0


# ---------------------------------------------------------------------------
# Shutdown: a real stdio server writes every count on its way out
# ---------------------------------------------------------------------------


def _stdio_env(index: Path) -> dict[str, str]:
    return {
        "VAULT_PATH": str(FIXTURE_VAULT),
        "KNOWLEDGE_INDEX_DIR": str(index),
        "MCP_TRANSPORT": "stdio",
        "MEMORY_HTTP_TOKEN": "",
        "KNOWLEDGE_MEMORY_URL": "",
        "CONFLUENCE_URL": "",
        "CONFLUENCE_EMAIL": "",
        "CONFLUENCE_TOKEN": "",
        "KNOWLEDGE_STRUCTURED_OUTPUT": "",
        "KNOWLEDGE_PROFILE": "",
        "KNOWLEDGE_CONFIG_JSON": os.environ["KNOWLEDGE_CONFIG_JSON"],
        "PYTHONHASHSEED": "0",
    }


async def _stdio_calls(index: Path, calls: list[tuple[str, dict[str, Any]]]) -> list[Any]:
    params = StdioServerParameters(
        command=sys.executable, args=[str(SERVER_SCRIPT)], env=_stdio_env(index), cwd=str(TESTS_DIR.parent)
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(
            read,
            write,
            read_timeout_seconds=timedelta(seconds=180),
            client_info=Implementation(name="shutdown-probe", version="0"),
        ) as session:
            await session.initialize()
            return [await session.call_tool(name, arguments=arguments) for name, arguments in calls]


def test_a_stdio_server_writes_its_pending_usage_when_it_exits(tmp_path: Path) -> None:
    index = tmp_path / "index"
    results = asyncio.run(
        _stdio_calls(index, [("search", {"query": "middleware outage", "limit": 3}), ("list_vaults", {})])
    )

    assert [r.isError for r in results] == [False, False]
    raw = json.loads((index / "usage_stats.json").read_text())
    rows = [json.loads(block.text) for block in results[0].content]
    assert {fp: e["uses"] for fp, e in raw["files"].items()} == {row["file_path"]: 1.0 for row in rows}
    calls: dict[str, int] = {}
    for tools in raw["tool_calls"]["days"].values():
        for tool, clients in tools.items():
            calls[tool] = calls.get(tool, 0) + clients["shutdown-probe"][usage.STDIO_PRINCIPAL]["calls"]
    assert calls == {"search": 1, "list_vaults": 1}
    assert not list(index.glob("*.tmp"))


def test_a_stdio_server_leaves_its_learned_weights_on_disk_when_it_exits(tmp_path: Path) -> None:
    index = tmp_path / "index"
    search = ("search", {"query": "middleware outage login session", "limit": 10})
    results = asyncio.run(_stdio_calls(index, [search] * 15))

    assert not any(r.isError for r in results)
    text = (index / "link_graph.json").read_text()
    assert _compact(text)
    assert json.loads(text)["weights"], "fifteen identical searches over linked documents learn some weight"
    days = json.loads((index / "usage_stats.json").read_text())["tool_calls"]["days"]
    counted = sum(tools["search"]["shutdown-probe"][usage.STDIO_PRINCIPAL]["calls"] for tools in days.values())
    assert counted == 15
    assert not list(index.glob("*.tmp"))
