"""Query -> open pairs: which document is read after which search.

The pairs are the implicit relevance signal the retrieval evaluation is
calibrated against, so the attribution rules are pinned here: the newest
search that returned the document wins, a read the search did not surface is
still credited to the newest search with an empty rank, and a read with no
recent search records nothing. The server-level cases run the real tools over
the fixture vault and check that recording leaves every response untouched.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from knowledge import errors, usage
from knowledge.errors import KnowledgeToolError
from knowledge.usage import UsageTracker

RUNBOOK = "Runbooks/Runbook - Middleware Outage.md"
MIDDLEWARE = "Services/Middleware Service.md"
PORTAL = "Services/Portal Service.md"


@pytest.fixture
def tracker(tmp_path: Path) -> UsageTracker:
    return UsageTracker(tmp_path / "usage_stats.json")


def test_open_after_search_records_the_rank_it_was_returned_at(tracker: UsageTracker) -> None:
    tracker.record_query("middleware outage", [RUNBOOK, MIDDLEWARE, PORTAL])

    event = tracker.record_open(MIDDLEWARE)

    assert event is not None
    assert event["query"] == "middleware outage"
    assert event["tool"] == "search"
    assert event["file_path"] == MIDDLEWARE
    assert event["rank"] == 2
    assert tracker.query_opens() == [event]


def test_newest_search_that_returned_the_document_wins(tracker: UsageTracker) -> None:
    tracker.record_query("portal", [PORTAL])
    tracker.record_query("middleware outage", [RUNBOOK, MIDDLEWARE])
    tracker.record_query("login flow", ["Flows/Successful Login Flow.md"])

    event = tracker.record_open(MIDDLEWARE)

    assert event is not None
    assert (event["query"], event["rank"]) == ("middleware outage", 2)


def test_open_outside_every_result_goes_to_the_newest_search_without_rank(tracker: UsageTracker) -> None:
    tracker.record_query("portal", [PORTAL])
    tracker.record_query("middleware outage", [RUNBOOK], tool="search_deep")

    event = tracker.record_open("Architecture/Auth Architecture.md")

    assert event is not None
    assert event["query"] == "middleware outage"
    assert event["tool"] == "search_deep"
    assert event["rank"] is None


def test_open_without_a_recent_search_records_nothing(tracker: UsageTracker) -> None:
    assert tracker.record_open(RUNBOOK) is None
    assert tracker.query_opens() == []


def test_a_search_older_than_the_attribution_window_explains_nothing(
    tracker: UsageTracker, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = {"now": 1000.0}
    monkeypatch.setattr(usage.time, "monotonic", lambda: clock["now"])
    tracker.record_query("middleware outage", [RUNBOOK])

    clock["now"] += usage._QUERY_ATTRIBUTION_SECONDS + 1

    assert tracker.record_open(RUNBOOK) is None


def test_blank_queries_are_not_remembered(tracker: UsageTracker) -> None:
    tracker.record_query("   ", [RUNBOOK])

    assert tracker.record_open(RUNBOOK) is None


def test_query_whitespace_is_normalised(tracker: UsageTracker) -> None:
    tracker.record_query("  middleware \n outage ", [RUNBOOK])

    event = tracker.record_open(RUNBOOK)

    assert event is not None
    assert event["query"] == "middleware outage"


def test_pairs_survive_a_flush_and_a_reload(tmp_path: Path) -> None:
    path = tmp_path / "usage_stats.json"
    first = UsageTracker(path)
    first.record_query("middleware outage", [RUNBOOK])
    event = first.record_open(RUNBOOK)
    first.flush()

    assert json.loads(path.read_text())["query_opens"] == [event]
    assert UsageTracker(path).query_opens() == [event]


def test_overlay_without_pairs_still_loads(tmp_path: Path) -> None:
    """A usage file written before pairs existed loads with no pairs."""
    path = tmp_path / "usage_stats.json"
    path.write_text(json.dumps({"decayed_at": 0, "files": {}, "pairs": {}}))

    assert UsageTracker(path).query_opens() == []


def test_persisted_pairs_are_bounded(tracker: UsageTracker, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(usage, "_MAX_QUERY_OPENS", 3)
    tracker.record_query("middleware outage", [RUNBOOK])

    for _ in range(5):
        tracker.record_open(RUNBOOK)

    assert len(tracker.query_opens()) == 3


# --- through the real tools ------------------------------------------------


@pytest.fixture
def fresh_usage(indexed_server, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> UsageTracker:
    """The server's tracker swapped for an empty one, so earlier tests do not leak in."""
    tracker = UsageTracker(tmp_path / "usage_stats.json")
    monkeypatch.setattr(indexed_server.default_runtime(), "usage", tracker)
    return tracker


def test_search_then_get_document_records_the_pair(indexed_server, fresh_usage: UsageTracker) -> None:
    results = asyncio.run(indexed_server.search("middleware outage", limit=5))
    returned = [r["file_path"] for r in results]
    target = returned[1]

    asyncio.run(indexed_server.get_document(target))

    [event] = fresh_usage.query_opens()
    assert event["query"] == "middleware outage"
    assert event["tool"] == "search"
    assert event["file_path"] == target
    assert event["rank"] == 2


def test_search_deep_then_get_document_records_the_pair(indexed_server, fresh_usage: UsageTracker) -> None:
    results = asyncio.run(indexed_server.search_deep("middleware outage", depth=1, limit=5))
    target = results[0]["file_path"]

    asyncio.run(indexed_server.get_document(target))

    [event] = fresh_usage.query_opens()
    assert (event["tool"], event["rank"]) == ("search_deep", 1)


def test_a_missing_document_records_no_pair(indexed_server, fresh_usage: UsageTracker) -> None:
    asyncio.run(indexed_server.search("middleware outage", limit=5))

    with pytest.raises(KnowledgeToolError) as excinfo:
        asyncio.run(indexed_server.get_document("Nope/Does Not Exist.md"))

    assert excinfo.value.code == errors.E_FILE_NOT_FOUND
    assert fresh_usage.query_opens() == []


def test_recording_does_not_change_any_response(indexed_server, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A tracker whose recording raises yields exactly the responses a working one does.

    Both runs start from an empty tracker, so the usage boost is identical and
    any difference could only come from the recording path.
    """

    class _Broken(UsageTracker):
        def record_query(self, *args, **kwargs):  # noqa: ANN002, ANN003
            raise RuntimeError("recording down")

        def record_open(self, *args, **kwargs):  # noqa: ANN002, ANN003
            raise RuntimeError("recording down")

    def run_session(tracker: UsageTracker) -> tuple[object, object]:
        monkeypatch.setattr(indexed_server.default_runtime(), "usage", tracker)
        found = asyncio.run(indexed_server.search("middleware outage", limit=5))
        read = asyncio.run(indexed_server.get_document(RUNBOOK))
        return found, read

    working = UsageTracker(tmp_path / "working.json")
    with_recording = run_session(working)
    without_recording = run_session(_Broken(tmp_path / "broken.json"))

    assert working.query_opens(), "the working tracker should have recorded a pair"
    assert with_recording == without_recording
