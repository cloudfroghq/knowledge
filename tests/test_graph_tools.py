"""Interface tests for the wiki-link graph tools.

``find_path``, ``get_related`` and ``get_impact`` all read the same structure:
the link graph the indexer rebuilds on every vault pass. The fixture vault
wires a deliberate topology so each traversal has a single knowable answer:

    Portal Service ─┬─> Middleware Service ─┬─> Runbook - Middleware Outage
                    └─> Lookup Service ─────┘
    Auth Architecture ────> Middleware Service, Runbook - Middleware Outage
    Session Architecture ─> Middleware Service, Runbook - Middleware Outage

``get_impact`` walks that graph backwards (who would I break?), ``find_path``
walks it undirected, and ``get_related`` reads one node's neighbourhood.
"""

from __future__ import annotations

import asyncio
import itertools

import pytest

from knowledge.graph import LinkGraph

RUNBOOK = "Runbooks/Runbook - Middleware Outage.md"
MIDDLEWARE = "Services/Middleware Service.md"
PORTAL = "Services/Portal Service.md"
LOOKUP = "Services/Lookup Service.md"
AUTH_ARCH = "Architecture/Auth Architecture.md"
SESSION_ARCH = "Architecture/Session Architecture.md"


# --- find_path -------------------------------------------------------------


def test_find_path_walks_two_hops_between_real_documents(indexed_server) -> None:
    result = asyncio.run(indexed_server.find_path(PORTAL, RUNBOOK))

    assert result["found"] is True
    assert result["length"] == 2
    assert result["path"][0] == PORTAL
    assert result["path"][-1] == RUNBOOK
    # Both two-hop routes go through a service node, never straight across.
    assert result["path"][1] in {MIDDLEWARE, LOOKUP}


def test_find_path_from_a_node_to_itself_is_length_zero(indexed_server) -> None:
    result = asyncio.run(indexed_server.find_path(MIDDLEWARE, MIDDLEWARE))

    assert result["found"] is True
    assert result["length"] == 0
    assert result["path"] == [MIDDLEWARE]


@pytest.mark.parametrize(
    ("ends", "missing"),
    [
        (("Nowhere/Ghost.md", RUNBOOK), ["Nowhere/Ghost.md"]),
        ((RUNBOOK, "Nowhere/Ghost.md"), ["Nowhere/Ghost.md"]),
        (("Nowhere/A.md", "Nowhere/B.md"), ["Nowhere/A.md", "Nowhere/B.md"]),
        (("Nowhere/Ghost.md", "Nowhere/Ghost.md"), ["Nowhere/Ghost.md"]),
    ],
    ids=["from", "to", "both", "itself"],
)
def test_find_path_names_the_ends_missing_from_the_graph(indexed_server, ends, missing) -> None:
    from knowledge import errors
    from knowledge.errors import KnowledgeToolError

    with pytest.raises(KnowledgeToolError) as excinfo:
        asyncio.run(indexed_server.find_path(*ends))

    assert excinfo.value.code == errors.E_FILE_NOT_IN_GRAPH
    assert str(missing) in str(excinfo.value)


def test_find_path_max_depth_below_the_real_distance_finds_nothing(indexed_server) -> None:
    result = asyncio.run(indexed_server.find_path(PORTAL, RUNBOOK, max_depth=1))

    assert result["found"] is False
    assert result["path"] is None


def test_weighted_find_path_reports_its_traversal_cost(indexed_server) -> None:
    result = asyncio.run(indexed_server.find_path(PORTAL, RUNBOOK, weighted=True))

    assert result["found"] is True
    assert result["weighted"] is True
    assert result["length"] == 2
    # Dijkstra runs on cost 1/weight and weights only ever grow (Hebbian
    # reinforcement from earlier retrievals), so a learned route is cheaper
    # than its hop count but never free.
    assert 0 < result["cost"] <= 2.0


# --- get_related -----------------------------------------------------------


def test_get_related_returns_outgoing_and_incoming_links(indexed_server) -> None:
    results = asyncio.run(indexed_server.get_related(MIDDLEWARE, limit=10))

    by_direction: dict[str, set[str]] = {}
    for entry in results:
        by_direction.setdefault(entry["direction"], set()).add(entry["file_path"])

    assert by_direction["outgoing"] == {LOOKUP, RUNBOOK}
    assert by_direction["incoming"] == {PORTAL, AUTH_ARCH, SESSION_ARCH}


def test_get_related_limit_bounds_the_neighbourhood(indexed_server) -> None:
    results = asyncio.run(indexed_server.get_related(MIDDLEWARE, limit=2))

    assert len(results) == 2


def test_get_related_for_an_unknown_file_is_empty_not_an_error(indexed_server) -> None:
    results = asyncio.run(indexed_server.get_related("Nowhere/Ghost.md"))

    assert results == []


def test_include_suggested_adds_unlinked_but_related_documents(indexed_server) -> None:
    """The portal and the outage runbook share two neighbours and no link."""
    linked = asyncio.run(indexed_server.get_related(PORTAL, limit=6))
    assert all(r["direction"] != "suggested" for r in linked)

    with_suggestions = asyncio.run(
        indexed_server.get_related(PORTAL, limit=6, include_suggested=True)
    )
    suggested = [r for r in with_suggestions if r["direction"] == "suggested"]

    assert {r["file_path"] for r in suggested} == {RUNBOOK}
    assert suggested[0]["shared_neighbors"] >= 2
    assert 0 < suggested[0]["confidence"] <= 1


# --- get_impact ------------------------------------------------------------


def test_get_impact_reports_direct_and_transitive_backlinks(indexed_server) -> None:
    result = asyncio.run(indexed_server.get_impact(RUNBOOK, max_depth=2))

    assert result["source"] == RUNBOOK
    assert set(result["depth_1"]) == {LOOKUP, MIDDLEWARE, AUTH_ARCH, SESSION_ARCH}
    # The portal never references the runbook: it only reaches it through the
    # two services it does reference.
    assert result["depth_2"] == [PORTAL]
    assert result["total_affected"] == 5


def test_get_impact_depth_one_stops_at_direct_references(indexed_server) -> None:
    result = asyncio.run(indexed_server.get_impact(RUNBOOK, max_depth=1))

    assert set(result["depth_1"]) == {LOOKUP, MIDDLEWARE, AUTH_ARCH, SESSION_ARCH}
    assert result["total_affected"] == 4
    assert "depth_2" not in result


def test_get_impact_follows_backlinks_not_outgoing_links(indexed_server) -> None:
    """The portal points at everything and nothing points at it."""
    result = asyncio.run(indexed_server.get_impact(PORTAL, max_depth=3))

    assert result["total_affected"] == 0


# --- declared order, on graphs built by hand -------------------------------
#
# The fixture vault has one answer per question, so it cannot show how a tie
# is broken. These graphs are wired with ties on purpose. Each is built from
# several insertion orders: the answer must be the declared one every time,
# never the walk order of a ``set``.

def _graph(links: dict[str, list[str]], order: tuple[str, ...] | None = None) -> LinkGraph:
    graph = LinkGraph()
    for source in order or tuple(links):
        graph.set_links(source, links[source])
    return graph


def _orders(links: dict[str, list[str]]) -> list[tuple[str, ...]]:
    return list(itertools.permutations(links))


# Two paths of two hops from ``start`` to ``end``: through ``m-zeta`` and
# through ``m-alpha``. Three paths of three hops from ``s`` to ``t``.
DIAMOND = {"start": ["m-zeta", "m-alpha"], "m-zeta": ["end"], "m-alpha": ["end"]}
LADDER = {"s": ["b", "a"], "a": ["z", "y"], "b": ["c"], "z": ["t"], "y": ["t"], "c": ["t"]}


@pytest.mark.parametrize("order", _orders(DIAMOND))
def test_shortest_path_picks_the_lexicographically_smallest_of_equal_length(order: tuple[str, ...]) -> None:
    graph = _graph(DIAMOND, order)

    assert graph.shortest_path("start", "end") == ["start", "m-alpha", "end"]
    assert graph.shortest_path("end", "start") == ["end", "m-alpha", "start"]


@pytest.mark.parametrize("order", _orders(LADDER)[:24])
def test_shortest_path_breaks_the_tie_at_every_step(order: tuple[str, ...]) -> None:
    graph = _graph(LADDER, order)

    assert graph.shortest_path("s", "t") == ["s", "a", "y", "t"]


def test_shortest_path_respects_max_hops() -> None:
    graph = _graph(LADDER)

    assert graph.shortest_path("s", "t", max_hops=2) is None
    assert graph.shortest_path("s", "t", max_hops=3) == ["s", "a", "y", "t"]
    assert graph.shortest_path("s", "s", max_hops=1) == ["s"]


def test_shortest_path_between_disconnected_nodes_is_none() -> None:
    graph = _graph({"a": ["b"], "c": ["d"]})

    assert graph.shortest_path("a", "d") is None


@pytest.mark.parametrize("order", _orders(DIAMOND))
def test_weighted_path_breaks_equal_cost_ties_by_path(order: tuple[str, ...]) -> None:
    graph = _graph(DIAMOND, order)

    path, cost = graph.shortest_weighted_path("start", "end")

    assert path == ["start", "m-alpha", "end"]
    assert cost == 2.0


def test_weighted_path_prefers_a_learned_highway_over_the_smaller_path() -> None:
    graph = _graph(DIAMOND)
    graph.reinforce("start", "m-zeta", co_occurrence=50)
    graph.reinforce("m-zeta", "end", co_occurrence=50)

    path, cost = graph.shortest_weighted_path("start", "end")

    assert path == ["start", "m-zeta", "end"]
    assert cost < 2.0


def test_weighted_path_respects_max_hops() -> None:
    graph = _graph(LADDER)

    assert graph.shortest_weighted_path("s", "t", max_hops=2) == (None, float("inf"))


HUB = {"hub": ["c.md", "a.md", "b.md"], "d.md": ["hub"], "0.md": ["hub"]}


@pytest.mark.parametrize("order", _orders(HUB))
def test_related_orders_by_weight_then_direction_then_path(order: tuple[str, ...]) -> None:
    graph = _graph(HUB, order)

    rows = graph.related("hub", limit=10)

    assert [(r["direction"], r["file"]) for r in rows] == [
        ("outgoing", "a.md"),
        ("outgoing", "b.md"),
        ("outgoing", "c.md"),
        ("incoming", "0.md"),
        ("incoming", "d.md"),
    ]


@pytest.mark.parametrize("order", _orders(HUB))
def test_related_cuts_a_tie_at_the_limit_after_sorting(order: tuple[str, ...]) -> None:
    graph = _graph(HUB, order)

    assert [r["file"] for r in graph.related("hub", limit=2)] == ["a.md", "b.md"]


def test_related_puts_a_stronger_learned_edge_first() -> None:
    graph = _graph(HUB)
    graph.reinforce("hub", "d.md", co_occurrence=3)

    rows = graph.related("hub", limit=2)

    assert [(r["direction"], r["file"]) for r in rows] == [("incoming", "d.md"), ("outgoing", "a.md")]


FAN_IN = {"z.md": ["src"], "a.md": ["src"], "m.md": ["src"], "y.md": ["a.md"], "b.md": ["z.md"]}


@pytest.mark.parametrize("order", _orders(FAN_IN)[:24])
def test_impact_lists_each_depth_by_path(order: tuple[str, ...]) -> None:
    graph = _graph(FAN_IN, order)

    impact = graph.get_impact("src", max_depth=2)

    assert impact["depth_1"] == ["a.md", "m.md", "z.md"]
    assert impact["depth_2"] == ["b.md", "y.md"]
    assert impact["total_affected"] == 5


# ``x`` links n1-n3. ``a`` shares all three (confidence 1.0), ``c`` three of
# its four (1.0 over x's three), ``b`` two of its two (1.0) and ``d`` two of
# its five (0.67 over x's three); none of them links ``x``.
SHARED = {
    "x": ["n1", "n2", "n3"],
    "a": ["n1", "n2", "n3"],
    "b": ["n1", "n2"],
    "c": ["n1", "n2", "n3", "n4"],
    "d": ["n1", "n2", "n4", "n5", "n6"],
}


def test_suggest_for_orders_by_confidence_then_shared_neighbours_then_path() -> None:
    graph = _graph(SHARED)

    rows = graph.suggest_for("x")

    assert [(r["file"], r["confidence"], r["shared_neighbors"]) for r in rows] == [
        ("a", 1.0, 3),
        ("c", 1.0, 3),
        ("b", 1.0, 2),
        ("d", 0.67, 2),
    ]


@pytest.mark.parametrize("document", ["x", "a", "n1", "d", "nowhere"])
def test_suggest_for_is_the_global_report_cut_to_one_document(document: str) -> None:
    graph = _graph(SHARED)

    everything = graph.suggest_edges(limit=10_000)
    expected = {
        (s["target"] if s["source"] == document else s["source"], s["shared_neighbors"], s["confidence"])
        for s in everything
        if document in (s["source"], s["target"])
    }

    assert {(r["file"], r["shared_neighbors"], r["confidence"]) for r in graph.suggest_for(document)} == expected


def test_suggested_rows_are_cut_at_the_limit_in_their_order(indexed_server, monkeypatch: pytest.MonkeyPatch) -> None:
    """``get_related`` appends the document's suggestions in their order, cut at ``limit``."""
    ghost = "Nowhere/Ghost.md"
    graph = indexed_server.default_runtime().indexer.graph
    monkeypatch.setattr(graph, "suggest_for", lambda path, **_: [
        {"file": name, "confidence": 0.9, "shared_neighbors": 2} for name in ("a.md", "m.md", "b.md", "z.md")
    ] if path == ghost else [])

    rows = asyncio.run(indexed_server.get_related(ghost, limit=3, include_suggested=True))

    assert [r["file_path"] for r in rows] == ["a.md", "m.md", "b.md"]
    assert all(r["direction"] == "suggested" for r in rows)


def test_related_merges_a_two_way_link_into_one_row() -> None:
    graph = _graph({"hub": ["a.md", "b.md"], "a.md": ["hub"], "c.md": ["hub"]})

    rows = graph.related("hub", limit=10)

    assert [(r["direction"], r["file"]) for r in rows] == [("both", "a.md"), ("outgoing", "b.md"), ("incoming", "c.md")]


def test_related_rounds_the_weight_before_it_sorts() -> None:
    graph = _graph(HUB)
    graph.reinforce("hub", "d.md", co_occurrence=3)

    rows = graph.related("hub", limit=1, ndigits=3)

    assert rows == [{"file": "d.md", "direction": "incoming", "weight": round(graph.weight("hub", "d.md"), 3)}]


def test_find_path_on_the_fixture_takes_the_smaller_of_the_two_service_routes(indexed_server) -> None:
    """Portal reaches the runbook through Lookup or Middleware; Lookup sorts first."""
    result = asyncio.run(indexed_server.find_path(PORTAL, RUNBOOK))

    assert result["path"] == [PORTAL, LOOKUP, RUNBOOK]
