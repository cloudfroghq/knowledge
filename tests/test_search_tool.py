"""Interface tests for the ``search`` tool.

``search`` is the entry point every other retrieval path is built on, and it
has three ranking modes plus a memory-exclusion rule that is easy to break
without noticing: consolidated memories live in the vault like any
other document, and letting them compete with curated docs quietly degrades
every answer the server gives.
"""

from __future__ import annotations

import asyncio


def test_hybrid_search_ranks_the_obvious_match_first(indexed_server) -> None:
    results = asyncio.run(indexed_server.search("middleware outage", limit=3))

    assert results
    assert results[0]["file_path"] == "Runbooks/Runbook - Middleware Outage.md"
    assert results[0]["score"] > 0


def test_results_are_sorted_by_descending_score(indexed_server) -> None:
    results = asyncio.run(indexed_server.search("middleware", limit=5))

    scores = [r["score"] for r in results]
    assert scores == sorted(scores, reverse=True)


def test_limit_bounds_the_result_count(indexed_server) -> None:
    results = asyncio.run(indexed_server.search("service", limit=2))

    assert len(results) <= 2


def test_semantic_mode_ranks_on_meaning_not_on_the_literal_term(indexed_server) -> None:
    results = asyncio.run(indexed_server.search("token session", mode="semantic", limit=3))

    assert results
    assert results[0]["file_path"] == "Flows/Successful Login Flow.md"
    # Cosine similarity is clamped at zero: a score is never negative.
    assert all(r["score"] >= 0 for r in results)


def test_keyword_mode_matches_on_the_literal_term(indexed_server) -> None:
    results = asyncio.run(indexed_server.search("portal", mode="keyword", limit=3))

    assert results
    assert any("Portal Service" in r["file_path"] for r in results)


def test_unknown_mode_falls_back_to_hybrid_instead_of_failing(indexed_server) -> None:
    results = asyncio.run(indexed_server.search("middleware outage", mode="nonsense", limit=3))

    assert results
    assert results[0]["file_path"] == "Runbooks/Runbook - Middleware Outage.md"


def test_excerpt_chars_bounds_the_excerpt(indexed_server) -> None:
    results = asyncio.run(
        indexed_server.search("middleware outage", limit=1, excerpt_chars=15, fields=["file_path", "excerpt"])
    )

    assert results
    assert len(results[0]["excerpt"]) <= 15


def test_stale_documents_are_annotated_not_penalised(indexed_server) -> None:
    """Review-decay only annotates: an old doc still ranks on relevance."""
    results = asyncio.run(indexed_server.search("OTP issuance session creation", limit=5))

    annotated = [r for r in results if r.get("needs_review")]
    assert annotated, "the fixture vault's dated architecture docs must surface"
    assert all(r["age_days"] > 365 for r in annotated)


def test_memories_are_excluded_by_default(server_on_temp_vault) -> None:
    """A document under the memory export folder must not compete by default.

    Consolidated memories live in the vault like any other file. If they leak
    into ordinary search results they outrank curated docs on recency-flavoured
    wording, and every answer the server gives quietly degrades.
    """
    from knowledge import config

    server = server_on_temp_vault
    memory_dir = config.VAULT_PATH / config.MEMORY_FOLDER
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / "Consolidated Middleware Outage Memory.md").write_text(
        "---\ntitle: Consolidated Middleware Outage Memory\ntype: memory\n---\n\n"
        "# Consolidated Middleware Outage Memory\n\nmiddleware outage middleware outage\n"
    )
    server._indexer.index(force=True, target="vault")

    default_paths = {r["file_path"] for r in asyncio.run(server.search("middleware outage", limit=10))}
    assert default_paths, "the query must still match curated docs"
    assert not any(p.startswith(config.MEMORY_FOLDER) for p in default_paths)

    with_memories = {
        r["file_path"]
        for r in asyncio.run(server.search("middleware outage", limit=10, include_memories=True))
    }
    assert any(p.startswith(config.MEMORY_FOLDER) for p in with_memories)


# --- ties ---------------------------------------------------------------------
#
# Two documents with identical text score identically on every path (same
# embedding, same keyword weight, same usage), so they tie exactly. A tie is
# broken by ``file_path``: the published list is ordered by the emitted score
# and then by path, and ``limit`` cuts after that order. Each test writes its
# twins under a folder of its own: usage counters outlive a test, and a twin
# another test already returned would carry a usage boost and break the tie.

_TWIN_TEXT = "---\ntitle: Twin\ntype: service\n---\n\n# Twin\n\nquokka middleware gateway quokka\n"


def _write_twins(server, folder: str) -> tuple[str, str]:
    """Write two identical documents under ``folder``; return their paths, smaller first."""
    from knowledge import config

    alpha, beta = f"{folder}/Twin Alpha.md", f"{folder}/Twin Beta.md"
    (config.VAULT_PATH / folder).mkdir(parents=True, exist_ok=True)
    # Beta is written first so creation order cannot pass for the tie-break.
    for name in (beta, alpha):
        (config.VAULT_PATH / name).write_text(_TWIN_TEXT, encoding="utf-8")
    server.default_runtime().indexer.index(force=True, target="vault")
    return alpha, beta


def _only(paths: tuple[str, str], results: list[dict]) -> list[dict]:
    return [r for r in results if r["file_path"] in paths]


def test_rank_key_orders_by_emitted_score_then_path(indexed_server) -> None:
    rows = [
        {"file_path": "b.md", "score": 0.5},
        {"file_path": "c.md", "score": 0.9},
        {"file_path": "a.md", "score": 0.5},
    ]

    assert [r["file_path"] for r in sorted(rows, key=indexed_server._rank_key)] == ["c.md", "a.md", "b.md"]


def test_keyword_tie_at_the_limit_keeps_the_smaller_path(server_on_temp_vault) -> None:
    alpha, _ = _write_twins(server_on_temp_vault, "Twins Keyword")

    results = asyncio.run(server_on_temp_vault.search("quokka", mode="keyword", limit=1))

    assert [r["file_path"] for r in results] == [alpha]


def test_hybrid_tie_is_ordered_by_path(server_on_temp_vault) -> None:
    twins = _write_twins(server_on_temp_vault, "Twins Hybrid")

    results = asyncio.run(server_on_temp_vault.search("quokka", limit=10))
    rows = _only(twins, results)

    assert [r["file_path"] for r in rows] == list(twins)
    assert rows[0]["score"] == rows[1]["score"]
    scores = [r["score"] for r in results]
    assert scores == sorted(scores, reverse=True)


def test_semantic_tie_is_ordered_by_path(server_on_temp_vault) -> None:
    twins = _write_twins(server_on_temp_vault, "Twins Semantic")

    results = asyncio.run(server_on_temp_vault.search("middleware gateway", mode="semantic", limit=20))
    rows = _only(twins, results)

    assert [r["file_path"] for r in rows] == list(twins)
    assert rows[0]["score"] == rows[1]["score"]


def test_search_deep_tie_is_ordered_by_path(server_on_temp_vault) -> None:
    twins = _write_twins(server_on_temp_vault, "Twins Deep")

    results = asyncio.run(server_on_temp_vault.search_deep("quokka", depth=1, limit=10))
    rows = _only(twins, results)

    assert [r["file_path"] for r in rows] == list(twins)
    assert rows[0]["score"] == rows[1]["score"]
    scores = [r["score"] for r in results]
    assert scores == sorted(scores, reverse=True)


def test_a_row_found_only_by_keywords_carries_its_title_and_excerpt(indexed_server) -> None:
    """Keyword mode has no dense side: title, excerpt and review state come from the document and its chunk."""
    results = asyncio.run(
        indexed_server.search(
            "middleware outage",
            mode="keyword",
            limit=5,
            fields=["file_path", "title", "excerpt", "needs_review", "age_days"],
        )
    )

    assert results
    assert all(row["title"] for row in results)
    assert all(row["excerpt"] for row in results)
    auth = next(row for row in results if row["file_path"] == "Architecture/Auth Architecture.md")
    assert auth["needs_review"] is True and auth["age_days"] > 0


def test_memories_are_excluded_by_type_and_by_folder_on_both_sides(server_on_temp_vault) -> None:
    """A ``type: memory`` note outside the folder and an untyped note inside it are both memories."""
    from knowledge import config

    vault = config.VAULT_PATH
    (vault / "Notes").mkdir(exist_ok=True)
    (vault / "Notes" / "Outage Memory.md").write_text(
        "---\ntitle: Outage Memory\ntype: memory\n---\n\nMiddleware outage runbook middleware outage.\n"
    )
    folder = vault / config.MEMORY_FOLDER
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "Untyped.md").write_text("---\ntitle: Untyped\n---\n\nMiddleware outage runbook middleware outage.\n")
    server_on_temp_vault.default_runtime().indexer.index(target="vault")
    memories = {"Notes/Outage Memory.md", f"{config.MEMORY_FOLDER}/Untyped.md"}

    for mode in ("hybrid", "semantic", "keyword"):
        found = {r["file_path"] for r in asyncio.run(server_on_temp_vault.search("middleware outage", mode=mode, limit=20))}
        assert not found & memories, mode
        with_memories = asyncio.run(
            server_on_temp_vault.search("middleware outage", mode=mode, limit=20, include_memories=True)
        )
        assert memories <= {r["file_path"] for r in with_memories}, mode
