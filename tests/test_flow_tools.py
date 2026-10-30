"""Tests for get_flow / compare_flows / search_deep (T-01.01).

Before the fix, ``get_flow`` and ``search`` were offloaded to a worker
thread via the ``@_offloaded`` decorator, which turns the registered tool
into an async, coroutine-returning wrapper. ``compare_flows`` and
``search_deep`` called them as if they were still plain sync functions —
every real call raised:

    TypeError: argument of type 'coroutine' is not iterable

The fix splits each tool's core logic into an undecorated ``_*_impl``
helper that in-process callers use directly, while the ``@mcp.tool()``
entry point still offloads for real MCP dispatch. These tests call the
real, decorated tool entry points end to end (via ``asyncio.run``, exactly
as FastMCP would await them) against a fixture vault — not mocks of the
business logic.
"""

from __future__ import annotations

import asyncio

import pytest

from knowledge import errors, server
from knowledge.errors import KnowledgeToolError
from knowledge.graph import LinkGraph


def test_get_flow_reads_a_real_provider_flow() -> None:
    result = asyncio.run(server.get_flow("Telvia"))

    assert result["provider"] == "Telvia"
    assert len(result["steps"]) == 4
    assert result["shared_login_steps"]
    assert any("access token" in step.lower() for step in result["shared_login_steps"])


def test_get_flow_unknown_provider_is_a_coded_error() -> None:
    """R13: a provider without a flow document is ``E_FLOW_NOT_FOUND``, never an ``{"error": ...}`` success."""
    with pytest.raises(KnowledgeToolError) as excinfo:
        asyncio.run(server.get_flow("Nonexistent Provider"))

    assert excinfo.value.code == errors.E_FLOW_NOT_FOUND
    assert "'Nonexistent Provider'" in excinfo.value.message


def test_compare_flows_diffs_two_real_providers() -> None:
    result = asyncio.run(server.compare_flows("Telvia", "Comet Argentina"))

    assert "error" not in result
    assert result["total_steps"]["Telvia"] == 4
    assert result["total_steps"]["Comet Argentina"] == 5
    assert len(result["common_steps"]) >= 2
    assert any("dashboard" in step.lower() for step in result["only_in_provider2"])
    # Both flows source shared_login_steps from the same file — must match.
    assert result["shared_login_steps"]
    assert any("access token" in s.lower() for s in result["shared_login_steps"])


def test_compare_flows_names_every_missing_provider() -> None:
    """R13: ``compare_flows`` is ``E_FLOW_NOT_FOUND`` naming each provider without a flow, not only the first."""
    with pytest.raises(KnowledgeToolError) as one:
        asyncio.run(server.compare_flows("Telvia", "Nonexistent Provider"))
    with pytest.raises(KnowledgeToolError) as both:
        asyncio.run(server.compare_flows("Nobody", "Nonexistent Provider"))

    assert one.value.code == both.value.code == errors.E_FLOW_NOT_FOUND
    assert "'Nonexistent Provider'" in one.value.message and "'Telvia'" not in one.value.message
    assert "'Nobody', 'Nonexistent Provider'" in both.value.message


def test_search_deep_returns_results_for_a_real_query() -> None:
    results = asyncio.run(server.search_deep("middleware outage", depth=1, limit=5))

    assert isinstance(results, list)
    assert results
    assert all("file_path" in r for r in results)


def test_search_deep_depth_zero_skips_expansion_but_still_returns_results() -> None:
    results = asyncio.run(server.search_deep("lookup service", depth=0, limit=3))

    assert isinstance(results, list)
    assert results


def test_search_deep_caps_depth_above_three_without_crashing() -> None:
    results = asyncio.run(server.search_deep("middleware outage", depth=10, limit=5))

    assert isinstance(results, list)
    assert results


def test_search_deep_falls_back_to_plain_search_when_graph_is_empty(
    monkeypatch,
) -> None:
    # Warm the index first: index() rebuilds the graph from scratch, so it
    # must run before we force an empty graph below, not after.
    asyncio.run(server.search("warmup", limit=1))

    monkeypatch.setattr(server._indexer, "_graph", LinkGraph())

    results = asyncio.run(server.search_deep("middleware outage", depth=2, limit=5))

    assert isinstance(results, list)
    assert results
