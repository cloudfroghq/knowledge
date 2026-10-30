"""``vault=`` on every tool: one call, one vault, nothing shared with the call beside it.

Each tool declares ``vault: str | None = None`` in its own signature (that is
what puts it in the input schema) and ``_in_vault`` resolves it into a context
variable for the duration of the call. ``None`` is the default vault, an
unknown slug is ``E_VAULT_UNKNOWN`` before the tool does any work, and naming a
vault opens that vault's runtime only.

The two-vault cases run on copies of ``tests/fixtures/vaults`` (``alpha`` and
``beta``), never on the golden fixture vault.
"""

from __future__ import annotations

import asyncio
import shutil
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from knowledge import errors, server, settings
from knowledge.errors import KnowledgeConfigError, KnowledgeToolError
from knowledge.registry import VaultRegistry

FIXTURE_VAULTS = Path(__file__).resolve().parent / "fixtures" / "vaults"
ALPHA_FILES = {"Services/Gateway Service.md", "Runbooks/Login Runbook.md"}
BETA_FILES = {"Services/Portal Dashboard.md", "Architecture/Lookup Architecture.md"}
ALPHA_QUERY = "gateway telvia token login"
BETA_QUERY = "portal dashboard comet"

RUNBOOK = "Runbooks/Runbook - Middleware Outage.md"

#: One minimal call per tool, ``vault`` excluded.
CALLS: dict[str, dict[str, Any]] = {
    "get_document": {"file_path": RUNBOOK},
    "search": {"query": "middleware"},
    "search_by_metadata": {"type": "service"},
    "search_deep": {"query": "middleware"},
    "find_path": {"from_path": RUNBOOK, "to_path": RUNBOOK},
    "get_related": {"file_path": RUNBOOK},
    "suggest_links": {},
    "get_stale_docs": {},
    "get_flow": {"provider": "Telvia"},
    "compare_flows": {"provider1": "Telvia", "provider2": "Comet Argentina"},
    "get_impact": {"file_path": RUNBOOK},
    "sync_confluence": {},
    "search_code": {"query": "anything"},
    "reindex": {},
    "context_pack": {"query": "middleware"},
}

#: Tools that take no ``vault``: ``list_vaults`` is the one that enumerates them.
UNROUTED = frozenset({"list_vaults"})


def _registered() -> dict[str, Any]:
    """Every tool of the ``legacy`` profile: the catalog of 1.4.0, extension tools and the retired one included."""
    from mcp.server.fastmcp import FastMCP

    probe = FastMCP("routing-probe")
    server.register_tools(probe, structured=True, profile="legacy")
    return {tool.name: tool for tool in asyncio.run(probe.list_tools())}


def test_every_tool_is_covered_here() -> None:
    assert set(CALLS) | UNROUTED == set(_registered())
    assert not set(CALLS) & UNROUTED


@pytest.mark.parametrize("name", sorted(UNROUTED))
def test_a_tool_that_lists_the_vaults_takes_no_vault(name: str) -> None:
    assert "vault" not in _registered()[name].inputSchema.get("properties", {})


@pytest.mark.parametrize("name", sorted(CALLS))
def test_vault_is_in_the_input_schema_of_every_tool(name: str) -> None:
    schema = _registered()[name].inputSchema

    assert "vault" not in schema.get("required", [])
    prop = schema["properties"]["vault"]
    assert prop["default"] is None
    assert {"type": "string"} in prop["anyOf"] and {"type": "null"} in prop["anyOf"]


@pytest.mark.parametrize("name", sorted(CALLS))
def test_an_unknown_vault_fails_before_the_tool_runs(name: str) -> None:
    with pytest.raises(KnowledgeConfigError) as excinfo:
        asyncio.run(getattr(server, name)(**CALLS[name], vault="no-such-vault"))

    assert excinfo.value.code == errors.E_VAULT_UNKNOWN
    assert "no-such-vault" in str(excinfo.value)


def test_an_unknown_vault_reaches_the_client_as_a_coded_tool_error() -> None:
    from mcp.server.fastmcp.exceptions import ToolError

    with pytest.raises(ToolError) as excinfo:
        asyncio.run(server.mcp.call_tool("search", {"query": "x", "vault": "no-such-vault"}))

    assert "E_VAULT_UNKNOWN: " in str(excinfo.value)


def test_the_current_vault_is_reset_after_every_call(indexed_server) -> None:
    slug = indexed_server.default_runtime().spec.slug
    asyncio.run(indexed_server.search("middleware", vault=slug))

    assert indexed_server._current_vault.get() is None


def test_naming_the_default_vault_is_the_same_as_naming_none(indexed_server) -> None:
    slug = indexed_server.default_runtime().spec.slug

    named = asyncio.run(indexed_server.get_document(RUNBOOK, vault=slug))
    unnamed = asyncio.run(indexed_server.get_document(RUNBOOK))

    assert named == unnamed


def test_a_tool_without_a_vault_parameter_cannot_be_routed() -> None:
    def no_vault(query: str) -> None:
        del query

    with pytest.raises(TypeError):
        server._in_vault(no_vault)


# --- two vaults -----------------------------------------------------------------


@pytest.fixture()
def two_vaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[[str], VaultRegistry]:
    """Install a registry over copies of alpha and beta, with ``default`` as the default."""

    def install(default: str) -> VaultRegistry:
        vaults = tmp_path / "vaults"
        if not vaults.exists():
            shutil.copytree(FIXTURE_VAULTS, vaults)
        entries = [
            {"slug": slug, "path": str(vaults / slug), **({"default": True} if slug == default else {})}
            for slug in ("alpha", "beta")
        ]
        vaults_file = tmp_path / "vaults.yaml"
        vaults_file.write_text(yaml.safe_dump({"version": 1, "vaults": entries}, sort_keys=False))
        root = tmp_path / "root"
        root.mkdir(exist_ok=True)
        env = {
            "KNOWLEDGE_HOME": str(tmp_path / "home"),
            "KNOWLEDGE_EMBEDDINGS_PROVIDER": "bag-of-words",
            "KNOWLEDGE_VAULTS": str(vaults_file),
        }
        reg = VaultRegistry.from_settings(settings.load_settings(env, project_root=root))
        monkeypatch.setattr(server, "_registry", reg)
        return reg

    return install


def _files(rows: list[dict[str, Any]]) -> set[str]:
    return {row["file_path"] for row in rows}


def test_vault_routes_one_call_to_that_vault(two_vaults: Callable[[str], VaultRegistry]) -> None:
    two_vaults("alpha")

    hits = asyncio.run(server.search(BETA_QUERY, limit=5, vault="beta"))
    doc = asyncio.run(server.get_document("Services/Portal Dashboard.md", vault="beta"))
    with pytest.raises(KnowledgeToolError) as elsewhere:
        asyncio.run(server.get_document("Services/Portal Dashboard.md"))

    assert hits and _files(hits) <= BETA_FILES
    assert doc["file_path"] == "Services/Portal Dashboard.md" and "content" in doc
    # The default vault is alpha: the beta document is not there.
    assert elsewhere.value.code == errors.E_FILE_NOT_FOUND and "'alpha'" in str(elsewhere.value)


def test_calling_only_with_vault_beta_never_opens_alpha(two_vaults: Callable[[str], VaultRegistry]) -> None:
    reg = two_vaults("alpha")

    asyncio.run(server.search(BETA_QUERY, limit=5, vault="beta"))
    asyncio.run(server.get_related("Services/Portal Dashboard.md", vault="beta"))
    asyncio.run(server.search_by_metadata(type="service", vault="beta"))

    assert reg.opened() == ("beta",)


def test_pointers_follow_the_routed_vault(two_vaults: Callable[[str], VaultRegistry]) -> None:
    two_vaults("alpha")

    rows = asyncio.run(server.search(BETA_QUERY, limit=5, fields=["file_path", "chunk_id", "section"], vault="beta"))
    first = rows[0]
    doc = asyncio.run(server.get_document(first["file_path"], chunk_id=first["chunk_id"], vault="beta"))

    assert first["section"] is not None
    assert doc["section"] == first["section"]


def test_concurrent_calls_on_different_vaults_do_not_share_the_vault(
    two_vaults: Callable[[str], VaultRegistry], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two calls held inside the server at the same moment each keep their own vault.

    Both worker threads wait on a barrier inside ``_ensure_indexed``, so neither
    finishes before the other has set its vault; a context variable shared
    between them would hand one of the calls the other's documents.
    """
    two_vaults("alpha")
    barrier = threading.Barrier(2, timeout=30)
    real = server._ensure_indexed
    seen: dict[str, str] = {}

    def gated(rt: Any = None) -> Any:
        barrier.wait()
        resolved = real(rt)
        seen[threading.current_thread().name] = resolved.spec.slug
        return resolved

    monkeypatch.setattr(server, "_ensure_indexed", gated)

    async def both() -> tuple[Any, Any]:
        return await asyncio.gather(
            server.search(ALPHA_QUERY, limit=5, vault="alpha"),
            server.search(BETA_QUERY, limit=5, vault="beta"),
        )

    for _ in range(3):
        alpha_rows, beta_rows = asyncio.run(both())
        assert alpha_rows and _files(alpha_rows) <= ALPHA_FILES
        assert beta_rows and _files(beta_rows) <= BETA_FILES

    assert set(seen.values()) == {"alpha", "beta"}
    assert server._current_vault.get() is None


# --- sync_confluence never writes into a vault ------------------------------------------


@pytest.fixture()
def recorded_sync(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Replace the Confluence sync with a recorder: no credentials, no network, no writes."""
    from knowledge import confluence_sync

    calls: list[dict[str, Any]] = []

    def fake_sync(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"recorded": True}

    monkeypatch.setattr(confluence_sync, "sync", fake_sync)
    return calls


@pytest.mark.parametrize("vault", [None, "alpha", "beta"])
def test_sync_confluence_refuses_to_write_into_any_vault(
    two_vaults: Callable[[str], VaultRegistry], recorded_sync: list[dict[str, Any]], vault: str | None
) -> None:
    """The fence answers in the tool, before the sync runs, whichever vault is named."""
    from knowledge.errors import KnowledgeToolError

    two_vaults("alpha")

    with pytest.raises(KnowledgeToolError) as excinfo:
        asyncio.run(server.sync_confluence(dry_run=False, vault=vault))

    assert excinfo.value.code == errors.E_WRITE_FENCED
    assert "confluence export" in str(excinfo.value)
    assert recorded_sync == []


def test_sync_confluence_dry_run_reads_the_named_vault(
    two_vaults: Callable[[str], VaultRegistry], recorded_sync: list[dict[str, Any]]
) -> None:
    reg = two_vaults("alpha")

    asyncio.run(server.sync_confluence(dry_run=True, vault="beta"))

    assert [(call["dry_run"], call["vault_path"]) for call in recorded_sync] == [(True, reg.spec("beta").path)]
    assert recorded_sync[0]["integrations"] == reg.spec("beta").integrations
