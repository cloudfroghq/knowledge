"""Interface tests for the retired ``search_code`` tool.

ADR-039 retired the code-embeddings collection this tool used to query:
graphify is now the platform's only code-understanding engine, and it runs
local, not through this server. The tool stays registered — removing it
outright would break the exact tool-set freeze in ``test_tool_registry.py``
and hand a caller a bare protocol error instead of guidance — but every call
now returns the same redirect regardless of its arguments, naming the concrete
graphify/rg/vault commands that replace it.

A test that only checks "it returns something" would pass on a redirect that
forgot to name a replacement, so every case here pins actual command names.
"""

from __future__ import annotations

import asyncio


def test_search_code_always_returns_a_single_advisory_row(indexed_server) -> None:
    results = asyncio.run(indexed_server.search_code("anything"))

    assert len(results) == 1
    assert "file_path" not in results[0], "a redirect must not look like a hit"


def test_the_advisory_cites_adr_039_as_the_reason(indexed_server) -> None:
    results = asyncio.run(indexed_server.search_code("session token"))

    assert "ADR-039" in results[0]["info"]


def test_the_advisory_names_graphify_for_relationships_and_impact(indexed_server) -> None:
    info = asyncio.run(indexed_server.search_code("anything"))[0]["info"]

    assert "graphify explain" in info
    assert "graphify affected" in info


def test_the_advisory_names_graphify_query_for_a_real_symbol(indexed_server) -> None:
    info = asyncio.run(indexed_server.search_code("anything"))[0]["info"]

    assert "graphify query" in info
    assert "symbol" in info


def test_the_advisory_names_rg_for_literal_text(indexed_server) -> None:
    info = asyncio.run(indexed_server.search_code("anything"))[0]["info"]

    assert "rg" in info


def test_the_advisory_names_search_and_search_deep_for_intent(indexed_server) -> None:
    info = asyncio.run(indexed_server.search_code("anything"))[0]["info"]

    assert "search_deep" in info
    assert "vault" in info


def test_the_advisory_states_it_is_scheduled_for_removal(indexed_server) -> None:
    info = asyncio.run(indexed_server.search_code("anything"))[0]["info"]

    assert "removal" in info


def test_the_advisory_ignores_every_argument(indexed_server) -> None:
    """The redirect does not depend on query, language, limit or excerpt_chars.

    The tool keeps its old input schema on purpose — an MCP client that
    cached it from before the retirement must not fail on an unknown
    parameter — even though the arguments no longer change the answer.
    """
    default = asyncio.run(indexed_server.search_code("anything"))
    varied = asyncio.run(
        indexed_server.search_code("something else entirely", language="php", limit=50, excerpt_chars=5)
    )

    assert default == varied
