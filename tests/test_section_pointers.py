"""Field projection and section pointers on ``search``, ``search_deep`` and ``get_document``.

A default search row carries the pointers to read next (``chunk_id``,
``section``) and no text; ``doc_id``, ``snippet``, ``excerpt`` and the outline
(``sections``) of ``get_document`` are on demand. These cases call the real
tool entry points the way FastMCP awaits them.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from knowledge import errors, pointers
from knowledge.errors import KnowledgeToolError

RUNBOOK = "Runbooks/Runbook - Middleware Outage.md"
MIDDLEWARE = "Services/Middleware Service.md"
POINTER_KEYS = {"doc_id", "chunk_id", "section", "snippet"}


def _code(excinfo: pytest.ExceptionInfo[BaseException]) -> str:
    assert isinstance(excinfo.value, KnowledgeToolError)
    return excinfo.value.code


# --- search -----------------------------------------------------------------


def test_default_rows_point_at_a_section_and_carry_no_text(indexed_server) -> None:
    rows = asyncio.run(indexed_server.search("middleware outage", limit=5))

    assert rows
    for row in rows:
        assert POINTER_KEYS & set(row) == {"chunk_id", "section"}
        assert "excerpt" not in row
        assert row["chunk_id"] is None or row["chunk_id"].startswith(pointers.doc_id(row["file_path"]) + ":")
    first = rows[0]
    assert (first["file_path"], first["chunk_id"]) == (RUNBOOK, f"{pointers.doc_id(RUNBOOK)}:1")


def test_default_rows_drop_a_title_that_repeats_the_file_name(indexed_server) -> None:
    """Every fixture title is its file name: the default row omits it, ``fields`` still names it."""
    rows = asyncio.run(indexed_server.search("middleware outage", limit=5))
    named = asyncio.run(indexed_server.search("middleware outage", limit=5, fields=["file_path", "title"]))

    assert all("title" not in row for row in rows)
    assert all(row["title"] for row in named)


def test_a_title_that_says_more_than_the_file_name_stays(server_on_temp_vault) -> None:
    from knowledge import config

    path = "Titled/rb-quokka.md"
    (config.VAULT_PATH / "Titled").mkdir(exist_ok=True)
    (config.VAULT_PATH / path).write_text(
        "---\ntitle: Quokka gateway runbook\ntype: runbook\n---\n\n# Quokka\n\nquokka gateway quokka\n",
        encoding="utf-8",
    )
    server_on_temp_vault.default_runtime().indexer.index(force=True, target="vault")

    rows = asyncio.run(server_on_temp_vault.search("quokka", mode="keyword", limit=5))
    row = next(r for r in rows if r["file_path"] == path)

    assert row["title"] == "Quokka gateway runbook"


def test_excerpt_comes_back_when_fields_asks_for_it(indexed_server) -> None:
    rows = asyncio.run(
        indexed_server.search("middleware outage", limit=2, excerpt_chars=40, fields=["file_path", "excerpt"])
    )

    assert rows and all(list(row) == ["file_path", "excerpt"] for row in rows)
    assert all(0 < len(row["excerpt"]) <= 40 for row in rows)


@pytest.mark.parametrize(("tool", "limit"), [("search", 0), ("search", 51), ("search_deep", 0), ("search_deep", 21)])
def test_a_limit_outside_its_range_is_refused_before_searching(
    indexed_server, monkeypatch: pytest.MonkeyPatch, tool: str, limit: int
) -> None:
    rt = indexed_server.default_runtime()
    counted: list[str] = []
    for name in ("record_uses", "record_search", "record_query"):
        monkeypatch.setattr(rt.usage, name, lambda *a, _name=name, **k: counted.append(_name))

    with pytest.raises(KnowledgeToolError) as excinfo:
        asyncio.run(getattr(indexed_server, tool)("middleware outage", limit=limit))

    assert _code(excinfo) == errors.E_ARGUMENT_OUT_OF_RANGE
    assert f"got {limit}" in str(excinfo.value)
    assert counted == []


def test_fields_project_rows_in_canonical_order(indexed_server) -> None:
    rows = asyncio.run(
        indexed_server.search("middleware outage", limit=5, fields=["section", "score", "file_path", "chunk_id"])
    )

    assert rows
    assert all(list(row) == ["file_path", "score", "chunk_id", "section"] for row in rows)
    first = rows[0]
    assert first["file_path"] == RUNBOOK
    assert first["chunk_id"] == f"{pointers.doc_id(RUNBOOK)}:1"
    assert first["section"] == "Runbook - Middleware Outage"


def test_doc_id_is_the_pointer_of_the_row_path(indexed_server) -> None:
    rows = asyncio.run(indexed_server.search("middleware outage", limit=5, fields=["file_path", "doc_id"]))

    assert all(row["doc_id"] == pointers.doc_id(row["file_path"]) for row in rows)


def test_snippet_adds_one_key_to_the_default_row(indexed_server) -> None:
    plain = asyncio.run(indexed_server.search("otp session", limit=3))
    with_snippet = asyncio.run(indexed_server.search("otp session", limit=3, snippet=True))

    assert [r["file_path"] for r in with_snippet] == [r["file_path"] for r in plain]
    for row in with_snippet:
        assert list(row)[-1] == "snippet"
        assert row["snippet"] is None or len(row["snippet"]) <= 120


def test_snippet_respects_excerpt_chars(indexed_server) -> None:
    rows = asyncio.run(indexed_server.search("otp session", limit=3, snippet=True, excerpt_chars=30))

    assert any(row["snippet"] for row in rows)
    assert all(row["snippet"] is None or len(row["snippet"]) <= 30 for row in rows)


def test_snippet_survives_a_projection_that_does_not_name_it(indexed_server) -> None:
    rows = asyncio.run(indexed_server.search("otp session", limit=3, fields=["file_path"], snippet=True))

    assert all(list(row) == ["file_path", "snippet"] for row in rows)


@pytest.mark.parametrize("fields", [["file_path", "nope"], ["discovered_via"], []])
def test_an_unknown_field_is_rejected_with_the_allowed_list(indexed_server, fields: list[str]) -> None:
    with pytest.raises(KnowledgeToolError) as excinfo:
        asyncio.run(indexed_server.search("middleware", fields=fields))

    assert _code(excinfo) == errors.E_FIELD_UNKNOWN
    assert "allowed: file_path, title, score" in str(excinfo.value)


def test_pointers_do_not_touch_usage_or_link_weights(indexed_server, monkeypatch: pytest.MonkeyPatch) -> None:
    """The selector only reads files: both calls make the same usage calls."""
    rt = indexed_server.default_runtime()
    calls: list[tuple[str, Any]] = []
    for name in ("record_uses", "record_pairs", "record_query", "record_open"):
        real = getattr(rt.usage, name)

        def spy(*args: Any, _name: str = name, _real: Any = real, **kwargs: Any) -> Any:
            calls.append((_name, args))
            return _real(*args, **kwargs)

        monkeypatch.setattr(rt.usage, name, spy)
    reinforced: list[tuple[str, str]] = []
    real_reinforce = rt.indexer.graph.reinforce

    def spy_reinforce(a: str, b: str, *args: Any, **kwargs: Any) -> Any:
        reinforced.append((a, b))
        return real_reinforce(a, b, *args, **kwargs)

    monkeypatch.setattr(rt.indexer.graph, "reinforce", spy_reinforce)

    asyncio.run(indexed_server.search("middleware outage", limit=5))
    plain_calls, plain_reinforced = list(calls), list(reinforced)
    calls.clear()
    reinforced.clear()
    asyncio.run(
        indexed_server.search(
            "middleware outage", limit=5, fields=["file_path", "doc_id", "chunk_id", "section"], snippet=True
        )
    )

    assert calls == plain_calls
    assert reinforced == plain_reinforced


# --- search_deep ------------------------------------------------------------


def test_search_deep_default_rows_point_at_a_section(indexed_server) -> None:
    rows = asyncio.run(indexed_server.search_deep("middleware outage", depth=2, limit=5))

    assert rows
    for row in rows:
        assert POINTER_KEYS & set(row) == {"chunk_id", "section"}
        assert "excerpt" not in row and "title" not in row
        assert row["discovered_via"]


def test_search_deep_projects_hop_rows_too(indexed_server) -> None:
    rows = asyncio.run(
        indexed_server.search_deep(
            "middleware outage", depth=2, limit=3, fields=["file_path", "discovered_via", "chunk_id", "section"]
        )
    )

    assert rows
    assert all(list(row) == ["file_path", "discovered_via", "chunk_id", "section"] for row in rows)
    assert any(row["discovered_via"] != "direct" for row in rows)
    for row in rows:
        assert row["chunk_id"] is None or row["chunk_id"].startswith(pointers.doc_id(row["file_path"]) + ":")


# --- get_document -----------------------------------------------------------


def test_default_document_keys(indexed_server) -> None:
    result = asyncio.run(indexed_server.get_document(RUNBOOK))

    assert list(result) == ["file_path", "metadata", "content"]


def test_a_chunk_id_alone_reads_its_block(indexed_server) -> None:
    row = asyncio.run(indexed_server.search("middleware outage", limit=1))[0]

    doc = asyncio.run(indexed_server.get_document(chunk_id=row["chunk_id"]))

    assert (doc["file_path"], doc["chunk_id"], doc["section"]) == (row["file_path"], row["chunk_id"], row["section"])
    assert doc == asyncio.run(indexed_server.get_document(row["file_path"], chunk_id=row["chunk_id"]))


def test_neither_file_path_nor_chunk_id_is_an_argument_missing(indexed_server) -> None:
    with pytest.raises(KnowledgeToolError) as excinfo:
        asyncio.run(indexed_server.get_document())

    assert _code(excinfo) == errors.E_ARGUMENT_MISSING
    assert "chunk_id" in str(excinfo.value) and "file_path" in str(excinfo.value)


@pytest.mark.parametrize("chunk_id", ["0000000000:1", "not-a-chunk"])
def test_a_chunk_id_alone_must_name_a_document(indexed_server, chunk_id: str) -> None:
    with pytest.raises(KnowledgeToolError) as excinfo:
        asyncio.run(indexed_server.get_document(chunk_id=chunk_id))

    assert _code(excinfo) in (errors.E_DOC_ID_UNKNOWN, errors.E_CHUNK_ID_INVALID)


def test_a_search_pointer_reads_back_its_section(indexed_server) -> None:
    row = asyncio.run(indexed_server.search("middleware outage", limit=1))[0]

    doc = asyncio.run(indexed_server.get_document(row["file_path"], chunk_id=row["chunk_id"]))

    assert doc["chunk_id"] == row["chunk_id"]
    assert doc["section"] == row["section"]
    assert doc["content"].startswith(f"# {row['section']}")
    assert list(doc) == ["file_path", "metadata", "content", "chunk_id", "section"]


def test_chunk_zero_is_the_preamble(server_on_temp_vault) -> None:
    from knowledge import config

    path = "Runbooks/Runbook - Preamble.md"
    (config.VAULT_PATH / path).write_text("---\ntitle: P\n---\n\nIntro first.\n\n# Heading\n\nBody.\n", encoding="utf-8")

    doc = asyncio.run(server_on_temp_vault.get_document(path, chunk_id=f"{pointers.doc_id(path)}:0"))

    assert doc["content"] == "Intro first."
    assert doc["section"] is None


def test_chunk_id_reads_are_bounded_by_max_chars(indexed_server) -> None:
    doc = asyncio.run(
        indexed_server.get_document(MIDDLEWARE, chunk_id=f"{pointers.doc_id(MIDDLEWARE)}:1", max_chars=20)
    )

    assert doc["content"] == "# Middleware Service"[:20]
    assert doc["truncated"] is True
    assert doc["next_offset"] == 20


@pytest.mark.parametrize(
    "extra",
    [{"section": "Middleware Service"}, {"start_line": 1}, {"end_line": 3}],
    ids=["section", "start_line", "end_line"],
)
def test_chunk_id_conflicts_with_the_other_selectors(indexed_server, extra: dict[str, Any]) -> None:
    with pytest.raises(KnowledgeToolError) as excinfo:
        asyncio.run(indexed_server.get_document(MIDDLEWARE, chunk_id=f"{pointers.doc_id(MIDDLEWARE)}:1", **extra))

    assert _code(excinfo) == errors.E_ARGUMENT_CONFLICT


def test_a_chunk_id_of_another_document_names_its_owner(indexed_server) -> None:
    with pytest.raises(KnowledgeToolError) as excinfo:
        asyncio.run(indexed_server.get_document(MIDDLEWARE, chunk_id=f"{pointers.doc_id(RUNBOOK)}:1"))

    assert _code(excinfo) == errors.E_ARGUMENT_CONFLICT
    assert RUNBOOK in str(excinfo.value)


def test_an_unknown_doc_id_is_reported(indexed_server) -> None:
    with pytest.raises(KnowledgeToolError) as excinfo:
        asyncio.run(indexed_server.get_document(MIDDLEWARE, chunk_id="0000000000:1"))

    assert _code(excinfo) == errors.E_DOC_ID_UNKNOWN


@pytest.mark.parametrize("chunk_id", ["not-a-chunk", "b13146a824", "b13146a824:x"])
def test_a_malformed_chunk_id_is_invalid(indexed_server, chunk_id: str) -> None:
    with pytest.raises(KnowledgeToolError) as excinfo:
        asyncio.run(indexed_server.get_document(MIDDLEWARE, chunk_id=chunk_id))

    assert _code(excinfo) == errors.E_CHUNK_ID_INVALID


def test_a_chunk_id_past_the_last_heading_lists_the_headings(indexed_server) -> None:
    with pytest.raises(KnowledgeToolError) as excinfo:
        asyncio.run(indexed_server.get_document(MIDDLEWARE, chunk_id=f"{pointers.doc_id(MIDDLEWARE)}:2"))

    assert _code(excinfo) == errors.E_CHUNK_ID_INVALID
    assert "Middleware Service" in str(excinfo.value)


def test_a_missing_file_is_reported_before_its_chunk_id(indexed_server) -> None:
    with pytest.raises(KnowledgeToolError) as excinfo:
        asyncio.run(indexed_server.get_document("Nowhere/Ghost.md", chunk_id="0000000000:1"))

    assert _code(excinfo) == errors.E_FILE_NOT_FOUND
    assert "Nowhere/Ghost.md" in str(excinfo.value)


def test_document_fields_project_and_add_doc_id(indexed_server) -> None:
    result = asyncio.run(indexed_server.get_document(RUNBOOK, fields=["content", "doc_id", "file_path"]))

    assert list(result) == ["file_path", "doc_id", "content"]
    assert result["doc_id"] == pointers.doc_id(RUNBOOK)


def test_an_unknown_document_field_is_rejected(indexed_server) -> None:
    with pytest.raises(KnowledgeToolError) as excinfo:
        asyncio.run(indexed_server.get_document(RUNBOOK, fields=["score"]))

    assert _code(excinfo) == errors.E_FIELD_UNKNOWN


def test_section_errors_are_raised_whatever_fields_asks(indexed_server) -> None:
    with pytest.raises(KnowledgeToolError) as excinfo:
        asyncio.run(indexed_server.get_document(RUNBOOK, section="No Such Section", fields=["content"]))

    assert _code(excinfo) == errors.E_SECTION_NOT_FOUND


def test_sections_outline_the_document_and_report_truncation(server_on_temp_vault) -> None:
    from knowledge import config

    path = "Runbooks/Runbook - Long.md"
    headings = "".join(f"## Step {n}\n\nDo step {n}.\n\n" for n in range(1, 36))
    fenced = "```\n# not a heading\n```\n\n"
    (config.VAULT_PATH / path).write_text(f"---\ntitle: Long\n---\n\n# Long\n\n{fenced}{headings}", encoding="utf-8")

    result = asyncio.run(server_on_temp_vault.get_document(path, fields=["file_path", "sections"], max_chars=10))

    assert list(result) == ["file_path", "sections", "sections_truncated"]
    assert result["sections_truncated"] is True
    sections = result["sections"]
    assert len(sections) == 30
    assert sections[0] == {
        "chunk_id": f"{pointers.doc_id(path)}:1",
        "section": "Long",
        "level": 1,
        "chars": sections[0]["chars"],
    }
    assert sections[1]["section"] == "Step 1"
    assert all(set(entry) == {"chunk_id", "section", "level", "chars"} for entry in sections)


# --- the doc_id map ---------------------------------------------------------


def test_the_doc_id_map_is_rebuilt_for_a_file_added_after_the_first_lookup(server_on_temp_vault) -> None:
    from knowledge import config

    rt = server_on_temp_vault.default_runtime()
    assert rt.indexer.doc_id_paths(pointers.doc_id(RUNBOOK)) == (RUNBOOK,)
    added = "Runbooks/Runbook - Added Later.md"
    (config.VAULT_PATH / added).write_text("# Added\n\nNew.\n", encoding="utf-8")

    with pytest.raises(KnowledgeToolError) as excinfo:
        asyncio.run(server_on_temp_vault.get_document(RUNBOOK, chunk_id=f"{pointers.doc_id(added)}:1"))

    assert _code(excinfo) == errors.E_ARGUMENT_CONFLICT
    assert added in str(excinfo.value)


def test_index_drops_the_doc_id_map(indexed_server) -> None:
    rt = indexed_server.default_runtime()
    rt.indexer.doc_id_paths(pointers.doc_id(RUNBOOK))
    assert rt.indexer._doc_ids is not None

    rt.indexer.index(force=False, target="vault")

    assert rt.indexer._doc_ids is None


def test_a_doc_id_collision_is_ambiguous(indexed_server, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = indexed_server.default_runtime()
    monkeypatch.setattr(rt.indexer, "doc_id_paths", lambda doc, refresh=False: ("a.md", "b.md"))

    with pytest.raises(KnowledgeToolError) as excinfo:
        indexed_server._resolve_doc_id(rt, "0123456789")

    assert _code(excinfo) == errors.E_DOC_ID_AMBIGUOUS
    assert "a.md" in str(excinfo.value) and "b.md" in str(excinfo.value)


# --- through the MCP layer ----------------------------------------------------


def test_argument_errors_reach_the_client_as_coded_tool_errors(indexed_server) -> None:
    from mcp.server.fastmcp.exceptions import ToolError

    with pytest.raises(ToolError) as excinfo:
        asyncio.run(indexed_server.mcp.call_tool("search", {"query": "x", "fields": ["nope"]}))

    message = str(excinfo.value)
    assert "E_FIELD_UNKNOWN: " in message
    assert "Traceback" not in message
