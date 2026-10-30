"""The declarative registry of extensions and how the server applies it.

``knowledge.extensions`` describes each optional extension (the tools and
routes it brings, the module that implements it, the variables its
credentials come from, the transports that load it); ``knowledge.server``
loads the modules on demand, registers the tools of gated extensions only
when a served vault declares them, checks credentials at start and decides
whether ``/webhook/reindex`` exists. Every vault here is a temporary
directory or ``tests/fixtures/``; no test reaches Jira, Confluence or memory.
"""

from __future__ import annotations

import asyncio
import dataclasses
import importlib.util
import re
import sys
import textwrap
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest
from mcp.server.fastmcp import FastMCP

from knowledge import contract, errors, extensions, settings
from knowledge.errors import KnowledgeConfigError

_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _spec(**overrides: Any) -> settings.VaultSpec:
    base: dict[str, Any] = {
        "slug": "alpha",
        "path": None,
        "is_default": True,
        "index_dir": Path("/nonexistent"),
        "memory_folder": "90 - Memory",
        "review_thresholds": {"default": 270},
        "thresholds_from_config": False,
        "embeddings": settings.EmbeddingSettings(provider="bag-of-words"),
    }
    base.update(overrides)
    return settings.VaultSpec(**base)


def _settings_for(tmp_path: Path, vaults: list[dict[str, Any]], manifests: dict[str, str], **env: str) -> settings.Settings:
    """A ``Settings`` over a temporary ``vaults.yaml`` whose vaults carry the given ``vault.yaml`` bodies."""
    import yaml

    entries = []
    for entry in vaults:
        directory = tmp_path / entry["slug"]
        directory.mkdir(parents=True)
        (directory / "vault.yaml").write_text(
            f"slug: {entry['slug']}\n" + textwrap.dedent(manifests.get(entry["slug"], "")), encoding="utf-8"
        )
        entries.append({"path": str(directory), **entry})
    vaults_file = tmp_path / "vaults.yaml"
    vaults_file.write_text(yaml.safe_dump({"version": 1, "data_dir": str(tmp_path / "data"), "vaults": entries}))
    base = {"KNOWLEDGE_VAULTS": str(vaults_file), "KNOWLEDGE_EMBEDDINGS_PROVIDER": "bag-of-words"}
    base.update(env)
    return settings.load_settings(base, project_root=tmp_path)


# --- the registry -------------------------------------------------------------


def test_every_feature_is_an_extension_and_no_other_is() -> None:
    assert sorted(extensions.features()) == sorted(settings.VAULT_FEATURES)
    assert len(set(extensions.names())) == len(extensions.EXTENSIONS)


def test_only_rerank_is_turned_on_by_a_block_instead_of_features() -> None:
    blocks = {ext.name: ext.block for ext in extensions.EXTENSIONS if ext.block is not None}

    assert blocks == {"rerank": "rerank"}
    assert "rerank" not in settings.VAULT_FEATURES
    assert set(extensions.names()) - set(extensions.features()) == {"rerank"}


def test_the_rerank_extension_loads_the_judges_over_both_transports() -> None:
    ext = extensions.get("rerank")

    assert ext.module == "knowledge.judges"
    assert ext.tools == () and ext.routes == () and not ext.gated
    assert ext.env_optional
    assert extensions.loads_on(ext, "stdio") and extensions.loads_on(ext, "http")


def test_the_rerank_extension_reads_the_variable_the_vault_names() -> None:
    from knowledge.settings import RerankSettings

    ext = extensions.get("rerank")
    named = dataclasses.replace(_spec(), rerank=RerankSettings(provider="jev", model="jev-1.13.0", key_env="INITECH_TYPESAFE_API_KEY"))

    assert extensions.env_names(_spec(), ext) == ("TYPESAFE_API_KEY", "KEV_API_KEY")
    assert extensions.env_names(named, ext) == ("INITECH_TYPESAFE_API_KEY",)
    assert extensions.missing_env(named, ext, {"INITECH_TYPESAFE_API_KEY": "k"}) == []
    assert extensions.enabled(named) == frozenset()


@pytest.mark.parametrize("ext", extensions.EXTENSIONS, ids=lambda e: e.name)
def test_each_extension_is_well_formed(ext: extensions.Extension) -> None:
    assert ext.transports and ext.transports <= extensions.TRANSPORTS
    for name in (*ext.requires_env, *ext.fixed_env):
        assert _ENV_NAME.fullmatch(name), name
    assert len(ext.requires_env) <= max(len(ext.secret_keys), len(ext.requires_env))
    if ext.secret_keys:
        assert ext.integration is not None
    if ext.module is not None:
        assert ext.module.startswith("knowledge.")
        assert importlib.util.find_spec(ext.module) is not None, ext.module
    for route in ext.routes:
        assert route.startswith("/")


def test_every_tool_of_the_registry_belongs_to_the_contract() -> None:
    tools = [tool for ext in extensions.EXTENSIONS for tool in ext.tools]

    assert len(tools) == len(set(tools))
    for tool in tools:
        assert tool in contract.DESCRIPTIONS, tool
        assert tool in contract.TOOL_SINCE, tool


def test_every_extension_with_tools_is_gated_and_jira_loads_over_stdio_only() -> None:
    assert [ext.name for ext in extensions.EXTENSIONS if ext.gated] == ["flows", "governance", "confluence", "jira"]
    assert [ext.name for ext in extensions.EXTENSIONS if ext.legacy_always] == ["flows", "governance", "confluence"]
    assert all(ext.gated for ext in extensions.EXTENSIONS if ext.tools)
    assert {tool: ext.name for ext in extensions.EXTENSIONS for tool in ext.tools} == dict(contract.TOOL_EXTENSION)
    for name in ("jira", "memory-bridge"):
        ext = extensions.get(name)
        assert extensions.loads_on(ext, "stdio") and not extensions.loads_on(ext, "http")
    assert not extensions.loads_on(extensions.get("git-webhook"), "stdio")


# --- the catalog the server registers ------------------------------------------


def _catalog(s: settings.Settings, transport: str, profile: str) -> list[str]:
    """The names a server over ``s`` registers under ``profile``, as ``serve`` registers them, in order."""
    from knowledge import server

    app = FastMCP("probe")
    server.register_tools(app, structured=True, profile=profile)
    server.register_gated_tools(app, dataclasses.replace(s, http=dataclasses.replace(s.http, transport=transport)), structured=True)
    return [tool.name for tool in asyncio.run(app.list_tools())]


def _expected(s: settings.Settings, transport: str, profile: str) -> set[str]:
    """The registered catalog the contract promises: one expression for every test that checks it."""
    retired = set(contract.TOOL_REMOVED) if profile == contract.LEGACY_PROFILE else set()
    return (set(contract.TOOL_SINCE) | retired) - extensions.gated_off_tools(s.vaults, transport, profile)


_FEATURE_SETS = ("", "features: [flows]\n", "features: [governance]\n", "features: [flows, governance, confluence, jira]\n")


@pytest.mark.parametrize("profile", contract.PUBLIC_PROFILES)
@pytest.mark.parametrize("transport", ["stdio", "http"])
@pytest.mark.parametrize("manifest", _FEATURE_SETS, ids=["none", "flows", "governance", "every"])
def test_the_registered_catalog_is_the_contract_minus_the_gated_tools_turned_off(
    tmp_path: Path, manifest: str, transport: str, profile: str
) -> None:
    """(TOOL_SINCE ∪ TOOL_REMOVED under legacy) − gated_off_tools(specs, transport, profile) is what registers."""
    s = _settings_for(tmp_path, [{"slug": "alpha"}], {"alpha": manifest})

    assert set(_catalog(s, transport, profile)) == _expected(s, transport, profile)


def test_the_live_registry_is_the_contract_minus_the_gated_tools_turned_off() -> None:
    from knowledge import server

    s = settings.current()
    registered = {tool.name for tool in asyncio.run(server.mcp.list_tools())}

    assert registered == _expected(s, s.http.transport, contract.DEFAULT_PROFILE)


def test_extension_tools_are_kept_aside_and_the_core_ones_registered() -> None:
    from knowledge import server

    assert not extensions.gated_tools() & set(server._TOOL_FUNCTIONS)
    assert extensions.gated_tools() == set(server._GATED_TOOL_FUNCTIONS)
    assert not set(contract.TOOL_REMOVED) & set(server._TOOL_FUNCTIONS)


def test_with_every_extension_declared_every_contract_tool_is_registered(tmp_path: Path) -> None:
    """The core tools in their order, then each extension's tools in registry order, ``sync_jira`` last."""
    from knowledge import profiles, server

    s = _settings_for(tmp_path, [{"slug": "alpha"}], {"alpha": "features: [flows, governance, confluence, jira]\n"})
    app = FastMCP("probe")
    app._tool_manager._tools = dict(server.mcp._tool_manager._tools)
    ext_order = ["get_flow", "compare_flows", "get_stale_docs", "suggest_links", "sync_confluence", "sync_jira"]

    assert server.register_gated_tools(app, s, structured=True) == ext_order
    tools = asyncio.run(app.list_tools())
    names = [tool.name for tool in tools]
    assert set(names) == _expected(s, "stdio", "all") == set(contract.TOOL_SINCE)
    assert names[-len(ext_order) :] == ext_order
    assert names[: -len(ext_order)] == [t.name for t in asyncio.run(server.mcp.list_tools())]
    assert {tool.name: tool.description for tool in tools} == {
        name: text for name, text in contract.DESCRIPTIONS.items() if name not in contract.TOOL_REMOVED
    }
    assert profiles.select("all", names) == names
    assert profiles.PROFILES["all"] == set(names)
    assert len(contract.DESCRIPTIONS["sync_jira"]) <= 200
    assert contract.TOOL_SINCE["sync_jira"] == "1.3.0"


def test_under_legacy_the_1_4_0_extension_tools_register_without_any_feature(tmp_path: Path) -> None:
    from knowledge import profiles

    s = _settings_for(tmp_path, [{"slug": "alpha"}], {})

    assert _catalog(s, "stdio", "legacy") == list(profiles.LEGACY_ORDER)
    assert extensions.gated_off_tools(s.vaults, "stdio", "legacy") == {"sync_jira"}


def test_a_vault_that_does_not_declare_the_extension_is_refused_by_tools_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two vaults, one with ``flows``: the tool is registered, and the other vault answers ``E_FEATURE_DISABLED``."""
    from mcp.server.fastmcp.exceptions import ToolError

    from knowledge import server

    s = _settings_for(
        tmp_path, [{"slug": "alpha"}, {"slug": "beta", "default": True}], {"alpha": "features: [flows]\n"}
    )
    app = FastMCP("probe")
    server.register_tools(app, structured=True)
    assert server.register_gated_tools(app, s, structured=True) == ["get_flow", "compare_flows"]
    monkeypatch.setattr(server, "_registry", server._registry_for(s))

    for arguments in ({"provider": "Telvia", "vault": "beta"}, {"provider": "Telvia"}):
        with pytest.raises(ToolError) as excinfo:
            asyncio.run(app.call_tool("get_flow", arguments))
        message = str(excinfo.value)
        assert "E_FEATURE_DISABLED: beta does not declare features: [flows]" in message
        assert "use vault= with one that does: alpha" in message

    # alpha declares flows, so the call reaches the tool: its vault has no flow document for Telvia.
    with pytest.raises(ToolError) as reached:
        asyncio.run(app.call_tool("get_flow", {"provider": "Telvia", "vault": "alpha"}))
    assert "E_FLOW_NOT_FOUND: " in str(reached.value)


def test_a_retired_or_unloaded_tool_answers_with_a_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """A call the catalog does not list but the contract knows gets a stable code, not the SDK's unknown tool."""
    from mcp.server.fastmcp.exceptions import ToolError

    from knowledge import server
    from knowledge.errors import KnowledgeToolError

    app = server.KnowledgeMCP("probe")
    server.register_tools(app, structured=True)

    with pytest.raises(KnowledgeToolError) as retired:
        asyncio.run(app.call_tool("search_code", {"query": "anything"}))
    assert retired.value.code == errors.E_TOOL_RETIRED
    assert retired.value.message == contract.retired_message("search_code")
    assert "graphify explain" in retired.value.message and "2.0.0" in retired.value.message

    with pytest.raises(KnowledgeToolError) as unloaded:
        asyncio.run(app.call_tool("get_flow", {"provider": "Telvia"}))
    assert unloaded.value.code == errors.E_FEATURE_DISABLED
    assert "features: [flows]" in unloaded.value.message

    with pytest.raises(ToolError, match="Unknown tool"):
        asyncio.run(app.call_tool("no_such_tool", {}))


def _with_gated_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give ``jira`` one tool, ``probe_gated``, for the length of a test."""
    from knowledge import server

    probe = dataclasses.replace(extensions.get("jira"), tools=("probe_gated",))
    monkeypatch.setattr(extensions, "EXTENSIONS", tuple(probe if e.name == "jira" else e for e in extensions.EXTENSIONS))
    monkeypatch.setattr(extensions, "_BY_NAME", {e.name: e for e in extensions.EXTENSIONS})
    monkeypatch.setattr(contract, "DESCRIPTIONS", MappingProxyType({**contract.DESCRIPTIONS, "probe_gated": "Probe."}))

    def probe_gated(vault: str | None = None) -> dict[str, Any]:
        return {"ok": True}

    monkeypatch.setitem(server._GATED_TOOL_FUNCTIONS, "probe_gated", probe_gated)


def test_a_gated_tool_registers_last_and_only_when_a_vault_declares_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from knowledge import server

    _with_gated_probe(monkeypatch)
    plain = _settings_for(tmp_path / "a", [{"slug": "alpha"}], {})
    declared = _settings_for(tmp_path / "b", [{"slug": "alpha"}, {"slug": "beta", "default": True}], {"alpha": "features: [jira]\n"})

    app = FastMCP("probe")
    server.register_tools(app, structured=True)
    base = [tool.name for tool in asyncio.run(app.list_tools())]

    assert server.register_gated_tools(app, plain, structured=True) == []
    assert "probe_gated" in extensions.gated_off_tools(plain.vaults, "stdio")

    assert server.register_gated_tools(app, declared, structured=True) == ["probe_gated"]
    assert [tool.name for tool in asyncio.run(app.list_tools())] == [*base, "probe_gated"]
    assert "probe_gated" not in extensions.gated_off_tools(declared.vaults, "stdio")
    # Registering again adds nothing.
    assert server.register_gated_tools(app, declared, structured=True) == []


def test_a_gated_tool_stays_absent_over_http(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from knowledge import server

    _with_gated_probe(monkeypatch)
    s = _settings_for(tmp_path, [{"slug": "alpha"}], {"alpha": "features: [jira]\n"}, MCP_TRANSPORT="http")
    app = FastMCP("probe")

    assert server.register_gated_tools(app, s, structured=True) == []
    assert "probe_gated" in extensions.gated_off_tools(s.vaults, "http")


# --- per-vault declarations and credentials ------------------------------------


def test_enabled_reads_each_vault_on_its_own(tmp_path: Path) -> None:
    s = _settings_for(
        tmp_path,
        [{"slug": "alpha", "features": ["flows", "jira"]}, {"slug": "beta", "default": True}],
        {"beta": "features: [confluence]\n"},
    )

    assert extensions.enabled(s.vault("alpha")) == {"flows", "jira"}
    assert extensions.enabled(s.vault("beta")) == {"confluence"}


def test_env_names_default_and_override() -> None:
    confluence = extensions.get("confluence")
    jira = extensions.get("jira")

    assert extensions.env_names(_spec(), confluence) == ("CONFLUENCE_EMAIL", "CONFLUENCE_TOKEN")
    overridden = _spec(
        integrations={"confluence": {"secrets": {"token_env": "ALPHA_CONFLUENCE_TOKEN"}}, "jira": {"secrets": {"email_env": "ALPHA_JIRA_EMAIL", "token_env": "ALPHA_JIRA_TOKEN"}}}
    )
    assert extensions.env_names(overridden, confluence) == ("CONFLUENCE_EMAIL", "ALPHA_CONFLUENCE_TOKEN")
    assert extensions.env_names(overridden, jira) == ("ALPHA_JIRA_EMAIL", "ALPHA_JIRA_TOKEN", "MEMORY_HTTP_TOKEN")
    assert extensions.env_names(_spec(), jira) == ("MEMORY_HTTP_TOKEN",)
    assert extensions.env_names(_spec(), extensions.get("git-webhook")) == ("MCP_WEBHOOK_SECRET",)


def test_missing_env_names_only_what_is_absent_or_empty() -> None:
    confluence = extensions.get("confluence")
    env = {"CONFLUENCE_EMAIL": "bot@example.test", "CONFLUENCE_TOKEN": "  "}

    assert extensions.missing_env(_spec(), confluence, env) == ["CONFLUENCE_TOKEN"]
    assert extensions.missing_env(_spec(), confluence, {**env, "CONFLUENCE_TOKEN": "placeholder"}) == []


def test_the_manifest_integrations_are_read_frozen(tmp_path: Path) -> None:
    s = _settings_for(
        tmp_path,
        [{"slug": "alpha"}],
        {"alpha": "integrations:\n  jira:\n    projects: [ACME]\n    secrets: {email_env: ALPHA_JIRA_EMAIL, token_env: ALPHA_JIRA_TOKEN}\n"},
    )
    jira = s.vault("alpha").integrations["jira"]

    assert jira["projects"] == ("ACME",)
    assert jira["secrets"]["token_env"] == "ALPHA_JIRA_TOKEN"
    with pytest.raises(TypeError):
        jira["projects"] = ("X",)  # type: ignore[index]


def test_a_credential_inside_integrations_is_refused(tmp_path: Path) -> None:
    with pytest.raises(KnowledgeConfigError) as excinfo:
        _settings_for(tmp_path, [{"slug": "alpha"}], {"alpha": "integrations:\n  jira:\n    token: abc\n"})

    assert excinfo.value.code == errors.E_SECRET_IN_MANIFEST
    assert "abc" not in str(excinfo.value)


def test_a_vault_without_integrations_has_an_empty_mapping() -> None:
    assert dict(settings.current().default_vault().integrations) == {}


# --- startup checks (server) ---------------------------------------------------


def test_stdio_warns_about_missing_credentials_and_starts(tmp_path: Path) -> None:
    from knowledge import server

    s = _settings_for(tmp_path, [{"slug": "alpha"}], {"alpha": "features: [confluence, jira, memory-bridge]\n"})
    warnings = server.extension_warnings(s, {})

    assert [w.split(":")[0] for w in warnings] == [errors.W_EXTENSION_UNCONFIGURED] * 3
    assert "CONFLUENCE_EMAIL, CONFLUENCE_TOKEN" in warnings[0]
    assert "MEMORY_HTTP_TOKEN" in warnings[1]


def test_http_refuses_an_extension_it_serves_without_credentials(tmp_path: Path) -> None:
    from knowledge import server

    s = _settings_for(
        tmp_path,
        [{"slug": "alpha"}],
        {"alpha": "features: [confluence]\nintegrations:\n  confluence:\n    secrets: {email_env: ALPHA_CONFLUENCE_EMAIL, token_env: ALPHA_CONFLUENCE_TOKEN}\n"},
        MCP_TRANSPORT="http",
    )

    with pytest.raises(KnowledgeConfigError) as excinfo:
        server.extension_warnings(s, {"ALPHA_CONFLUENCE_EMAIL": "bot@example.test"})
    assert excinfo.value.code == errors.E_SECRET_MISSING
    assert "ALPHA_CONFLUENCE_TOKEN" in str(excinfo.value)
    assert server.extension_warnings(s, {"ALPHA_CONFLUENCE_EMAIL": "bot@example.test", "ALPHA_CONFLUENCE_TOKEN": "placeholder"}) == []


def test_http_skips_jira_and_memory_bridge_with_a_warning_never_an_error(tmp_path: Path) -> None:
    from knowledge import server

    s = _settings_for(tmp_path, [{"slug": "alpha"}], {"alpha": "features: [jira, memory-bridge]\n"}, MCP_TRANSPORT="http")
    warnings = server.extension_warnings(s, {})

    assert [w.split(":")[0] for w in warnings] == [errors.W_EXTENSION_UNAVAILABLE_HTTP] * 2
    assert all("tools are absent" in w for w in warnings)


def test_a_missing_webhook_secret_is_left_to_the_settings_warning(tmp_path: Path) -> None:
    from knowledge import server

    s = _settings_for(tmp_path, [{"slug": "alpha", "repo": "git@example.test:a.git", "features": ["git-webhook"]}], {}, MCP_TRANSPORT="http")

    assert server.extension_warnings(s, {}) == []


def test_the_suite_vault_declares_nothing_so_nothing_warns() -> None:
    from knowledge import server

    assert server.extension_warnings(settings.current(), {}) == []


# --- lazy loading --------------------------------------------------------------


def test_extensions_load_on_demand_and_once(monkeypatch: pytest.MonkeyPatch) -> None:
    from knowledge import server

    monkeypatch.setattr(server, "_LOADED_EXTENSIONS", {})
    monkeypatch.delitem(sys.modules, "knowledge.flows", raising=False)

    module = server._load_extension("flows")

    assert module.__name__ == "knowledge.flows"
    assert server._load_extension("flows") is module
    assert server._load_extension("jira").__name__ == "knowledge.jira_sync"


def test_an_extension_without_a_module_refuses_to_load(monkeypatch: pytest.MonkeyPatch) -> None:
    from knowledge import server

    bare = dataclasses.replace(extensions.get("governance"), module=None)
    monkeypatch.setattr(extensions, "_BY_NAME", {**extensions._BY_NAME, "governance": bare})
    monkeypatch.setattr(server, "_LOADED_EXTENSIONS", {})

    with pytest.raises(RuntimeError, match="no module"):
        server._load_extension("governance")


def test_the_flow_tools_answer_through_the_extension(indexed_server) -> None:
    from knowledge import flows

    rt = indexed_server.default_runtime()
    direct = flows.get_flow(rt.vault_path, rt.spec.memory_folder, "comet argentina")

    assert "error" not in direct
    assert asyncio.run(indexed_server.get_flow("comet argentina")) == direct


# --- /webhook/reindex (D6-4) ---------------------------------------------------


@pytest.mark.parametrize(
    ("entry", "env", "served"),
    [
        pytest.param({"slug": "alpha", "repo": "git@example.test:a.git", "features": ["git-webhook"]}, {}, True, id="feature-and-repo"),
        pytest.param({"slug": "alpha", "repo": "git@example.test:a.git"}, {}, True, id="repo-without-feature"),
        pytest.param({"slug": "alpha", "features": ["git-webhook"]}, {}, False, id="feature-without-repo"),
        pytest.param({"slug": "alpha"}, {}, False, id="nothing"),
    ],
)
def test_the_reindex_webhook_follows_the_repository_of_a_vaults_yaml(
    tmp_path: Path, entry: dict[str, Any], env: dict[str, str], served: bool
) -> None:
    s = _settings_for(tmp_path, [entry], {}, **env)

    assert extensions.serves_reindex_webhook(s.default_vault()) is served


@pytest.mark.parametrize(("remote", "served"), [("git@example.test:v.git", True), ("", False)])
def test_the_reindex_webhook_follows_vault_remote(tmp_path: Path, remote: str, served: bool) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    s = settings.load_settings(
        {"VAULT_PATH": str(vault), "VAULT_REMOTE": remote, "KNOWLEDGE_EMBEDDINGS_PROVIDER": "bag-of-words"},
        project_root=tmp_path,
    )

    assert extensions.serves_reindex_webhook(s.default_vault()) is served


class _FakeApp:
    def add_middleware(self, _middleware: Any) -> None:
        return None


class _FakeMCP:
    def __init__(self) -> None:
        self.routes: list[str] = []

    def custom_route(self, path: str, methods: list[str]) -> Any:
        def register(fn: Any) -> Any:
            self.routes.append(path)
            return fn

        return register

    def streamable_http_app(self) -> _FakeApp:
        return _FakeApp()


@pytest.mark.parametrize(("serve_webhook", "routes"), [(True, ["/health", "/webhook/reindex"]), (False, ["/health"])])
def test_run_http_registers_the_reindex_route_only_when_asked(
    monkeypatch: pytest.MonkeyPatch, serve_webhook: bool, routes: list[str]
) -> None:
    import uvicorn

    from knowledge import server

    fake = _FakeMCP()
    monkeypatch.setattr(server, "mcp", fake)
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)

    server._run_http(settings.HttpSettings(transport="http"), serve_reindex_webhook=serve_webhook)

    assert fake.routes == routes


def test_serve_passes_the_webhook_rule_to_the_http_transport(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from knowledge import server

    seen: dict[str, Any] = {}
    # serve() re-registers the tools on the module's app; keep that inside the test.
    monkeypatch.setattr(server.mcp._tool_manager, "_tools", dict(server.mcp._tool_manager._tools))
    monkeypatch.setattr(server, "_run_http", lambda http, *, serve_reindex_webhook: seen.update(webhook=serve_reindex_webhook))
    monkeypatch.setattr(server, "set_registry", lambda reg: None)
    monkeypatch.setattr(server, "default_runtime", lambda: None)
    monkeypatch.setattr(server, "registry", lambda: type("R", (), {"close": lambda self: None})())
    monkeypatch.setattr(server, "_configure_transport", lambda app, http: None)
    monkeypatch.setattr(server, "register_gated_tools", lambda app, s, *, structured: [])
    monkeypatch.setattr(settings, "install", lambda s: None)

    s = _settings_for(tmp_path, [{"slug": "alpha"}], {}, MCP_TRANSPORT="http", MCP_API_KEY="placeholder-not-a-credential")
    server.serve(s)

    assert seen == {"webhook": False}
