"""Drift gate for ``docs/tools/inventory.md``.

The inventory is generated from the catalog the server serves over stdio
(``scripts/tool_inventory.py``). This module regenerates it from a live
``tools/list`` and fails when the committed file differs, so a tool that is
added, removed, renamed or re-described cannot ship with a stale inventory.
The server is the golden launcher on the fixture vault: no network, no index.
"""

from __future__ import annotations

import asyncio
import copy
import importlib.metadata
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from knowledge import contract

PROJECT_ROOT = Path(__file__).resolve().parent.parent
GOLDEN_DIR = PROJECT_ROOT / "tests" / "golden" / f"v{contract.CONTRACT_VERSION}"
#: The catalog with ``flows``, ``governance`` and ``confluence`` on; the
#: inventory's adds ``sync_jira``, the last tool of the ``jira`` catalog.
FEATURES_CATALOG = GOLDEN_DIR / "tools_list__features.json"
JIRA_CATALOG = GOLDEN_DIR / "tools_list__jira.json"


def _load():
    spec = importlib.util.spec_from_file_location("tool_inventory", PROJECT_ROOT / "scripts" / "tool_inventory.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["tool_inventory"] = module
    spec.loader.exec_module(module)
    return module


inv = _load()


@pytest.fixture(scope="module")
def live_catalog() -> dict:
    return asyncio.run(inv.fetch_catalog())


def test_committed_inventory_matches_the_live_catalog(live_catalog: dict) -> None:
    committed = inv.INVENTORY.read_text(encoding="utf-8")

    assert committed == inv.render(live_catalog), (
        f"docs/tools/inventory.md is out of date with tools/list; regenerate it with `{inv.REGENERATE}`"
    )


def test_live_catalog_is_the_golden_catalog(live_catalog: dict) -> None:
    """The inventory and the golden masters describe the same tools, in the same order: every tool of the contract."""
    features = json.loads(FEATURES_CATALOG.read_text(encoding="utf-8"))["tools"]
    jira = json.loads(JIRA_CATALOG.read_text(encoding="utf-8"))["tools"]
    golden = [*features, jira[-1]]

    assert [t["name"] for t in live_catalog["tools"]] == [t["name"] for t in golden]
    assert live_catalog["tools"] == golden
    assert {t["name"] for t in golden} == set(contract.TOOL_SINCE)


def test_inventory_states_the_declared_contract_version(live_catalog: dict) -> None:
    rendered = inv.render(live_catalog)

    assert f"**{contract.CONTRACT_VERSION}.**" in rendered
    assert "**Ninguna declarada.**" not in rendered
    assert f"`version: {live_catalog['server_info']['version']}`" in rendered


def test_the_handshake_announces_the_contract_version_on_its_first_line(live_catalog: dict) -> None:
    first_line = live_catalog["instructions"].splitlines()[0]

    assert first_line == f"knowledge-contract: {contract.CONTRACT_VERSION}"
    assert inv.declared_contract_version(live_catalog["instructions"]) == contract.CONTRACT_VERSION


def test_server_info_still_carries_the_sdk_version(live_catalog: dict) -> None:
    """The contract version travels in ``instructions``; ``serverInfo`` is left to the SDK."""
    assert live_catalog["server_info"]["version"] == importlib.metadata.version("mcp")


def test_an_inventory_without_instructions_declares_no_version(live_catalog: dict) -> None:
    bare = {**live_catalog, "instructions": None}

    assert "**Ninguna declarada.**" in inv.render(bare)


def test_a_reworded_description_is_drift(live_catalog: dict) -> None:
    changed = copy.deepcopy(live_catalog)
    changed["tools"][0]["description"] = "Something else entirely."

    assert inv.render(changed) != inv.render(live_catalog)


def test_check_mode_fails_on_a_stale_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, live_catalog: dict) -> None:
    async def cached() -> dict:
        return live_catalog

    monkeypatch.setattr(inv, "fetch_catalog", cached)
    stale = tmp_path / "inventory.md"
    stale.write_text("# stale\n", encoding="utf-8")

    assert inv.main(["--check", "--output", str(stale)]) == 1
    assert inv.main(["--output", str(stale)]) == 0
    assert inv.main(["--check", "--output", str(stale)]) == 0


def test_the_inventory_says_since_when_each_tool_exists(live_catalog: dict) -> None:
    rendered = inv.render(live_catalog)

    for name, since in contract.TOOL_SINCE.items():
        assert f"| `{name}` | {since} |" in rendered, name


def test_the_inventory_names_the_retired_tool_the_profiles_and_the_output_mode(live_catalog: dict) -> None:
    rendered = inv.render(live_catalog)

    assert "| `search_code` | 2.0.0 | `E_TOOL_RETIRED`; solo el perfil `legacy` la lista |" in rendered
    assert "## Perfiles" in rendered and "`KNOWLEDGE_PROFILE`" in rendered
    assert "Obsoleta" not in rendered
    for name, extension in contract.TOOL_EXTENSION.items():
        assert f"| `{name}` | {contract.TOOL_SINCE[name]} | `{extension}` |" in rendered, name
    assert "Salida de texto (`KNOWLEDGE_STRUCTURED_OUTPUT=0`, el default)" in rendered


def test_summaries_never_break_a_table_row() -> None:
    assert inv._summary("Search (mode: a|b). Rest.") == "Search (mode: a\\|b)."
    assert inv._summary("Filter by tag (e.g. x/y) and status. Rest.") == "Filter by tag (e.g. x/y) and status."
