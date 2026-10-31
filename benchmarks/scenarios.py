"""Worker side of the harness: scenarios that import ``knowledge`` and run in a clean subprocess.

``benchmarks/run.py`` starts one worker per group with the benchmark
environment (``common.bench_env``)::

    python benchmarks/scenarios.py <group> --spec spec.json

and reads the JSON result the worker writes to ``spec["result"]``. Groups:

* ``ensure-index``: build the index of the vault when it has none;
* ``index``: full build, no-op reindex and one-changed reindex;
* ``tools``: ``search`` / ``get_document`` / ``search_deep`` / ``context_pack`` through the
  server's own async wrappers, the way FastMCP awaits them;
* ``usage``: the cost of ``UsageTracker`` persistence at a given history size.

A worker never touches the network and never opens any vault but the one in its spec.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import shutil
import sys
import time
import traceback
from pathlib import Path
from typing import Any

_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from benchmarks.common import rss_mib, sample, summarize  # noqa: E402


def _indexer(vault: Path, index_dir: Path) -> Any:
    from knowledge.embeddings import build_embedder
    from knowledge.indexer import VaultIndexer

    embedder = build_embedder("bag-of-words", model=None, api_key=None)
    return VaultIndexer.open(vault_path=vault, index_dir=index_dir, embedder=embedder)


def _close(indexer: Any) -> None:
    close = getattr(indexer.store, "close", None)
    if callable(close):
        close()


def _timed(fn: Any) -> float:
    started = time.perf_counter()
    fn()
    return (time.perf_counter() - started) * 1000.0


# ---------------------------------------------------------------------------
# index
# ---------------------------------------------------------------------------


def ensure_index(spec: dict[str, Any]) -> dict[str, Any]:
    vault, index_dir = Path(spec["vault"]), Path(spec["index"])
    index_dir.mkdir(parents=True, exist_ok=True)
    indexer = _indexer(vault, index_dir)
    built = False
    if not indexer.is_indexed():
        indexer.index()
        built = True
    chunks = indexer.store.count()
    _close(indexer)
    return {"results": {}, "extra": {"built": built, "chunks": chunks}}


def index_group(spec: dict[str, Any]) -> dict[str, Any]:
    """Full build (fresh index each rep), no-op reindex and a reindex after one edited file."""
    vault, index_dir, scratch = Path(spec["vault"]), Path(spec["index"]), Path(spec["scratch"])
    docs = int(spec["docs"])
    build_reps, reps = int(spec["build_reps"]), int(spec["reps"])
    paths: list[str] = spec["paths"]
    results: dict[str, Any] = {}
    extra: dict[str, Any] = {}

    # Full build. Rep 0 leaves its index in ``index_dir`` for the other groups.
    full: list[float] = []
    for rep in range(build_reps):
        target = index_dir if rep == 0 else scratch / f"build-{rep}"
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True)
        indexer = _indexer(vault, target)
        full.append(_timed(indexer.index))
        if rep == 0:
            extra["chunks"] = indexer.store.count()
            extra["index_sqlite_mib"] = round((target / "index.sqlite").stat().st_size / 2**20, 2)
        _close(indexer)
        if rep:
            shutil.rmtree(target, ignore_errors=True)
    results["index.full"] = summarize(full, docs=docs)

    # No-op reindex on a warm indexer: what a webhook push of an unchanged tree costs.
    indexer = _indexer(vault, index_dir)
    indexer.index()
    results["index.noop"] = summarize(sample(indexer.index, runs=reps), docs=docs)
    _close(indexer)

    # One changed file, on a copy so the pristine vault and index stay what other groups measure.
    copy_vault, copy_index = scratch / "vault-edit", scratch / "index-edit"
    for path in (copy_vault, copy_index):
        if path.exists():
            shutil.rmtree(path)
    shutil.copytree(vault, copy_vault)
    shutil.copytree(index_dir, copy_index)
    indexer = _indexer(copy_vault, copy_index)
    indexer.index()
    stride = max(1, len(paths) // max(reps, 1))
    changed: list[float] = []
    for rep in range(reps):
        target_file = copy_vault / paths[(rep * stride) % len(paths)]
        with target_file.open("a", encoding="utf-8") as handle:
            handle.write(f"\n\nBenchmark edit {rep}.\n")
        changed.append(_timed(indexer.index))
    results["index.one_changed"] = summarize(changed, docs=docs)
    _close(indexer)
    shutil.rmtree(copy_vault, ignore_errors=True)
    shutil.rmtree(copy_index, ignore_errors=True)
    return {"results": results, "extra": extra}


# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------


def tools_group(spec: dict[str, Any]) -> dict[str, Any]:
    """Tool latency in process, with usage tracking on (as served) and a usage file that starts empty."""
    index_dir = Path(spec["index"])
    (index_dir / "usage_stats.json").unlink(missing_ok=True)
    calls, warmup = int(spec["calls"]), int(spec["warmup"])
    docs = int(spec["docs"])
    paths: list[str] = spec["paths"]
    modes: list[str] = spec["modes"]

    from knowledge import server

    runtime = server.default_runtime()
    started = time.perf_counter()
    server._ensure_indexed(runtime)
    extra: dict[str, Any] = {"ensure_indexed_ms": round((time.perf_counter() - started) * 1000.0, 1)}
    store = runtime.indexer.store
    loop = asyncio.new_event_loop()
    results: dict[str, Any] = {}
    chunk_ids: list[str] = []

    def run(awaitable: Any) -> Any:
        return loop.run_until_complete(awaitable)

    def measure(name: str, call: Any, items: list[Any], **info: Any) -> None:
        """``warmup`` untimed calls, one cold sample, then ``calls`` timed ones, cycling through ``items``."""
        counter = {"i": 0}

        def one() -> Any:
            item = items[counter["i"] % len(items)]
            counter["i"] += 1
            return run(call(item))

        samples = sample(one, runs=calls, warmup=warmup)
        results[name] = summarize(samples, **info)

    # The first call after opening the index is its own, cold, sample.
    first_query = next(iter(spec["queries"].values()))[0]
    results["search.first_call"] = summarize([_timed(lambda: run(server.search(first_query, limit=5)))])

    for label, query_list in spec["queries"].items():
        sel = f"sel{label}" if label != "zipf" else "zipf"
        shown = query_list[:40]
        matched = sorted(len(store.lexical(q, docs, None)) / max(docs, 1) for q in shown)
        info = {"query_matched_docs_median": round(matched[len(matched) // 2], 4), "queries": len(query_list)}

        for mode in modes:
            measure(
                f"search.{mode}.{sel}",
                lambda q, mode=mode: server.search(q, limit=5, mode=mode),
                query_list,
                **info,
            )
        for q in query_list[:60]:
            rows = run(server.search(q, limit=5))
            chunk_ids.extend(row["chunk_id"] for row in rows if row.get("chunk_id"))
        measure(f"search.limit20_snippet.{sel}", lambda q: server.search(q, limit=20, snippet=True), query_list, **info)
        measure(f"search_deep.{sel}", lambda q: server.search_deep(q, depth=2, limit=5), query_list, **info)
        measure(f"context_pack.{sel}", lambda q: server.context_pack(q, budget_tokens=3000), query_list, **info)

    shuffled = list(paths)
    random.Random(f"get_document:{spec['seed']}").shuffle(shuffled)
    measure("get_document.file_path", lambda p: server.get_document(file_path=p), shuffled)
    if chunk_ids:
        measure("get_document.chunk_id", lambda c: server.get_document(chunk_id=c, max_chars=800), chunk_ids)

    results["memory.rss_after_tools"] = summarize([rss_mib() or 0.0], unit="MiB")
    loop.close()
    return {"results": results, "extra": extra}


# ---------------------------------------------------------------------------
# usage
# ---------------------------------------------------------------------------


def usage_group(spec: dict[str, Any]) -> dict[str, Any]:
    """Persistence cost of ``UsageTracker`` at several history sizes, through its public recording API."""
    from knowledge.usage import UsageTracker

    scratch = Path(spec["scratch"])
    paths: list[str] = spec["paths"]
    results: dict[str, Any] = {}
    for history in spec["usage_history"]:
        directory = scratch / f"usage-{history}"
        if directory.exists():
            shutil.rmtree(directory)
        directory.mkdir(parents=True)
        file = directory / "usage_stats.json"
        tracker = UsageTracker(file)
        rng = random.Random(f"usage:{history}:{spec['seed']}")

        def one_search(tracker: UsageTracker = tracker, rng: random.Random = rng) -> None:
            files = rng.sample(paths, 5)
            tracker.record_uses(files)
            tracker.record_pairs(files)

        for _ in range(history):
            one_search()
        tracker.flush()
        size_kib = round(file.stat().st_size / 1024, 1)
        results[f"usage.file_kib.h{history}"] = summarize([size_kib], unit="KiB")
        # An explicit flush at this history size: read, merge, rewrite.
        results[f"usage.flush.h{history}"] = summarize(
            sample(tracker.flush, runs=int(spec["flush_reps"])), history=history
        )
        # What one recorded search costs the caller, inline flushes included (the request-thread tail).
        recorded = sample(one_search, runs=int(spec["record_calls"]))
        results[f"usage.record_search.h{history}"] = summarize(recorded, history=history)
    return {"results": results, "extra": {}}


GROUPS = {
    "ensure-index": ensure_index,
    "index": index_group,
    "tools": tools_group,
    "usage": usage_group,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("group", choices=sorted(GROUPS))
    parser.add_argument("--spec", required=True, help="JSON file with the group's parameters")
    args = parser.parse_args(argv)
    spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))
    try:
        outcome = GROUPS[args.group](spec)
    except Exception:
        traceback.print_exc()
        outcome = {"error": traceback.format_exc(limit=8)}
    Path(spec["result"]).write_text(json.dumps(outcome), encoding="utf-8")
    sys.stdout.flush()
    return 1 if "error" in outcome else os.EX_OK


if __name__ == "__main__":
    sys.exit(main())
