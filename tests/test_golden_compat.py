"""The compatibility comparator (``golden_compat``) on synthetic responses."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest
from golden_compat import (
    ReplaySession,
    catalog_problems,
    crosses_store_boundary,
    initialize_problems,
    migrate_1_4_to_2_0,
    response_problems,
)


def _result(rows: list[dict[str, Any]], *, wrapped: bool = True, is_error: bool = False) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(row, indent=2)} for row in rows],
        "isError": is_error,
        "structuredContent": {"result": rows} if wrapped else rows[0],
    }


def _object(obj: dict[str, Any]) -> dict[str, Any]:
    return _result([obj], wrapped=False)


def test_identical_responses_are_compatible() -> None:
    old = _result([{"file_path": "a.md", "score": 1.0}])

    assert response_problems("search", old, copy.deepcopy(old)) == []


def test_an_added_key_is_compatible() -> None:
    old = _result([{"file_path": "a.md", "score": 1.0}])
    new = _result([{"file_path": "a.md", "score": 1.0, "doc_id": "abc"}])

    assert response_problems("search", old, new) == []


def test_a_removed_key_is_a_problem() -> None:
    old = _result([{"file_path": "a.md", "score": 1.0}])
    new = _result([{"file_path": "a.md"}])

    assert response_problems("search", old, new) != []


def test_a_changed_value_is_a_problem() -> None:
    old = _result([{"file_path": "a.md", "score": 1.0}])
    new = _result([{"file_path": "a.md", "score": 0.9}])

    assert response_problems("search", old, new) != []


def test_a_different_block_count_is_a_problem() -> None:
    old = _result([{"file_path": "a.md", "score": 1.0}])
    new = _result([{"file_path": "a.md", "score": 1.0}, {"file_path": "b.md", "score": 0.5}])

    assert response_problems("search", old, new) != []


def test_is_error_must_match() -> None:
    old = _result([{"error": "File not found: x"}], wrapped=False)
    new = _result([{"error": "File not found: x"}], wrapped=False, is_error=True)

    assert response_problems("get_document", old, new) != []


def test_whitespace_in_the_text_block_is_not_a_difference() -> None:
    old = _result([{"file_path": "a.md", "score": 1.0}])
    new = copy.deepcopy(old)
    new["content"] = [{"type": "text", "text": json.dumps({"file_path": "a.md", "score": 1.0}, separators=(",", ":"))}]

    assert response_problems("search", old, new) == []


def test_search_ties_are_a_set_but_score_order_is_not() -> None:
    rows = [
        {"file_path": "a.md", "score": 1.0},
        {"file_path": "b.md", "score": 0.5},
        {"file_path": "c.md", "score": 0.5},
    ]
    tie_swapped = [rows[0], rows[2], rows[1]]
    score_swapped = [rows[1], rows[0], rows[2]]

    assert response_problems("search", _result(rows), _result(tie_swapped)) == []
    assert response_problems("search", _result(rows), _result(score_swapped)) != []


def test_get_related_rows_are_a_set() -> None:
    rows = [{"file": "a.md", "weight": 1.0}, {"file": "b.md", "weight": 1.0}]

    assert response_problems("get_related", _result(rows), _result(rows[::-1])) == []
    assert response_problems("get_related", _result(rows), _result(rows[:1])) != []


def test_get_related_rows_are_projected_onto_the_frozen_keys_as_a_multiset() -> None:
    old = [
        {"direction": "outgoing", "file": "a.md", "weight": 1.0},
        {"direction": "incoming", "file": "b.md", "weight": 1.0},
    ]
    reordered_with_new_keys = [{**old[1], "doc_id": "bb"}, {**old[0], "doc_id": "aa"}]
    lost_a_key = [{"direction": "incoming", "file": "b.md"}, old[0]]
    duplicated = [old[0], old[0]]

    assert response_problems("get_related", _result(old), _result(reordered_with_new_keys)) == []
    assert response_problems("get_related", _result(old), _result(lost_a_key)) != []
    assert response_problems("get_related", _result(old), _result(duplicated)) != []


def test_get_impact_depths_are_sets() -> None:
    old = _object({"source": "s.md", "depth_1": ["a.md", "b.md"], "total_affected": 2})
    new = _object({"source": "s.md", "depth_1": ["b.md", "a.md"], "total_affected": 2})
    wrong = _object({"source": "s.md", "depth_1": ["a.md", "c.md"], "total_affected": 2})

    assert response_problems("get_impact", old, new) == []
    assert response_problems("get_impact", old, wrong) != []


def test_find_path_may_pick_another_path_of_the_same_length() -> None:
    base = {"from": "a.md", "to": "c.md", "length": 2, "found": True}
    old = _object({**base, "path": ["a.md", "b.md", "c.md"]})
    other = _object({**base, "path": ["a.md", "x.md", "c.md"]})
    longer = _object({**base, "length": 3, "path": ["a.md", "x.md", "y.md", "c.md"]})

    assert response_problems("find_path", old, other) == []
    assert response_problems("find_path", old, longer) != []


def test_an_unordered_tool_still_compares_lists_in_order() -> None:
    old = _object({"items": ["a", "b"]})
    new = _object({"items": ["b", "a"]})

    assert response_problems("get_stale_docs", old, new) != []


def _catalog() -> dict[str, Any]:
    return {
        "tools": [
            {
                "name": "get_document",
                "description": "Read.",
                "inputSchema": {
                    "properties": {"file_path": {"title": "File Path", "type": "string"}},
                    "required": ["file_path"],
                    "type": "object",
                },
                "outputSchema": {"type": "object"},
            }
        ]
    }


def test_an_identical_catalog_is_compatible() -> None:
    assert catalog_problems(_catalog(), _catalog()) == []


def test_catalog_growth_and_rewording_are_compatible() -> None:
    new = _catalog()
    new["tools"][0]["description"] = "Reworded."
    new["tools"][0]["inputSchema"]["properties"]["vault"] = {"type": "string", "default": None}
    new["tools"].append({"name": "list_vaults", "inputSchema": {"properties": {}, "type": "object"}})

    assert catalog_problems(_catalog(), new) == []


@pytest.mark.parametrize(
    "mutate",
    [
        lambda c: c["tools"][0]["inputSchema"]["properties"].pop("file_path"),
        lambda c: c["tools"][0]["inputSchema"]["properties"]["file_path"].update(type="integer"),
        lambda c: c["tools"][0]["inputSchema"].update(required=["file_path", "extra"]),
        lambda c: c["tools"][0].update(outputSchema={"type": "array"}),
        lambda c: c["tools"].insert(0, {"name": "first", "inputSchema": {"properties": {}, "type": "object"}}),
        lambda c: c["tools"].pop(0),
    ],
    ids=["removed-parameter", "changed-type", "newly-required", "output-schema", "reordered", "removed-tool"],
)
def test_catalog_regressions_are_problems(mutate) -> None:
    new = _catalog()
    mutate(new)

    assert catalog_problems(_catalog(), new) != []


def test_a_relaxed_required_is_compatible() -> None:
    new = _catalog()
    new["tools"][0]["inputSchema"]["required"] = []

    assert catalog_problems(_catalog(), new) == []


# --- values relaxed across a version -------------------------------------------

ACROSS = {"frozen_version": "1.1.0", "live_version": "1.2.0"}
WITHIN = {"frozen_version": "1.1.0", "live_version": "1.1.0"}


def test_the_store_boundary_is_crossed_only_from_below_1_2() -> None:
    assert crosses_store_boundary("1.1.0", "1.2.0") is True
    assert crosses_store_boundary("1.0.0", "1.3.1") is True
    assert crosses_store_boundary("1.1.0", "1.1.0") is False
    assert crosses_store_boundary("1.2.0", "1.3.0") is False
    assert crosses_store_boundary(None, "1.2.0") is False


def test_store_bound_scores_and_order_are_free_across_the_boundary() -> None:
    old = _result([{"file_path": "a.md", "score": 1.0}, {"file_path": "b.md", "score": 0.5}])
    new = _result(
        [{"file_path": "c.md", "score": 0.7}, {"file_path": "a.md", "score": 0.6}, {"file_path": "b.md", "score": 0.1}]
    )

    assert response_problems("search", old, new, arguments={"limit": 5}, **ACROSS) == []
    assert response_problems("search", old, new, arguments={"limit": 5}, **WITHIN) != []
    assert response_problems("search", old, new, arguments={"limit": 5}) != []


def test_only_the_store_bound_tools_are_relaxed() -> None:
    old = _result([{"file_path": "a.md", "score": 1.0}])
    new = _result([{"file_path": "b.md", "score": 0.5}])

    assert response_problems("search_by_metadata", old, new, **ACROSS) != []
    assert response_problems("get_related", old, new, **ACROSS) != []


@pytest.mark.parametrize(
    ("rows", "limit"),
    [
        ([{"file_path": "a.md"}], 5),
        ([{"file_path": "a.md", "score": "high"}], 5),
        ([], 5),
        ([{"file_path": f"{n}.md", "score": 0.1} for n in range(3)], 2),
    ],
    ids=["lost-a-key", "changed-type", "no-rows", "over-limit"],
)
def test_store_bound_shape_regressions_are_problems(rows: list[dict[str, Any]], limit: int) -> None:
    old = _result([{"file_path": "a.md", "score": 1.0}])

    assert response_problems("search", old, _result(rows), arguments={"limit": limit}, **ACROSS) != []


def test_a_nullable_pointer_may_become_null_across_the_boundary() -> None:
    old = _result([{"file_path": "a.md", "score": 1.0, "snippet": "x"}])
    new = _result([{"file_path": "a.md", "score": 0.4, "snippet": None}])

    assert response_problems("search", old, new, arguments={"limit": 2}, **ACROSS) == []


def _pack(**overrides: Any) -> dict[str, Any]:
    pack = {
        "query": "q",
        "vault": "v",
        "budget_tokens": 600,
        "used_tokens": 500,
        "items": [{"source": "vault", "file_path": "a.md", "score": 1.0, "text": "a"}],
        "dropped": [],
        "sources_used": ["vault"],
    }
    pack.update(overrides)
    return _object(pack)


def test_a_context_pack_is_compared_by_shape_and_budget() -> None:
    reranked = _pack(used_tokens=420, items=[{"source": "vault", "file_path": "b.md", "score": 0.3, "text": "b"}])

    assert response_problems("context_pack", _pack(), reranked, **ACROSS) == []
    assert response_problems("context_pack", _pack(), _pack(used_tokens=601), **ACROSS) != []
    assert response_problems("context_pack", _pack(), _pack(query="other"), **ACROSS) != []
    assert response_problems("context_pack", _pack(), _pack(items=[]), **ACROSS) != []
    assert response_problems("context_pack", _pack(), reranked, **WITHIN) != []


def test_the_contract_key_must_name_the_live_version() -> None:
    old = _object({"contract": "1.1.0", "default": "v"})

    assert response_problems("list_vaults", old, _object({"contract": "1.2.0", "default": "v"}), **ACROSS) == []
    assert response_problems("list_vaults", old, _object({"contract": "1.1.0", "default": "v"}), **ACROSS) != []
    assert response_problems("list_vaults", old, _object({"contract": "1.2.0", "default": "v"})) != []


def test_a_presence_bound_key_may_change_value_but_not_disappear(monkeypatch: pytest.MonkeyPatch) -> None:
    """The rule still works for a key declared presence-bound; 2.0.0 declares none (R11 retires ``export_hint``)."""
    import golden_compat

    assert golden_compat.PRESENCE_BOUND == frozenset()
    monkeypatch.setattr(golden_compat, "PRESENCE_BOUND", frozenset({"reindex.export_hint"}))
    old = _object({"vault": "v", "export_hint": "old hint"})

    assert response_problems("reindex", old, _object({"vault": "v", "export_hint": "new hint"}), **ACROSS) == []
    assert response_problems("reindex", old, _object({"vault": "v"}), **ACROSS) != []
    assert response_problems("reindex", old, _object({"vault": "v", "export_hint": "new hint"}), **WITHIN) != []


def test_the_first_instructions_line_follows_the_live_version() -> None:
    old = {"instructions": "knowledge-contract: 1.1.0\nRead by section.", "serverInfo": {"name": "Knowledge"}}
    bumped = {"instructions": "knowledge-contract: 1.2.0\nRead by section.", "serverInfo": {"name": "Knowledge"}}
    reworded = {"instructions": "knowledge-contract: 1.2.0\nRead whole files.", "serverInfo": {"name": "Knowledge"}}

    assert initialize_problems(old, old, **WITHIN) == []
    assert initialize_problems(old, bumped, **ACROSS) == []
    assert initialize_problems(old, bumped, **WITHIN) != []
    assert initialize_problems(old, reworded, **ACROSS) != []


# --- the declared migration from 1.4.0 to 2.0.0 --------------------------------


def test_r19_drops_the_structured_view_of_a_response() -> None:
    frozen = _result([{"file_path": "a.md", "score": 1.0}])

    migrated = migrate_1_4_to_2_0("search__pointers", frozen, {"limit": 5})

    assert "structuredContent" not in migrated
    assert migrated["content"] == frozen["content"] and migrated["isError"] is False
    assert "structuredContent" in frozen, "the frozen golden is never edited in place"


def test_r19_drops_the_output_schema_of_every_catalog_tool() -> None:
    frozen = {"tools": [{"name": "search", "inputSchema": {"type": "object"}, "outputSchema": {"type": "object"}}]}

    for name in ("tools_list", "tools_list__jira"):
        assert migrate_1_4_to_2_0(name, frozen) == {"tools": [{"name": "search", "inputSchema": {"type": "object"}}]}


def _named_catalog(*names: str) -> dict[str, Any]:
    return {"tools": [{"name": name, "inputSchema": {"type": "object"}} for name in names]}


_CATALOG_1_4_0 = (
    "get_document",
    "suggest_links",
    "get_flow",
    "get_impact",
    "sync_confluence",
    "search_code",
    "list_vaults",
    "sync_jira",
)


def test_r12_to_r16_keep_the_core_and_append_only_the_extensions_a_vault_declares() -> None:
    frozen = _named_catalog(*_CATALOG_1_4_0)

    plain = migrate_1_4_to_2_0("tools_list", frozen)
    flows = migrate_1_4_to_2_0("tools_list", frozen, session=ReplaySession(features=frozenset({"flows"})))

    assert [t["name"] for t in plain["tools"]] == ["get_document", "get_impact", "list_vaults", "sync_jira"]
    assert [t["name"] for t in flows["tools"]] == ["get_document", "get_impact", "list_vaults", "get_flow", "sync_jira"]


def test_under_legacy_the_catalog_keeps_its_names_and_order_and_announces_the_removal() -> None:
    frozen = _named_catalog(*_CATALOG_1_4_0)
    frozen["tools"][5]["_meta"] = {"knowledge/deprecated": True, "knowledge/removal": "2.0.0"}

    legacy = migrate_1_4_to_2_0("tools_list", frozen, session=ReplaySession(profile="legacy"))

    assert [t["name"] for t in legacy["tools"]] == list(_CATALOG_1_4_0)
    assert legacy["tools"][5]["_meta"] == {"knowledge/deprecated": True, "knowledge/removal": "2.1.0"}


def test_a_call_to_an_extension_tool_off_or_a_retired_one_migrates_to_its_coded_error() -> None:
    frozen = _object({"provider": "Telvia", "steps": []})

    disabled = migrate_1_4_to_2_0("get_flow", frozen, {"provider": "Telvia"})
    served = migrate_1_4_to_2_0("get_flow", frozen, {"provider": "Telvia"}, session=ReplaySession(profile="legacy"))
    retired = migrate_1_4_to_2_0("search_code", _result([{"info": "redirect"}]), {"query": "x"})

    assert disabled["isError"] is True and disabled["content"][0]["text"].startswith("E_FEATURE_DISABLED: ")
    assert served == {k: v for k, v in frozen.items() if k != "structuredContent"}
    assert retired["isError"] is True and retired["content"][0]["text"].startswith("E_TOOL_RETIRED: search_code ")


def test_a_changed_meta_is_a_catalog_problem() -> None:
    old = _named_catalog("search_code")
    new = _named_catalog("search_code")
    new["tools"][0]["_meta"] = {"knowledge/removal": "2.1.0"}

    assert catalog_problems(old, new) == ["search_code: _meta None -> {'knowledge/removal': '2.1.0'}"]


def _texts(result: dict[str, Any]) -> list[Any]:
    return [json.loads(block["text"]) for block in result["content"]]


def test_r4_cuts_a_default_read_at_3000_characters_and_the_metadata_to_the_whitelist() -> None:
    frozen = _result(
        [{"file_path": "a.md", "metadata": {"title": "A", "type": "note", "severity": "P1"}, "content": "x" * 3500}],
        wrapped=False,
    )

    migrated = migrate_1_4_to_2_0("get_document", frozen, {"file_path": "a.md"})
    asked = migrate_1_4_to_2_0("get_document", frozen, {"file_path": "a.md", "max_chars": 4000})

    assert _texts(migrated) == [
        {"file_path": "a.md", "metadata": {"type": "note"}, "content": "x" * 3000, "truncated": True, "next_offset": 3000}
    ]
    assert "structuredContent" not in migrated  # R19
    assert _texts(asked)[0]["content"] == "x" * 3500


def test_r6_turns_the_rows_into_one_page_by_path_and_r7_keeps_the_facet_order() -> None:
    rows = [{"file_path": f"{name}.md"} for name in "zyxwvutsrqponmlkjihgfedcba"]
    facets = [{"facet": "type", "value": value, "count": 1} for value in "zyxwvutsrqponmlkjihgfedcba"]

    page = _texts(migrate_1_4_to_2_0("search_by_metadata", _result(rows), {}))
    paged = _texts(migrate_1_4_to_2_0("search_by_metadata__page", _result(rows[:2]), {"limit": 2, "offset": 1}))
    facet_page = _texts(migrate_1_4_to_2_0("search_by_metadata__facets", _result(facets), {"facets": True}))

    assert page == [{"total": 26, "offset": 0, "items": sorted(rows, key=lambda r: r["file_path"])[:20]}]
    assert paged == [{"offset": 1, "items": rows[:2]}]
    assert facet_page == [{"total": 26, "offset": 0, "items": facets[:20]}]


def test_r8_merges_a_two_way_link_drops_file_and_rounds_the_weight() -> None:
    rows = [
        {"file": "a.md", "file_path": "a.md", "direction": "outgoing", "weight": 1.138629436111989},
        {"file": "a.md", "file_path": "a.md", "direction": "incoming", "weight": 1.138629436111989},
        {"file": "b.md", "file_path": "b.md", "direction": "incoming", "weight": 1.0},
    ]

    migrated = _texts(migrate_1_4_to_2_0("get_related", _result(rows), {"file_path": "hub.md"}))

    assert migrated == [
        {"file_path": "a.md", "direction": "both", "weight": 1.139},
        {"file_path": "b.md", "direction": "incoming", "weight": 1.0},
    ]


def test_r9_caps_each_depth_at_twenty_and_reports_omitted() -> None:
    impact = {"source": "s.md", "depth_1": [f"{n:02d}.md" for n in range(25)], "depth_2": ["z.md"], "total_affected": 26}

    migrated = _texts(migrate_1_4_to_2_0("get_impact", _result([impact], wrapped=False), {"file_path": "s.md"}))
    asked = migrate_1_4_to_2_0("get_impact", _result([impact], wrapped=False), {"file_path": "s.md", "max_items": 30})

    assert migrated == [{**impact, "depth_1": impact["depth_1"][:20], "omitted": {"depth_1": 5}}]
    assert _texts(asked) == [impact]


def test_r11_drops_the_export_hint_and_a_default_call_no_longer_pulls() -> None:
    stats = {"files": 2, "export_hint": "hint", "vault_sync": "skipped_no_repo", "index_version": 1}

    default = _texts(migrate_1_4_to_2_0("reindex", _object(stats), {"target": "vault"}))
    asked = _texts(migrate_1_4_to_2_0("reindex", _object(stats), {"target": "vault", "pull": True}))

    assert default == [{"files": 2, "vault_sync": "skipped_pull_false", "index_version": 1}]
    assert asked == [{"files": 2, "vault_sync": "skipped_no_repo", "index_version": 1}]


def test_r12_turns_the_missing_credentials_object_into_e_secret_missing() -> None:
    features = ReplaySession(features=frozenset({"confluence"}))
    default = _object({"error": "Confluence credentials not configured. Set CONFLUENCE_URL, CONFLUENCE_EMAIL, and CONFLUENCE_TOKEN in your .env file."})
    declared = _object({"error": "Confluence credentials not configured. Set CONFLUENCE_EMAIL, VAULT_CONFLUENCE_TOKEN in the environment."})

    plain = migrate_1_4_to_2_0("sync_confluence", default, {"dry_run": True}, session=features)
    named = migrate_1_4_to_2_0("sync_confluence", declared, {"dry_run": True}, session=features)

    assert plain["isError"] is True and named["isError"] is True
    assert plain["content"][0]["text"] == (
        "Error executing tool sync_confluence: E_SECRET_MISSING: sync_confluence needs CONFLUENCE_URL, "
        "CONFLUENCE_EMAIL, CONFLUENCE_TOKEN; export them or declare them under integrations.confluence in the "
        "vault's vault.yaml"
    )
    assert named["content"][0]["text"].startswith(
        "Error executing tool sync_confluence: E_SECRET_MISSING: sync_confluence needs CONFLUENCE_EMAIL, "
        "VAULT_CONFLUENCE_TOKEN, which"
    )


def test_r12_drops_space_key_and_page_id_from_the_catalog() -> None:
    tool = {
        "name": "sync_confluence",
        "inputSchema": {"type": "object", "properties": {"space_key": {}, "page_id": {}, "dry_run": {}}},
    }

    migrated = migrate_1_4_to_2_0(
        "tools_list", {"tools": [tool]}, session=ReplaySession(features=frozenset({"confluence"}))
    )

    assert list(migrated["tools"][0]["inputSchema"]["properties"]) == ["dry_run"]


def test_r13_turns_a_missing_flow_object_into_e_flow_not_found_naming_every_provider() -> None:
    flows = ReplaySession(features=frozenset({"flows"}))
    single = _object({"error": "No flow file found for provider 'Ghost'."})
    pair = _object({"error": "Provider 1: No flow file found for provider 'Ghost'."})

    one = migrate_1_4_to_2_0("get_flow", single, {"provider": "Ghost"}, session=flows)
    both = migrate_1_4_to_2_0("compare_flows", pair, {"provider1": "Ghost", "provider2": "Nobody"}, session=flows)

    assert one["isError"] is True and both["isError"] is True
    assert one["content"][0]["text"].startswith(
        "Error executing tool get_flow: E_FLOW_NOT_FOUND: the vault has no flow document for provider 'Ghost':"
    )
    assert "provider 'Ghost', 'Nobody':" in both["content"][0]["text"]
