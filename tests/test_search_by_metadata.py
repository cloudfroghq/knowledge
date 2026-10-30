"""Tests for search_by_metadata's tag filter.

Each frontmatter tag is its own row of ``doc_tags`` and the ``tag`` filter is
a membership test on those rows. A tag list flattened into one comma-joined
string would match nothing and raise nothing, so the filter would return
``[]`` for every tag. These tests exercise the real, decorated tool entry
point against a fixture vault (not mocks of the business logic).
"""

from __future__ import annotations

import asyncio
from typing import Any

from knowledge import server


def _items(call: Any) -> list[dict[str, Any]]:
    """The rows of one ``search_by_metadata`` page, after checking the page names all of them."""
    page = asyncio.run(call)
    assert page["offset"] == 0 and page["total"] == len(page["items"]), page
    return page["items"]


def test_tag_filter_finds_the_matching_document() -> None:
    results = _items(server.search_by_metadata(tag="service/middleware"))

    assert results
    paths = {r["file_path"] for r in results}
    assert any("Runbook - Middleware Outage" in p for p in paths)


def test_tag_filter_excludes_documents_without_the_tag() -> None:
    results = _items(server.search_by_metadata(tag="lang/go"))

    assert results
    paths = {r["file_path"] for r in results}
    assert all("Lookup Service" in p for p in paths)


def test_tag_filter_unknown_tag_returns_empty_list() -> None:
    results = _items(server.search_by_metadata(tag="partner/nonexistent"))

    assert results == []


def test_tag_filter_combines_with_type_filter() -> None:
    results = _items(server.search_by_metadata(type="runbook", tag="severity/P1"))

    assert results
    assert all(r.get("type") == "runbook" for r in results)


def test_type_filter_still_works_without_tag() -> None:
    # Sanity check: type/service/status are scalar equalities, apart from the
    # membership test of the tag filter.
    results = _items(server.search_by_metadata(type="flow"))

    assert len(results) == 3


def test_returned_tags_are_a_list_not_a_joined_string() -> None:
    results = _items(server.search_by_metadata(tag="service/middleware"))

    assert results
    assert isinstance(results[0]["tags"], list)
    assert "service/middleware" in results[0]["tags"]


def test_type_filter_for_templates_returns_exactly_five() -> None:
    """Verify that search_by_metadata(type="template") returns exactly 5 documents.

    The fixture vault contains five normalized template documents:
      - Auth Issue Template.md
      - File Save Failure Template.md
      - Missing Files Issue Template.md
      - Registration Issue Template.md
      - Sync Issue Template.md

    This assertion was verified by temporarily reverting one template's
    frontmatter from 'type: template' to 'type: runbook' and confirming
    the test fails (returns 4 instead of 5).
    """
    results = _items(server.search_by_metadata(type="template"))

    assert len(results) == 5, f"Expected 5 templates, got {len(results)}"
    assert all(r.get("type") == "template" for r in results)


def test_runbooks_do_not_include_templates() -> None:
    """Verify that search_by_metadata(type="runbook") excludes all templates.

    The fixture vault contains one runbook (Runbook - Middleware Outage.md)
    and five templates in Runbooks/Templates/. A query for type="runbook"
    should return only the runbook, not any of the templates.

    This assertion was verified by:
      1. Counting runbook results (must be 1 or more)
      2. Verifying no file_path contains 'Templates/'
      3. Reverting one template's type from 'template' to 'runbook' and
         confirming that field inclusion breaks this test (templates then
         appear in runbook results)
    """
    results = _items(server.search_by_metadata(type="runbook"))

    assert results, "Expected at least one runbook"
    for r in results:
        assert r.get("type") == "runbook"
        assert "Templates" not in r.get("file_path", ""), (
            f"Runbook result contains Templates/ path: {r.get('file_path')}"
        )


def test_tag_filter_for_templates_matches_type_filter() -> None:
    """Verify that filtering by tag 'template' returns the same documents as type='template'.

    Both mechanisms (type field and tags list membership) should identify the
    same five documents. This tests the cross-check required by the acceptance
    criteria.

    This assertion was verified by:
      1. Comparing the set of file_paths from both filters
      2. Reverting one template's 'template' tag and confirming the test fails
         (tag filter then returns 4 instead of 5, diverging from type filter)
    """
    type_results = _items(server.search_by_metadata(type="template"))
    tag_results = _items(server.search_by_metadata(tag="template"))

    assert len(type_results) == 5
    assert len(tag_results) == 5

    type_paths = {r["file_path"] for r in type_results}
    tag_paths = {r["file_path"] for r in tag_results}
    assert type_paths == tag_paths, (
        f"Type filter and tag filter return different documents.\n"
        f"Only in type filter: {type_paths - tag_paths}\n"
        f"Only in tag filter: {tag_paths - type_paths}"
    )
