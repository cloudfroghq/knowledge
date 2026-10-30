"""Interface tests for the vault-curation tools.

``suggest_links`` and ``get_stale_docs`` are the two tools whose output drives
human work on the vault rather than answering a question, so a silent
regression in either shows up as "nobody was told" instead of as an error.
Both run against the fixture vault's deliberate topology and dates.
"""

from __future__ import annotations

import asyncio

RUNBOOK = "Runbooks/Runbook - Middleware Outage.md"
PORTAL = "Services/Portal Service.md"
LOOKUP = "Services/Lookup Service.md"
AUTH_ARCH = "Architecture/Auth Architecture.md"
SESSION_ARCH = "Architecture/Session Architecture.md"


# --- suggest_links ---------------------------------------------------------


def test_suggest_links_finds_unlinked_pairs_that_share_neighbours(indexed_server) -> None:
    results = asyncio.run(indexed_server.suggest_links())

    pairs = {(s["source"], s["target"]) for s in results}
    # The two architecture notes cite exactly the same two documents and never
    # each other — the textbook case this tool exists to surface.
    assert (AUTH_ARCH, SESSION_ARCH) in pairs
    # The portal and the runbook share both services and are not linked.
    assert (RUNBOOK, PORTAL) in pairs


def test_suggest_links_never_proposes_an_edge_that_already_exists(indexed_server) -> None:
    results = asyncio.run(indexed_server.suggest_links())
    pairs = {frozenset((s["source"], s["target"])) for s in results}

    # Lookup Service links to the runbook directly; suggesting it is noise.
    assert frozenset((LOOKUP, RUNBOOK)) not in pairs


def test_suggest_links_is_sorted_by_descending_confidence(indexed_server) -> None:
    results = asyncio.run(indexed_server.suggest_links())

    ranking = [(s["confidence"], s["shared_neighbors"]) for s in results]
    assert ranking == sorted(ranking, reverse=True)


def test_min_shared_above_the_data_returns_nothing(indexed_server) -> None:
    results = asyncio.run(indexed_server.suggest_links(min_shared=99))

    assert results == []


def test_limit_bounds_the_curation_report(indexed_server) -> None:
    results = asyncio.run(indexed_server.suggest_links(limit=1))

    assert len(results) == 1


# --- get_stale_docs --------------------------------------------------------


def test_stale_docs_lists_documents_past_their_review_threshold(indexed_server) -> None:
    results = asyncio.run(indexed_server.get_stale_docs())

    paths = {r["file_path"] for r in results}
    assert {AUTH_ARCH, SESSION_ARCH} <= paths
    assert all(r["needs_review"] is True for r in results)


def test_stale_docs_are_ordered_oldest_first(indexed_server) -> None:
    results = asyncio.run(indexed_server.get_stale_docs())

    ages = [r["age_days"] for r in results]
    assert ages == sorted(ages, reverse=True)


def test_documents_without_a_last_updated_date_are_not_stale(indexed_server) -> None:
    """No data is not the same as stale: undated docs must never be listed."""
    results = asyncio.run(indexed_server.get_stale_docs())
    paths = {r["file_path"] for r in results}

    assert RUNBOOK not in paths
    assert PORTAL not in paths


def test_type_filter_scopes_the_stale_queue(indexed_server) -> None:
    results = asyncio.run(indexed_server.get_stale_docs(type="architecture"))

    assert results
    assert all(r["type"] == "architecture" for r in results)


def test_type_filter_with_no_stale_documents_returns_empty(indexed_server) -> None:
    results = asyncio.run(indexed_server.get_stale_docs(type="runbook"))

    assert results == []


def test_limit_bounds_the_stale_queue(indexed_server) -> None:
    results = asyncio.run(indexed_server.get_stale_docs(limit=1))

    assert len(results) == 1
    # The oldest document survives the cut, not an arbitrary one.
    assert results[0]["file_path"] == AUTH_ARCH
