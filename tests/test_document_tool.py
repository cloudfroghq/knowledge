"""Interface tests for the ``get_document`` tool.

``get_document`` is the only tool that serves raw vault content, so it is the
one that has to be bounded: an unbounded read of a large architecture note
blows a client's context window. The tests below exercise every retrieval mode
it exposes — whole document, named section, explicit line range — plus the
coded errors, calling the real decorated tool entry point through
``asyncio.run`` exactly as FastMCP would await it.
"""

from __future__ import annotations

import asyncio

import pytest

from knowledge import errors
from knowledge.errors import KnowledgeToolError

RUNBOOK = "Runbooks/Runbook - Middleware Outage.md"
MIDDLEWARE = "Services/Middleware Service.md"


def test_reads_a_document_with_its_frontmatter(indexed_server) -> None:
    result = asyncio.run(indexed_server.get_document(RUNBOOK))

    assert result["file_path"] == RUNBOOK
    assert result["metadata"]["type"] == "runbook"
    assert "Steps to triage a middleware outage." in result["content"]
    # Frontmatter is metadata, never content: it must not leak into the body.
    assert "type: runbook" not in result["content"]


def test_metadata_carries_only_the_whitelisted_keys(indexed_server) -> None:
    """The runbook's frontmatter has ``title`` and ``severity`` too; neither comes back by default."""
    result = asyncio.run(indexed_server.get_document(RUNBOOK))

    assert result["metadata"] == {
        "type": "runbook",
        "service": "middleware",
        "tags": ["type/runbook", "service/middleware", "severity/P1"],
        "status": "active",
    }
    assert set(result["metadata"]) <= set(indexed_server.DOCUMENT_METADATA_KEYS)


def test_full_metadata_returns_the_whole_frontmatter(indexed_server) -> None:
    result = asyncio.run(indexed_server.get_document(RUNBOOK, full_metadata=True))

    assert result["metadata"]["title"] == "Runbook - Middleware Outage"
    assert result["metadata"]["severity"] == "P1"


def test_fields_without_metadata_omit_the_frontmatter(indexed_server) -> None:
    result = asyncio.run(indexed_server.get_document(RUNBOOK, fields=["file_path", "content"]))

    assert "metadata" not in result
    assert result["content"]


@pytest.mark.parametrize("value", [True, False])
def test_the_retired_include_metadata_is_refused_with_its_replacement(indexed_server, value: bool) -> None:
    with pytest.raises(KnowledgeToolError) as excinfo:
        asyncio.run(indexed_server.get_document(RUNBOOK, include_metadata=value))

    assert excinfo.value.code == errors.E_ARGUMENT_RETIRED
    assert "full_metadata=true" in str(excinfo.value)


@pytest.mark.parametrize("max_chars", [0, -5])
def test_max_chars_below_one_is_out_of_range(indexed_server, max_chars: int) -> None:
    with pytest.raises(KnowledgeToolError) as excinfo:
        asyncio.run(indexed_server.get_document(RUNBOOK, max_chars=max_chars))

    assert excinfo.value.code == errors.E_ARGUMENT_OUT_OF_RANGE
    assert "max_chars must be at least 1" in str(excinfo.value)


def test_missing_file_is_a_coded_error(indexed_server) -> None:
    with pytest.raises(KnowledgeToolError) as excinfo:
        asyncio.run(indexed_server.get_document("Nowhere/Not A Real Doc.md"))

    assert excinfo.value.code == errors.E_FILE_NOT_FOUND
    assert "Nowhere/Not A Real Doc.md" in str(excinfo.value)


def test_a_directory_is_not_a_document(indexed_server) -> None:
    with pytest.raises(KnowledgeToolError) as excinfo:
        asyncio.run(indexed_server.get_document("Runbooks"))

    assert excinfo.value.code == errors.E_FILE_NOT_FOUND


@pytest.mark.parametrize(
    "outside",
    ["../../../README.md", "../../../pyproject.toml", "Runbooks/../../../../README.md", "/etc/hosts"],
)
def test_a_path_outside_the_vault_reads_as_a_missing_file(indexed_server, outside: str) -> None:
    """``..`` segments and absolute paths never leave the vault, and the answer does not say whether the target exists."""
    with pytest.raises(KnowledgeToolError) as excinfo:
        asyncio.run(indexed_server.get_document(outside))

    assert excinfo.value.code == errors.E_FILE_NOT_FOUND
    assert str(excinfo.value).startswith(f"E_FILE_NOT_FOUND: no document {outside!r} in vault ")


def test_a_symlink_that_leaves_the_vault_reads_as_a_missing_file(server_on_temp_vault, tmp_path) -> None:
    from knowledge import config

    secret = tmp_path / "outside.md"
    secret.write_text("# Outside\n\nnot vault content\n", encoding="utf-8")
    (config.VAULT_PATH / "Architecture" / "Escape.md").symlink_to(secret)

    with pytest.raises(KnowledgeToolError) as excinfo:
        asyncio.run(server_on_temp_vault.get_document("Architecture/Escape.md"))

    assert excinfo.value.code == errors.E_FILE_NOT_FOUND
    assert "not vault content" not in str(excinfo.value)


def test_a_symlink_inside_the_vault_still_reads(server_on_temp_vault) -> None:
    from knowledge import config

    (config.VAULT_PATH / "Architecture" / "Alias.md").symlink_to(config.VAULT_PATH / RUNBOOK)

    result = asyncio.run(server_on_temp_vault.get_document("Architecture/Alias.md"))

    assert "Steps to triage a middleware outage." in result["content"]


def test_section_mode_returns_only_that_section(indexed_server) -> None:
    result = asyncio.run(
        indexed_server.get_document(MIDDLEWARE, section="Middleware Service")
    )

    assert "API gateway" in result["content"]


def test_unknown_section_lists_the_available_ones(indexed_server) -> None:
    with pytest.raises(KnowledgeToolError) as excinfo:
        asyncio.run(indexed_server.get_document(RUNBOOK, section="No Such Section"))

    assert excinfo.value.code == errors.E_SECTION_NOT_FOUND
    assert "Runbook - Middleware Outage" in str(excinfo.value)


def test_a_failed_read_records_no_use(indexed_server, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = indexed_server.default_runtime()
    used: list[object] = []
    monkeypatch.setattr(rt.usage, "record_uses", lambda *a, **k: used.append(a))

    for kwargs in ({"file_path": "Nowhere/Ghost.md"}, {"file_path": RUNBOOK, "section": "No Such Section"}):
        with pytest.raises(KnowledgeToolError):
            asyncio.run(indexed_server.get_document(**kwargs))

    assert used == []


def test_line_range_slices_the_body(indexed_server) -> None:
    result = asyncio.run(indexed_server.get_document(MIDDLEWARE, start_line=1, end_line=2))

    assert "# Middleware Service" in result["content"]
    # The body continues past line 2; the slice must stop there.
    assert "API gateway" not in result["content"]


def test_max_chars_truncates_and_reports_the_next_offset(indexed_server) -> None:
    result = asyncio.run(indexed_server.get_document(MIDDLEWARE, max_chars=20))

    assert result["truncated"] is True
    assert result["next_offset"] == 20
    assert len(result["content"]) == 20


def test_the_default_read_stops_at_3000_characters(server_on_temp_vault) -> None:
    from knowledge import config

    path = "Architecture/Long Note.md"
    (config.VAULT_PATH / path).write_text("# Long Note\n\n" + "word " * 1000, encoding="utf-8")

    result = asyncio.run(server_on_temp_vault.get_document(path))

    assert len(result["content"]) == 3000
    assert (result["truncated"], result["next_offset"]) == (True, 3000)


def test_content_under_the_limit_is_not_flagged_as_truncated(indexed_server) -> None:
    result = asyncio.run(indexed_server.get_document(RUNBOOK, max_chars=4000))

    assert "truncated" not in result
    assert "next_offset" not in result


# ---------------------------------------------------------------------------
# Sections and fenced code: a ``#`` line inside a fence is code, not a heading
# ---------------------------------------------------------------------------

FENCED_BODY = "# Doc\n\n## Run\n\n```bash\ncmd a\n# alt:\ncmd b\n```\n\n## Next\n\ntext\n"
FENCED_RUN = "## Run\n\n```bash\ncmd a\n# alt:\ncmd b\n```"


def test_a_comment_inside_a_fence_is_not_a_heading() -> None:
    from knowledge import pointers
    from knowledge.server import _extract_section, _list_headers

    assert _list_headers(FENCED_BODY) == ["Doc", "Run", "Next"]
    assert _extract_section(FENCED_BODY, "Run") == FENCED_RUN
    assert _extract_section(FENCED_BODY, "alt:") is None
    # The section boundary is the one every ``chunk_id`` uses.
    assert _list_headers(FENCED_BODY) == [head.text for head in pointers.headings(FENCED_BODY)]


@pytest.mark.parametrize(
    ("opening", "closing"),
    [
        ("```", "```"),
        ("~~~", "~~~"),
        ("```bash", "```"),
        ("~~~sh", "~~~"),
        (" ```", " ```"),
        ("   ```", "```"),
        ("````", "````"),
        ("```", "`````"),
    ],
)
def test_every_fence_form_keeps_its_comments_in_the_section(opening: str, closing: str) -> None:
    from knowledge.server import _extract_section, _list_headers

    body = f"# Doc\n\n## Run\n\n{opening}\ncmd a\n# alt:\n## also code\ncmd b\n{closing}\n\n## Next\n\ntext\n"

    assert _list_headers(body) == ["Doc", "Run", "Next"]
    assert _extract_section(body, "Run") == (
        f"## Run\n\n{opening}\ncmd a\n# alt:\n## also code\ncmd b\n{closing}"
    )
    assert _extract_section(body, "Next") == "## Next\n\ntext"


def test_a_longer_opening_fence_survives_a_shorter_fence_line_inside() -> None:
    from knowledge.server import _extract_section, _list_headers

    body = "# Doc\n\n## Run\n\n````md\n```\n# not a heading\n```\n````\n\n## Next\n\ntext\n"

    assert _list_headers(body) == ["Doc", "Run", "Next"]
    assert _extract_section(body, "Run") == "## Run\n\n````md\n```\n# not a heading\n```\n````"


def test_an_unclosed_fence_runs_to_the_end_of_the_body() -> None:
    from knowledge import pointers
    from knowledge.server import _extract_section, _list_headers

    body = "# Doc\n\n## Run\n\n```bash\ncmd a\n# alt:\n\n## Next\n\ntext\n"

    assert _list_headers(body) == ["Doc", "Run"]
    assert _list_headers(body) == [head.text for head in pointers.headings(body)]
    assert _extract_section(body, "Run") == "## Run\n\n```bash\ncmd a\n# alt:\n\n## Next\n\ntext"
    assert _extract_section(body, "Next") is None


def test_headings_after_a_fence_are_still_found_with_their_levels() -> None:
    from knowledge.server import _extract_section, _list_headers

    body = (
        "# Doc\n\n## Run\n\n```\n# one\n```\n\n### Detail\n\nmore\n\n"
        "~~~\n# two\n~~~\n\n## Next\n\ntext\n"
    )

    assert _list_headers(body) == ["Doc", "Run", "Detail", "Next"]
    # A deeper heading stays inside its parent section; the fences do not end it.
    assert _extract_section(body, "Run") == (
        "## Run\n\n```\n# one\n```\n\n### Detail\n\nmore\n\n~~~\n# two\n~~~"
    )
    assert _extract_section(body, "Detail") == "### Detail\n\nmore\n\n~~~\n# two\n~~~"


def test_get_document_section_keeps_a_fenced_comment_inside_the_section(server_on_temp_vault) -> None:
    from knowledge import config

    path = "Runbooks/Runbook - Fenced Comment.md"
    (config.VAULT_PATH / path).write_text(
        "---\ntype: runbook\nstatus: active\n---\n# Fenced Comment\n\n## Run\n\n"
        "```bash\ncmd a\n# alt:\ncmd b\n```\n\nafter the fence\n\n## Next\n\ntext\n",
        encoding="utf-8",
    )
    server_on_temp_vault.default_runtime().indexer.index(force=False, target="vault")

    result = asyncio.run(server_on_temp_vault.get_document(path, section="Run"))

    assert result["content"] == "## Run\n\n```bash\ncmd a\n# alt:\ncmd b\n```\n\nafter the fence"

    with pytest.raises(KnowledgeToolError) as excinfo:
        asyncio.run(server_on_temp_vault.get_document(path, section="No Such Section"))

    assert excinfo.value.code == errors.E_SECTION_NOT_FOUND
    assert "['Fenced Comment', 'Run', 'Next']" in str(excinfo.value)
    assert "alt:" not in str(excinfo.value)


def test_fuzzy_and_numbered_matching_is_unchanged_and_still_skips_fences() -> None:
    from knowledge.server import _extract_section

    body = "# Doc\n\n## 1. Rollback steps\n\n```sh\n# undo\nrun\n```\n\n## Other\n\ntext\n"

    # Exact beats fuzzy; with no exact match the first substring hit wins.
    assert _extract_section(body, "rollback steps") == "## 1. Rollback steps\n\n```sh\n# undo\nrun\n```"
    assert _extract_section(body, "rollback") == "## 1. Rollback steps\n\n```sh\n# undo\nrun\n```"
    assert _extract_section(body, "undo") is None
    assert _extract_section(body, "") is None
