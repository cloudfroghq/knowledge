"""Response-shape freeze for every tool the server registers.

This module is the contract. Interface behaviour is tested per tool area
elsewhere; here every registered tool gets its response frozen — required
keys with their types, and nothing outside the declared set. The count is not
written down on purpose: ``test_tool_registry`` reads the live FastMCP
registry and fails when a tool reaches it without a freeze here, so a number
in this prose could only ever go stale. A field added to
a tool without touching this file fails the suite instead of shipping silently
to every client, and a field removed fails it just as loudly.

Where a tool has more than one response shape (a hit and an error, a found and
a not-found path) each shape is frozen separately: the error branch is part of
the contract too, and it is the one clients handle worst.

``optional`` is not a loophole. It marks keys the tool emits conditionally on
the data — an annotation that only fires for a stale document, a pagination
marker that only fires past the character budget — never keys whose presence
is unknown.
"""

from __future__ import annotations

import asyncio

import pytest

RUNBOOK = "Runbooks/Runbook - Middleware Outage.md"
MIDDLEWARE = "Services/Middleware Service.md"
PORTAL = "Services/Portal Service.md"
AUTH_ARCH = "Architecture/Auth Architecture.md"

NUMBER = (int, float)
OPTIONAL_STR = (str, type(None))

# Pointer keys a search row carries when ``fields`` names them (``chunk_id``
# and ``section`` also by default; ``snippet`` with ``snippet=True``). ``None``
# when the row's file holds no query term.
_POINTER_OPTIONAL = {"doc_id": str, "chunk_id": OPTIONAL_STR, "section": OPTIONAL_STR, "snippet": OPTIONAL_STR}
_POINTER_FIELDS = ["file_path", "title", "score", "excerpt", "doc_id", "chunk_id", "section", "snippet"]

# The default search row: pointers to read next, never text. ``title`` only
# when it says more than the file name; review keys only on a stale document.
_DEFAULT_ROW_REQUIRED = {"file_path": str, "score": NUMBER, "chunk_id": OPTIONAL_STR, "section": OPTIONAL_STR}
_DEFAULT_ROW_OPTIONAL = {"title": str, "needs_review": bool, "age_days": int}


# --- get_document ----------------------------------------------------------


def test_get_document_shape(indexed_server, assert_shape) -> None:
    result = asyncio.run(indexed_server.get_document(RUNBOOK))
    assert_shape(
        result,
        required={"file_path": str},
        optional={"metadata": dict, "content": str, "truncated": bool, "next_offset": int},
        where="get_document",
    )


def test_get_document_truncated_shape(indexed_server, assert_shape) -> None:
    result = asyncio.run(indexed_server.get_document(MIDDLEWARE, max_chars=20))
    assert_shape(
        result,
        required={"file_path": str, "content": str, "truncated": bool, "next_offset": int},
        optional={"metadata": dict},
        where="get_document (truncated)",
    )


def test_get_document_chunk_shape(indexed_server, assert_shape) -> None:
    from knowledge import pointers

    result = asyncio.run(indexed_server.get_document(RUNBOOK, chunk_id=f"{pointers.doc_id(RUNBOOK)}:1"))
    assert_shape(
        result,
        required={"file_path": str, "content": str, "chunk_id": str, "section": OPTIONAL_STR},
        optional={"metadata": dict, "truncated": bool, "next_offset": int},
        where="get_document (chunk_id)",
    )


def test_get_document_fields_shape(indexed_server, assert_shape) -> None:
    result = asyncio.run(
        indexed_server.get_document(RUNBOOK, fields=["file_path", "doc_id", "content", "sections"])
    )
    assert_shape(
        result,
        required={"file_path": str, "doc_id": str, "content": str, "sections": list},
        optional={"truncated": bool, "next_offset": int, "sections_truncated": bool},
        where="get_document (fields)",
    )
    for i, entry in enumerate(result["sections"]):
        assert_shape(
            entry,
            required={"chunk_id": str, "section": str, "level": int, "chars": int},
            where=f"get_document.sections[{i}]",
        )


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"file_path": "Nowhere/Ghost.md"}, "E_FILE_NOT_FOUND"),
        ({"file_path": RUNBOOK, "section": "No Such Section"}, "E_SECTION_NOT_FOUND"),
        ({}, "E_ARGUMENT_MISSING"),
    ],
    ids=["missing file", "missing section", "no target"],
)
def test_get_document_errors_are_coded(indexed_server, kwargs, code) -> None:
    """Every ``get_document`` error is raised with its code; no error object comes back as a result."""
    from knowledge.errors import KnowledgeToolError

    with pytest.raises(KnowledgeToolError) as excinfo:
        asyncio.run(indexed_server.get_document(**kwargs))
    assert excinfo.value.code == code


def test_get_document_metadata_is_the_whitelist_unless_full(indexed_server) -> None:
    default = asyncio.run(indexed_server.get_document(RUNBOOK))
    full = asyncio.run(indexed_server.get_document(RUNBOOK, full_metadata=True))

    assert set(default["metadata"]) <= set(indexed_server.DOCUMENT_METADATA_KEYS)
    assert set(default["metadata"]) < set(full["metadata"])


# --- search ----------------------------------------------------------------


def test_search_shape(indexed_server, assert_shape) -> None:
    results = asyncio.run(indexed_server.search("middleware outage", limit=5))
    assert results, "the freeze needs at least one row to inspect"
    for i, row in enumerate(results):
        assert_shape(row, required=_DEFAULT_ROW_REQUIRED, optional=_DEFAULT_ROW_OPTIONAL, where=f"search[{i}]")


def test_search_pointer_shape(indexed_server, assert_shape) -> None:
    results = asyncio.run(indexed_server.search("middleware outage", limit=5, fields=_POINTER_FIELDS))
    assert results
    for i, row in enumerate(results):
        assert_shape(
            row,
            required={"file_path": str, "title": str, "score": NUMBER, "excerpt": str, **_POINTER_OPTIONAL},
            where=f"search (fields)[{i}]",
        )


def test_search_snippet_shape(indexed_server, assert_shape) -> None:
    results = asyncio.run(indexed_server.search("middleware outage", limit=5, snippet=True))
    assert results
    for i, row in enumerate(results):
        assert_shape(
            row,
            required={**_DEFAULT_ROW_REQUIRED, "snippet": OPTIONAL_STR},
            optional=_DEFAULT_ROW_OPTIONAL,
            where=f"search (snippet)[{i}]",
        )


@pytest.mark.parametrize("mode", ["hybrid", "semantic", "keyword"])
def test_search_shape_is_identical_across_modes(indexed_server, assert_shape, mode) -> None:
    results = asyncio.run(indexed_server.search("middleware", mode=mode, limit=3))
    assert results, f"mode {mode} returned nothing to inspect"
    for i, row in enumerate(results):
        assert_shape(row, required=_DEFAULT_ROW_REQUIRED, optional=_DEFAULT_ROW_OPTIONAL, where=f"search({mode})[{i}]")


# --- search_by_metadata ----------------------------------------------------


def _page(indexed_server, assert_shape, **kwargs) -> list:
    """One ``search_by_metadata`` page, frozen as ``{total, offset, items}``; its items."""
    page = asyncio.run(indexed_server.search_by_metadata(**kwargs))
    assert_shape(page, required={"total": int, "offset": int, "items": list}, where="search_by_metadata (page)")
    assert page["total"] >= len(page["items"])
    return page["items"]


def test_search_by_metadata_shape(indexed_server, assert_shape) -> None:
    results = _page(indexed_server, assert_shape, type="service")
    assert results
    for i, row in enumerate(results):
        assert_shape(
            row,
            required={"file_path": str},
            optional={"title": str, "type": str, "service": str, "status": str, "tags": list},
            where=f"search_by_metadata[{i}]",
        )


def test_search_by_metadata_doc_id_shape(indexed_server, assert_shape) -> None:
    results = _page(indexed_server, assert_shape, type="service", fields=["file_path", "doc_id"])
    assert results
    for i, row in enumerate(results):
        assert_shape(row, required={"file_path": str, "doc_id": str}, where=f"search_by_metadata (fields)[{i}]")


def test_search_by_metadata_facet_shape(indexed_server, assert_shape) -> None:
    results = _page(indexed_server, assert_shape, type="service", facets=True)
    assert results
    for i, row in enumerate(results):
        assert_shape(
            row,
            required={"facet": str, "value": (str, int, float, bool), "count": int},
            where=f"search_by_metadata (facets)[{i}]",
        )


# --- search_deep -----------------------------------------------------------


def test_search_deep_shape(indexed_server, assert_shape) -> None:
    results = asyncio.run(indexed_server.search_deep("middleware outage", depth=2, limit=5))
    assert results
    for i, row in enumerate(results):
        assert_shape(
            row,
            required={**_DEFAULT_ROW_REQUIRED, "discovered_via": str},
            optional=_DEFAULT_ROW_OPTIONAL,
            where=f"search_deep[{i}]",
        )


def test_search_deep_pointer_shape(indexed_server, assert_shape) -> None:
    results = asyncio.run(
        indexed_server.search_deep("middleware outage", depth=2, limit=3, fields=[*_POINTER_FIELDS, "discovered_via"])
    )
    assert results
    for i, row in enumerate(results):
        assert_shape(
            row,
            required={
                "file_path": str,
                "title": str,
                "score": NUMBER,
                "excerpt": str,
                "discovered_via": str,
                **_POINTER_OPTIONAL,
            },
            where=f"search_deep (fields)[{i}]",
        )


# --- find_path -------------------------------------------------------------


def test_find_path_found_shape(indexed_server, assert_shape) -> None:
    result = asyncio.run(indexed_server.find_path(PORTAL, RUNBOOK))
    assert_shape(
        result,
        required={"from": str, "to": str, "path": list, "length": int, "found": bool},
        where="find_path (found)",
    )


def test_find_path_weighted_shape(indexed_server, assert_shape) -> None:
    result = asyncio.run(indexed_server.find_path(PORTAL, RUNBOOK, weighted=True))
    assert_shape(
        result,
        required={
            "from": str,
            "to": str,
            "path": list,
            "length": int,
            "cost": NUMBER,
            "weighted": bool,
            "found": bool,
        },
        where="find_path (weighted)",
    )


def test_find_path_not_found_shape(indexed_server, assert_shape) -> None:
    result = asyncio.run(indexed_server.find_path(PORTAL, RUNBOOK, max_depth=1))
    assert_shape(
        result,
        required={"from": str, "to": str, "path": type(None), "found": bool, "max_depth": int},
        optional={"weighted": bool},
        where="find_path (not found)",
    )


def test_find_path_missing_node_is_a_coded_error(indexed_server) -> None:
    from knowledge.errors import KnowledgeToolError

    with pytest.raises(KnowledgeToolError) as excinfo:
        asyncio.run(indexed_server.find_path("Nowhere/Ghost.md", RUNBOOK))
    assert excinfo.value.code == "E_FILE_NOT_IN_GRAPH"


# --- get_related -----------------------------------------------------------


def test_get_related_shape(indexed_server, assert_shape) -> None:
    results = asyncio.run(indexed_server.get_related(MIDDLEWARE, limit=10))
    assert results
    for i, row in enumerate(results):
        assert_shape(
            row,
            required={"file_path": str, "direction": str, "weight": NUMBER},
            where=f"get_related[{i}]",
        )
        assert row["direction"] in {"outgoing", "incoming", "both"}


def test_get_related_fields_shape(indexed_server, assert_shape) -> None:
    results = asyncio.run(indexed_server.get_related(MIDDLEWARE, limit=10, fields=["file_path", "doc_id", "weight"]))
    assert results
    for i, row in enumerate(results):
        assert_shape(row, required={"file_path": str, "doc_id": str, "weight": NUMBER}, where=f"get_related (fields)[{i}]")


def test_get_related_suggested_shape(indexed_server, assert_shape) -> None:
    results = asyncio.run(indexed_server.get_related(PORTAL, limit=6, include_suggested=True))
    suggested = [r for r in results if r["direction"] == "suggested"]
    assert suggested
    for i, row in enumerate(suggested):
        assert_shape(
            row,
            required={
                "file_path": str,
                "direction": str,
                "confidence": NUMBER,
                "shared_neighbors": int,
            },
            where=f"get_related (suggested)[{i}]",
        )


# --- suggest_links ---------------------------------------------------------


def test_suggest_links_shape(indexed_server, assert_shape) -> None:
    results = asyncio.run(indexed_server.suggest_links())
    assert results
    for i, row in enumerate(results):
        assert_shape(
            row,
            required={
                "source": str,
                "target": str,
                "shared_neighbors": int,
                "confidence": NUMBER,
            },
            where=f"suggest_links[{i}]",
        )


# --- get_stale_docs --------------------------------------------------------


# Attribution fields: present once a document (or its service hub) resolves
# an owner, absent otherwise.
_STALE_OPTIONAL = {
    "service": str,
    "owner": str,
    "owner_source": str,
    "last_verified": str,
    "threshold_days": int,
}
_STALE_REQUIRED = {
    "file_path": str,
    "title": str,
    "type": str,
    "last_updated": str,
    "needs_review": bool,
    "reason": str,
}


def _assert_stale_row(assert_shape, row, where: str) -> None:
    """Freeze one review-queue row, branching on why it is queued.

    ``age_days`` is required on an overdue row and forbidden on an unreadable
    one: a document whose date nobody can parse has no age, and emitting a
    placeholder number there is exactly the lie this freeze exists to catch.
    """
    from knowledge import review

    required = dict(_STALE_REQUIRED)
    if row.get("reason") == review.REASON_OVERDUE:
        required["age_days"] = int
        optional = dict(_STALE_OPTIONAL)
    else:
        assert row.get("reason") in (review.REASON_LAST_UPDATED_INVALID, review.REASON_LAST_UPDATED_MISSING), (
            f"{where}: unknown reason {row.get('reason')!r}"
        )
        optional = dict(_STALE_OPTIONAL)
        assert "age_days" not in row, f"{where}: an unreadable or missing date must carry no age_days"
    assert_shape(row, required=required, optional=optional, where=where)


def test_get_stale_docs_shape(indexed_server, assert_shape) -> None:
    results = asyncio.run(indexed_server.get_stale_docs())
    assert results
    for i, row in enumerate(results):
        _assert_stale_row(assert_shape, row, f"get_stale_docs[{i}]")


def test_get_stale_docs_missing_date_shape(indexed_server, assert_shape) -> None:
    from knowledge import review

    results = asyncio.run(indexed_server.get_stale_docs(missing_last_updated=True))
    missing = [r for r in results if r["reason"] == review.REASON_LAST_UPDATED_MISSING]

    assert missing
    for i, row in enumerate(missing):
        _assert_stale_row(assert_shape, row, f"get_stale_docs.missing[{i}]")


def test_get_stale_docs_unreadable_date_shape(
    server_on_temp_vault, assert_shape, tmp_path
) -> None:
    """The other half of the contract: a row queued for an unreadable date.

    Frozen against a real indexed document, not a hand-built dict, so the
    tool cannot start emitting a different shape on this branch without the
    suite noticing.
    """
    from knowledge import config, review

    broken = config.VAULT_PATH / "Runbooks" / "Runbook - Broken Date.md"
    broken.write_text(
        "---\ntitle: Broken Date\ntype: runbook\nservice: middleware\n"
        "last_updated: YYYY-MM-DD\n---\n\n# Broken Date\n\nUnrendered template date.\n",
        encoding="utf-8",
    )
    server_on_temp_vault._indexer.index(force=True, target="vault")

    results = asyncio.run(server_on_temp_vault.get_stale_docs(limit=100))
    unreadable = [r for r in results if r["reason"] == review.REASON_LAST_UPDATED_INVALID]

    assert [r["file_path"] for r in unreadable] == ["Runbooks/Runbook - Broken Date.md"]
    for i, row in enumerate(unreadable):
        _assert_stale_row(assert_shape, row, f"get_stale_docs.unreadable[{i}]")


def test_get_stale_docs_can_opt_out_of_unreadable_rows(server_on_temp_vault) -> None:
    from knowledge import config, review

    broken = config.VAULT_PATH / "Runbooks" / "Runbook - Broken Date.md"
    broken.write_text(
        "---\ntitle: Broken Date\ntype: runbook\nservice: middleware\n"
        "last_updated: not-a-date\n---\n\n# Broken Date\n",
        encoding="utf-8",
    )
    server_on_temp_vault._indexer.index(force=True, target="vault")

    results = asyncio.run(
        server_on_temp_vault.get_stale_docs(limit=100, include_unreadable=False)
    )

    assert all(r["reason"] == review.REASON_OVERDUE for r in results)
    assert "Runbooks/Runbook - Broken Date.md" not in {r["file_path"] for r in results}


# --- get_flow / compare_flows ----------------------------------------------


def test_get_flow_shape(indexed_server, assert_shape) -> None:
    result = asyncio.run(indexed_server.get_flow("Telvia"))
    assert_shape(
        result,
        required={"provider": str, "file": str, "steps": list, "shared_login_steps": list},
        where="get_flow",
    )


def test_get_flow_unknown_provider_is_a_coded_error(indexed_server) -> None:
    from knowledge.errors import KnowledgeToolError

    with pytest.raises(KnowledgeToolError) as excinfo:
        asyncio.run(indexed_server.get_flow("Nonexistent Provider"))
    assert excinfo.value.code == "E_FLOW_NOT_FOUND"


def test_compare_flows_shape(indexed_server, assert_shape) -> None:
    result = asyncio.run(indexed_server.compare_flows("Telvia", "Comet Argentina"))
    assert_shape(
        result,
        required={
            "provider1": str,
            "provider2": str,
            "files": dict,
            "total_steps": dict,
            "common_steps": list,
            "only_in_provider1": list,
            "only_in_provider2": list,
            "shared_login_steps": list,
        },
        where="compare_flows",
    )
    for i, pair in enumerate(result["common_steps"]):
        assert_shape(
            pair,
            required={"Telvia": str, "Comet Argentina": str},
            where=f"compare_flows.common_steps[{i}]",
        )


def test_compare_flows_unknown_provider_is_a_coded_error(indexed_server) -> None:
    from knowledge.errors import KnowledgeToolError

    with pytest.raises(KnowledgeToolError) as excinfo:
        asyncio.run(indexed_server.compare_flows("Telvia", "Nonexistent Provider"))
    assert excinfo.value.code == "E_FLOW_NOT_FOUND"


# --- get_impact ------------------------------------------------------------


def test_get_impact_shape(indexed_server, assert_shape) -> None:
    result = asyncio.run(indexed_server.get_impact(RUNBOOK, max_depth=2))
    assert_shape(
        result,
        required={"source": str, "depth_1": list, "depth_2": list, "total_affected": int},
        where="get_impact",
    )


def test_get_impact_depth_keys_follow_max_depth(indexed_server, assert_shape) -> None:
    result = asyncio.run(indexed_server.get_impact(RUNBOOK, max_depth=3))
    assert_shape(
        result,
        required={
            "source": str,
            "depth_1": list,
            "depth_2": list,
            "depth_3": list,
            "total_affected": int,
        },
        where="get_impact (max_depth=3)",
    )


def test_get_impact_capped_shape(indexed_server, assert_shape) -> None:
    result = asyncio.run(indexed_server.get_impact(RUNBOOK, max_depth=2, max_items=1, counts=True))
    assert_shape(
        result,
        required={
            "source": str,
            "depth_1": list,
            "depth_2": list,
            "total_affected": int,
            "counts": dict,
            "omitted": dict,
        },
        where="get_impact (max_items, counts)",
    )


# --- sync_confluence -------------------------------------------------------


@pytest.mark.parametrize(
    ("configured", "code"), [(False, "E_SECRET_MISSING"), (True, "E_SCOPE_EMPTY")], ids=["unconfigured", "nothing tracked"]
)
def test_sync_confluence_errors_are_coded(indexed_server, monkeypatch, configured, code) -> None:
    from knowledge import confluence_sync
    from knowledge.errors import KnowledgeToolError

    monkeypatch.setattr(confluence_sync, "_is_configured", lambda: configured)
    with pytest.raises(KnowledgeToolError) as excinfo:
        asyncio.run(indexed_server.sync_confluence())
    assert excinfo.value.code == code


def test_sync_confluence_report_shape(server_on_temp_vault, assert_shape, monkeypatch) -> None:
    from knowledge import config, confluence_sync

    monkeypatch.setattr(confluence_sync, "_is_configured", lambda: True)
    monkeypatch.setattr(
        confluence_sync,
        "fetch_page",
        lambda cid: {"id": cid, "title": "Imported Page", "last_modified": "2026-01-01", "body_html": "<p>Content</p>"},
    )
    monkeypatch.setattr(confluence_sync, "_needs_update", lambda page, path: True)

    conventions_dir = config.VAULT_PATH / "Conventions"
    conventions_dir.mkdir(parents=True, exist_ok=True)
    (conventions_dir / "Imported Page.md").write_text(
        "---\ntitle: Imported Page\ntype: convention\nconfluence_id: '424242'\n---\n\n# Imported Page\n"
    )

    result = asyncio.run(server_on_temp_vault.sync_confluence(dry_run=True))
    assert_shape(
        result,
        required={
            "dry_run": bool,
            "scope_prefix": str,
            "total_in_scope": int,
            "total_out_of_scope": int,
            "out_of_scope_files": list,
            "up_to_date": list,
            "needs_update": list,
            "updated": list,
            "errors": list,
        },
        where="sync_confluence (report)",
    )
    for i, entry in enumerate(result["needs_update"]):
        assert_shape(
            entry,
            required={
                "file": str,
                "title": str,
                "confluence_id": str,
                "remote_modified": str,
            },
            where=f"sync_confluence.needs_update[{i}]",
        )


# --- search_code -------------------------------------------------------
# ADR-039 retired the code-embeddings collection this tool queried: every
# call now returns the same redirect advisory, never a hit row.


def test_search_code_advisory_shape(indexed_server, assert_shape) -> None:
    results = asyncio.run(indexed_server.search_code("anything"))
    assert len(results) == 1
    assert_shape(results[0], required={"info": str}, where="search_code (advisory)")


# --- reindex ---------------------------------------------------------------


def test_reindex_shape(indexed_server, assert_shape) -> None:
    result = asyncio.run(indexed_server.reindex(target="vault"))
    assert_shape(
        result,
        required={
            "files": int,
            "new": int,
            "updated": int,
            "unchanged": int,
            "chunks": int,
            "index_version": int,
            "vault": str,
            "vault_sync": str,
            "validation": dict,
        },
        # The skipped-file markers appear only when a document could not be
        # indexed. ADR-039 retired code indexing along with the code_files /
        # code_chunks / code_roots_unusable counters it used to add here.
        # ``memory_bridge`` appears only for a vault that declares the
        # memory-bridge feature (tests/test_memory_bridge.py).
        optional={
            "skipped_count": int,
            "skipped_log": str,
            "memory_bridge": dict,
        },
        where="reindex",
    )
    assert "memory_bridge" not in result, "the fixture vault declares no memory-bridge"
    assert_shape(
        result["validation"],
        required={"ok": bool, "errors": int, "warnings": int},
        where="reindex.validation",
    )


# --- context_pack ----------------------------------------------------------

_PACK_REQUIRED = {
    "query": str,
    "vault": str,
    "budget_tokens": int,
    "used_tokens": int,
    "items": list,
    "dropped": list,
    "sources_used": list,
}


def _assert_pack_vault_item(assert_shape, item, where: str) -> None:
    assert_shape(
        item,
        required={
            "source": str,
            "doc_id": str,
            "file_path": str,
            "chunk_id": str,
            "section": OPTIONAL_STR,
            "score": NUMBER,
            "text": str,
        },
        # Only on an item cut to the room left in the budget.
        optional={"truncated": bool},
        where=where,
    )


def test_context_pack_shape(indexed_server, assert_shape) -> None:
    result = asyncio.run(indexed_server.context_pack("middleware outage", budget_tokens=600))
    assert_shape(result, required=_PACK_REQUIRED, where="context_pack")
    assert result["items"], "the fixture vault answers this query"
    for item in result["items"]:
        _assert_pack_vault_item(assert_shape, item, "context_pack.items[]")
    for entry in result["dropped"]:
        assert_shape(
            entry,
            required={"source": str, "doc_id": str, "file_path": str, "chunk_id": str, "reason": str},
            where="context_pack.dropped[]",
        )


def test_context_pack_degraded_shape(indexed_server, assert_shape) -> None:
    """Memory asked for without a bearer: the pack still answers, and says why memory is missing."""
    result = asyncio.run(
        indexed_server.context_pack("middleware outage", budget_tokens=600, include_memories=True)
    )
    assert_shape(result, required={**_PACK_REQUIRED, "degraded": list}, where="context_pack (degraded)")
    for entry in result["degraded"]:
        assert_shape(entry, required={"source": str, "code": str}, where="context_pack.degraded[]")


# --- list_vaults -----------------------------------------------------------


def test_list_vaults_shape(indexed_server, assert_shape) -> None:
    result = asyncio.run(indexed_server.list_vaults())
    assert_shape(result, required={"contract": str, "default": str, "vaults": list}, where="list_vaults")
    for row in result["vaults"]:
        assert_shape(
            row,
            required={"slug": str, "name": str, "default": bool, "features": list},
            where="list_vaults.vaults[]",
        )


# --- sync_jira ---------------------------------------------------------------
#
# Registered only when a served vault declares ``jira``, so the freeze serves
# a temporary vault that does, against the loopback Jira and memory doubles.

_SYNC_JIRA_REPORT = {
    "vault": str,
    "memory_project": str,
    "dry_run": bool,
    "jql": str,
    "since": str,
    "next_since": str,
    "truncated": bool,
    "fetched": int,
    "retried": int,
    "upserted": int,
    "created": int,
    "skipped": list,
    "status_unmapped": list,
}


@pytest.fixture()
def jira_vault_server(indexed_server, tmp_path, monkeypatch):
    """``indexed_server`` serving one temporary vault that declares ``jira``, and the fake Jira and memory."""
    import yaml
    from jira_fakes import ENV, FakeJira, FakeMemoryTasks, issue, jira_server, manifest, memory_server

    from knowledge import server, settings

    jira, memory = FakeJira(), FakeMemoryTasks()
    jira.pages = [[issue("ACME-1", "2026-10-01T00:00:00Z"), issue("bad-key", "2026-10-02T00:00:00Z")]]
    with jira_server(jira) as site, memory_server(memory) as memory_url:
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "vault.yaml").write_text("slug: globex\n" + manifest(site), encoding="utf-8")
        vaults_file = tmp_path / "vaults.yaml"
        vaults_file.write_text(
            yaml.safe_dump({"version": 1, "data_dir": str(tmp_path / "data"), "vaults": [{"slug": "globex", "path": str(vault)}]}),
            encoding="utf-8",
        )
        for name, value in ENV.items():
            monkeypatch.setenv(name, value)
        loaded = settings.load_settings(
            {"KNOWLEDGE_VAULTS": str(vaults_file), "KNOWLEDGE_EMBEDDINGS_PROVIDER": "bag-of-words", "KNOWLEDGE_MEMORY_URL": memory_url, **ENV},
            project_root=tmp_path,
        )
        previous, original = settings.current(), server._registry
        settings.install(loaded)
        server.set_registry(server._registry_for(loaded))
        try:
            yield indexed_server
        finally:
            server.registry().close()
            server.set_registry(original)
            settings.install(previous)


@pytest.mark.parametrize("dry_run", [True, False], ids=["dry_run", "write"])
def test_sync_jira_report_shape(jira_vault_server, assert_shape, dry_run) -> None:
    result = asyncio.run(jira_vault_server.sync_jira(dry_run=dry_run, since="2026-09-01"))

    assert_shape(result, required=_SYNC_JIRA_REPORT, where="sync_jira")
    for i, entry in enumerate(result["skipped"]):
        assert_shape(entry, required={"key": str, "reason": str}, where=f"sync_jira.skipped[{i}]")
    for i, status in enumerate(result["status_unmapped"]):
        assert isinstance(status, str), f"sync_jira.status_unmapped[{i}]"
