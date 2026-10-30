"""Tests for review-decay ownership resolution (roadmap T-11.01).

``get_stale_docs`` used to answer "which documents are overdue" without
answering "who has to revalidate them", so its output was a general list
nobody owned. Ownership is declared once, on the ``type: service`` hubs
(vault contract: ``Conventions/Frontmatter.md``); runbooks and playbooks
carry ``service`` and no ``owner``. The resolution chain
``doc.service -> service hub -> hub.owner`` therefore has to happen in code,
and it has to give the same answer to the MCP tool (reading the documents of
the index) and to the offline weekly report (reading vault frontmatter), which
is why both call ``knowledge.review``.

These tests exercise the real indexer and the real, decorated ``get_stale_docs``
tool against a disposable vault and index directory — never ``vault/``.
"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from knowledge import review, server
from knowledge.embeddings import BagOfWordsEmbeddings
from knowledge.indexer import VaultIndexer, get_skipped_files
from knowledge.ports import ChunkFilter

# Fabricated string, never a real key: the webhook canaries only need the
# comparison to succeed, and a real value has no business in a test file.
_SYNTHETIC_BEARER = "synthetic-bearer-for-canaries-only"


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _days_ago(days: int) -> str:
    return (date.today() - timedelta(days=days)).isoformat()


# ---------------------------------------------------------------------------
# Pure resolution rules
# ---------------------------------------------------------------------------


def test_service_owner_map_only_reads_service_hubs() -> None:
    owners = review.build_service_owner_map(
        [
            {"type": "service", "service": "middleware", "owner": "dev-php"},
            {"type": "service", "service": "metadata", "owner": "dev-async"},
            # A runbook must never seed the map, even if it declared an owner.
            {"type": "runbook", "service": "middleware", "owner": "someone-else"},
            # A hub without an owner contributes nothing rather than an empty entry.
            {"type": "service", "service": "portal", "owner": ""},
        ]
    )

    assert owners == {"middleware": "dev-php", "metadata": "dev-async"}


def test_service_owner_map_is_case_insensitive_on_the_join_key() -> None:
    owners = review.build_service_owner_map(
        [{"type": "service", "service": "Middleware", "owner": "dev-php"}]
    )

    assert review.resolve_owner({"type": "runbook", "service": "middleware"}, owners) == (
        "dev-php",
        review.OWNER_FROM_SERVICE_HUB,
    )


def test_declared_owner_wins_over_the_service_hub() -> None:
    owners = {"middleware": "dev-php"}

    assert review.resolve_owner(
        {"type": "runbook", "service": "middleware", "owner": "lead"}, owners
    ) == ("lead", review.OWNER_FROM_FRONTMATTER)


def test_runbook_inherits_the_owner_of_its_service_hub() -> None:
    owners = {"nextcloud": "dev-nextcloud"}

    assert review.resolve_owner({"type": "runbook", "service": "nextcloud"}, owners) == (
        "dev-nextcloud",
        review.OWNER_FROM_SERVICE_HUB,
    )


@pytest.mark.parametrize(
    "meta",
    [
        {"type": "runbook"},
        {"type": "runbook", "service": ""},
        {"type": "runbook", "service": "service-without-a-hub"},
        {},
        None,
    ],
)
def test_unresolvable_owner_is_flagged_not_guessed(meta: dict | None) -> None:
    owner, source = review.resolve_owner(meta, {"middleware": "dev-php"})

    assert owner == review.UNASSIGNED
    assert source == review.OWNER_UNRESOLVED


def test_unassigned_group_is_reported_last() -> None:
    grouped = review.group_by_owner(
        [
            {"owner": review.UNASSIGNED, "age_days": 500, "file_path": "z.md"},
            {"owner": "dev-php", "age_days": 100, "file_path": "b.md"},
            {"owner": "dev-php", "age_days": 300, "file_path": "a.md"},
            {"owner": "dev-async", "age_days": 120, "file_path": "c.md"},
        ]
    )

    assert [owner for owner, _ in grouped] == ["dev-async", "dev-php", review.UNASSIGNED]
    # Oldest first inside a group, regardless of input order.
    assert [d["file_path"] for d in grouped[1][1]] == ["a.md", "b.md"]


# ---------------------------------------------------------------------------
# Corpus exclusions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rel_path",
    [
        ".trash/Deleted Runbook.md",
        "Runbooks/Templates/.space/templates/Stub.md",
        ".obsidian/plugins/whatever.md",
        "90 - Memory/memory/some-memory.md",
        "",
    ],
)
def test_non_curated_paths_are_out_of_the_review_corpus(rel_path: str) -> None:
    assert review.is_curated(rel_path) is False


@pytest.mark.parametrize(
    "rel_path",
    ["Runbooks/RB-001.md", "Services/Middleware/Middleware.md", "Flows/Flow - Telvia.md"],
)
def test_curated_paths_stay_in_the_review_corpus(rel_path: str) -> None:
    assert review.is_curated(rel_path) is True


# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------


def test_threshold_comes_from_the_documents_type() -> None:
    # config.json: runbook/playbook 90, architecture 365, default 270.
    assert review.threshold_for("runbook") == 90
    assert review.threshold_for("architecture") == 365
    assert review.threshold_for("some-unknown-type") == review.threshold_for("default")


def test_review_status_needs_a_parseable_date() -> None:
    today = date(2026, 9, 5)

    assert review.review_status({"type": "runbook"}, today=today) == {}
    assert review.review_status({"type": "runbook", "last_updated": "soon"}, today=today) == {}
    assert review.review_status(None, today=today) == {}


@pytest.mark.parametrize(
    ("meta", "expected"),
    [
        ({"last_updated": "2026-01-01"}, (review.LAST_UPDATED_DATED, "2026-01-01")),
        ({"last_updated": "2026-01-01T10:00:00"}, (review.LAST_UPDATED_DATED, "2026-01-01T10:00:00")),
        ({}, (review.LAST_UPDATED_MISSING, "")),
        ({"last_updated": ""}, (review.LAST_UPDATED_MISSING, "")),
        ({"last_updated": "   "}, (review.LAST_UPDATED_MISSING, "")),
        (None, (review.LAST_UPDATED_MISSING, "")),
        # The two shapes that were invisible: a free-text date, and the
        # unrendered placeholder the vault's own Runbook Template carries.
        ({"last_updated": "not-a-date"}, (review.LAST_UPDATED_INVALID, "not-a-date")),
        ({"last_updated": "YYYY-MM-DD"}, (review.LAST_UPDATED_INVALID, "YYYY-MM-DD")),
        ({"last_updated": "05/09/2026"}, (review.LAST_UPDATED_INVALID, "05/09/2026")),
        ({"last_updated": "hace un mes"}, (review.LAST_UPDATED_INVALID, "hace un mes")),
    ],
)
def test_last_updated_state_separates_missing_from_unreadable(
    meta: dict | None, expected: tuple[str, str]
) -> None:
    """"No data" and "data I could not read" are different answers.

    Collapsing them is what made a document with a broken date invisible to
    the report *and* to the tool: not overdue, not counted as undated, not
    listed anywhere.
    """
    assert review.last_updated_state(meta) == expected


def test_known_owners_covers_the_whole_corpus_not_just_the_overdue() -> None:
    """A fresh document's owner is still a real owner.

    ``--owner`` is validated against this set, so it has to include teams
    that happen to have nothing overdue this week — otherwise the guard
    against a misspelled owner would itself reject correct input.
    """
    service_owners = {"middleware": "dev-php", "metadata": "dev-async"}
    metas = [
        {"type": "runbook", "service": "middleware"},
        {"type": "playbook", "service": "ghost"},
        {"type": "architecture", "owner": "arquitectura"},
    ]

    assert review.known_owners(metas, service_owners) == {
        "dev-php",
        "dev-async",
        "arquitectura",
        review.UNASSIGNED,
    }


def test_review_status_flags_only_past_the_threshold() -> None:
    today = date(2026, 9, 5)
    at_limit = {"type": "runbook", "last_updated": (today - timedelta(days=90)).isoformat()}
    past_limit = {"type": "runbook", "last_updated": (today - timedelta(days=91)).isoformat()}

    assert review.review_status(at_limit, today=today) == {}
    assert review.review_status(past_limit, today=today) == {"needs_review": True, "age_days": 91}


# ---------------------------------------------------------------------------
# Indexing + the get_stale_docs tool, end to end
# ---------------------------------------------------------------------------


@pytest.fixture()
def ownership_indexer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> VaultIndexer:
    """An indexed, disposable vault holding one hub and three runbooks."""
    vault_dir = tmp_path / "vault"
    index_dir = tmp_path / "index"
    vault_dir.mkdir()

    _write(
        vault_dir / "Services" / "Middleware.md",
        f"""---
title: Middleware
type: service
service: middleware
status: complete
owner: dev-php
last_updated: {_days_ago(10)}
last_verified: {_days_ago(10)}
---

# Middleware

Service hub for the middleware API gateway.
""",
    )
    _write(
        vault_dir / "Runbooks" / "Overdue Middleware Runbook.md",
        f"""---
title: Overdue Middleware Runbook
type: runbook
service: middleware
last_updated: {_days_ago(200)}
---

# Overdue Middleware Runbook

Steps to triage a middleware outage.
""",
    )
    _write(
        vault_dir / "Runbooks" / "Orphan Runbook.md",
        f"""---
title: Orphan Runbook
type: runbook
service: a-service-with-no-hub
last_updated: {_days_ago(150)}
---

# Orphan Runbook

Nobody declared a hub for this service.
""",
    )
    _write(
        vault_dir / ".trash" / "Deleted Runbook.md",
        f"""---
title: Deleted Runbook
type: runbook
service: middleware
last_updated: {_days_ago(400)}
---

# Deleted Runbook

Already in the trash — must never be reported as needing review.
""",
    )

    isolated = VaultIndexer.open(vault_path=vault_dir, index_dir=index_dir, embedder=BagOfWordsEmbeddings())
    isolated.index(force=True, target="vault")
    return isolated


def test_indexer_stores_owner_and_last_verified_in_chunk_metadata(
    ownership_indexer: VaultIndexer,
) -> None:
    assert get_skipped_files() == []

    store = ownership_indexer.store
    (hub,) = store.documents(ChunkFilter(file_paths=("Services/Middleware.md",)))
    assert hub.owner == "dev-php"
    assert hub.last_verified == _days_ago(10)

    (runbook,) = store.documents(ChunkFilter(file_paths=("Runbooks/Overdue Middleware Runbook.md",)))
    # A runbook declares no owner: the field is indexed empty, never guessed.
    assert runbook.owner == ""
    assert runbook.service == "middleware"


def test_get_stale_docs_reports_the_owner_inherited_from_the_service_hub(
    ownership_indexer: VaultIndexer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server.default_runtime(), "indexer", ownership_indexer)
    monkeypatch.setitem(server._index_state, "ready", True)

    results = asyncio.run(server.get_stale_docs())
    by_path = {d["file_path"]: d for d in results}

    overdue = by_path["Runbooks/Overdue Middleware Runbook.md"]
    assert overdue["owner"] == "dev-php"
    assert overdue["owner_source"] == review.OWNER_FROM_SERVICE_HUB
    assert overdue["threshold_days"] == 90
    assert overdue["needs_review"] is True

    orphan = by_path["Runbooks/Orphan Runbook.md"]
    assert orphan["owner"] == review.UNASSIGNED
    assert orphan["owner_source"] == review.OWNER_UNRESOLVED

    # Oldest first.
    assert results[0]["file_path"] == "Runbooks/Overdue Middleware Runbook.md"


def test_get_stale_docs_excludes_trashed_documents(
    ownership_indexer: VaultIndexer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server.default_runtime(), "indexer", ownership_indexer)
    monkeypatch.setitem(server._index_state, "ready", True)

    paths = {d["file_path"] for d in asyncio.run(server.get_stale_docs())}

    assert ".trash/Deleted Runbook.md" not in paths


def test_get_stale_docs_filters_by_owner(
    ownership_indexer: VaultIndexer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server.default_runtime(), "indexer", ownership_indexer)
    monkeypatch.setitem(server._index_state, "ready", True)

    results = asyncio.run(server.get_stale_docs(owner="dev-php"))

    assert [d["file_path"] for d in results] == ["Runbooks/Overdue Middleware Runbook.md"]


def test_get_stale_docs_surfaces_a_document_whose_date_nobody_can_read(
    ownership_indexer: VaultIndexer, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The hole this closes: a broken date was invisible on both routes.

    Not overdue (no age to compare), not "undated" (the field is there), not
    listed anywhere. It comes back on the queue with its own ``reason`` and
    deliberately without ``age_days`` — there is no age to report.
    """

    _write(
        ownership_indexer.vault_path / "Runbooks" / "Broken Date Runbook.md",
        """---
title: Broken Date Runbook
type: runbook
service: middleware
last_updated: YYYY-MM-DD
---

# Broken Date Runbook

The template placeholder was never replaced.
""",
    )
    ownership_indexer.index(force=True, target="vault")
    monkeypatch.setattr(server.default_runtime(), "indexer", ownership_indexer)
    monkeypatch.setitem(server._index_state, "ready", True)

    results = asyncio.run(server.get_stale_docs())
    by_path = {d["file_path"]: d for d in results}
    broken = by_path["Runbooks/Broken Date Runbook.md"]

    assert broken["reason"] == review.REASON_LAST_UPDATED_INVALID
    assert broken["needs_review"] is True
    assert "age_days" not in broken
    # Ownership still resolves: somebody has to repair it.
    assert broken["owner"] == "dev-php"
    # Overdue documents keep their place at the head of the queue.
    assert results[0]["reason"] == review.REASON_OVERDUE
    assert results[-1]["file_path"] == "Runbooks/Broken Date Runbook.md"


def test_get_stale_docs_never_queues_a_document_with_no_date_at_all(
    ownership_indexer: VaultIndexer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other side of the rule: absent is not unreadable.

    Without this, "surface the unreadable ones" would quietly become "surface
    everything that is not dated", and every draft in the vault would land on
    somebody's queue.
    """

    _write(
        ownership_indexer.vault_path / "Runbooks" / "Undated Runbook.md",
        "---\ntitle: Undated Runbook\ntype: runbook\nservice: middleware\n---\n\n# Undated\n",
    )
    ownership_indexer.index(force=True, target="vault")
    monkeypatch.setattr(server.default_runtime(), "indexer", ownership_indexer)
    monkeypatch.setitem(server._index_state, "ready", True)

    paths = {d["file_path"] for d in asyncio.run(server.get_stale_docs())}

    assert "Runbooks/Undated Runbook.md" not in paths


def test_get_stale_docs_resolves_owner_even_when_filtered_by_type(
    ownership_indexer: VaultIndexer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``type`` filter hides the hubs from the main query — the owner map
    is fetched separately, so filtering must not silently unassign everyone."""
    monkeypatch.setattr(server.default_runtime(), "indexer", ownership_indexer)
    monkeypatch.setitem(server._index_state, "ready", True)

    results = asyncio.run(server.get_stale_docs(type="runbook"))
    by_path = {d["file_path"]: d for d in results}

    assert by_path["Runbooks/Overdue Middleware Runbook.md"]["owner"] == "dev-php"


# ---------------------------------------------------------------------------
# Backfilling the new metadata on an index built before it existed
# ---------------------------------------------------------------------------


def test_incremental_reindex_skips_unchanged_files(ownership_indexer: VaultIndexer) -> None:
    """The reason `force` has to be reachable at all.

    An index built before `owner` existed keeps its old metadata forever:
    the file did not change, so an incremental run never touches it and
    `get_stale_docs` would report `unassigned` for the whole vault.
    """
    stats = ownership_indexer.index(force=False, target="vault")

    assert stats["unchanged"] == stats["files"]
    assert stats["updated"] == 0


def test_forced_reindex_reembeds_every_file(ownership_indexer: VaultIndexer) -> None:
    stats = ownership_indexer.index(force=True, target="vault")

    assert stats["unchanged"] == 0
    assert stats["updated"] == stats["files"]


def test_reindex_tool_passes_force_and_pull_through(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[bool, bool]] = []

    def _recorder(force: bool = False, pull: bool = False) -> tuple[dict[str, int], str, None]:
        calls.append((force, pull))
        return {"files": 0, "chunks": 0}, "skipped_no_repo", None

    monkeypatch.setattr(server, "_reindex_blocking", _recorder)

    asyncio.run(server.reindex(target="vault", force=True))
    asyncio.run(server.reindex(target="vault"))
    asyncio.run(server.reindex(target="vault", pull=True))

    assert calls == [(True, False), (False, False), (False, True)]


# ---------------------------------------------------------------------------
# The forced reindex, on both routes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "manual_auth", "expected"),
    [
        ("true", True, True),
        ("1", True, True),
        ("yes", True, True),
        ("on", True, True),
        ("TRUE", True, True),
        ("false", True, False),
        ("", True, False),
        (None, True, False),
        # A repository push authenticates by HMAC signature, and must never be
        # able to spend a full embedding run of the whole vault.
        ("true", False, False),
        ("1", False, False),
    ],
)
def test_webhook_only_accepts_force_from_a_human(
    raw: str | None, manual_auth: bool, expected: bool
) -> None:
    from knowledge import webhook

    assert webhook.force_requested(raw, manual_auth=manual_auth) is expected


def test_webhook_reindex_job_passes_force_through() -> None:
    """The webhook's own route to a forced reindex, not just the MCP tool.

    Both entry points had to be able to backfill metadata added after an
    index was built; only the tool could, so the claim that "the tool and the
    webhook" were both wired covered one of the two.
    """
    from knowledge import webhook

    calls: list[bool] = []

    class _FakeIndexer:
        def index(self, force: bool = False, target: str = "vault") -> dict[str, int]:
            calls.append(force)
            return {"files": 0}

    webhook._run_reindex_job(_FakeIndexer(), force=True)
    webhook._run_reindex_job(_FakeIndexer())

    assert calls == [True, False]


def _reindex_route(monkeypatch: pytest.MonkeyPatch):
    """Register the webhook routes on a stand-in and hand back the handler."""
    from knowledge import webhook

    captured: dict[str, Any] = {}

    class _FakeMCP:
        def custom_route(self, path: str, methods: list[str]):
            def _wrap(fn):
                captured[path] = fn
                return fn

            return _wrap

    class _FakeIndexer:
        class store:  # noqa: N801 - stands in for the index
            vector_backend = "python"

            @staticmethod
            def count() -> int:
                return 0

            @staticmethod
            def get_meta() -> dict[str, str]:
                return {}

    indexer = _FakeIndexer()
    webhook.register_webhook_routes(_FakeMCP(), indexer, index_ready_flag={"ready": True})
    monkeypatch.setattr(webhook.config, "MCP_API_KEY", _SYNTHETIC_BEARER)
    monkeypatch.setattr(webhook.config, "MCP_WEBHOOK_SECRET", "")
    return captured["/webhook/reindex"], indexer


def _post(query: str, auth_header: str | None):
    """Build the minimal ASGI request the route reads."""
    from starlette.requests import Request

    headers = [(b"authorization", auth_header.encode())] if auth_header else []
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/webhook/reindex",
        "query_string": query.encode(),
        "headers": headers,
    }

    async def _receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"{}", "more_body": False}

    return Request(scope, _receive)


def test_webhook_route_wires_force_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    """`POST /webhook/reindex?force=true` really reaches the indexer.

    The MCP tool was wired for `force`; this route was not, so the only way
    to backfill metadata was the tool. Pinned at the HTTP layer, not just on
    the helper, because that is where the wiring was missing.
    """
    handler, indexer = _reindex_route(monkeypatch)
    ANY_INDEXER = indexer  # noqa: N806 - names the object the task must receive

    response = asyncio.run(
        handler(_post("force=true", f"Bearer {_SYNTHETIC_BEARER}"))
    )

    assert response.status_code == 202
    assert b'"force":true' in response.body
    # The background task is what actually runs the reindex: the response body
    # saying "force" and the job receiving it are two different claims.
    assert response.background.args == (ANY_INDEXER, True)


def test_webhook_route_denies_force_to_a_signed_push(monkeypatch: pytest.MonkeyPatch) -> None:
    """A push must never be able to spend a full embedding run."""
    from knowledge import webhook

    handler, indexer = _reindex_route(monkeypatch)
    monkeypatch.setattr(webhook, "_verify_hmac_signature", lambda body, header: True)

    response = asyncio.run(handler(_post("force=true", None)))

    assert response.status_code == 202
    assert b'"mode":"signature"' in response.body
    assert b'"force":false' in response.body
    assert response.background.args == (indexer, False)


@pytest.mark.parametrize("target", ["code", "all"])
def test_webhook_route_rejects_a_retired_target(monkeypatch: pytest.MonkeyPatch, target: str) -> None:
    """ADR-039 retired code indexing: these used to fall back to "vault" silently.

    A request for a target the server can no longer honor must be refused,
    not queued as if it had been understood.
    """
    handler, _indexer = _reindex_route(monkeypatch)

    response = asyncio.run(handler(_post(f"target={target}", f"Bearer {_SYNTHETIC_BEARER}")))

    assert response.status_code == 400
    assert b"retired" in response.body
    assert response.background is None, "a rejected target must never queue a reindex"
