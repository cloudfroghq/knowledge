"""Interface tests for the two maintenance tools: ``reindex`` and ``sync_confluence``.

Both reach outside the process in production — one pulls the repository that
holds the vault, the other calls the Confluence REST API — so both are pinned
here to the boundary. ``gitsync._run_git`` is neutralised suite-wide (see
``conftest``) and Confluence's configuration check is driven explicitly, so
these tests assert the tool's own logic and never the network's mood.
"""

from __future__ import annotations

import asyncio

import pytest

# --- reindex ---------------------------------------------------------------


def test_reindex_vault_reports_indexing_stats(indexed_server) -> None:
    result = asyncio.run(indexed_server.reindex(target="vault"))

    assert result["files"] == 14, "the fixture vault has fourteen documents"
    # Every file is accounted for exactly once. `chunks` counts only what was
    # re-embedded on this pass, so it is 0 on an already-current index.
    assert result["new"] + result["updated"] + result["unchanged"] == result["files"]
    assert result["chunks"] >= 0


def test_reindex_is_incremental_on_an_unchanged_vault(indexed_server) -> None:
    asyncio.run(indexed_server.reindex(target="vault"))
    second = asyncio.run(indexed_server.reindex(target="vault"))

    assert second["unchanged"] == second["files"]
    assert second["new"] == 0
    assert second["updated"] == 0


def test_reindex_picks_up_an_edited_document(server_on_temp_vault) -> None:
    from knowledge import config

    server = server_on_temp_vault
    asyncio.run(server.reindex(target="vault"))

    doc = config.VAULT_PATH / "Runbooks" / "Runbook - Middleware Outage.md"
    doc.write_text(doc.read_text() + "\nA newly documented triage step.\n")

    result = asyncio.run(server.reindex(target="vault"))

    assert result["updated"] == 1
    assert result["unchanged"] == result["files"] - 1


def test_reindex_never_shells_out_to_git_in_the_suite(indexed_server, monkeypatch) -> None:
    """The vault pull must go through gitsync, which the suite has disarmed.

    Without this the tool would run ``git pull --ff-only`` against whatever
    repository happens to contain the vault — here, the developer's own
    checkout.
    """
    from knowledge import gitsync

    calls: list[list[str]] = []
    original = gitsync._run_git

    def _record(args: list[str], *, timeout: int = 120):
        calls.append(args)
        return original(args, timeout=timeout)

    monkeypatch.setattr(gitsync, "_run_git", _record)
    asyncio.run(indexed_server.reindex(target="vault"))

    assert all("pull" not in args for args in calls)


def test_reindex_returns_the_index_version_and_no_export_hint(indexed_server) -> None:
    """R11: ``export_hint`` is retired (ADR-042 deferred it to 2.0); ``index_version`` stays."""
    from knowledge.stores import SCHEMA_VERSION

    result = asyncio.run(indexed_server.reindex(target="vault"))

    assert "export_hint" not in result
    assert result["index_version"] == SCHEMA_VERSION == 1
    assert result["vault_sync"] == "skipped_pull_false"


def test_reindex_rejects_the_retired_code_target(indexed_server) -> None:
    """ADR-039 retired code indexing: "code" used to fall back to a vault-only
    run silently; it must fail loudly instead, so a caller cannot mistake a
    rejected request for a completed one."""
    with pytest.raises(ValueError, match="retired"):
        asyncio.run(indexed_server.reindex(target="code"))


def test_reindex_rejects_the_retired_all_target(indexed_server) -> None:
    with pytest.raises(ValueError, match="retired"):
        asyncio.run(indexed_server.reindex(target="all"))


def test_reindex_rejects_an_unknown_target_instead_of_indexing_everything(indexed_server) -> None:
    """An unrecognized target used to fall back to a full reindex silently."""
    with pytest.raises(ValueError, match="only 'vault' is supported"):
        asyncio.run(indexed_server.reindex(target="nonsense"))


# --- sync_confluence -------------------------------------------------------


@pytest.fixture()
def confluence_configured(monkeypatch):
    from knowledge import confluence_sync

    monkeypatch.setattr(confluence_sync, "_is_configured", lambda: True)
    return confluence_sync


@pytest.fixture()
def confluence_unconfigured(monkeypatch):
    from knowledge import confluence_sync

    monkeypatch.setattr(confluence_sync, "_is_configured", lambda: False)
    return confluence_sync


def test_missing_credentials_are_a_coded_error_instead_of_calling_out(
    indexed_server, confluence_unconfigured
) -> None:
    """R12: no credentials is ``E_SECRET_MISSING`` naming the settings, never an ``{"error": ...}`` success."""
    from knowledge.errors import E_SECRET_MISSING, KnowledgeToolError

    with pytest.raises(KnowledgeToolError) as excinfo:
        asyncio.run(indexed_server.sync_confluence())

    assert excinfo.value.code == E_SECRET_MISSING
    assert "sync_confluence needs CONFLUENCE_" in excinfo.value.message


def test_a_vault_with_no_tracked_pages_is_a_coded_error(indexed_server, confluence_configured) -> None:
    """R12: no file with confluence_id under the scope is ``E_SCOPE_EMPTY``, never a report with ``error``."""
    from knowledge.errors import E_SCOPE_EMPTY, KnowledgeToolError

    with pytest.raises(KnowledgeToolError) as excinfo:
        asyncio.run(indexed_server.sync_confluence(dry_run=True))

    assert excinfo.value.code == E_SCOPE_EMPTY
    assert "'Conventions/'" in excinfo.value.message


def test_tracked_pages_are_reported_without_writing_on_a_dry_run(
    server_on_temp_vault, confluence_configured, monkeypatch
) -> None:
    """A dry run must classify every tracked file and change none of them."""
    from knowledge import config

    server = server_on_temp_vault
    conventions_dir = config.VAULT_PATH / "Conventions"
    conventions_dir.mkdir(parents=True, exist_ok=True)

    doc = conventions_dir / "Imported Page.md"
    doc.write_text(
        "---\ntitle: Imported Page\ntype: convention\nconfluence_id: '424242'\n---\n\n"
        "# Imported Page\n\nBody as it stands in the vault.\n"
    )
    before = doc.read_text()

    monkeypatch.setattr(
        confluence_configured,
        "fetch_page",
        lambda cid: {"id": cid, "title": "Imported Page", "version": {"number": 9}, "body_html": "<p>New content</p>"},
    )
    monkeypatch.setattr(confluence_configured, "_needs_update", lambda page, path: True)

    result = asyncio.run(server.sync_confluence(dry_run=True))

    assert result["dry_run"] is True
    assert result["total_in_scope"] == 1
    assert len(result["needs_update"]) == 1
    assert result["updated"] == []
    assert result["errors"] == []
    assert doc.read_text() == before, "a dry run must never write to the vault"


def test_a_failing_page_fetch_is_reported_not_raised(
    server_on_temp_vault, confluence_configured, monkeypatch
) -> None:
    from knowledge import config

    server = server_on_temp_vault
    conventions_dir = config.VAULT_PATH / "Conventions"
    conventions_dir.mkdir(parents=True, exist_ok=True)

    doc = conventions_dir / "Imported Page.md"
    doc.write_text(
        "---\ntitle: Imported Page\ntype: convention\nconfluence_id: '424242'\n---\n\n"
        "# Imported Page\n\nBody.\n"
    )

    def _boom(cid: str):
        raise RuntimeError("confluence unreachable")

    monkeypatch.setattr(confluence_configured, "fetch_page", _boom)

    result = asyncio.run(server.sync_confluence(dry_run=True))

    assert result["total_in_scope"] == 1
    assert len(result["errors"]) == 1
    assert "confluence unreachable" in result["errors"][0]["error"]


# --- Confluence transport guard --------------------------------------------


@pytest.mark.parametrize("bad_url", ["file:///etc/passwd", "ftp://internal/pages", "/no/scheme"])
def test_a_non_http_confluence_url_is_refused_before_any_request(monkeypatch, bad_url) -> None:
    """``urlopen`` reads local files as happily as it fetches URLs.

    A ``CONFLUENCE_URL`` pointing at ``file://`` would turn every API call into
    an arbitrary-file read that also hands over the Authorization header, so
    the scheme is checked before the request object is built.
    """
    import urllib.request

    from knowledge import config, confluence_sync

    monkeypatch.setattr(config, "CONFLUENCE_URL", bad_url)

    def _explode(*a, **k):
        raise AssertionError("a request must never be opened for a refused scheme")

    monkeypatch.setattr(urllib.request, "urlopen", _explode)

    with pytest.raises(RuntimeError, match="must be an http"):
        confluence_sync._api_get("/wiki/api/v2/pages/1")


def test_an_https_confluence_url_passes_the_scheme_guard(monkeypatch) -> None:
    """The guard rejects the scheme, not the call: https must still go through."""
    import urllib.request

    from knowledge import config, confluence_sync

    monkeypatch.setattr(config, "CONFLUENCE_URL", "https://example.invalid/")
    monkeypatch.setattr(confluence_sync, "_headers", lambda: {"Accept": "application/json"})

    opened: list[str] = []

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"ok": true}'

    def _capture(req, timeout=None):
        opened.append(req.full_url)
        return _Response()

    monkeypatch.setattr(urllib.request, "urlopen", _capture)
    monkeypatch.setattr("time.sleep", lambda _s: None)  # _api_get rate-limits after a hit

    result = confluence_sync._api_get("/wiki/api/v2/pages/1")

    assert result == {"ok": True}
    assert opened == ["https://example.invalid/wiki/api/v2/pages/1"]
