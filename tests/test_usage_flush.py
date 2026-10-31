"""What a flush of the usage tracker writes, pinned against a reference model.

A seeded random sequence of ``record_uses``, ``record_pairs``,
``record_query``/``record_open``, ``record_call``, ``record_search`` and
unpaid ``record_rerank`` goes into a tracker. After the last write, the file
must hold exactly what an independent model of those calls predicts: uses per
file, pair counts per ISO week, every query -> open event with its rank, the
rerank aggregates and the tool calls. Only the clock fields (``last_used``,
``at``, ``decayed_at``) are left out of the comparison. ``uses`` and ``boost``
must equal a frozen copy of the ``boost`` formula of 0.9.0 for every path,
also after a decay applied when the file is loaded.
"""

from __future__ import annotations

import json
import math
import random
import time
from itertools import combinations
from pathlib import Path
from typing import Any

import pytest

from knowledge import usage
from knowledge.usage import UsageTracker

PATHS = [f"Docs/doc-{n:02d}.md" for n in range(12)]
TOOLS = ("search", "get_document", "list_vaults")
CLIENTS = ("claude-code", "claude-ai")


def frozen_boost(uses: dict[str, float], file_path: str, *, beta: float = 0.1) -> float:
    """``UsageTracker.boost`` as 0.9.0 computes it: the maximum scanned on every call."""
    u = float(uses.get(file_path, 0.0))
    if u <= 0:
        return 1.0
    max_uses = max(uses.values(), default=0.0)
    if max_uses <= 0:
        return 1.0
    return 1.0 + beta * math.log1p(u) / math.log1p(max_uses)


class Model:
    """What the file must hold after the calls below, computed without the tracker."""

    def __init__(self) -> None:
        self.uses: dict[str, float] = {}
        self.pairs: dict[str, dict[str, int]] = {}
        self.recent: list[tuple[str, str, tuple[str, ...]]] = []
        self.opens: list[dict[str, Any]] = []
        self.calls: dict[str, dict[str, dict[str, int]]] = {}
        self.rerank = {"calls": 0, "applied": 0, "skipped": {}, "units": 0, "cache_hits": 0}
        self.searches = {"searches": 0, "reranked": 0}

    def record_uses(self, fps: list[str], weight: float) -> None:
        for fp in fps:
            self.uses[fp] = self.uses.get(fp, 0.0) + weight

    def record_pairs(self, fps: list[str]) -> None:
        unique = sorted(set(fps))
        week = usage._week_key()
        for a, b in combinations(unique, 2):
            bucket = self.pairs.setdefault(f"{a}||{b}", {})
            bucket[week] = bucket.get(week, 0) + 1

    def record_query(self, query: str, fps: list[str], tool: str) -> None:
        self.recent = [*self.recent, (query, tool, tuple(fps))][-usage._RECENT_QUERIES_KEPT :]

    def record_open(self, fp: str) -> None:
        if not self.recent:
            return
        chosen, rank = self.recent[-1], None
        for entry in reversed(self.recent):
            if fp in entry[2]:
                chosen, rank = entry, entry[2].index(fp) + 1
                break
        self.opens.append({"query": chosen[0], "tool": chosen[1], "file_path": fp, "rank": rank})

    def record_call(self, tool: str, client: str, error: bool) -> None:
        leaf = self.calls.setdefault(tool, {}).setdefault(client, {"calls": 0, "errors": 0})
        leaf["calls"] += 1
        leaf["errors"] += int(error)

    def record_rerank(self, applied: bool, reason: str, units: int, cache_hits: int) -> None:
        self.rerank["calls"] += 1
        if applied:
            self.rerank["applied"] += 1
        else:
            self.rerank["skipped"][reason] = self.rerank["skipped"].get(reason, 0) + 1
        self.rerank["units"] += units
        self.rerank["cache_hits"] += cache_hits


def drive(tracker: UsageTracker, seed: int, steps: int = 400) -> Model:
    """Feed one seeded sequence into ``tracker`` and into a model; returns the model."""
    rng = random.Random(seed)
    model = Model()
    for step in range(steps):
        op = rng.choice(("uses", "uses", "pairs", "query", "open", "call", "call", "rerank", "search"))
        if op == "uses":
            fps = rng.sample(PATHS, rng.randint(1, 4))
            weight = rng.choice((1.0, 2.0))
            tracker.record_uses(fps, weight=weight)
            model.record_uses(fps, weight)
        elif op == "pairs":
            fps = rng.sample(PATHS, rng.randint(1, 5))
            tracker.record_pairs(fps)
            model.record_pairs(fps)
        elif op == "query":
            query = f"probe term{rng.randint(0, 30)} step{step}"
            fps = rng.sample(PATHS, rng.randint(0, 5))
            tool = rng.choice(("search", "search_deep"))
            tracker.record_query(query, fps, tool=tool)
            model.record_query(query, fps, tool)
        elif op == "open":
            fp = rng.choice(PATHS)
            tracker.record_open(fp)
            model.record_open(fp)
        elif op == "call":
            tool, client, error = rng.choice(TOOLS), rng.choice(CLIENTS), rng.random() < 0.2
            tracker.record_call(tool, client=client, principal="stdio", error=error)
            model.record_call(tool, client, error)
        elif op == "rerank":
            applied = rng.random() < 0.5
            reason = rng.choice(("gap", "timeout"))
            units, hits = rng.randint(0, 6), rng.randint(0, 3)
            tracker.record_rerank(applied=applied, reason=reason, units=units, cache_hits=hits)
            model.record_rerank(applied, reason, units, hits)
        else:
            reranked = rng.random() < 0.3
            tracker.record_search(reranked=reranked)
            model.searches["searches"] += 1
            model.searches["reranked"] += int(reranked)
    return model


def assert_file_matches(path: Path, model: Model) -> None:
    raw = json.loads(path.read_text())
    assert list(raw)[:4] == ["decayed_at", "files", "pairs", "query_opens"]
    assert {fp: entry["uses"] for fp, entry in raw["files"].items()} == model.uses
    assert all(isinstance(entry["last_used"], str) for entry in raw["files"].values())
    assert raw["pairs"] == model.pairs
    assert [{k: v for k, v in e.items() if k != "at"} for e in raw["query_opens"]] == model.opens
    section = raw["rerank"]
    assert {k: section[k] for k in model.rerank} == model.rerank
    assert section["searches_by_day"] == {usage.utc_day(): model.searches}
    days = raw["tool_calls"]["days"]
    assert list(days) == [usage.utc_day()]
    on_disk = {tool: {c: p["stdio"] for c, p in clients.items()} for tool, clients in days[usage.utc_day()].items()}
    assert on_disk == model.calls


@pytest.mark.parametrize("seed", [1, 7, 42])
def test_the_flushed_file_holds_exactly_what_was_recorded(tmp_path: Path, seed: int) -> None:
    path = tmp_path / "usage_stats.json"
    tracker = UsageTracker(path)

    model = drive(tracker, seed)
    tracker.flush()

    assert_file_matches(path, model)
    assert tracker.pending_tool_calls() == {}
    for fp in PATHS:
        assert tracker.uses(fp) == model.uses.get(fp, 0.0)
        assert tracker.boost(fp) == frozen_boost(model.uses, fp)
        assert tracker.boost(fp, beta=0.25) == frozen_boost(model.uses, fp, beta=0.25)


@pytest.mark.parametrize("seed", [1, 7])
def test_a_reloaded_tracker_reads_back_the_same_signals(tmp_path: Path, seed: int) -> None:
    path = tmp_path / "usage_stats.json"
    tracker = UsageTracker(path)
    model = drive(tracker, seed)
    tracker.flush()

    again = UsageTracker(path)

    assert again.query_opens() == tracker.query_opens()
    assert again.recent_pairs() == tracker.recent_pairs()
    for fp in PATHS:
        assert again.boost(fp) == frozen_boost(model.uses, fp)


def test_boost_after_a_decay_on_load_matches_the_frozen_formula(tmp_path: Path) -> None:
    path = tmp_path / "usage_stats.json"
    uses = {fp: float(n % 5 + 1) * 3 for n, fp in enumerate(PATHS)}
    uses[PATHS[0]] = 0.052  # decays below the kept minimum and is dropped
    ten_days_ago = time.time() - 10 * 86400
    path.write_text(
        json.dumps({"decayed_at": ten_days_ago, "files": {fp: {"uses": u} for fp, u in uses.items()}})
    )

    tracker = UsageTracker(path)

    decayed = {fp: tracker.uses(fp) for fp in PATHS if tracker.uses(fp) > 0}
    assert PATHS[0] not in decayed and len(decayed) == len(PATHS) - 1
    assert all(decayed[fp] < uses[fp] for fp in decayed)
    for fp in PATHS:
        assert tracker.uses(fp) == decayed.get(fp, 0.0)
        assert tracker.boost(fp) == frozen_boost(decayed, fp)

    tracker.record_uses([PATHS[3]], weight=2.0)
    decayed[PATHS[3]] += 2.0
    for fp in PATHS:
        assert tracker.boost(fp) == frozen_boost(decayed, fp)


def test_an_empty_or_unused_tracker_boosts_nothing(tmp_path: Path) -> None:
    tracker = UsageTracker(tmp_path / "usage_stats.json")

    assert [tracker.boost(fp) for fp in PATHS] == [1.0] * len(PATHS)
    tracker.record_uses([PATHS[0]])
    assert tracker.boost(PATHS[0]) == 1.1 and tracker.boost(PATHS[1]) == 1.0
