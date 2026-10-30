"""``context_pack`` and ``list_vaults``, and the contract version ``/health`` reports.

``context_pack`` is checked at two levels: its fill rules on hand-built
candidates (hard cap, trimming, duplicates, the memory share), where every
number is chosen by the test, and the tool itself over the fixture vault. The
memory server is never real here: ``tests/test_memory_http.py`` drives the
client against a fake one.
"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from knowledge import contract, errors, server, settings
from knowledge.errors import KnowledgeToolError
from knowledge.registry import VaultRegistry

FIXTURE_VAULTS = Path(__file__).resolve().parent / "fixtures" / "vaults"


def _vault_item(n: int, text: str) -> dict[str, Any]:
    doc = f"{n:010x}"
    return {
        "source": "vault",
        "doc_id": doc,
        "file_path": f"Docs/Doc {n}.md",
        "chunk_id": f"{doc}:1",
        "section": f"Doc {n}",
        "score": 1.0,
        "text": text,
    }


def _memory_item(n: int, text: str) -> dict[str, Any]:
    return {"source": "memory", "id": n, "type": "decision", "title": f"Memory {n}", "text": text}


# --- the estimator -------------------------------------------------------------------


def test_the_estimate_is_a_quarter_of_the_utf8_bytes_rounded_up() -> None:
    assert server.estimate_tokens("") == 1  # two bytes of JSON quotes
    assert server.estimate_tokens("abcdef") == 2  # "abcdef" is 8 bytes
    assert server.estimate_tokens("ñ") == 1  # 4 bytes: two quotes, two for the letter
    assert server.estimate_tokens({"text": "x" * 400}) == 103  # 411 bytes


# --- fill rules -----------------------------------------------------------------------


def test_items_that_fit_are_taken_whole_in_order() -> None:
    candidates = [_vault_item(1, "alpha " * 20), _vault_item(2, "beta " * 20)]

    items, dropped, used = server._fill(candidates, 1000)

    assert items == candidates
    assert dropped == []
    assert used == sum(server.estimate_tokens(item) for item in candidates)


def test_an_item_that_does_not_fit_is_trimmed_to_the_room_left() -> None:
    big = _vault_item(1, "word " * 2000)

    items, dropped, used = server._fill([big], 500)

    (taken,) = items
    assert taken["truncated"] is True
    assert big["text"].startswith(taken["text"])
    assert used == server.estimate_tokens(taken) <= 500
    # The cut is as long as the room allows: one more character would not fit.
    longer = {**taken, "text": big["text"][: len(taken["text"]) + 1]}
    assert server.estimate_tokens(longer) > 500
    assert dropped == []


def test_below_the_trim_floor_an_item_is_dropped_for_budget() -> None:
    first = _vault_item(1, "x" * (4 * 380))
    second = _vault_item(2, "y" * 2000)

    items, dropped, used = server._fill([first, second], 500)

    assert items == [first]
    assert 500 - used < server.PACK_MIN_TRIM_TOKENS
    assert dropped == [
        {
            "source": "vault",
            "doc_id": second["doc_id"],
            "file_path": second["file_path"],
            "chunk_id": second["chunk_id"],
            "reason": "budget",
        }
    ]


def test_a_smaller_item_after_a_dropped_one_still_fits() -> None:
    first = _vault_item(1, "x" * (4 * 380))
    big = _vault_item(2, "y" * 4000)
    small = _vault_item(3, "z" * 40)

    items, dropped, _ = server._fill([first, big, small], 500)

    assert items == [first, small]
    assert [entry["file_path"] for entry in dropped] == [big["file_path"]]


def test_the_same_chunk_or_a_contained_block_is_a_duplicate() -> None:
    whole = _vault_item(1, "the middleware outage runbook and its escalation path")
    again = {**whole, "score": 0.5}
    contained = _vault_item(2, "its escalation path")
    memory_echo = _memory_item(9, "middleware outage runbook")

    items, dropped, _ = server._fill([whole, again, contained, memory_echo], 1000)

    assert items == [whole]
    assert [entry["reason"] for entry in dropped] == ["duplicate"] * 3
    assert dropped[-1] == {"source": "memory", "id": 9, "reason": "duplicate"}


def test_memory_items_never_take_more_than_their_share() -> None:
    budget = 1000
    cap = int(budget * server.PACK_MEMORY_SHARE)
    memories = [_memory_item(n, f"memory {n} " + "m" * 600) for n in range(1, 4)]

    items, dropped, used = server._fill(memories, budget)

    memory_cost = sum(server.estimate_tokens(item) for item in items)
    assert memory_cost <= cap
    assert used == memory_cost
    assert dropped and all(entry["reason"] == "budget" for entry in dropped)


def test_the_hard_cap_holds_for_every_budget() -> None:
    candidates = [_vault_item(n, f"doc {n} " + "w" * (97 * n)) for n in range(1, 30)]
    candidates += [_memory_item(n, "m" * (53 * n)) for n in range(1, 10)]
    fused = server._fused(candidates[:29], candidates[29:])

    for budget in (server.PACK_BUDGET_MIN, 777, 1500, 3000, server.PACK_BUDGET_MAX):
        items, _, used = server._fill(fused, budget)
        assert used == sum(server.estimate_tokens(item) for item in items) <= budget


def test_fusion_interleaves_by_reciprocal_rank_with_the_vault_first_on_a_tie() -> None:
    vault = [_vault_item(n, f"v{n}") for n in (1, 2, 3)]
    memory = [_memory_item(n, f"m{n}") for n in (1, 2)]

    fused = server._fused(vault, memory)

    assert [item["text"] for item in fused] == ["v1", "m1", "v2", "m2", "v3"]
    assert server._fused(vault, []) == vault


# --- the tool --------------------------------------------------------------------------


@pytest.mark.parametrize("budget", [server.PACK_BUDGET_MIN - 1, server.PACK_BUDGET_MAX + 1, 0, -5])
def test_a_budget_out_of_range_is_a_coded_error(indexed_server: Any, budget: int) -> None:
    with pytest.raises(KnowledgeToolError) as excinfo:
        asyncio.run(indexed_server.context_pack("middleware", budget_tokens=budget))

    assert excinfo.value.code == errors.E_ARGUMENT_OUT_OF_RANGE


def test_the_pack_reads_the_section_a_search_pointer_names(indexed_server: Any) -> None:
    """A vault item is the block ``get_document(chunk_id=…)`` returns for the same pointer."""
    pack = asyncio.run(indexed_server.context_pack("middleware outage", budget_tokens=3000))

    assert pack["vault"] == indexed_server.default_runtime().spec.slug
    assert pack["dropped"] == []
    for item in pack["items"]:
        doc = asyncio.run(
            indexed_server.get_document(item["file_path"], chunk_id=item["chunk_id"], max_chars=100_000)
        )
        assert doc["content"] == item["text"]
        assert doc["section"] == item["section"]


def test_the_pack_answers_the_same_twice(indexed_server: Any) -> None:
    first = asyncio.run(indexed_server.context_pack("otp session", budget_tokens=800))
    second = asyncio.run(indexed_server.context_pack("otp session", budget_tokens=800))

    assert [i["chunk_id"] for i in first["items"]] == [i["chunk_id"] for i in second["items"]]
    assert first["used_tokens"] == second["used_tokens"] <= 800


def test_without_memory_asked_nothing_is_degraded(indexed_server: Any) -> None:
    pack = asyncio.run(indexed_server.context_pack("middleware outage", budget_tokens=600))

    assert "degraded" not in pack
    assert pack["sources_used"] == ["vault"]


def test_memory_without_a_bearer_degrades_with_its_code(indexed_server: Any) -> None:
    pack = asyncio.run(indexed_server.context_pack("middleware outage", budget_tokens=600, include_memories=True))

    assert pack["degraded"] == [{"source": "memory", "code": errors.E_MEMORY_TOKEN_MISSING}]


def test_an_unexpected_memory_failure_degrades_instead_of_failing(
    indexed_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Broken:
        def search(self, query: str, *, project: str, limit: int = 5) -> list[dict[str, Any]]:
            raise KeyError("not a memory error")

    monkeypatch.setattr(indexed_server.default_runtime(), "memory", Broken())

    pack = asyncio.run(indexed_server.context_pack("middleware outage", budget_tokens=600, include_memories=True))

    assert pack["degraded"] == [{"source": "memory", "code": errors.E_MEMORY_UNAVAILABLE}]


def test_an_empty_memory_is_no_source_and_no_degradation(
    indexed_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from knowledge.ports import NullMemoryClient

    monkeypatch.setattr(indexed_server.default_runtime(), "memory", NullMemoryClient())

    pack = asyncio.run(indexed_server.context_pack("middleware outage", budget_tokens=600, include_memories=True))

    assert "degraded" not in pack
    assert pack["sources_used"] == ["vault"]


# --- list_vaults -------------------------------------------------------------------------


@pytest.fixture()
def two_vaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[[], VaultRegistry]:
    """A registry over copies of alpha and beta, beta the default; alpha's manifest names it."""

    def install() -> VaultRegistry:
        vaults = tmp_path / "vaults"
        shutil.copytree(FIXTURE_VAULTS, vaults)
        manifest = vaults / "alpha" / "vault.yaml"
        manifest.write_text(manifest.read_text(encoding="utf-8") + 'name: "Alpha Docs"\n', encoding="utf-8")
        entries = [
            {"slug": "alpha", "path": str(vaults / "alpha"), "features": ["jira", "flows"], "repo": "git@x:a.git"},
            {"slug": "beta", "path": str(vaults / "beta"), "default": True},
        ]
        vaults_file = tmp_path / "vaults.yaml"
        vaults_file.write_text(yaml.safe_dump({"version": 1, "vaults": entries}, sort_keys=False))
        root = tmp_path / "root"
        root.mkdir()
        env = {
            "KNOWLEDGE_HOME": str(tmp_path / "home"),
            "KNOWLEDGE_EMBEDDINGS_PROVIDER": "bag-of-words",
            "KNOWLEDGE_VAULTS": str(vaults_file),
        }
        reg = VaultRegistry.from_settings(settings.load_settings(env, project_root=root))
        monkeypatch.setattr(server, "_registry", reg)
        return reg

    return install


def test_list_vaults_names_every_vault_and_opens_none(two_vaults: Callable[[], VaultRegistry]) -> None:
    reg = two_vaults()

    listing = asyncio.run(server.list_vaults())

    assert listing == {
        "contract": contract.CONTRACT_VERSION,
        "default": "beta",
        "vaults": [
            {"slug": "alpha", "name": "Alpha Docs", "default": False, "features": ["flows", "jira"]},
            {"slug": "beta", "name": "beta", "default": True, "features": []},
        ],
    }
    assert reg.opened() == ()


def test_list_vaults_carries_no_path_repo_or_environment(two_vaults: Callable[[], VaultRegistry]) -> None:
    two_vaults()

    listing = asyncio.run(server.list_vaults())
    text = repr(listing)

    assert "git@x" not in text and "/" not in text
    for row in listing["vaults"]:
        assert len(repr(row)) <= 300


def test_context_pack_routes_to_the_named_vault_only(two_vaults: Callable[[], VaultRegistry]) -> None:
    reg = two_vaults()

    pack = asyncio.run(server.context_pack("gateway telvia token login", budget_tokens=800, vault="alpha"))

    assert pack["vault"] == "alpha"
    assert pack["items"]
    assert {item["file_path"] for item in pack["items"]} <= {"Services/Gateway Service.md", "Runbooks/Login Runbook.md"}
    assert reg.opened() == ("alpha",)


# --- /health ------------------------------------------------------------------------------


def test_health_reports_the_contract_version(tmp_path: Path) -> None:
    from mcp.server.fastmcp import FastMCP
    from starlette.testclient import TestClient

    from knowledge import webhook

    class _Store:
        vector_backend = "sqlite-vec"

        def count(self) -> int:
            return 3

        def get_meta(self) -> dict[str, str]:
            return {"index_version": "1"}

    class _Indexer:
        store = _Store()

    app = FastMCP("probe")
    webhook.register_webhook_routes(
        app, _Indexer(), index_ready_flag={"ready": True}, vault_root=tmp_path, state_dir=tmp_path
    )

    with TestClient(app.streamable_http_app()) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["contract_version"] == contract.CONTRACT_VERSION
    assert response.json()["indexed"] == 3
    assert response.json()["index_version"] == 1
    assert response.json()["vector_backend"] == "sqlite-vec"
    assert response.json()["profile"] == "all"


def test_health_reports_the_profile_the_server_serves(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from mcp.server.fastmcp import FastMCP
    from starlette.testclient import TestClient

    from knowledge import settings, webhook

    class _Store:
        vector_backend = "sqlite-vec"

        def count(self) -> int:
            return 0

        def get_meta(self) -> dict[str, str]:
            return {}

    class _Indexer:
        store = _Store()

    monkeypatch.setenv("KNOWLEDGE_PROFILE", "legacy")
    settings.reset_current()
    try:
        app = FastMCP("probe")
        webhook.register_webhook_routes(
            app, _Indexer(), index_ready_flag={"ready": True}, vault_root=tmp_path, state_dir=tmp_path
        )
        with TestClient(app.streamable_http_app()) as client:
            assert client.get("/health").json()["profile"] == "legacy"
    finally:
        monkeypatch.delenv("KNOWLEDGE_PROFILE")
        settings.reset_current()
