"""Guards on the tool surface itself.

The per-tool tests only protect the tools someone remembered to write a test
for. These check the surface: every ``@_tool`` in the module reaches the
FastMCP registry, every registered tool is described, and — the one that
actually catches drift — every registered tool is covered by the response
freeze in ``test_response_schemas``. Registering another tool without
freezing its response fails here.

Neither contract module writes down how many tools there are. The expected
*set* is frozen (a rename or a removal is a breaking change and has to be
declared), but a total spelled out in prose is drift waiting to happen: this
docstring already shipped naming an ordinal the registry had never reached.
``test_the_contract_docstrings_carry_no_tool_count`` keeps that out.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import re
from pathlib import Path

import pytest
from mcp.server.fastmcp import FastMCP

TESTS_DIR = Path(__file__).parent
SCHEMA_MODULE = TESTS_DIR / "test_response_schemas.py"

# The fixtures that hand a test the live server module. A call on one of these
# is a call on a tool, and it is what "this tool's response is frozen" means.
_SERVER_FIXTURES = {"indexed_server", "server_on_temp_vault"}

# Tools that legitimately reach outside the process on every call, so the
# suite drives them through a boundary double rather than calling them raw.
_EXTERNAL = {"sync_confluence", "reindex", "search_code", "sync_jira"}

# The two modules that state the tool contract. Their module docstrings are
# read by anyone auditing the surface, which is exactly why a number in them
# is load-bearing prose and has to stay out.
_CONTRACT_MODULES = ("test_tool_registry.py", "test_response_schemas.py")

_COUNT_WORDS = (
    "one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|"
    "fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty"
)
# "the fifteen tools", "a sixteenth tool", "15 tools" — a number standing in
# for the size of the registry. Applied to module docstrings only: a per-area
# module saying "the two tools this file covers" is a local, correct claim.
_TOOL_COUNT_IN_PROSE = re.compile(
    rf"\b(?:\d+|{_COUNT_WORDS})(?:th|st|nd|rd)?\s+(?:more\s+|other\s+)?tools?\b",
    re.IGNORECASE,
)


@pytest.fixture(scope="module")
def registered_tools(request):
    """The live registry of the suite's server: the ``all`` profile over a vault that declares no extension."""
    from knowledge import server

    return {t.name: t for t in asyncio.run(server.mcp.list_tools())}


def _legacy_app() -> FastMCP:
    from knowledge import server

    probe = FastMCP("legacy-probe")
    server.register_tools(probe, structured=True, profile="legacy")
    return probe


@pytest.fixture(scope="module")
def catalog_tools(request):
    """Every tool a server can list without ``jira``: the ``legacy`` profile, the catalog of 1.4.0."""
    return {t.name: t for t in asyncio.run(_legacy_app().list_tools())}


def _decorator_lines(prefix: str) -> list[str]:
    from knowledge import server

    source = Path(inspect.getfile(server)).read_text()
    # Only decorator lines: the module docstrings mention the decorators in prose.
    return [line.strip() for line in source.splitlines() if line.strip().startswith(prefix)]


def test_every_declared_tool_reaches_the_registry(registered_tools) -> None:
    """A tool that is defined but not registered is dead code with a docstring."""
    declared = _decorator_lines("@_tool")

    assert declared == ["@_tool"] * len(registered_tools), (
        f"{len(declared)} core tools declared in server.py, {len(registered_tools)} registered: "
        f"{sorted(registered_tools)}"
    )


def test_every_extension_and_retired_tool_is_declared_as_such() -> None:
    """``@_gated_tool("<extension>")`` names the extension the contract assigns; ``@_legacy_tool`` the retired ones."""
    from knowledge import contract, server

    assert len(_decorator_lines("@_gated_tool(")) == len(_EXTENSION_TOOLS)
    assert len(_decorator_lines("@_legacy_tool")) == len(_RETIRED_TOOLS)
    assert dict(server._GATED_EXTENSION) == dict(contract.TOOL_EXTENSION)
    assert set(server._LEGACY_TOOL_FUNCTIONS) == set(contract.TOOL_REMOVED) == _RETIRED_TOOLS


#: Tools that only need an indexed vault: every server registers them, in this set.
_CORE_TOOLS = frozenset(
    {
        "get_document",
        "search",
        "search_by_metadata",
        "search_deep",
        "find_path",
        "get_related",
        "get_impact",
        "reindex",
        "context_pack",
        "list_vaults",
    }
)

#: Tools of gated extensions: registered after the core tools, and only when
#: a vault the process serves declares their extension. The suite vault
#: declares none, so the live registry is ``_CORE_TOOLS`` alone.
_EXTENSION_TOOLS = frozenset(
    {"get_flow", "compare_flows", "get_stale_docs", "suggest_links", "sync_confluence", "sync_jira"}
)

#: Tools the contract retired: only the ``legacy`` profile lists them.
_RETIRED_TOOLS = frozenset({"search_code"})

#: The 1.4.0 catalog: its order is the order of the core tools and, name by
#: name, the catalog of the ``legacy`` profile.
_V1_4_0_CATALOG = TESTS_DIR / "golden" / "v1.4.0" / "tools_list.json"


def _v1_4_0_names() -> list[str]:
    import json

    return [tool["name"] for tool in json.loads(_V1_4_0_CATALOG.read_text(encoding="utf-8"))["tools"]]


def test_the_registry_holds_the_expected_tool_set(registered_tools) -> None:
    """Freezes the public surface: a rename or a removal is a breaking change."""
    assert set(registered_tools) == _CORE_TOOLS


def test_the_legacy_catalog_is_the_1_4_0_catalog_name_by_name(catalog_tools) -> None:
    """``legacy`` lists the tools of 1.4.0 in the order of ``tests/golden/v1.4.0/tools_list.json``."""
    from knowledge import profiles

    assert list(catalog_tools) == _v1_4_0_names() == list(profiles.LEGACY_ORDER)


def test_every_tool_is_described(registered_tools) -> None:
    """The description is the only thing a model sees when choosing a tool."""
    undescribed = sorted(n for n, t in registered_tools.items() if not (t.description or "").strip())

    assert undescribed == []


def test_every_tool_declares_an_input_schema(registered_tools) -> None:
    for name, tool in registered_tools.items():
        schema = tool.inputSchema
        assert schema.get("type") == "object", f"{name}: input schema is not an object"
        assert "properties" in schema, f"{name}: input schema declares no properties"


def test_offloaded_tools_keep_their_real_signature(catalog_tools) -> None:
    """``@_offloaded`` wraps each tool; without functools.wraps the generated
    schema would collapse to ``*args, **kwargs`` and every parameter would
    vanish from the model's view."""
    assert "file_path" in catalog_tools["get_document"].inputSchema["properties"]
    assert "provider1" in catalog_tools["compare_flows"].inputSchema["properties"]
    assert "query" in catalog_tools["search"].inputSchema["properties"]


def _frozen_tool_names(module_path: Path) -> set[str]:
    """Tool names the freeze module actually *calls*, read from its syntax tree.

    Not a text search. A substring match over the raw source counts a
    commented-out call, a name inside a docstring, or a mention in a TODO as
    coverage — which is the difference between a gate and a gate-shaped
    comment. Parsing keeps only real ``Call`` nodes of the form
    ``<fixture>.<tool>(...)``: comments and string literals are not nodes, so
    they cannot satisfy this.

    ``indexed_server._indexer.index_code()`` and friends are excluded for
    free: their callee is an attribute of an attribute, not of the fixture
    name itself.
    """
    tree = ast.parse(module_path.read_text(), filename=str(module_path))
    called: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id in _SERVER_FIXTURES
        ):
            called.add(func.attr)
    return called


def test_every_registered_tool_has_a_response_freeze(catalog_tools) -> None:
    """No tool ships without its response shape pinned in the freeze module."""
    frozen = _frozen_tool_names(SCHEMA_MODULE)
    unfrozen = sorted(name for name in catalog_tools if name not in frozen)

    assert unfrozen == [], f"tools with no response freeze: {unfrozen}"


def test_the_freeze_module_has_no_leftover_tools(catalog_tools) -> None:
    """The other half of the contract: a freeze for a tool that no longer exists.

    Pinning only "registry ⊆ frozen" makes the check one-directional, and the
    direction it leaves open is the one a removal takes. A freeze that outlives
    its tool keeps passing forever while testing a name nobody serves.
    """
    frozen = _frozen_tool_names(SCHEMA_MODULE)
    orphaned = sorted(name for name in frozen if name not in catalog_tools and name not in _EXTENSION_TOOLS)

    assert orphaned == [], (
        f"{SCHEMA_MODULE.name} freezes names that are not registered tools: {orphaned}"
    )


def test_the_contract_docstrings_carry_no_tool_count() -> None:
    """Prose that counts the tools goes stale the next time one is added.

    The registry is the only place that knows how many tools there are, and
    every guard here reads it. A module docstring saying "the fifteen tools"
    is a second, unowned source of truth — both of these shipped wrong once.
    """
    offenders: list[str] = []
    for name in _CONTRACT_MODULES:
        path = TESTS_DIR / name
        assert path.is_file(), f"contract module is missing: {path}"
        docstring = ast.get_docstring(ast.parse(path.read_text(), filename=str(path))) or ""
        for match in _TOOL_COUNT_IN_PROSE.finditer(docstring):
            offenders.append(f"{name}: {match.group(0).strip()!r}")

    assert offenders == [], (
        "a tool count is written down in a contract docstring; say 'every tool' "
        f"and let the registry be the count: {offenders}"
    )


def test_tools_that_touch_the_network_are_known(catalog_tools) -> None:
    """Keeps the list of externally-reaching tools honest.

    Every name here is driven through a double in the suite. A new tool that
    calls out has to be added deliberately, which is the moment to notice it
    needs one.
    """
    from knowledge import extensions

    assert extensions.gated_tools() == _EXTENSION_TOOLS
    assert set(catalog_tools) | _EXTENSION_TOOLS >= _EXTERNAL


# ---------------------------------------------------------------------------
# Profiles. A server serves ``all`` unless ``serve --profile`` or
# ``KNOWLEDGE_PROFILE`` asks for ``legacy``; ``core`` and ``ext`` exist only
# for composition. Each is proven here on a throwaway app, never on the live
# registry.
# ---------------------------------------------------------------------------


def _names_in_order(app: FastMCP) -> list[str]:
    return [t.name for t in asyncio.run(app.list_tools())]


def _throwaway_copy() -> FastMCP:
    from knowledge import server

    probe = FastMCP("probe")
    probe._tool_manager._tools = dict(server.mcp._tool_manager._tools)
    return probe


def test_all_profile_is_exactly_the_registered_set_in_order() -> None:
    from knowledge import profiles, server

    in_order = _names_in_order(server.mcp)

    assert profiles.DEFAULT_PROFILE == "all"
    assert profiles.PUBLIC_PROFILES == ("all", "legacy")
    assert profiles.select("all", in_order) == in_order
    assert profiles.PROFILES["all"] == _CORE_TOOLS | _EXTENSION_TOOLS
    assert profiles.PROFILES["legacy"] == _CORE_TOOLS | _EXTENSION_TOOLS | _RETIRED_TOOLS
    assert profiles.select("all", [*in_order, *sorted(_EXTENSION_TOOLS)]) == [*in_order, *sorted(_EXTENSION_TOOLS)]
    assert profiles.select("legacy", list(profiles.LEGACY_ORDER)) == list(profiles.LEGACY_ORDER)


def test_the_core_tools_keep_their_1_4_0_order() -> None:
    """``tools/list`` under ``all`` is the core tools in the order 1.4.0 listed them; extensions follow."""
    from knowledge import server

    assert _names_in_order(server.mcp) == [name for name in _v1_4_0_names() if name in _CORE_TOOLS]


def test_a_public_profile_is_all_or_legacy() -> None:
    from knowledge import profiles
    from knowledge.errors import E_PROFILE_UNKNOWN, KnowledgeConfigError

    assert [profiles.public(name) for name in ("all", "legacy")] == ["all", "legacy"]
    for name in ("core", "ext", "nope"):
        with pytest.raises(KnowledgeConfigError) as excinfo:
            profiles.public(name)
        assert excinfo.value.code == E_PROFILE_UNKNOWN
        assert name not in excinfo.value.message


def test_core_and_ext_partition_all() -> None:
    from knowledge import profiles

    core, ext = profiles.PROFILES["core"], profiles.PROFILES["ext"]

    assert not (core & ext)
    assert core | ext == profiles.PROFILES["all"]


def test_an_unknown_profile_is_a_coded_error() -> None:
    from knowledge import profiles
    from knowledge.errors import E_PROFILE_UNKNOWN, KnowledgeConfigError

    with pytest.raises(KnowledgeConfigError) as excinfo:
        profiles.select("everything", ["search"])

    assert excinfo.value.code == E_PROFILE_UNKNOWN
    assert "everything" not in excinfo.value.message


@pytest.mark.parametrize("profile", ["all", "core", "ext"])
def test_apply_profile_on_a_throwaway_app(profile: str) -> None:
    """Each profile keeps its tools in registration order and drops the rest."""
    from knowledge import profiles, server

    live_before = _names_in_order(server.mcp)
    probe = _throwaway_copy()

    server.apply_profile(probe, profile)

    expected = [name for name in live_before if name in profiles.PROFILES[profile]]
    assert _names_in_order(probe) == expected
    assert _names_in_order(server.mcp) == live_before


def _serve_without_running(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Patch ``serve`` down to registration: no transport, no vault, no logging setup. Returns the transports run."""
    from knowledge import server, settings

    ran: list[str] = []
    # serve() re-registers the tools on the module's app; keep that inside the test.
    monkeypatch.setattr(server.mcp._tool_manager, "_tools", dict(server.mcp._tool_manager._tools))
    monkeypatch.setattr(server.logging, "basicConfig", lambda **_: None)
    monkeypatch.setattr(settings, "install", lambda s: None)
    monkeypatch.setattr(server, "_configure_transport", lambda app, http: None)
    monkeypatch.setattr(server.VaultRegistry, "from_settings", classmethod(lambda cls, s, **_: None))
    monkeypatch.setattr(server, "set_registry", lambda reg: None)
    monkeypatch.setattr(server, "default_runtime", lambda: None)
    monkeypatch.setattr(server, "registry", lambda: type("R", (), {"close": lambda self: None})())
    monkeypatch.setattr(server.mcp, "run", lambda transport: ran.append(transport))
    return ran


def test_serve_registers_the_all_profile_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a profile, ``serve`` registers the core tools in their order and never narrows them."""
    from knowledge import server, settings

    live_before = _names_in_order(server.mcp)
    applied: list[str] = []
    ran = _serve_without_running(monkeypatch)
    monkeypatch.setattr(server, "apply_profile", lambda app, profile: applied.append(profile))

    server.serve(settings.load_settings())

    assert ran == ["stdio"]
    assert applied == []
    assert _names_in_order(server.mcp) == live_before


def test_serve_registers_the_legacy_profile_when_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    """``KNOWLEDGE_PROFILE=legacy`` serves the 1.4.0 catalog, name by name and in its order."""
    from knowledge import server, settings

    _serve_without_running(monkeypatch)
    monkeypatch.setenv("KNOWLEDGE_PROFILE", "legacy")

    server.serve(settings.load_settings())

    assert _names_in_order(server.mcp) == _v1_4_0_names()
    tools = {tool.name: tool for tool in server.mcp._tool_manager.list_tools()}
    assert tools["search_code"].meta == {"knowledge/deprecated": True, "knowledge/removal": "2.1.0"}


def test_the_profile_flag_wins_over_the_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    from knowledge import cli, settings

    monkeypatch.setenv("KNOWLEDGE_PROFILE", "all")

    assert settings.load_settings(cli._environment(None, None, "legacy")).output.profile == "legacy"
    assert settings.load_settings(cli._environment(None, None, None)).output.profile == "all"
    serve_parser = cli._build_parser()._subparsers._group_actions[0].choices["serve"]
    assert "--profile" in serve_parser.format_help()


def test_an_unknown_profile_stops_serve_with_status_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from knowledge import cli

    _serve_without_running(monkeypatch)

    assert cli.main(["serve", "--profile", "nope"]) == 2
    err = capsys.readouterr().err
    assert err.startswith("E_PROFILE_UNKNOWN: ") and "nope" not in err


# --- the catalog: descriptions and output modes ---------------------------------

#: Most characters a catalog description may take, and the retired tool's cap.
DESCRIPTION_LIMIT = 250
RETIRED_DESCRIPTION_LIMIT = 80

#: Parameters whose meaning the name alone does not give, and the tool whose
#: description has to name them: no input schema property carries a
#: description, so the tool description is the only place a model learns it.
_NON_OBVIOUS_PARAMETERS: dict[str, tuple[str, ...]] = {
    "get_document": ("chunk_id", "section", "max_chars", "fields", "full_metadata"),
    "search": ("mode", "include_memories", "fields", "snippet"),
    "search_by_metadata": ("facets", "limit", "offset", "fields"),
    "search_deep": ("depth", "fields", "snippet"),
    "find_path": ("max_depth", "weighted"),
    "get_related": ("include_suggested", "fields"),
    "get_stale_docs": ("owner", "include_unreadable", "missing_last_updated"),
    "get_impact": ("max_items", "counts"),
    "sync_confluence": ("scope_prefix", "dry_run"),
    "reindex": ("force", "pull", "target"),
    "context_pack": ("budget_tokens", "include_memories"),
}


def test_the_catalog_descriptions_are_the_contract_ones(catalog_tools) -> None:
    """Every tool any profile lists carries its contract description; ``sync_jira`` has one waiting."""
    from knowledge import contract

    listed = {name: tool.description for name, tool in catalog_tools.items()}

    assert listed == {name: text for name, text in contract.DESCRIPTIONS.items() if name != "sync_jira"}
    assert set(contract.DESCRIPTIONS) == _CORE_TOOLS | _EXTENSION_TOOLS | _RETIRED_TOOLS


def test_catalog_descriptions_stay_short(catalog_tools) -> None:
    long = {
        name: len(tool.description)
        for name, tool in catalog_tools.items()
        if len(tool.description) > (RETIRED_DESCRIPTION_LIMIT if name == "search_code" else DESCRIPTION_LIMIT)
    }

    assert long == {}


def test_non_obvious_parameters_are_named_in_their_description(catalog_tools) -> None:
    unnamed = [
        f"{tool}.{param}"
        for tool, params in _NON_OBVIOUS_PARAMETERS.items()
        for param in params
        if param not in catalog_tools[tool].description
    ]
    absent = [
        f"{tool}.{param}"
        for tool, params in _NON_OBVIOUS_PARAMETERS.items()
        for param in params
        if param not in catalog_tools[tool].inputSchema.get("properties", {})
    ]

    assert unnamed == []
    assert absent == [], "a listed parameter no longer exists"


@pytest.mark.parametrize("structured", [False, True])
def test_register_tools_keeps_the_profile_order(structured: bool) -> None:
    """Re-registering removes every tool first, so none moves to the end of the catalog."""
    from knowledge import server

    probe = _throwaway_copy()
    server.register_tools(probe, structured=structured)

    tools = probe._tool_manager.list_tools()
    assert [tool.name for tool in tools] == _names_in_order(server.mcp)
    legacy = _throwaway_copy()
    server.register_tools(legacy, structured=structured, profile="legacy")
    assert [tool.name for tool in legacy._tool_manager.list_tools()] == _v1_4_0_names()
    assert all((tool.output_schema is not None) is structured for tool in tools)
    live = {tool.name: tool for tool in server.mcp._tool_manager.list_tools()}
    for tool in tools:
        assert (tool.description, tool.meta, tool.parameters) == (
            live[tool.name].description,
            live[tool.name].meta,
            live[tool.name].parameters,
        )


def test_serve_re_registers_without_structured_output_when_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    from knowledge import server, settings

    calls: list[tuple[object, bool]] = []
    monkeypatch.setattr(
        server, "register_tools", lambda app, *, structured, profile="all": calls.append((app, structured))
    )
    monkeypatch.setattr(server.logging, "basicConfig", lambda **_: None)
    monkeypatch.setattr(settings, "install", lambda s: None)
    monkeypatch.setattr(server, "_configure_transport", lambda app, http: None)
    monkeypatch.setattr(server.VaultRegistry, "from_settings", classmethod(lambda cls, s, **_: None))
    monkeypatch.setattr(server, "set_registry", lambda reg: None)
    monkeypatch.setattr(server, "default_runtime", lambda: None)
    monkeypatch.setattr(server, "registry", lambda: type("R", (), {"close": lambda self: None})())
    monkeypatch.setattr(server.mcp, "run", lambda transport: None)

    monkeypatch.setenv("KNOWLEDGE_STRUCTURED_OUTPUT", "1")
    server.serve(settings.load_settings())
    assert calls == []

    monkeypatch.setenv("KNOWLEDGE_STRUCTURED_OUTPUT", "0")
    server.serve(settings.load_settings())
    assert calls == [(server.mcp, False)]


def test_a_title_named_parameter_survives_the_title_stripping() -> None:
    from knowledge import server

    schema = {
        "title": "probeArguments",
        "type": "object",
        "properties": {"title": {"title": "Title", "type": "string"}, "n": {"title": "N", "type": "integer"}},
        "required": ["title"],
    }

    assert server._untitled(schema) == {
        "type": "object",
        "properties": {"title": {"type": "string"}, "n": {"type": "integer"}},
        "required": ["title"],
    }
