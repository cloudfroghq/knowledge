"""Tool-call latency: one count per ``tools/call`` in a fixed bucket, per UTC day and tool.

The counts live in section ``tool_latency`` of the default vault's
``usage_stats.json`` and ``doctor`` reports their p50/p95 per tool in
``tool_calls.latency_ms``. Pinned here: the bucket bounds and their edges, the
percentile of a histogram, the additive merge between processes, the 120-day
retention, that only the day, the tool label and the bucket bound are kept,
and that the counting server times each call it serves.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import Implementation

from knowledge import server, settings, usage
from knowledge.usage import UsageTracker

FIXTURE_VAULT = Path(__file__).resolve().parent / "fixtures" / "vault"
BOUNDS = ("1", "2", "5", "10", "20", "50", "100", "200", "500", "1000", "2000", "5000", "inf")


def _timings(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())["tool_latency"]["days"]


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "usage_stats.json"


@pytest.mark.parametrize(
    ("latency_ms", "bucket"),
    [
        (0.0, "1"),
        (-3.0, "1"),  # a clock that went backwards still lands in the first bucket
        (0.4, "1"),
        (1.0, "1"),  # a bound belongs to its own bucket
        (1.0001, "2"),
        (49.99, "50"),
        (50.0, "50"),
        (5000.0, "5000"),
        (5000.001, "inf"),
        (1e9, "inf"),
    ],
)
def test_a_call_lands_in_the_first_bucket_that_holds_it(path: Path, latency_ms: float, bucket: str) -> None:
    tracker = UsageTracker(path)
    tracker.record_call("search", client="c", principal="stdio", error=False, latency_ms=latency_ms)

    assert tracker.pending_tool_latency() == {usage.utc_day(): {"search": {bucket: 1}}}


def test_the_bounds_are_fixed() -> None:
    assert tuple(str(b) for b in usage.TOOL_LATENCY_BUCKETS_MS) + ("inf",) == BOUNDS


@pytest.mark.parametrize(
    ("histogram", "p50", "p95"),
    [
        ({}, None, None),
        ({"1": 1}, 1, 1),
        ({"2": 10}, 2, 2),
        ({"1": 50, "5": 45, "100": 5}, 1, 5),
        ({"1": 49, "5": 46, "100": 5}, 5, 5),
        ({"1": 94, "1000": 6}, 1, 1000),
        ({"10": 1, "inf": 1}, 10, None),  # p95 past the last bound reads as open
        ({"inf": 3}, None, None),
    ],
)
def test_the_percentiles_are_bucket_bounds(histogram: dict[str, int], p50: int | None, p95: int | None) -> None:
    bounds = usage.TOOL_LATENCY_BUCKETS_MS
    assert usage._percentile_ms(histogram, 0.5, bounds) == p50
    assert usage._percentile_ms(histogram, 0.95, bounds) == p95


def test_the_histogram_reaches_the_file_with_the_calls(path: Path) -> None:
    tracker = UsageTracker(path)
    for latency in (0.5, 3, 3, 4, 80, 9000):
        tracker.record_call("search", client="claude-code", principal="stdio", error=False, latency_ms=latency)
    tracker.record_call("list_vaults", client="claude-ai", principal="service", error=True, latency_ms=12)
    tracker.flush()

    assert _timings(path) == {
        usage.utc_day(): {
            "search": {"1": 1, "5": 3, "100": 1, "inf": 1},
            "list_vaults": {"20": 1},
        }
    }
    assert list(_timings(path)[usage.utc_day()]["search"]) == ["1", "5", "100", "inf"]  # bound order
    assert tracker.pending_tool_latency() == {}


def test_a_call_without_a_duration_writes_no_section(path: Path) -> None:
    tracker = UsageTracker(path)
    tracker.record_call("search", client="c", principal="stdio", error=False)
    tracker.flush()

    assert "tool_latency" not in json.loads(path.read_text())
    assert "latency_ms" not in usage.tool_calls_summary(path)


def test_two_trackers_over_one_file_add_their_histograms(path: Path) -> None:
    """Two trackers over one ``usage_stats.json`` stand for two processes over one vault."""
    first, second = UsageTracker(path), UsageTracker(path)
    first.record_call("search", client="a", principal="stdio", error=False, latency_ms=3)
    second.record_call("search", client="b", principal="stdio", error=False, latency_ms=3)
    second.record_call("search", client="b", principal="stdio", error=False, latency_ms=700)

    first.flush()
    second.flush()
    first.flush()  # nothing pending: rewriting the file keeps what the other process wrote

    assert _timings(path) == {usage.utc_day(): {"search": {"5": 2, "1000": 1}}}


def test_days_older_than_the_retention_and_malformed_entries_are_dropped(path: Path) -> None:
    today = date.fromisoformat(usage.utc_day())
    kept = (today - timedelta(days=usage.TOOL_CALL_DAYS_KEPT - 1)).isoformat()
    dropped = (today - timedelta(days=usage.TOOL_CALL_DAYS_KEPT)).isoformat()
    path.write_text(
        json.dumps(
            {
                "tool_latency": {
                    "days": {
                        dropped: {"search": {"5": 3}},
                        kept: {"search": {"5": 3, "7": 9, "inf": "many", "20": -1, "50": True}, "bad": [1]},
                    }
                }
            }
        )
    )

    tracker = UsageTracker(path)
    tracker.record_call("search", client="c", principal="p", error=False, latency_ms=5)
    tracker.flush()

    assert _timings(path) == {kept: {"search": {"5": 3}}, today.isoformat(): {"search": {"5": 1}}}


def test_only_the_day_the_tool_label_and_the_bound_are_kept(path: Path) -> None:
    tracker = UsageTracker(path)
    tracker.record_call(
        "search about passwords?", client="Claude Desktop/1.0 secret", principal="alice-token", error=False, latency_ms=2
    )
    tracker.flush()

    section = json.loads(path.read_text())["tool_latency"]
    strings: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                strings.append(key)
                walk(value)
        else:
            assert isinstance(node, int) and not isinstance(node, bool)

    walk(section)
    assert strings == ["days", usage.utc_day(), "search_about_passwords_", "2"]
    text = json.dumps(section)
    assert "Claude" not in text and "alice" not in text and "?" not in text
    assert all(re.fullmatch(r"[A-Za-z0-9._-]+", s) for s in strings)


def test_tools_past_the_daily_cap_fold_into_other(path: Path) -> None:
    tracker = UsageTracker(path)
    for n in range(70):
        tracker.record_call(f"tool-{n:02d}", client="c", principal="p", error=False, latency_ms=1)
    tracker.flush()

    day = _timings(path)[usage.utc_day()]
    assert len(day) == 65 and day[usage.OTHER_LABEL] == {"1": 6}


def test_the_summary_reports_calls_and_percentiles_per_tool(path: Path) -> None:
    today = date(2026, 10, 12)
    old = (today - timedelta(days=40)).isoformat()
    path.write_text(
        json.dumps(
            {
                "tool_latency": {
                    "days": {
                        old: {"search": {"5": 10}},
                        today.isoformat(): {"search": {"20": 10}, "get_document": {"1": 19, "200": 1}},
                    }
                }
            }
        )
    )

    summary = usage.tool_calls_summary(path, day=today.isoformat())

    assert summary["latency_ms"] == {
        "get_document": {"calls": 20, "p50": 1, "p95": 1},
        "search": {"calls": 20, "p50": 5, "p95": 20},
    }
    assert list(summary["latency_ms"]) == ["get_document", "search"]


# ---------------------------------------------------------------------------
# The counting server times every call it serves
# ---------------------------------------------------------------------------


def test_the_server_times_each_call_and_the_response_is_unchanged(tmp_path: Path) -> None:
    loaded = settings.load_settings(
        {"VAULT_PATH": str(FIXTURE_VAULT), "KNOWLEDGE_INDEX_DIR": str(tmp_path / "index")}, project_root=tmp_path
    )
    settings.install(loaded)
    reg = server._registry_for(loaded)
    server.set_registry(reg)
    try:
        server.default_runtime()  # open, so the calls are counted

        async def session(cls: type[FastMCP]) -> list[Any]:
            app = cls("latency-probe")
            server.register_tools(app, structured=True)
            calls = [("list_vaults", {}), ("list_vaults", {}), ("no_such_tool", {})]
            async with create_connected_server_and_client_session(
                app, client_info=Implementation(name="probe", version="0")
            ) as s:
                return [(await s.call_tool(name, args)).model_dump() for name, args in calls]

        plain = asyncio.run(session(FastMCP))
        timed = asyncio.run(session(server.KnowledgeMCP))
        server.drain_tool_call_counts(timeout=10)
        reg.close()
    finally:
        server.set_registry(None)
        settings.reset_current()

    assert timed == plain
    day = _timings(tmp_path / "index" / "usage_stats.json")[usage.utc_day()]
    assert sorted(day) == ["list_vaults", "no_such_tool"]
    assert sum(day["list_vaults"].values()) == 2 and sum(day["no_such_tool"].values()) == 1
    assert set(day["list_vaults"]) <= set(BOUNDS)
