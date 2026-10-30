"""Paged listings, capped graph fan-out and the opt-in review rows.

Every expectation here is written out by hand from the frontmatter and the
links of ``tests/fixtures/vault/``, never copied from a tool's output:

* ``search_by_metadata`` answers one page, ``{total, offset, items}``, cut
  from a stable path order (``limit`` 20 by default, ``offset``), projects
  rows (``fields``) and counts the values a filtered set carries
  (``facets``);
* ``get_related`` names each row's document as ``file_path``, and ``fields``
  trims rows to the keys asked for;
* ``get_impact`` caps each depth (``max_items``, 20 by default) and reports
  what it cut (``omitted``) and how big each depth is (``counts``);
* ``get_stale_docs(missing_last_updated=True)`` queues the undated documents
  after every other reason, and those rows count toward ``limit``.

The golden master pins every default byte for byte; the cases below pin it on
the parsed rows.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from knowledge import errors, pointers, review
from knowledge.errors import KnowledgeToolError

RUNBOOK = "Runbooks/Runbook - Middleware Outage.md"
MIDDLEWARE = "Services/Middleware Service.md"
PORTAL = "Services/Portal Service.md"
LOOKUP = "Services/Lookup Service.md"
AUTH_ARCH = "Architecture/Auth Architecture.md"
SESSION_ARCH = "Architecture/Session Architecture.md"

# The fourteen fixture documents in path order.
ALL_PATHS = [
    AUTH_ARCH,
    SESSION_ARCH,
    "Flows/Flow - Comet Argentina.md",
    "Flows/Flow - Telvia.md",
    "Flows/Successful Login Flow.md",
    RUNBOOK,
    "Runbooks/Templates/Auth Issue Template.md",
    "Runbooks/Templates/File Save Failure Template.md",
    "Runbooks/Templates/Missing Files Issue Template.md",
    "Runbooks/Templates/Registration Issue Template.md",
    "Runbooks/Templates/Sync Issue Template.md",
    LOOKUP,
    MIDDLEWARE,
    PORTAL,
]

# The documents whose frontmatter carries no ``last_updated``, in path order.
UNDATED = [
    "Flows/Flow - Comet Argentina.md",
    "Flows/Flow - Telvia.md",
    "Flows/Successful Login Flow.md",
    RUNBOOK,
    LOOKUP,
    MIDDLEWARE,
    PORTAL,
]


def _call(coro: Any) -> Any:
    return asyncio.run(coro)


def _items(coro: Any) -> list[dict[str, Any]]:
    """The rows of one ``search_by_metadata`` page."""
    page = asyncio.run(coro)
    assert set(page) == {"total", "offset", "items"}, page
    rows: list[dict[str, Any]] = page["items"]
    return rows


def _code(excinfo: pytest.ExceptionInfo[BaseException]) -> str:
    assert isinstance(excinfo.value, KnowledgeToolError)
    return excinfo.value.code


# --- search_by_metadata: paging ----------------------------------------------------


def test_a_page_is_cut_from_the_path_order(indexed_server) -> None:
    page = _call(indexed_server.search_by_metadata(limit=2, offset=1))

    assert (page["total"], page["offset"]) == (len(ALL_PATHS), 1)
    assert [row["file_path"] for row in page["items"]] == ALL_PATHS[1:3]


def test_pages_cover_the_whole_set_without_overlap(indexed_server) -> None:
    pages = [_items(indexed_server.search_by_metadata(limit=5, offset=start)) for start in (0, 5, 10)]

    assert [row["file_path"] for page in pages for row in page] == ALL_PATHS


def test_offset_alone_pages_from_there_to_the_end(indexed_server) -> None:
    rows = _items(indexed_server.search_by_metadata(offset=12))

    assert [row["file_path"] for row in rows] == ALL_PATHS[12:]


def test_an_offset_past_the_end_is_an_empty_page_with_the_total(indexed_server) -> None:
    assert _call(indexed_server.search_by_metadata(limit=3, offset=14)) == {
        "total": len(ALL_PATHS),
        "offset": 14,
        "items": [],
    }


def test_the_default_page_is_every_row_by_path_when_it_fits(indexed_server) -> None:
    page = _call(indexed_server.search_by_metadata())
    services = _items(indexed_server.search_by_metadata(type="service"))

    assert page["total"] == len(page["items"]) == len(ALL_PATHS)
    assert [row["file_path"] for row in page["items"]] == ALL_PATHS
    assert [row["file_path"] for row in services] == [LOOKUP, MIDDLEWARE, PORTAL]
    assert all("doc_id" not in row for row in services)


def test_the_default_page_holds_twenty_rows_and_the_total_counts_them_all(server_on_temp_vault) -> None:
    from knowledge import config

    folder = config.VAULT_PATH / "Extra"
    folder.mkdir()
    for n in range(10):
        (folder / f"Extra {n:02d}.md").write_text(f"---\ntype: note\n---\n\n# Extra {n}\n", encoding="utf-8")
    server_on_temp_vault.default_runtime().indexer.index(force=True, target="vault")

    page = _call(server_on_temp_vault.search_by_metadata())
    expected = sorted([*ALL_PATHS, *(f"Extra/Extra {n:02d}.md" for n in range(10))])

    assert page["total"] == len(expected) == 24
    assert [row["file_path"] for row in page["items"]] == expected[: server_on_temp_vault.METADATA_LIMIT_DEFAULT]


@pytest.mark.parametrize(
    "kwargs", [{"limit": 0}, {"limit": 201}, {"offset": -1}], ids=["limit-0", "limit-201", "negative-offset"]
)
def test_paging_bounds_are_coded_errors(indexed_server, kwargs: dict[str, int]) -> None:
    with pytest.raises(KnowledgeToolError) as excinfo:
        _call(indexed_server.search_by_metadata(**kwargs))

    assert _code(excinfo) == errors.E_ARGUMENT_OUT_OF_RANGE


# --- search_by_metadata: fields ----------------------------------------------------


def test_fields_project_rows_in_canonical_order(indexed_server) -> None:
    rows = _items(indexed_server.search_by_metadata(type="service", fields=["doc_id", "file_path"], limit=3))

    assert rows == [{"file_path": path, "doc_id": pointers.doc_id(path)} for path in (LOOKUP, MIDDLEWARE, PORTAL)]
    assert [list(row) for row in rows] == [["file_path", "doc_id"]] * 3


def test_an_unknown_field_lists_the_allowed_ones(indexed_server) -> None:
    with pytest.raises(KnowledgeToolError) as excinfo:
        _call(indexed_server.search_by_metadata(fields=["owner"]))

    assert _code(excinfo) == errors.E_FIELD_UNKNOWN
    assert "file_path, title, type, service, status, tags, doc_id" in str(excinfo.value)


def test_fields_and_facets_are_an_argument_conflict(indexed_server) -> None:
    with pytest.raises(KnowledgeToolError) as excinfo:
        _call(indexed_server.search_by_metadata(facets=True, fields=["file_path"]))

    assert _code(excinfo) == errors.E_ARGUMENT_CONFLICT


# --- search_by_metadata: facets ----------------------------------------------------


def _facet(facet: str, *pairs: tuple[Any, int]) -> list[dict[str, Any]]:
    return [{"facet": facet, "value": value, "count": count} for value, count in pairs]


def test_facets_of_the_middleware_tag_counted_by_hand(indexed_server) -> None:
    """Six documents carry ``service/middleware``: two architecture notes, the
    runbook, the middleware hub and two templates (auth, registration)."""
    rows = _items(indexed_server.search_by_metadata(tag="service/middleware", facets=True))

    assert rows == [
        *_facet("type", ("architecture", 2), ("template", 2), ("runbook", 1), ("service", 1)),
        *_facet("service", ("middleware", 6)),
        *_facet("status", ("active", 4), ("verified", 2)),
        *_facet(
            "tags",
            ("service/middleware", 6),
            ("template", 2),
            ("type/architecture", 2),
            ("type/template", 2),
            ("category/auth", 1),
            ("category/registration", 1),
            ("lang/php", 1),
            ("pattern/auth-access", 1),
            ("pattern/registration-subscription", 1),
            ("service/bss", 1),
            ("severity/P1", 1),
            ("type/runbook", 1),
            ("type/service", 1),
        ),
    ]


def test_facets_of_the_service_hubs_counted_by_hand(indexed_server) -> None:
    rows = _items(indexed_server.search_by_metadata(type="service", facets=True))

    assert rows == [
        *_facet("type", ("service", 3)),
        *_facet("service", ("lookup", 1), ("middleware", 1), ("portal", 1)),
        *_facet("status", ("active", 3)),
        *_facet(
            "tags",
            ("type/service", 3),
            ("lang/go", 1),
            ("lang/php", 1),
            ("lang/ts", 1),
            ("service/lookup", 1),
            ("service/middleware", 1),
            ("service/portal", 1),
        ),
    ]


def test_a_document_without_a_value_is_not_counted_for_that_facet(indexed_server) -> None:
    """``Successful Login Flow`` declares no ``service``: three flows, two services."""
    rows = _items(indexed_server.search_by_metadata(type="flow", facets=True))

    assert [row for row in rows if row["facet"] == "service"] == _facet("service", ("middleware", 2))
    assert [row for row in rows if row["facet"] == "type"] == _facet("type", ("flow", 3))


def test_limit_and_offset_page_the_facet_rows(indexed_server) -> None:
    every = _call(indexed_server.search_by_metadata(type="service", facets=True))
    page = _call(indexed_server.search_by_metadata(type="service", facets=True, limit=2, offset=1))

    assert every["total"] == len(every["items"]) == 12
    assert page == {"total": 12, "offset": 1, "items": every["items"][1:3]}


def test_the_facet_rows_are_paged_like_the_documents(indexed_server) -> None:
    """``service/middleware`` brings 20 facet rows (four types, one service, two statuses, 13 tags)."""
    page = _call(indexed_server.search_by_metadata(tag="service/middleware", facets=True, limit=19))
    rest = _items(indexed_server.search_by_metadata(tag="service/middleware", facets=True, offset=18))

    assert (page["total"], len(page["items"])) == (20, 19)
    assert rest == _facet("tags", ("type/runbook", 1), ("type/service", 1))


def test_facets_over_no_match_are_empty(indexed_server) -> None:
    assert _call(indexed_server.search_by_metadata(type="no-such-type", facets=True)) == {
        "total": 0,
        "offset": 0,
        "items": [],
    }


# --- get_related -------------------------------------------------------------------


def test_every_related_row_names_its_document_as_file_path(indexed_server) -> None:
    rows = _call(indexed_server.get_related(MIDDLEWARE, limit=10))

    assert [list(row) for row in rows] == [["file_path", "direction", "weight"]] * len(rows)
    assert all(row["weight"] == round(row["weight"], 3) for row in rows)


@pytest.mark.parametrize("limit", [0, 201])
def test_a_related_limit_outside_its_range_is_a_coded_error(indexed_server, limit: int) -> None:
    with pytest.raises(KnowledgeToolError) as excinfo:
        _call(indexed_server.get_related(MIDDLEWARE, limit=limit))

    assert _code(excinfo) == errors.E_ARGUMENT_OUT_OF_RANGE


def test_related_fields_trim_rows_and_add_doc_id_on_demand(indexed_server) -> None:
    # The order follows learned weights, which earlier retrievals move: the
    # projection is compared with the full rows of the same graph state.
    full = _call(indexed_server.get_related(MIDDLEWARE, limit=10))
    rows = _call(indexed_server.get_related(MIDDLEWARE, limit=10, fields=["doc_id", "file_path"]))

    assert {row["file_path"] for row in full} == {RUNBOOK, LOOKUP, AUTH_ARCH, SESSION_ARCH, PORTAL}
    assert rows == [{"file_path": row["file_path"], "doc_id": pointers.doc_id(row["file_path"])} for row in full]
    assert [list(row) for row in rows] == [["file_path", "doc_id"]] * len(full)


def test_suggested_rows_carry_file_path_and_project_their_own_keys(indexed_server) -> None:
    rows = _call(
        indexed_server.get_related(
            PORTAL, limit=6, include_suggested=True, fields=["file_path", "direction", "confidence"]
        )
    )

    suggested = [row for row in rows if row["direction"] == "suggested"]
    linked = [row for row in rows if row["direction"] != "suggested"]
    assert [row["file_path"] for row in suggested] == [RUNBOOK]
    assert set(suggested[0]) == {"file_path", "direction", "confidence"}
    # A linked row has no confidence: projection never invents a key.
    assert all(set(row) == {"file_path", "direction"} for row in linked)


@pytest.mark.parametrize("field", ["title", "file"])
def test_related_unknown_field_is_a_coded_error(indexed_server, field: str) -> None:
    with pytest.raises(KnowledgeToolError) as excinfo:
        _call(indexed_server.get_related(MIDDLEWARE, fields=[field]))

    assert _code(excinfo) == errors.E_FIELD_UNKNOWN


# --- get_impact --------------------------------------------------------------------


def test_impact_under_the_default_cap_lists_every_file(indexed_server) -> None:
    result = _call(indexed_server.get_impact(RUNBOOK, max_depth=2))

    assert result == {
        "source": RUNBOOK,
        "depth_1": [AUTH_ARCH, SESSION_ARCH, LOOKUP, MIDDLEWARE],
        "depth_2": [PORTAL],
        "total_affected": 5,
    }


def test_max_items_caps_each_depth_and_reports_what_it_cut(indexed_server) -> None:
    result = _call(indexed_server.get_impact(RUNBOOK, max_depth=2, max_items=1, counts=True))

    assert result == {
        "source": RUNBOOK,
        "depth_1": [AUTH_ARCH],
        "depth_2": [PORTAL],
        "total_affected": 5,
        "counts": {"depth_1": 4, "depth_2": 1},
        "omitted": {"depth_1": 3},
    }


def test_max_items_zero_with_counts_is_counts_only(indexed_server) -> None:
    result = _call(indexed_server.get_impact(RUNBOOK, max_depth=2, max_items=0, counts=True))

    assert result["depth_1"] == [] and result["depth_2"] == []
    assert result["counts"] == {"depth_1": 4, "depth_2": 1}
    assert result["omitted"] == {"depth_1": 4, "depth_2": 1}
    assert result["total_affected"] == 5


def test_a_cap_nothing_exceeds_omits_nothing(indexed_server) -> None:
    result = _call(indexed_server.get_impact(RUNBOOK, max_depth=2, max_items=10))

    assert "omitted" not in result and "counts" not in result
    assert result["depth_1"] == [AUTH_ARCH, SESSION_ARCH, LOOKUP, MIDDLEWARE]


@pytest.mark.parametrize("max_items", [-1, 1001])
def test_a_cap_outside_its_range_is_a_coded_error(indexed_server, max_items: int) -> None:
    with pytest.raises(KnowledgeToolError) as excinfo:
        _call(indexed_server.get_impact(RUNBOOK, max_items=max_items))

    assert _code(excinfo) == errors.E_ARGUMENT_OUT_OF_RANGE


def test_the_default_cap_is_twenty_per_depth(server_on_temp_vault) -> None:
    from knowledge import config

    folder = config.VAULT_PATH / "Fans"
    folder.mkdir()
    for n in range(25):
        (folder / f"Fan {n:02d}.md").write_text(f"# Fan {n}\n\nSee [[Portal Service]].\n", encoding="utf-8")
    server_on_temp_vault.default_runtime().indexer.index(force=True, target="vault")

    result = _call(server_on_temp_vault.get_impact(PORTAL, max_depth=1))

    assert len(result["depth_1"]) == server_on_temp_vault.IMPACT_MAX_ITEMS_DEFAULT == 20
    assert result["total_affected"] == len(result["depth_1"]) + result["omitted"]["depth_1"]
    assert result["depth_1"] == sorted(result["depth_1"])


@pytest.mark.parametrize(("tool", "limit"), [("suggest_links", 201), ("get_stale_docs", 501), ("get_stale_docs", 0)])
def test_every_listing_limit_is_bounded(indexed_server, tool: str, limit: int) -> None:
    with pytest.raises(KnowledgeToolError) as excinfo:
        _call(getattr(indexed_server, tool)(limit=limit))

    assert _code(excinfo) == errors.E_ARGUMENT_OUT_OF_RANGE


# --- get_stale_docs ----------------------------------------------------------------


def test_undated_documents_stay_out_by_default(indexed_server) -> None:
    rows = _call(indexed_server.get_stale_docs())

    assert [row["file_path"] for row in rows] == [AUTH_ARCH, SESSION_ARCH]
    assert all(row["reason"] != review.REASON_LAST_UPDATED_MISSING for row in rows)


def test_missing_last_updated_queues_undated_documents_last_by_path(indexed_server) -> None:
    rows = _call(indexed_server.get_stale_docs(missing_last_updated=True))

    assert [row["file_path"] for row in rows] == [AUTH_ARCH, SESSION_ARCH, *UNDATED]
    missing = rows[2:]
    assert {row["reason"] for row in missing} == {review.REASON_LAST_UPDATED_MISSING}
    assert all(row["needs_review"] is True and "age_days" not in row for row in missing)


def test_undated_rows_count_toward_limit(indexed_server) -> None:
    rows = _call(indexed_server.get_stale_docs(missing_last_updated=True, limit=3))

    assert [row["file_path"] for row in rows] == [AUTH_ARCH, SESSION_ARCH, UNDATED[0]]


def test_undated_rows_honour_the_type_filter(indexed_server) -> None:
    rows = _call(indexed_server.get_stale_docs(type="flow", missing_last_updated=True))

    assert [row["file_path"] for row in rows] == UNDATED[:3]
