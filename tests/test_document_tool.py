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
